"""The model wrapper that logs every model request to the agent register.

``wrap(model, deployment)`` puts a :class:`NexusLoggedModel` around a model.
While the register is on (:func:`agents.nexus.state.enabled`) each request
hands two log calls to the sender queue: ``logRequest`` before the call and
``logResponse`` after it, with one ``correlationId``. While it is off the
wrapper calls the model straight through and queues nothing.

The register is a side channel, so the wrapper is built to never be noticed:

* it never changes the model's answer and never raises anything of its own:
  building or queueing a log body that fails is swallowed, counted
  (``build_failed``) and logged once per exception class; the model's
  response, or the model's own exception, passes through unchanged;
* tokens are the response's own ``usage`` (one request), never the run's
  running total, so the register can sum them without double counting;
* a content-filter refusal is a RETURNED response whose ``finish_reason`` is
  ``content_filter`` (not an exception at this layer): ``FILTERED``;
* a model error is described by its class (``errorCode`` the class name or
  ``http_<status>``) and a masked message (:func:`agents.errors.describe_exception`)
  cut to 200 characters; the body of a ``ModelHTTPError`` (and the dumped
  response of an ``UnexpectedModelBehavior``) is never sent: a provider may
  echo the prompt there;
* prompt and response text are sent only with ``NEXUS_SEND_PAYLOAD=true``;
* ``modelVersion`` is not sent: it is a field of ``logRequest`` only and
  known only once the response is there.

The wrapper is a ``pydantic_ai`` ``WrapperModel``: every other attribute
(``model_name``, ``profile``, ``settings``, ``client`` ...) resolves to the
wrapped model.
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

if TYPE_CHECKING:
    from pydantic_ai import RunContext

from agents.errors import describe_exception, mask_text
from agents.nexus import sender, state
from agents.nexus.context import (
    MAX_ID,
    Identity,
    capture_identity,
    current_agent_name,
    current_nexus_run_id,
)

logger = logging.getLogger(__name__)

MAX_MODEL_NAME = 120
MAX_ERROR_CODE = 40
MAX_ERROR_MESSAGE = 200
CONTENT_FILTER = "content_filter"

COMPLETED, FAILED, FILTERED, TIMEOUT = "COMPLETED", "FAILED", "FILTERED", "TIMEOUT"
CANCELLED = "cancelled"

_counters: dict[str, int] = {"build_failed": 0, "untracked_streams": 0}
_warned_classes: set[str] = set()
_warned_no_run_id: set[str] = set()
_warned_untracked: list[bool] = [False]


def counters() -> dict[str, int]:
    """Counts of this module.

    ``build_failed``: log bodies that could not be built; ``untracked_streams``:
    streams whose end could not be tracked (logged COMPLETED on a normal exit).
    """
    return dict(_counters)


def reset_for_tests() -> None:
    for key in _counters:
        _counters[key] = 0
    _warned_classes.clear()
    _warned_no_run_id.clear()
    _warned_untracked[0] = False


def _swallowed(exc: BaseException) -> None:
    """Count a failure of the logging code and log it once per class (never its text)."""
    _counters["build_failed"] += 1
    name = type(exc).__name__
    if name not in _warned_classes:
        _warned_classes.add(name)
        logger.warning("agent register: a log call could not be built (%s)", name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _payload_on() -> bool:
    from agents.nexus import config
    return config.settings().send_payload


@dataclass(frozen=True)
class _Call:
    """What a ``logResponse`` repeats of its ``logRequest``."""

    correlation_id: str
    agent: str
    run_id: str
    use_case: str | None
    payload: bool
    # The user the pair is logged as (``None``: technical). Captured once, when
    # the model call is made, and handed to both log calls of the pair.
    identity: Identity | None = field(default=None, repr=False, compare=False)

    def base(self) -> dict[str, Any]:
        body: dict[str, Any] = {"correlationId": self.correlation_id}
        if self.agent:  # never ``agent: ""``
            body["agent"] = self.agent
        body["runId"] = self.run_id
        if self.use_case is not None:
            body["useCase"] = self.use_case
        return body


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _part_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    pieces = []
    for item in content or ():
        if isinstance(item, str):
            pieces.append(item)
        else:
            media = getattr(item, "media_type", None)
            pieces.append(f"[{media if isinstance(media, str) else type(item).__name__}]")
    return "\n".join(pieces)


def _system_prompt(messages: list[ModelMessage]) -> str:
    texts = [part.content for m in messages if isinstance(m, ModelRequest)
             for part in m.parts if isinstance(part, SystemPromptPart)]
    requests = [m for m in messages if isinstance(m, ModelRequest)]
    if requests and requests[-1].instructions:
        texts.append(requests[-1].instructions)
    return "\n\n".join(texts)


def _prompt(messages: list[ModelMessage]) -> str | None:
    requests = [m for m in messages if isinstance(m, ModelRequest)]
    if not requests:
        return None
    users = [p for p in requests[-1].parts if isinstance(p, UserPromptPart)]
    return _part_text(users[-1].content) if users else None


def _response_text(response: ModelResponse) -> str:
    return "".join(p.content for p in response.parts if isinstance(p, TextPart))


def _begin(deployment: str, model_settings: ModelSettings | None,
           messages: list[ModelMessage]) -> _Call | None:
    """Queue the ``logRequest``; ``None`` when that failed (the response is then not logged)."""
    try:
        from agents.nexus import config, register
        agent = (current_agent_name.get() or "").strip()[:MAX_ID]
        run_id = current_nexus_run_id.get()
        if not run_id:
            run_id = str(uuid.uuid4())
            if agent not in _warned_no_run_id:
                _warned_no_run_id.add(agent)
                logger.warning("agent register: a model call of agent %s ran without a run "
                               "id; logged under a fresh id", agent)
        call = _Call(str(uuid.uuid4()), agent, run_id[:MAX_ID],
                     register.use_case_for(agent), _payload_on(), capture_identity())
        body = call.base()
        body["modelName"] = (deployment or "")[:MAX_MODEL_NAME]
        body["resourceGroup"] = config.resource_group()
        settings = model_settings or {}
        for key, field in (("temperature", "temperature"), ("top_p", "topP")):
            value = _number(settings.get(key))
            if value is not None:
                body[field] = value
        max_tokens = settings.get("max_tokens")
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
            body["maxTokens"] = max_tokens
        body["requestedAt"] = _now()
        if call.payload:
            system = _system_prompt(messages)
            if system:
                body["systemPrompt"] = system
            prompt = _prompt(messages)
            if prompt is not None:
                body["prompt"] = prompt
        sender.enqueue("logRequest", body, call.identity)
        return call
    except Exception as e:  # noqa: BLE001 - logging must never break a model call
        _swallowed(e)
        return None


def _send_response(call: _Call, status: str, input_tokens: int, output_tokens: int, *,
                   error_code: str | None = None, error_message: str | None = None,
                   text: str | None = None) -> None:
    body = call.base()
    body["status"] = status
    body["inputTokens"] = int(input_tokens or 0)
    body["outputTokens"] = int(output_tokens or 0)
    body["respondedAt"] = _now()
    if error_code is not None:
        body["errorCode"] = error_code[:MAX_ERROR_CODE]
    if error_message is not None:
        body["errorMessage"] = error_message[:MAX_ERROR_MESSAGE]
    if call.payload and text:
        body["response"] = text
    sender.enqueue("logResponse", body, call.identity)


def _finish(call: _Call | None, response: ModelResponse) -> None:
    """Queue the ``logResponse`` of a returned response (COMPLETED or FILTERED)."""
    if call is None:
        return
    try:
        status = FILTERED if response.finish_reason == CONTENT_FILTER else COMPLETED
        usage = response.usage
        _send_response(call, status, usage.input_tokens, usage.output_tokens,
                       text=_response_text(response) if call.payload else None)
    except Exception as e:  # noqa: BLE001
        _swallowed(e)


def _is_content_filter(exc: BaseException) -> bool:
    body = getattr(exc, "body", None)
    if not isinstance(exc, ModelHTTPError) or not isinstance(body, dict):
        return False
    error = body.get("error")
    codes = [body.get("code")]
    if isinstance(error, dict):
        codes.append(error.get("code"))
    return CONTENT_FILTER in codes


def _status_of(exc: BaseException) -> str:
    if any("Timeout" in cls.__name__ for cls in type(exc).__mro__):
        return TIMEOUT
    if _is_content_filter(exc):
        return FILTERED
    return FAILED


def _code_of(exc: BaseException) -> str:
    if isinstance(exc, ModelHTTPError):
        return f"http_{exc.status_code}"
    if isinstance(exc, asyncio.CancelledError):
        return CANCELLED
    return type(exc).__name__


def _message_of(exc: BaseException) -> str:
    """The masked description, without a provider's body (it can echo the prompt)."""
    name = type(exc).__name__
    if isinstance(exc, ModelHTTPError):
        return f"{name}: status_code: {exc.status_code}"
    if isinstance(exc, UnexpectedModelBehavior):
        message = mask_text(str(exc.message or ""))
        return f"{name}: {message}" if message else name
    return describe_exception(exc, limit=MAX_ERROR_MESSAGE)


def _finish_failed(call: _Call | None, exc: BaseException, *,
                   streamed: StreamedResponse | None = None) -> None:
    """Queue the ``logResponse`` of a model call that raised (tokens counted so far)."""
    if call is None:
        return
    try:
        input_tokens = output_tokens = 0
        if streamed is not None:
            usage = streamed.usage()
            input_tokens, output_tokens = usage.input_tokens, usage.output_tokens
        _send_response(call, _status_of(exc), input_tokens, output_tokens,
                       error_code=_code_of(exc), error_message=_message_of(exc))
    except Exception as e:  # noqa: BLE001
        _swallowed(e)


class _Tracker:
    """Whether a stream was read to its end (``StreamedResponse`` has no such flag)."""

    def __init__(self) -> None:
        self.done = False
        self.tracking = False
        # An error the provider's stream raised while it was read: it reaches the
        # consumer, which may then leave the context in any way.
        self.error: BaseException | None = None


def _track(streamed: StreamedResponse, tracker: _Tracker) -> None:
    """Put a pass-through generator around the stream's events that notes their end.

    ``__aiter__`` only builds the pipeline (nothing is read); every event goes
    through unchanged. The replacement is checked: ``__aiter__`` must hand out
    the tracker afterwards. When it does not (a version whose stream has no
    ``_event_iterator``, or whose ``__aiter__`` no longer returns it), the
    original iterator is put back, the stream is not tracked, a normal exit
    counts as read to its end (never a false FAILED), and it is counted
    (``untracked_streams``) with one WARNING per process.
    """
    try:
        if not hasattr(streamed, "_event_iterator"):
            _untracked()
            return
        events = streamed.__aiter__()

        async def tracked() -> AsyncIterator[Any]:
            try:
                async for event in events:
                    yield event
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except BaseException as exc:
                tracker.error = exc
                raise
            tracker.done = True

        before = streamed._event_iterator
        streamed._event_iterator = tracked()
        if streamed.__aiter__() is not streamed._event_iterator:
            streamed._event_iterator = before
            _untracked()
            return
        tracker.tracking = True
    except Exception as e:  # noqa: BLE001
        _swallowed(e)


def _untracked() -> None:
    _counters["untracked_streams"] += 1
    if not _warned_untracked[0]:
        _warned_untracked[0] = True
        logger.warning("agent register: the end of a model stream is not tracked on this "
                       "pydantic-ai version; a stream left early is logged COMPLETED")


def _finish_stream(call: _Call | None, streamed: StreamedResponse, tracker: _Tracker) -> None:
    """Queue the ``logResponse`` of a stream whose context exited normally."""
    if call is None:
        return
    if tracker.error is not None:
        _finish_failed(call, tracker.error, streamed=streamed)
        return
    try:
        if tracker.tracking and not tracker.done:
            _finish_partial(call, streamed)
            return
        _finish(call, streamed.get())
    except Exception as e:  # noqa: BLE001
        _swallowed(e)


def _finish_partial(call: _Call | None, streamed: StreamedResponse | None) -> None:
    """A stream cancelled or left before its end: FAILED, ``cancelled``, tokens so far."""
    if call is None:
        return
    try:
        input_tokens = output_tokens = 0
        if streamed is not None:
            usage = streamed.usage()
            input_tokens, output_tokens = usage.input_tokens, usage.output_tokens
        _send_response(call, FAILED, input_tokens, output_tokens, error_code=CANCELLED,
                       error_message="the stream was cancelled or not read to its end")
    except Exception as e:  # noqa: BLE001
        _swallowed(e)


class NexusLoggedModel(WrapperModel):
    """A model whose every request is logged to the agent register (see the module)."""

    def __init__(self, wrapped: Model, deployment: str):
        super().__init__(wrapped)
        self._deployment = deployment

    async def request(self, messages: list[ModelMessage], model_settings: ModelSettings | None,
                      model_request_parameters: ModelRequestParameters) -> ModelResponse:
        if not state.enabled():
            return await self.wrapped.request(messages, model_settings, model_request_parameters)
        call = _begin(self._deployment, model_settings, messages)
        try:
            response = await self.wrapped.request(messages, model_settings,
                                                  model_request_parameters)
        except BaseException as exc:
            _finish_failed(call, exc)
            raise
        _finish(call, response)
        return response

    @asynccontextmanager
    async def request_stream(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        if not state.enabled():
            async with self.wrapped.request_stream(messages, model_settings,
                                                   model_request_parameters,
                                                   run_context) as streamed:
                yield streamed
            return
        call = _begin(self._deployment, model_settings, messages)
        tracker = _Tracker()
        streamed: StreamedResponse | None = None
        try:
            async with self.wrapped.request_stream(messages, model_settings,
                                                   model_request_parameters,
                                                   run_context) as streamed:
                if call is not None:
                    _track(streamed, tracker)
                yield streamed
        except (asyncio.CancelledError, GeneratorExit):
            if tracker.error is not None:
                _finish_failed(call, tracker.error, streamed=streamed)
            else:
                _finish_partial(call, streamed)
            raise
        except BaseException as exc:
            _finish_failed(call, exc, streamed=streamed)
            raise
        _finish_stream(call, streamed, tracker)


def wrap(model: Model, deployment: str) -> Model:
    """``model`` behind the logging wrapper; a model already wrapped is returned as is."""
    if isinstance(model, NexusLoggedModel):
        return model
    return NexusLoggedModel(model, deployment)


__all__ = ["NexusLoggedModel", "wrap", "counters", "reset_for_tests"]
