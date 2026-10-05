"""What a diagnose run carries, and the one switch that allows it.

**The switch.** :func:`is_non_production` is the only place that decides
whether a target may be diagnosed. A target flagged ``non_production`` in
its conventions is open to diagnose sessions: tool results reach the model
as ARC-1 sent them, the run's activity and finding detail are stored as
produced. Every other target -- including one whose conventions are
missing, could not be loaded, or carry anything but a real ``True`` in the
flag -- is treated as production. The switch fails towards production.

**Production means refused.** A diagnose session exists only on a
``non_production`` target (checked at create). When the switch says "not
non-production" for an existing session -- the flag was taken away, the
conventions are gone or unreadable -- runs are not started
(``stages.assert_can_run``, ``runner._start``) and the routes read nothing
from it (``routes._refuse_lost_flag``, ``routes._client_for``), all with 409
``target_not_non_production``; a trace approval answers 403. There is no
masked mode to fall back to: the diagnose masking layer of phase 1c was
removed (kept on the local branch ``park/ide-masking``).

**The binding.** :data:`current_diagnose` is set by the run task and reset
by that same task. A diagnose session without a binding for *that* session
is refused by the guard (``agents.ide.readonly.ReadOnlyGuard``).

**The target's server.** Diagnose rights belong to the session target's
ARC-1 server only (:func:`is_target_server`). A tool of any other server an
agent happens to have -- even one named ``..._SAPDiagnose`` -- is held to the
change policy.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable


def is_non_production(target_conventions: Any) -> bool:
    """True only when the target's conventions say ``non_production`` is ``True``.

    ``target_conventions`` is the target's ``IdeConventions`` row, or
    ``None`` when there is none or it could not be loaded. Anything but the
    boolean ``True`` in the flag (missing, ``None``, a string, a number, a
    dict standing in for a row) means production.
    """
    if target_conventions is None or isinstance(target_conventions, dict):
        return False
    return getattr(target_conventions, "non_production", None) is True


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
    """A tool result as text (what findings are extracted from)."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        return json.dumps(result, default=str)
    if isinstance(result, (bytes, bytearray)):
        return bytes(result).decode("utf-8", errors="replace")
    return str(result)
