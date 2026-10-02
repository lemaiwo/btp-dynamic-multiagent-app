"""What an API-triggered agent run is doing, while it is doing it.

A job run used to be a black box until it finished: the row held a status and,
at the end, a report. The admin already re-fetches a live run every few
seconds, so all it lacked was something to show. This module is that
something.

It plugs into the same side-channel the chat uses (:mod:`agents.progress`):
the job runner installs a sink for the run, and every ``report_*`` call a
specialist, a delegated peer or a deep sub-agent already makes lands here as
one entry in the run's activity. ``write_todos`` calls also replace the run's
current plan, so a deep agent's todo list can be shown as it changes.

The activity lives in memory while the run is live (it changes several times
a second; writing each change to Postgres would cost more than the run) and is
written to the run row once, when the run ends, so it stays readable after.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from agents.progress import ProgressUpdate, current_progress

# A long deep run makes a few hundred tool calls; keep the most recent ones.
MAX_EVENTS = 500
_DETAIL_CHARS = 240
_OUTPUT_CHARS = 400


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _detail(args: Any) -> str:
    """A one-line preview of a tool call's arguments."""
    if args is None:
        return ""
    if isinstance(args, dict):
        parts = []
        for key, value in args.items():
            shown = value if isinstance(value, str) else json.dumps(value, default=str)
            parts.append(f"{key}={shown}")
        return _clip(", ".join(parts), _DETAIL_CHARS)
    return _clip(str(args), _DETAIL_CHARS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunActivity:
    """The activity of one run: an ordered event list and the current plan.

    Installed directly as the progress sink. ``interactive = False`` tells the
    registry nobody behind this sink can click a sign-in link, so a run that
    needs one fails fast with the link as its answer, exactly as it did before
    runs recorded anything (see ``agents.progress.is_interactive``).
    """

    interactive = False

    def __call__(self, update: ProgressUpdate) -> None:
        self.record(update)

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.plan: list[dict[str, str]] = []
        self._open: dict[str, dict[str, Any]] = {}  # tool_call_id -> its event
        self._dropped = 0

    def _append(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if len(self.events) > MAX_EVENTS:
            gone = self.events.pop(0)
            self._open.pop(gone.get("id") or "", None)
            self._dropped += 1

    def record(self, update: ProgressUpdate) -> None:
        if update.kind == "tool_start":
            if update.tool_name == "write_todos" and isinstance(update.args, dict):
                self._set_plan(update.args.get("todos"))
            event = {
                "ts": _now(), "agent": update.agent, "kind": "tool",
                "id": update.tool_call_id, "tool": update.tool_name or "?",
                "detail": _detail(update.args), "status": "running", "output": "",
            }
            if update.tool_call_id:
                self._open[update.tool_call_id] = event
            self._append(event)
        elif update.kind == "tool_end":
            event = self._open.pop(update.tool_call_id or "", None)
            if event is not None:
                event["status"] = "ok" if update.ok else "error"
                event["output"] = _clip(update.output or "", _OUTPUT_CHARS)
                event["ended"] = _now()
        elif update.kind in ("note", "message", "delegation_start"):
            if update.text:
                self._append({
                    "ts": _now(), "agent": update.agent, "kind": update.kind,
                    "detail": _clip(update.text, _DETAIL_CHARS * 2),
                })

    def _set_plan(self, todos: Any) -> None:
        if not isinstance(todos, list):
            return
        plan = []
        for item in todos:
            if isinstance(item, dict) and item.get("content"):
                plan.append({
                    "content": _clip(str(item["content"]), _DETAIL_CHARS),
                    "status": str(item.get("status") or "pending"),
                })
        self.plan = plan

    def close_open_calls(self) -> None:
        """Mark calls that never returned (timeout, cancel) as interrupted."""
        for event in self._open.values():
            event["status"] = "error"
            event["output"] = "(interrupted)"
        self._open.clear()

    def to_dict(self) -> dict[str, Any]:
        return {"events": list(self.events), "plan": list(self.plan),
                "dropped": self._dropped}


# run id -> activity, for runs in progress in this process
_live: dict[str, RunActivity] = {}


@contextmanager
def recording(run_id: str) -> Iterator[RunActivity]:
    """Collect the progress reports of everything run inside this block.

    The sink is a contextvar, so it reaches every task the run spawns
    (delegations, sub-agents) and nothing outside it.
    """
    activity = RunActivity()
    _live[run_id] = activity
    token = current_progress.set(activity)
    try:
        yield activity
    finally:
        current_progress.reset(token)
        activity.close_open_calls()
        _live.pop(run_id, None)


def live_activity(run_id: str) -> dict[str, Any] | None:
    """The activity of a run still in progress here, or None."""
    activity = _live.get(run_id)
    return activity.to_dict() if activity is not None else None


def final_activity(run_id: str) -> dict[str, Any] | None:
    """The activity to store on a run that is ending: calls still open are
    closed as interrupted first, so the stored copy never shows one running."""
    activity = _live.get(run_id)
    if activity is None:
        return None
    activity.close_open_calls()
    return activity.to_dict()
