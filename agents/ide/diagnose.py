"""What a diagnose run carries, and the one switch that decides on masking.

**The switch.** :func:`masking_required` is the only place that decides
whether a target's diagnose data is masked. A target flagged
``non_production`` in its conventions is *raw*: tool results reach the model
as ARC-1 sent them, the run's activity is stored as produced and the user's
text is stored verbatim. Every other target is *masked* -- and so is one
whose conventions are missing, could not be loaded, or carry anything but a
real ``True`` in the flag. The switch fails towards masking.

**Masking required means refused.** A diagnose session exists only on a
``non_production`` target (checked at create). When the switch says "masked"
for an existing session -- the flag was taken away, the conventions are gone
or unreadable -- the target counts as production: runs are not started
(``stages.assert_can_run``, ``runner._start``) and the routes read nothing
from it (``routes._refuse_lost_flag``), all with 409
``target_not_non_production``. The masking described below is what still
stands behind those refusals: a run or client that was not told "raw" masks.

It is used in three places, each of which passes the conventions row (or
``None``):

* the runner (``agents.ide.runner``) evaluates it when a run starts and
  puts the answer on the :class:`DiagnoseRun` it binds; the guard around
  every agent toolset (``agents.ide.readonly.ReadOnlyGuard``) reads it
  there, so the top-level agent, its delegates and its deep sub-agents --
  which all run in the binding's context -- get the same treatment;
* the routes hand it to ``Arc1Client`` for direct calls;
* the runner uses the same answer for what it stores (the user's message,
  the activity).

**The binding.** :data:`current_diagnose` is set by the run task and reset
by that same task. A diagnose session without a binding for *that* session
is refused by the guard: without it nobody knows whether to mask.

**The target's server.** Diagnose rights belong to the session target's
ARC-1 server only (:func:`is_target_server`). A tool of any other server an
agent happens to have -- even one named ``..._SAPDiagnose`` -- is held to the
change policy, and in a masked run its results are masked like all others.

When masking is on it applies to *every* tool result, not only to the
``SAPDiagnose`` data actions: those go through the allowlist-first
``masking.mask_result``, everything else through :func:`mask_plain`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable

from agents.ide.masking import USER_KEYS, Masker, mask_text

logger = logging.getLogger(__name__)

STRIPPED_OUTPUT = "(not stored: diagnose data)"


def masking_required(target_conventions: Any) -> bool:
    """True unless the target's conventions say ``non_production`` is ``True``.

    ``target_conventions`` is the target's ``IdeConventions`` row, or
    ``None`` when there is none or it could not be loaded. Anything but the
    boolean ``True`` in the flag (missing, ``None``, a string, a number, a
    dict standing in for a row) means masking is required.
    """
    if target_conventions is None or isinstance(target_conventions, dict):
        return True
    return getattr(target_conventions, "non_production", None) is not True


@dataclass
class DiagnoseRun:
    """One run of a diagnose session. In memory only."""

    session_id: str
    owner: str
    target: str
    run_id: str
    # BTP destination of the target's ARC-1 server (``IdeConventions.
    # destination``); empty in local development (``IDE_ARC1_URL_<TARGET>``).
    destination: str = ""
    # The run's answer of ``masking_required``. Defaults to masked.
    masking: bool = True
    masker: Masker = field(default_factory=Masker)
    findings: list[dict] = field(default_factory=list)
    # The trace proposals of this run (``approvals.approval_json``), in order.
    approvals: list[dict] = field(default_factory=list)
    emit: Callable[[str, dict], None] | None = None
    # One proposal at a time: two identical ``trace_start`` calls in one model
    # response run concurrently and must end as one approval.
    proposal_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )


current_diagnose: ContextVar[DiagnoseRun | None] = ContextVar(
    "current_diagnose", default=None
)


def run_for(session_id: str) -> DiagnoseRun | None:
    """The bound run if it belongs to ``session_id``, else ``None``."""
    run = current_diagnose.get()
    if run is None or run.session_id != session_id:
        return None
    return run


_MAX_UNWRAP = 8


def is_target_server(toolset: Any, run: DiagnoseRun) -> bool:
    """True when ``toolset`` is the MCP server of the run's target.

    With a destination on the target's conventions: an MCP server whose
    requests go through that destination. Without one (local development):
    the server at ``IDE_ARC1_URL_<TARGET>``. Wrappers (the guard, a prefix)
    are looked through. Anything that cannot be recognised is not the
    target's server.
    """
    server = toolset
    for _ in range(_MAX_UNWRAP):
        inner = getattr(server, "wrapped", None)
        if inner is None:
            break
        server = inner
    if run.destination:
        from agents.destination_auth import DestinationAuth

        auth = getattr(getattr(server, "http_client", None), "auth", None)
        return (
            isinstance(auth, DestinationAuth)
            and auth.destination_name == run.destination
        )
    from agents.ide.arc1 import env_url_name
    from agents.shared import mcp_endpoint_url

    url = os.environ.get(env_url_name(run.target), "").strip()
    return bool(url) and getattr(server, "url", None) == mcp_endpoint_url(url)


def result_text(result: Any) -> str:
    """A tool result as the text that gets masked."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        return json.dumps(result, default=str)
    if isinstance(result, (bytes, bytearray)):
        return bytes(result).decode("utf-8", errors="replace")
    return str(result)


# Keys whose value names a user in results that are not diagnose data
# (transport owners, version authors, ATC "last changed by").
_PLAIN_USER_KEYS = frozenset(k.lower() for k in USER_KEYS) | frozenset(
    {"author", "creator", "modifiedby", "responsible", "createdby", "changedby",
     "lastchangedby", "userid", "user_id", "as4user"}
)
_MAX_PLAIN_NODES = 50_000


def _learn_users(data: Any, masker: Masker) -> None:
    """Register the values of user keys anywhere in a parsed JSON result."""
    todo, seen = [data], 0
    while todo and seen < _MAX_PLAIN_NODES:
        node = todo.pop()
        seen += 1
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    isinstance(key, str)
                    and key.lower() in _PLAIN_USER_KEYS
                    and isinstance(value, str)
                    and value.strip()
                ):
                    masker.pseudonym(value)
                elif isinstance(value, (dict, list)):
                    todo.append(value)
        elif isinstance(node, list):
            todo.extend(v for v in node if isinstance(v, (dict, list)))


def mask_plain(text: str, masker: Masker) -> str:
    """Mask a result that is not a diagnose-data payload (source, lint,
    search, transports): user names -- those the run already knows, the
    values of user keys and free-text mentions -- become pseudonyms, and
    e-mail addresses, IBANs and long numbers are redacted. The text keeps
    its shape, so source stays readable. Fails towards metadata only.
    """
    try:
        if text.lstrip()[:1] in ("{", "["):
            try:
                _learn_users(json.loads(text), masker)
            except (ValueError, RecursionError):
                pass
        return mask_text(text, masker)
    except Exception as exc:  # noqa: BLE001 -- never fall through with raw data
        logger.warning("plain result masking failed: %s", type(exc).__name__)
        return json.dumps(
            {"masked": True, "shape": "unknown",
             "chars": len(text) if isinstance(text, str) else 0, "keys": 0}
        )


def strip_activity(activity: dict) -> dict:
    """A copy of a run's activity without any tool output.

    Used for masked runs only: what a tool returned was masked before it was
    recorded, and is still not kept. Every tool event loses its output, not
    just the ``SAPDiagnose`` data calls -- the stored event carries the
    argument preview, not the arguments, so "was this a data call" cannot be
    answered reliably afterwards.
    """
    events = []
    for event in activity.get("events") or []:
        if isinstance(event, dict) and event.get("kind") == "tool":
            event = {**event, "output": STRIPPED_OUTPUT}
        events.append(event)
    return {**activity, "events": events}
