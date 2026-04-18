"""A2A (Agent-to-Agent) protocol server for SAP Joule integration.

Exposes the dynamic orchestrator as an A2A-compliant agent so it can be
registered in the Joule Agent Hub and invoked by Joule as a remote
code-based agent.

Endpoints (mounted by ``app.py``):

    GET  /.well-known/agent-card.json   — Agent Card (discovery document)
    GET  /.well-known/agent.json        — Legacy alias for agent-card.json
    POST /a2a                           — JSON-RPC 2.0 entry point
                                           methods: message/send, message/stream,
                                                    tasks/get, tasks/cancel

The JSON-RPC endpoint is protected by XSUAA (``require_user`` dependency)
so that Joule's outbound call carries a valid bearer token. Because the
existing ``JWTBindingMiddleware`` binds that token to ``current_jwt``,
downstream MCP servers still see the calling identity transparently.

Multi-turn conversations are supported via the A2A ``contextId``: each
context keeps an in-memory copy of the pydantic-ai message history so
follow-up calls keep the orchestrator's working memory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from agents.auth import require_user
from agents.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = "0.3.0"
DEFAULT_PROVIDER_ORG = "SAP BTP Dynamic Multi-Agent"
DEFAULT_PROVIDER_URL = "https://community.sap.com/t5/technology-blog-posts-by-sap/joule-a2a-connect-code-based-agents-into-joule/ba-p/14329279"


def _app_version() -> str:
    return os.environ.get("A2A_AGENT_VERSION", "2.1.0")


def _base_url(request: Request) -> str:
    """Public URL at which this app is reachable.

    Prefers ``A2A_PUBLIC_URL`` (set in ``mta.yaml``) because on Cloud
    Foundry the backend does not know the approuter hostname. Falls back
    to the request host, which is correct for local development.
    """
    override = os.environ.get("A2A_PUBLIC_URL")
    if override:
        return override.rstrip("/")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if host:
        return f"{scheme}://{host}"
    return str(request.base_url).rstrip("/")


def _security_schemes() -> dict[str, Any]:
    """Declare the auth scheme Joule should use when calling us.

    When bound to XSUAA on BTP we advertise an OAuth2 client-credentials
    flow so the Joule Agent Hub can obtain tokens from the tenant's UAA.
    Locally (no XSUAA binding) we fall back to a simple ``bearer`` scheme
    so the card is still valid.
    """
    from agents.auth import get_xsuaa_credentials

    creds = get_xsuaa_credentials()
    if creds:
        uaa_url = creds.get("url", "").rstrip("/")
        return {
            "xsuaa": {
                "type": "oauth2",
                "description": (
                    "SAP XSUAA — obtain a token via client_credentials using "
                    "the credentials of the service key bound to this app."
                ),
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": f"{uaa_url}/oauth/token",
                        "scopes": {},
                    }
                },
            }
        }
    return {
        "bearer": {
            "type": "http",
            "scheme": "bearer",
            "description": "Local development — provide a bearer token.",
        }
    }


async def build_agent_card(request: Request) -> dict[str, Any]:
    """Return the A2A AgentCard describing this orchestrator."""
    base = _base_url(request)
    build = registry.build
    enabled = [c for c in build.configs if c.get("enabled")]

    # Build one skill per enabled specialist so Joule/other clients see
    # the real capabilities. Additionally expose a general "orchestrate"
    # skill as the catch-all entry point.
    skills: list[dict[str, Any]] = [
        {
            "id": "orchestrate",
            "name": "SAP BTP Orchestrator",
            "description": (
                "Route an SAP BTP management request to the right "
                "specialist (Cloud Foundry, BTP platform, audit log, …) "
                "and return a synthesized answer."
            ),
            "tags": ["sap", "btp", "orchestrator", "multi-agent"],
            "examples": [
                "List the running applications in my dev space.",
                "Which subaccounts exist in my global account?",
                "Show the audit events for the last hour.",
            ],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
        }
    ]
    for cfg in enabled:
        skills.append(
            {
                "id": f"specialist.{cfg['name']}",
                "name": cfg["name"],
                "description": cfg["description"],
                "tags": ["sap", "btp", cfg["name"]],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
            }
        )

    security_schemes = _security_schemes()
    security = [{name: []} for name in security_schemes]

    name = os.environ.get("A2A_AGENT_NAME", "SAP BTP Multi-Agent Orchestrator")
    description = os.environ.get(
        "A2A_AGENT_DESCRIPTION",
        "Dynamic multi-agent orchestrator for SAP BTP. Delegates to "
        "specialist agents backed by BTP-hosted MCP servers.",
    )

    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": name,
        "description": description,
        "version": _app_version(),
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "provider": {
            "organization": os.environ.get("A2A_PROVIDER_ORG", DEFAULT_PROVIDER_ORG),
            "url": os.environ.get("A2A_PROVIDER_URL", DEFAULT_PROVIDER_URL),
        },
        "iconUrl": f"{base}/static/logo.svg",
        "documentationUrl": f"{base}/admin",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
        "securitySchemes": security_schemes,
        "security": security,
    }


# ---------------------------------------------------------------------------
# In-memory context + task store
#
# A2A conversations are identified by ``contextId``. We keep the
# pydantic-ai ``message_history`` per context so follow-up turns preserve
# the orchestrator's reasoning. Tasks are retained briefly so clients can
# call ``tasks/get`` after a ``message/send`` completes.
# ---------------------------------------------------------------------------
_CONTEXT_TTL_SECONDS = int(os.environ.get("A2A_CONTEXT_TTL", "3600"))
_TASK_TTL_SECONDS = int(os.environ.get("A2A_TASK_TTL", "900"))


class _ConversationStore:
    def __init__(self) -> None:
        self._contexts: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def get_history(self, context_id: str) -> list[Any]:
        async with self._lock:
            self._gc()
            entry = self._contexts.get(context_id)
            return list(entry["history"]) if entry else []

    async def set_history(self, context_id: str, history: list[Any]) -> None:
        async with self._lock:
            self._contexts[context_id] = {
                "history": history,
                "touched": time.time(),
            }

    async def save_task(self, task: dict[str, Any]) -> None:
        async with self._lock:
            self._tasks[task["id"]] = {"task": task, "touched": time.time()}

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        async with self._lock:
            self._gc()
            entry = self._tasks.get(task_id)
            return entry["task"] if entry else None

    async def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        async with self._lock:
            entry = self._tasks.get(task_id)
            if entry is None:
                return None
            task = entry["task"]
            # Only cancel in-flight tasks; completed/failed are terminal.
            if task["status"]["state"] in ("submitted", "working"):
                task["status"]["state"] = "canceled"
            return task

    def _gc(self) -> None:
        now = time.time()
        for cid in list(self._contexts):
            if now - self._contexts[cid]["touched"] > _CONTEXT_TTL_SECONDS:
                del self._contexts[cid]
        for tid in list(self._tasks):
            if now - self._tasks[tid]["touched"] > _TASK_TTL_SECONDS:
                del self._tasks[tid]


store = _ConversationStore()


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------
def _rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _extract_text(message: dict[str, Any]) -> str:
    """Concatenate ``text`` parts from an A2A Message."""
    parts = message.get("parts") or []
    chunks: list[str] = []
    for part in parts:
        if part.get("kind") == "text" and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks).strip()


def _make_agent_message(text: str, context_id: str) -> dict[str, Any]:
    return {
        "kind": "message",
        "role": "agent",
        "parts": [{"kind": "text", "text": text}],
        "messageId": str(uuid.uuid4()),
        "contextId": context_id,
    }


def _initial_task(
    task_id: str,
    context_id: str,
    user_message: dict[str, Any],
    state: str = "submitted",
) -> dict[str, Any]:
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": state,
            "timestamp": _iso_now(),
        },
        "history": [user_message],
        "artifacts": [],
    }


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Orchestrator invocation
# ---------------------------------------------------------------------------
async def _run_orchestrator(text: str, context_id: str) -> str:
    """Execute one orchestrator turn and persist the updated history."""
    history = await store.get_history(context_id)
    agent = registry.orchestrator
    try:
        result = await agent.run(text, message_history=history or None)
    except BaseException as exc:  # noqa: BLE001
        logger.exception("Orchestrator run failed (context=%s)", context_id)
        raise _OrchestratorError(str(exc)) from exc

    # Persist new conversation state for follow-up turns
    try:
        new_history = list(result.all_messages())
    except Exception:
        new_history = history
    await store.set_history(context_id, new_history)

    return str(result.output)


class _OrchestratorError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------
async def _handle_message_send(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    message = params.get("message")
    if not isinstance(message, dict):
        return _rpc_error(req_id, -32602, "Invalid params: 'message' is required")

    text = _extract_text(message)
    if not text:
        return _rpc_error(req_id, -32602, "Invalid params: message has no text parts")

    context_id: str = message.get("contextId") or str(uuid.uuid4())
    user_message = {
        "role": "user",
        "parts": message.get("parts") or [{"kind": "text", "text": text}],
        "messageId": message.get("messageId") or str(uuid.uuid4()),
        "contextId": context_id,
        "kind": "message",
    }
    task_id = str(uuid.uuid4())
    task = _initial_task(task_id, context_id, user_message, state="working")
    await store.save_task(task)

    try:
        output = await _run_orchestrator(text, context_id)
    except _OrchestratorError as exc:
        task["status"] = {
            "state": "failed",
            "message": _make_agent_message(f"Orchestrator error: {exc}", context_id),
            "timestamp": _iso_now(),
        }
        await store.save_task(task)
        return _rpc_result(req_id, task)

    agent_message = _make_agent_message(output, context_id)
    task["status"] = {
        "state": "completed",
        "message": agent_message,
        "timestamp": _iso_now(),
    }
    task["history"].append(agent_message)
    task["artifacts"].append(
        {
            "artifactId": str(uuid.uuid4()),
            "name": "response",
            "parts": [{"kind": "text", "text": output}],
        }
    )
    await store.save_task(task)
    return _rpc_result(req_id, task)


async def _handle_tasks_get(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    task_id = params.get("id")
    if not task_id:
        return _rpc_error(req_id, -32602, "Invalid params: 'id' required")
    task = await store.get_task(task_id)
    if task is None:
        return _rpc_error(req_id, -32001, f"Task not found: {task_id}")
    return _rpc_result(req_id, task)


async def _handle_tasks_cancel(req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    task_id = params.get("id")
    if not task_id:
        return _rpc_error(req_id, -32602, "Invalid params: 'id' required")
    task = await store.cancel_task(task_id)
    if task is None:
        return _rpc_error(req_id, -32001, f"Task not found: {task_id}")
    return _rpc_result(req_id, task)


# ---------------------------------------------------------------------------
# Streaming (Server-Sent Events) for message/stream
# ---------------------------------------------------------------------------
async def _stream_message(req_id: Any, params: dict[str, Any]) -> AsyncIterator[str]:
    message = params.get("message")
    if not isinstance(message, dict) or not _extract_text(message):
        yield _sse(_rpc_error(req_id, -32602, "Invalid params: message with text required"))
        return

    context_id: str = message.get("contextId") or str(uuid.uuid4())
    text = _extract_text(message)
    user_message = {
        "role": "user",
        "parts": message.get("parts") or [{"kind": "text", "text": text}],
        "messageId": message.get("messageId") or str(uuid.uuid4()),
        "contextId": context_id,
        "kind": "message",
    }
    task_id = str(uuid.uuid4())
    task = _initial_task(task_id, context_id, user_message, state="submitted")
    await store.save_task(task)

    # Emit the initial Task with state=submitted
    yield _sse(_rpc_result(req_id, task))

    # Transition to working
    working_event = {
        "kind": "status-update",
        "taskId": task_id,
        "contextId": context_id,
        "status": {"state": "working", "timestamp": _iso_now()},
        "final": False,
    }
    yield _sse(_rpc_result(req_id, working_event))

    try:
        output = await _run_orchestrator(text, context_id)
    except _OrchestratorError as exc:
        failure = {
            "kind": "status-update",
            "taskId": task_id,
            "contextId": context_id,
            "status": {
                "state": "failed",
                "message": _make_agent_message(
                    f"Orchestrator error: {exc}", context_id
                ),
                "timestamp": _iso_now(),
            },
            "final": True,
        }
        yield _sse(_rpc_result(req_id, failure))
        return

    agent_message = _make_agent_message(output, context_id)
    artifact_event = {
        "kind": "artifact-update",
        "taskId": task_id,
        "contextId": context_id,
        "artifact": {
            "artifactId": str(uuid.uuid4()),
            "name": "response",
            "parts": [{"kind": "text", "text": output}],
        },
        "append": False,
        "lastChunk": True,
    }
    yield _sse(_rpc_result(req_id, artifact_event))

    done = {
        "kind": "status-update",
        "taskId": task_id,
        "contextId": context_id,
        "status": {
            "state": "completed",
            "message": agent_message,
            "timestamp": _iso_now(),
        },
        "final": True,
    }
    # Persist final state so tasks/get after the stream still works
    task["status"] = done["status"]
    task["history"].append(agent_message)
    task["artifacts"].append(artifact_event["artifact"])
    await store.save_task(task)

    yield _sse(_rpc_result(req_id, done))


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------
router = APIRouter(tags=["a2a"])


@router.get("/.well-known/agent-card.json")
async def get_agent_card(request: Request) -> JSONResponse:
    card = await build_agent_card(request)
    return JSONResponse(card)


@router.get("/.well-known/agent.json")
async def get_agent_card_legacy(request: Request) -> JSONResponse:
    """Legacy path for older A2A clients; identical to agent-card.json."""
    card = await build_agent_card(request)
    return JSONResponse(card)


@router.post("/a2a", dependencies=[Depends(require_user)])
async def a2a_jsonrpc(
    request: Request,
    accept: str | None = Header(default=None),
):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON-RPC envelope must be an object")

    if body.get("jsonrpc") != "2.0":
        return JSONResponse(_rpc_error(body.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'"))

    method = body.get("method")
    params = body.get("params") or {}
    req_id = body.get("id")

    if not isinstance(params, dict):
        return JSONResponse(_rpc_error(req_id, -32602, "Invalid params: must be an object"))

    if method == "message/send":
        result = await _handle_message_send(req_id, params)
        status_code = 200 if "result" in result else 200
        return JSONResponse(result, status_code=status_code)

    if method == "message/stream":
        return StreamingResponse(
            _stream_message(req_id, params),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if method == "tasks/get":
        return JSONResponse(await _handle_tasks_get(req_id, params))

    if method == "tasks/cancel":
        return JSONResponse(await _handle_tasks_cancel(req_id, params))

    return JSONResponse(
        _rpc_error(req_id, -32601, f"Method not found: {method}"),
        status_code=status.HTTP_200_OK,
    )
