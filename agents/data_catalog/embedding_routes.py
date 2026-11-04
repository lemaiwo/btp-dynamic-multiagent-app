"""Admin API of the catalogue's embeddings: ``/admin/api/data-catalog/embeddings``.

Design addendum (l), point 2, behind the "Data catalogue embeddings" panel
of the UI5 admin's Settings page. ``agents/admin.py`` includes this router
under ``/admin`` next to the catalogue's (``agents/data_catalog/routes.py``,
whose body reader it shares, capped at 4 KB here). Every route carries
``require_admin`` itself; a refusal is ``<field>: <rule>``, never a value.

* ``GET ""`` -- the status: the stored setting, the model in effect and
  where it comes from (``embeddings.resolve_model_detail``), whether
  ``AICORE_EMBEDDING_MODEL`` is set, AI Core's embedding deployments
  (``deployed``, any status) and the scenario's models without a live one
  (``available``), the counts (``embeddings.vector_counts``), the run
  (``agents/data_catalog/embedding_run.py``), the resource group a deploy
  goes to and ``aicore_error`` (the code when AI Core could not be read: the
  rest of the status is answered all the same; ``available`` is then empty,
  so no deploy is offered for a model whose live state is unknown).
* ``PUT ""`` ``{enabled, model}`` -- both keys required; ``model`` ``null``
  (automatic) or a model name. AI Core is asked only when the PUT switches
  embeddings on with a model other than the stored one: that model must have
  a deployment there, in any status (the status then says whether it
  embeds). Switching off, or keeping the stored model, never waits for AI
  Core and is never refused by it: an admin whose saved model's deployment
  was deleted, or whose AI Core is unreachable, can still switch off.
  Answers the status.
* ``POST /run`` -- 202 ``{running: true}``; 409 while a run is in flight,
  or when no model is in effect (then with ``X-Embeddings-Error: no_model``,
  so the panel can tell the two apart). AI Core's lists are read only when
  no model resolves (a deployment may have reached RUNNING since), and the
  model is resolved once more after them: a run with a model in effect
  does not wait for them.
* ``POST /deploy`` ``{model}`` -- 202 ``{deployment_id, status, created}``.
  It bills, so it is logged as ONE WARNING naming the model, the caller's
  principal and the resource group, written once AI Core has confirmed that
  the scenario offers the model and before the request is sent (a request
  whose answer is lost may still have made a deployment), and its answer as
  one INFO line (deployment id, status, whether it was created). Idempotent in
  ``agents.aicore_deployments.deploy``: a live deployment is answered and
  nothing is made. The deploy runs in a task of its own that the request
  only waits for (at most :data:`DEPLOY_WAIT_SECONDS`, 300 s): a client that
  goes away must not cancel it between the create AI Core may already have
  received and the release of the module's deploy lock. A wait that runs out
  is ``deploy_failed`` (the deployment may still appear in the list). The
  lifespan waits for a deploy in flight at shutdown, bounded and without
  cancelling it (:func:`drain_deploys`). No undeploy here: that is an
  operations step.

AI Core (``agents.aicore_deployments``, imported where it is called): a
model the scenario does not offer is a 422 at ``model``; every other failure
is a 502 with the module's fixed text and its code in ``X-Embeddings-Error``
(``aicore_unavailable``, ``token_failed``, ``deploy_failed``); an exception
of another kind is ``aicore_unavailable``, logged by its class. The two
lists are cached for :data:`LISTS_CACHE_SECONDS`, a failed read as well,
because the panel polls the status every 2 s while a run is in flight and a
read may take its whole 30 s timeout; a deploy drops the cache, and the
PUT's check reads the deployments fresh. Every fresh list of deployments is
handed to ``embeddings.note_running``, so the model in effect follows what
AI Core says is RUNNING: not a discovery cached for ten minutes, nor the list
gen_ai_hub's proxy client read once for the whole process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, StrictBool, StrictStr

import agents.db as agents_db
from agents.auth import current_principal, require_admin
from agents.data_catalog import embedding_run, embeddings
from agents.data_catalog.routes import _json_body, _Strict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data-catalog/embeddings", tags=["data-catalog"])

BODY_BYTES = 4 * 1024
LISTS_CACHE_SECONDS = 10.0
# How long the shutdown waits for a deploy in flight.
DEPLOY_DRAIN_SECONDS = 15.0
# Above the module's own bounds: a deploy makes at least six AI API calls of
# at most 30 s each (model list, deployment list, executables, configuration
# list, configuration create, deployment create), plus a token request, the
# further pages of the two lists and, after an unconfirmed create, a re-read
# of the deployments.
DEPLOY_WAIT_SECONDS = 300.0
ERROR_HEADER = "X-Embeddings-Error"
MODEL_NOT_DEPLOYED = "model: not a deployed embedding model"
MODEL_NOT_OFFERED = "model: not an embedding model AI Core offers"
RUN_IN_FLIGHT = "An embedding run is already in progress"
NO_MODEL = "No embedding model is in effect"
_UNAVAILABLE = "aicore_unavailable"

_ModelName = Annotated[StrictStr, Field(min_length=1, max_length=128)]


class SettingBody(_Strict):
    enabled: StrictBool
    model: _ModelName | None


class DeployBody(_Strict):
    model: _ModelName


class RunBody(_Strict):
    pass


# The clock of the lists' cache (a seam for the tests).
_clock = time.monotonic
_lists_cache: (
    tuple[float, list[dict[str, Any]], list[dict[str, Any]], str | None] | None
) = None
# Deploys in flight, held so a task whose request went away is not
# collected; each is dropped when it ends.
_deploys: set[asyncio.Task] = set()


def reset_cache() -> None:
    global _lists_cache
    _lists_cache = None


def _code(exc: Exception) -> str:
    from agents.aicore_deployments import AICoreError

    if isinstance(exc, AICoreError):
        return exc.code
    logger.warning("data catalogue: AI Core call failed (%s)", type(exc).__name__)
    return _UNAVAILABLE


def _refused(code: str) -> HTTPException:
    from agents.aicore_deployments import ERROR_MESSAGES

    if code == "model_unknown":
        return HTTPException(status_code=422, detail=MODEL_NOT_OFFERED)
    return HTTPException(
        status_code=502, detail=ERROR_MESSAGES.get(code, ERROR_MESSAGES[_UNAVAILABLE]),
        headers={ERROR_HEADER: code},
    )


async def _aicore(call: Callable[..., Awaitable[Any]], *args: Any) -> Any:
    """``call(*args)``, or the refusal its failure maps to."""
    try:
        return await call(*args)
    except Exception as exc:  # noqa: BLE001 - mapped to a fixed answer; the class is logged
        raise _refused(_code(exc)) from None


async def _attempt(call: Callable[[], Awaitable[list[dict[str, Any]]]]) -> tuple[Any, str | None]:
    try:
        return await call(), None
    except Exception as exc:  # noqa: BLE001 - the status answers the code
        return None, _code(exc)


def _note(deployed: list[dict[str, Any]]) -> None:
    embeddings.note_running(d["model"] for d in deployed if d["status"] == "RUNNING")


async def _lists() -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """``(deployed, offered, error)``, cached, a failed read included."""
    global _lists_cache
    now = _clock()
    if _lists_cache is not None and now - _lists_cache[0] < LISTS_CACHE_SECONDS:
        return _lists_cache[1], _lists_cache[2], _lists_cache[3]
    from agents import aicore_deployments as aicore

    (deployed, failed), (offered, refused) = await asyncio.gather(
        _attempt(aicore.list_deployments), _attempt(aicore.list_models)
    )
    if failed is None:
        _note(deployed)
    deployed, offered, error = deployed or [], offered or [], failed or refused
    _lists_cache = (now, deployed, offered, error)
    return deployed, offered, error


async def _status() -> dict[str, Any]:
    from agents import aicore_deployments as aicore

    # The lists first: what they say is RUNNING is what the model resolves on.
    deployed, offered, error = await _lists()
    model, source = await embeddings.resolve_model_detail()
    async with agents_db.SessionLocal() as db:
        setting = await agents_db.get_embedding_setting(db)
        counts = await embeddings.vector_counts(db, model)
    live = {d["model"] for d in deployed if d["status"] in aicore.LIVE_STATUSES}
    return {
        "setting": setting,
        "effective_model": model,
        "source": source,
        "env_override": bool((os.environ.get(embeddings.MODEL_ENV) or "").strip()),
        "deployed": deployed,
        "available": [] if error else [m for m in offered if m["model"] not in live],
        "nodes": counts["nodes"],
        "vectors": counts["vectors"],
        "stale": counts["stale"],
        "running": embedding_run.running(),
        "progress": embedding_run.progress(),
        "last_run": embedding_run.last_run(),
        "resource_group": aicore.resource_group(),
        "aicore_error": error,
    }


def _settled(task: asyncio.Task) -> None:
    _deploys.discard(task)
    # Retrieved, so a deploy whose request went away raises no "never
    # retrieved" warning; ``agents.aicore_deployments`` logged the failure.
    if not task.cancelled():
        task.exception()


@router.get("", dependencies=[Depends(require_admin)])
async def api_status() -> dict[str, Any]:
    return await _status()


@router.put("", dependencies=[Depends(require_admin)])
async def api_put_setting(request: Request) -> dict[str, Any]:
    body, _ = await _json_body(request, SettingBody, limit=BODY_BYTES)
    async with agents_db.SessionLocal() as db:
        saved = await agents_db.get_embedding_setting(db)
    # Only a model that the PUT puts in effect anew is checked: an "off", or
    # the stored model kept, must not depend on a remote call.
    if body.enabled and body.model is not None and body.model != saved["model"]:
        from agents import aicore_deployments as aicore

        deployed = await _aicore(aicore.list_deployments)
        _note(deployed)
        if body.model not in {d["model"] for d in deployed}:
            raise HTTPException(status_code=422, detail=MODEL_NOT_DEPLOYED)
    async with agents_db.SessionLocal() as db:
        try:
            await agents_db.set_embedding_setting(db, body.enabled, body.model)
        except ValueError as exc:  # the storage gate names the field and the rule
            raise HTTPException(status_code=422, detail=str(exc)) from None
    return await _status()


@router.post("/run", status_code=202, dependencies=[Depends(require_admin)])
async def api_run(request: Request) -> dict[str, bool]:
    await _json_body(request, RunBody, empty_ok=True, limit=BODY_BYTES)
    if embedding_run.running():
        raise HTTPException(status_code=409, detail=RUN_IN_FLIGHT)
    model = await embeddings.resolve_model()
    if not model:
        await _lists()  # a deployment that reached RUNNING since counts now
        model = await embeddings.resolve_model()
    if not model:
        raise HTTPException(status_code=409, detail=NO_MODEL, headers={ERROR_HEADER: "no_model"})
    # Checked again in the one step that starts it: the resolve awaited.
    if not embedding_run.start(model):
        raise HTTPException(status_code=409, detail=RUN_IN_FLIGHT)
    return {"running": True}


@router.post("/deploy", status_code=202, dependencies=[Depends(require_admin)])
async def api_deploy(request: Request) -> dict[str, Any]:
    body, _ = await _json_body(request, DeployBody, limit=BODY_BYTES)
    model = body.model
    # The storage rule of a model name, before AI Core is asked or the name
    # reaches the log line below.
    if agents_db.EMBEDDING_MODEL_RE.fullmatch(model) is None:
        raise HTTPException(status_code=422, detail=MODEL_NOT_OFFERED)
    from agents import aicore_deployments as aicore

    offered = await _aicore(aicore.list_models)
    if model not in {m["model"] for m in offered}:
        raise HTTPException(status_code=422, detail=MODEL_NOT_OFFERED)
    logger.warning(
        "AI Core deployment of %s requested by %s in resource group %s",
        model, current_principal.get() or "unknown principal", aicore.resource_group(),
    )
    task = asyncio.create_task(aicore.deploy(model), name="aicore-deploy")
    _deploys.add(task)
    task.add_done_callback(_settled)
    try:
        async with asyncio.timeout(DEPLOY_WAIT_SECONDS) as waited:
            made = await asyncio.shield(task)
    except TimeoutError as exc:
        # Our wait ran out (the deploy goes on), or the deploy's own timeout.
        raise _refused("deploy_failed" if waited.expired() else _code(exc)) from None
    except Exception as exc:  # noqa: BLE001 - mapped to a fixed answer; the class is logged
        raise _refused(_code(exc)) from None
    finally:
        reset_cache()
    answer = {
        "deployment_id": made["deployment_id"],
        "status": made["status"],
        "created": bool(made.get("created")),
    }
    logger.info(
        "AI Core deployment of %s answered: %s, status %s, created %s",
        model, answer["deployment_id"], answer["status"], answer["created"],
    )
    return answer


async def drain_deploys(timeout: float = DEPLOY_DRAIN_SECONDS) -> int:
    """Wait at most ``timeout`` for the deploys in flight (shutdown), without
    cancelling them: a create may already be with AI Core. Answers how many
    are still running, logged as one WARNING."""
    pending = {t for t in _deploys if not t.done()}
    if pending:
        _, pending = await asyncio.wait(pending, timeout=timeout)
    if pending:
        logger.warning(
            "%d AI Core deployment request(s) still running at shutdown; "
            "the next deploy of that model finds what they made", len(pending),
        )
    return len(pending)
