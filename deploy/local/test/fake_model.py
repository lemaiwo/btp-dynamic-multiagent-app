"""A stand-in model for testing the stack without an API key (test only).

Speaks just enough of the OpenAI chat completions API for the app
(``OPENAI_BASE_URL``): it answers with fixed text, and when the prompt says
"ask the <agent>" and a delegation tool is offered, it calls that tool; an
agent with the OData tools searches the catalogue for the words of the
question and lists five rows of the first hit. So
the whole path (sign-in, chat stream, orchestrator, specialist, the
specialist's MCP session as the signed-in user) runs without a real model.
It understands nothing: use a real key to judge an answer.
"""

from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "test-model"


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def _arguments(schema: dict, text: str) -> str:
    """Fill a tool's required string parameters with the user's text."""
    properties = (schema or {}).get("properties") or {}
    wanted = (schema or {}).get("required") or list(properties)
    return json.dumps({
        name: text for name in wanted
        if (properties.get(name) or {}).get("type", "string") == "string"
    })


_STOP = {"please", "which", "what", "show", "list", "give", "from", "with", "have", "that",
         "this", "the", "and", "for", "are", "any", "all", "some", "about", "data", "read",
         "system", "there", "does", "tell"}


def _call_behind(messages: list, tool_message: dict) -> dict:
    """The tool call (name, arguments) a tool message answers."""
    wanted = tool_message.get("tool_call_id")
    for message in reversed(messages):
        for call in message.get("tool_calls") or []:
            if call.get("id") == wanted:
                return call.get("function") or {}
    return {}


def _find_target(node) -> dict | None:
    """The first ``{service, target}`` of a search result that can be listed."""
    if isinstance(node, dict):
        if isinstance(node.get("service"), str) and isinstance(node.get("target"), str):
            operations = node.get("operations")
            if not isinstance(operations, list) or "list" in operations:
                return node
        node = list(node.values())
    if isinstance(node, list):
        for item in node:
            found = _find_target(item)
            if found:
                return found
    return None


def _odata(body: dict, messages: list, last: dict) -> dict:
    """A scripted read through the OData tools: search, then list five rows."""
    if last.get("role") != "tool":
        text = re.sub(r"ask the [\w-]+", " ", _text_of(last), flags=re.IGNORECASE)
        words = [w for w in re.findall(r"[A-Za-z]{4,}", text) if w.lower() not in _STOP]
        return {"tool": "search_operations", "arguments": json.dumps({"query": " ".join(words[:2])})}
    behind = _call_behind(messages, last)
    content = _text_of(last)
    if behind.get("name") == "search_operations":
        try:
            target = _find_target(json.loads(content))
        except ValueError:
            target = None
        if target:
            return {"tool": "execute_operation", "arguments": json.dumps({
                "service": target["service"], "target": target["target"],
                "operation": "list", "top": 5})}
        asked = json.loads(behind.get("arguments") or "{}").get("query")
        if asked:  # nothing under those words: list everything once
            return {"tool": "search_operations", "arguments": json.dumps({"query": ""})}
        return {"text": "The catalogue search returned nothing I can list: " + content[:400]}
    return {"text": "Read from SAP as the signed-in user (stand-in model, first rows): "
                    + content[:1800]}


def decide(body: dict) -> dict:
    """``{"text": ...}`` or ``{"tool": name, "arguments": json}``."""
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    offered = {t["function"]["name"] for t in body.get("tools") or [] if t.get("type") == "function"}
    if {"search_operations", "execute_operation"} <= offered:
        return _odata(body, messages, last)
    if last.get("role") == "tool":
        return {"text": "The specialist answered: " + _text_of(last)[:2000]}
    text = _text_of(last)
    delegates = [
        tool["function"] for tool in body.get("tools") or []
        if tool.get("type") == "function"
        and tool["function"]["name"].startswith("delegate")
    ]
    wanted = re.search(r"ask the ([\w-]+)", text, re.IGNORECASE)
    if wanted and delegates:
        key = re.sub(r"[^a-z0-9]", "", wanted.group(1).lower())
        chosen = next(
            (d for d in delegates if key in re.sub(r"[^a-z0-9]", "", d["name"].lower())),
            delegates[0],
        )
        return {"tool": chosen["name"], "arguments": _arguments(chosen.get("parameters"), text)}
    tools = sorted(offered)
    return {"text": f"Test model reply. I was offered {len(tools)} tools"
                    + (f" ({', '.join(tools[:12])})" if tools else "") + f". You said: {text[:200]}"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # quiet
        return

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not self.path.endswith("/chat/completions"):
            self._json(404, {"error": {"message": "only chat completions here"}})
            return
        decision = decide(body)
        ident, created = f"chatcmpl-{int(time.time() * 1000)}", int(time.time())
        usage = {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49}
        call = None
        if "tool" in decision:
            call = {"id": f"call_{created}", "type": "function",
                    "function": {"name": decision["tool"], "arguments": decision["arguments"]}}
        if not body.get("stream"):
            message = {"role": "assistant", "content": decision.get("text")}
            if call:
                message["tool_calls"] = [call]
            self._json(200, {"id": ident, "object": "chat.completion", "created": created,
                             "model": MODEL, "usage": usage,
                             "choices": [{"index": 0, "message": message,
                                          "finish_reason": "tool_calls" if call else "stop"}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(delta: dict, finish: str | None = None, **extra) -> None:
            payload = {"id": ident, "object": "chat.completion.chunk", "created": created,
                       "model": MODEL,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

        chunk({"role": "assistant", "content": ""})
        if call:
            chunk({"tool_calls": [{"index": 0, **call}]})
            chunk({}, "tool_calls")
        else:
            for word in re.findall(r"\S+\s*", decision["text"]):
                chunk({"content": word})
                time.sleep(0.01)
            chunk({}, "stop")
        final = {"id": ident, "object": "chat.completion.chunk", "created": created,
                 "model": MODEL, "choices": [], "usage": usage}
        self.wfile.write(f"data: {json.dumps(final)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
