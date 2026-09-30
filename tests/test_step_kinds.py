"""Deterministic workflow step kinds: condition, transform, http, python.

Each executor is tested on its own, without the workflow runner: the runner's
job is to dispatch and record, and `tests/test_workflow_step_kinds_runner.py`
covers that. The http kind runs against an httpx MockTransport with a resolver
double, the python kind against the real subprocess sandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import step_kinds as sk  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def ctx(text: str = "", item: dict | None = None, sources: dict | None = None) -> sk.StepContext:
    return sk.StepContext(text=text, item=item, sources=sources or {})


async def run(kind: str, config: dict, context: sk.StepContext, **kw) -> sk.StepOutcome:
    return await sk.execute_step(kind, config, context, **kw)


def rule(when: dict, then: dict | None = None) -> dict:
    return {"when": when, "then": then or {"action": "continue"}}


# ---------------------------------------------------------------------------
# paths and templates
# ---------------------------------------------------------------------------
def test_get_path_walks_dicts_lists_and_indexes():
    data = {"a": {"b": [10, {"c": "hi"}]}, "n": 0, "z": None}
    assert sk.get_path(data, "a.b[1].c") == (True, "hi")
    assert sk.get_path(data, "a.b[0]") == (True, 10)
    assert sk.get_path(data, "a.b[-1].c") == (True, "hi")
    assert sk.get_path(data, "n") == (True, 0)
    assert sk.get_path(data, "z") == (True, None)
    assert sk.get_path(data, "") == (True, data)
    assert sk.get_path(data, "a.b[9]") == (False, None)
    assert sk.get_path(data, "a.nope") == (False, None)
    assert sk.get_path(data, "a.b.c") == (False, None)


def test_render_template_knows_text_item_json_and_source():
    c = ctx('{"k": {"v": 5}, "arr": [1, 2]}', item={"id": "m1", "title": "T"},
            sources={"reader": "from reader", "transform#2": "t2"})
    out = sk.render_template(
        "t=[{{text}}] id={{item.id}} v={{json.k.v}} arr={{json.arr}} "
        "s={{source.reader}} s2={{ source.transform#2 }} u={{nope}} i2={{item.missing}}",
        c,
    )
    assert out.startswith('t=[{"k": {"v": 5}, "arr": [1, 2]}] id=m1 v=5 arr=')
    assert "s=from reader s2=t2 u= i2=" in out
    assert "[\n  1,\n  2\n]" in out  # non-scalars render as JSON


def test_render_template_never_evaluates():
    c = ctx("x")
    assert sk.render_template("{{ 1 + 1 }} {{__import__('os')}}", c) == " "


def test_json_context_unwraps_code_fences_and_reports_parse_state():
    assert ctx('```json\n{"a": 1}\n```').json_data == {"a": 1}
    bad = ctx("not json")
    assert bad.json_ok() is False and bad.json_data is None


def test_context_from_sources_joins_several_sources():
    c = sk.context_from_sources([("abap", "dump"), ("fiori", "ui")], item=None)
    assert "## From abap\ndump" in c.text and "## From fiori\nui" in c.text
    assert c.sources == {"abap": "dump", "fiori": "ui"}
    one = sk.context_from_sources([("reader", "body")], item=SimpleNamespace(
        model_dump=lambda: {"id": "x"}))
    assert one.text == "body" and one.item == {"id": "x"}


# ---------------------------------------------------------------------------
# condition
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("op,value,text,expected", [
    ("contains", "dump", "Found a DUMP here", True),
    ("contains", "dump", "clean", False),
    ("not_contains", "dump", "clean", True),
    ("equals", "ok", "OK", True),
    ("not_equals", "ok", "OK", False),
    ("matches", r"^err(or)?:", "Error: x", True),
    ("not_matches", r"^err", "fine", True),
    ("gt", "3", "10", True),
    ("gt", "3", "2", False),
    ("lt", "3", "2.5", True),
    ("gt", "2025-12-31", "2026-01-02", True),  # lexical fallback for non-numbers
    ("is_empty", "", "   ", True),
    ("not_empty", "", "x", True),
    ("not_empty", "", "", False),
])
def test_condition_ops_on_text(op, value, text, expected):
    when = sk.ConditionWhen(source="text", op=op, value=value)
    assert sk.evaluate_rule(when, ctx(text)) is expected


def test_condition_case_sensitive_flag():
    assert sk.evaluate_rule(
        sk.ConditionWhen(op="contains", value="Dump", case_sensitive=True), ctx("a dump")
    ) is False
    assert sk.evaluate_rule(
        sk.ConditionWhen(op="matches", value="^A", case_sensitive=True), ctx("abc")
    ) is False


def test_condition_item_and_json_paths():
    c = ctx('{"issue": {"priority": "High", "labels": ["sap", "urgent"]}, "count": 3}',
            item={"id": "m1", "branches": ["abap"], "title": ""})
    assert sk.evaluate_rule(sk.ConditionWhen(source="item", field="id", op="equals", value="m1"), c)
    assert sk.evaluate_rule(sk.ConditionWhen(source="item", field="branches", op="contains", value="abap"), c)
    assert sk.evaluate_rule(sk.ConditionWhen(source="item", field="title", op="is_empty"), c)
    assert sk.evaluate_rule(sk.ConditionWhen(source="item", field="missing", op="is_empty"), c)
    assert sk.evaluate_rule(sk.ConditionWhen(source="json", field="issue.priority", op="equals", value="high"), c)
    assert sk.evaluate_rule(sk.ConditionWhen(source="json", field="issue.labels", op="contains", value="URGENT"), c)
    assert sk.evaluate_rule(sk.ConditionWhen(source="json", field="issue.labels[1]", op="equals", value="urgent"), c)
    assert sk.evaluate_rule(sk.ConditionWhen(source="json", field="count", op="gt", value="2"), c)
    assert sk.evaluate_rule(sk.ConditionWhen(source="json", field="count", op="lt", value="2"), c) is False


def test_condition_missing_operands_are_false():
    # No item outside a branch; text that is not JSON.
    c = ctx("plain text", item=None)
    assert sk.evaluate_rule(sk.ConditionWhen(source="item", field="id", op="not_empty"), c) is False
    assert sk.evaluate_rule(sk.ConditionWhen(source="item", field="id", op="equals", value=""), c) is False
    assert sk.evaluate_rule(sk.ConditionWhen(source="json", field="a", op="not_empty"), c) is False
    assert sk.evaluate_rule(sk.ConditionWhen(source="json", field="a", op="equals", value="x"), c) is False
    # is_empty on a missing operand is true: "nothing there" is empty.
    assert sk.evaluate_rule(sk.ConditionWhen(source="json", field="a", op="is_empty"), c) is True


async def test_condition_first_matching_rule_wins_and_renders_output():
    config = {
        "rules": [
            rule({"op": "contains", "value": "nothing"}, {"action": "stop", "output": "skip: {{text}}"}),
            rule({"op": "contains", "value": "dump"}, {"action": "continue", "output": "DUMP {{item.id}}"}),
            rule({"op": "contains", "value": "dump"}, {"action": "stop"}),
        ],
        "else": {"action": "stop", "output": "no rule"},
    }
    out = await run("condition", config, ctx("a dump", item={"id": "m9"}))
    assert (out.action, out.output, out.detail) == ("continue", "DUMP m9", "rule 2")
    out = await run("condition", config, ctx("nothing here"))
    assert (out.action, out.output) == ("stop", "skip: nothing here")
    out = await run("condition", config, ctx("other"))
    assert (out.action, out.output, out.detail) == ("stop", "no rule", "else")


async def test_condition_empty_output_passes_text_through():
    config = {"rules": [rule({"op": "not_empty"}, {"action": "continue", "output": ""})]}
    out = await run("condition", config, ctx("  keep me  "))
    assert out.output == "  keep me  " and out.action == "continue"
    out = await run("condition", {"rules": []}, ctx("fallthrough"))
    assert out.output == "fallthrough" and out.action == "continue" and out.detail == "else"


def test_condition_config_validation_messages():
    with pytest.raises(ValueError, match="op"):
        sk.parse_step_config("condition", {"rules": [rule({"op": "nope"})]})
    with pytest.raises(ValueError, match="action"):
        sk.parse_step_config("condition", {"else": {"action": "explode"}})
    with pytest.raises(ValueError, match="extra|forbidden|permitted"):
        sk.parse_step_config("condition", {"rulez": []})
    with pytest.raises(ValueError, match="JSON object"):
        sk.parse_step_config("condition", [1, 2])
    # A JSON string is accepted (that is how config_json is stored).
    parsed = sk.parse_step_config("condition", '{"rules": [], "else": {"action": "stop"}}')
    assert parsed.else_.action == "stop"
    assert json.loads(sk.config_to_json("condition", parsed.model_dump(by_alias=True)))["else"]["action"] == "stop"


# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------
async def test_transform_applies_stages_in_order():
    c = ctx('{"issue": {"key": "ABC-1", "summary": "Login FAILS on Monday"}}',
            item={"id": "m1"}, sources={"reader": "r"})
    out = await run("transform", {
        "extract_json": "issue.summary",
        "regex": {"pattern": "fails", "replace": "works", "flags": "i"},
        "template": "[{{item.id}}] {{text}} ({{json.issue.key}}) <{{source.reader}}>",
        "truncate": 60,
    }, c)
    assert out.output == "[m1] Login works on Monday (ABC-1) <r>"


async def test_transform_truncate_and_passthrough():
    out = await run("transform", {"truncate": 5}, ctx("abcdefghij"))
    assert out.output.startswith("abcde") and out.output.endswith("[truncated]")
    out = await run("transform", {}, ctx("unchanged"))
    assert out.output == "unchanged"


async def test_transform_extract_json_on_non_json_fails():
    with pytest.raises(sk.StepFailed, match="not"):
        await run("transform", {"extract_json": "a"}, ctx("plain"))
    with pytest.raises(sk.StepFailed, match="not present"):
        await run("transform", {"extract_json": "missing"}, ctx('{"a": 1}'))


async def test_transform_extract_json_renders_non_scalars_as_json():
    out = await run("transform", {"extract_json": "a"}, ctx('{"a": {"b": [1, 2]}}'))
    assert json.loads(out.output) == {"b": [1, 2]}


def test_transform_config_validation():
    with pytest.raises(ValueError, match="regex"):
        sk.parse_step_config("transform", {"regex": {"pattern": "(", "replace": ""}})
    with pytest.raises(ValueError, match="flag"):
        sk.parse_step_config("transform", {"regex": {"pattern": "a", "flags": "q"}})
    with pytest.raises(ValueError, match="truncate"):
        sk.parse_step_config("transform", {"truncate": 0})


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------
class FakeResolver:
    def __init__(self, url="https://api.example.com/base", token="Bearer t1"):
        self.url = url
        self.token = token
        self.resolves = 0
        self.invalidated = 0

    async def resolve(self, *, force=False):
        self.resolves += 1
        return SimpleNamespace(url=self.url, headers={"Authorization": self.token}, expires_at=1e12)

    def invalidate(self):
        self.invalidated += 1
        self.token = "Bearer t2"


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_http_get_pretty_prints_json_and_sends_destination_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["x"] = request.headers.get("x-extra")
        return httpx.Response(200, json={"b": 1, "a": [1]}, headers={"content-type": "application/json"})

    resolver = FakeResolver()
    out = await run("http", {
        "destination": "jira", "method": "get", "path": "/issue/{{item.id}}",
        "query": {"fields": "summary", "q": "{{text}}"}, "headers": {"X-Extra": "y"},
    }, ctx("hello world", item={"id": "ABC-7"}), http_resolver=resolver, http_transport=transport(handler))
    assert seen["url"] == "https://api.example.com/base/issue/ABC-7?fields=summary&q=hello+world"
    assert seen["auth"] == "Bearer t1" and seen["x"] == "y"
    assert json.loads(out.output) == {"b": 1, "a": [1]} and "\n" in out.output
    assert out.detail == "HTTP 200"


async def test_http_post_sends_rendered_body_and_content_type():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["ct"] = request.headers.get("content-type")
        seen["body"] = request.content.decode()
        return httpx.Response(201, text="created", headers={"content-type": "text/plain"})

    out = await run("http", {
        "destination": "d", "method": "POST", "path": "/comment",
        "body": '{"text": "{{json.summary}}"}',
    }, ctx('{"summary": "hi"}'), http_resolver=FakeResolver(), http_transport=transport(handler))
    assert (seen["method"], seen["ct"], seen["body"]) == ("POST", "application/json", '{"text": "hi"}')
    assert out.output == "created"


async def test_http_non_expected_status_fails_with_excerpt():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom " * 300)

    with pytest.raises(sk.StepFailed) as e:
        await run("http", {"destination": "d", "path": "/x"}, ctx(),
                  http_resolver=FakeResolver(), http_transport=transport(handler))
    msg = str(e.value)
    assert "HTTP 500" in msg and "2xx" in msg and "boom" in msg and len(msg) < 800

    # An explicit expect_status accepts what it lists.
    out = await run("http", {"destination": "d", "path": "/x", "expect_status": [500]}, ctx(),
                    http_resolver=FakeResolver(), http_transport=transport(handler))
    assert out.output.startswith("boom")


async def test_http_retries_once_after_401():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("authorization"))
        return httpx.Response(401 if len(calls) == 1 else 200, text="ok")

    resolver = FakeResolver()
    out = await run("http", {"destination": "d", "path": "/x"}, ctx(),
                    http_resolver=resolver, http_transport=transport(handler))
    assert out.output == "ok" and calls == ["Bearer t1", "Bearer t2"] and resolver.invalidated == 1


@pytest.mark.parametrize("path", [
    "https://evil.example.com/x", "//evil.example.com/x", "relative", "/a/../b",
    "/a/./b", "/a//b", "/a?b=1", "/a#frag", "/a\\b",
])
def test_http_path_confinement_at_save_time(path):
    with pytest.raises(ValueError, match="path"):
        sk.parse_step_config("http", {"destination": "d", "path": path})


async def test_http_path_confinement_after_rendering():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not be called")

    with pytest.raises(sk.StepFailed, match="'..'"):
        await run("http", {"destination": "d", "path": "/issue/{{item.id}}"},
                  ctx(item={"id": "../../admin"}), http_resolver=FakeResolver(),
                  http_transport=transport(handler))


def test_http_needs_a_destination_and_a_method():
    with pytest.raises(ValueError, match="destination"):
        sk.parse_step_config("http", {"path": "/x"})
    with pytest.raises(ValueError, match="destination"):
        sk.parse_step_config("http", {"destination": "  ", "path": "/x"})
    with pytest.raises(ValueError, match="method"):
        sk.parse_step_config("http", {"destination": "d", "method": "BREW"})
    with pytest.raises(ValueError, match="expect_status"):
        sk.parse_step_config("http", {"destination": "d", "expect_status": [999]})


async def test_http_without_binding_fails_clearly(monkeypatch):
    for key in ("VCAP_SERVICES", "DESTINATION_URL", "DESTINATION_CLIENT_ID"):
        monkeypatch.delenv(key, raising=False)
    sk._resolvers.clear()
    with pytest.raises(sk.StepFailed, match="destination 'nope'"):
        await run("http", {"destination": "nope", "path": "/x"}, ctx())


# ---------------------------------------------------------------------------
# python
# ---------------------------------------------------------------------------
async def test_python_output_string_and_json():
    out = await run("python", {"code": "output = text.upper() + '|' + item['id'] + '|' + sources['reader']"},
                    ctx("hi", item={"id": "m1"}, sources={"reader": "r"}))
    assert out.output == "HI|m1|r"
    out = await run("python", {"code": "output = {'n': json_data['n'] + 1, 'k': sorted(json_data)}"},
                    ctx('{"n": 1, "z": 0}'))
    assert json.loads(out.output) == {"n": 2, "k": ["n", "z"]}


async def test_python_preimported_modules_and_allowed_imports():
    code = (
        "import re, math\n"
        "from urllib.parse import quote\n"
        "from collections import Counter\n"
        "output = quote('a b') + str(math.floor(2.5)) + re.sub('x', 'y', 'x') "
        "+ str(Counter('aab')['a']) + datetime.date(2026, 1, 2).isoformat() + hashlib.sha256(b'x').hexdigest()[:4]"
    )
    out = await run("python", {"code": code}, ctx())
    assert out.output.startswith("a%20b2y22026-01-02")


@pytest.mark.parametrize("code,needle", [
    ("import os\noutput = 1", "not available"),
    ("import sys\noutput = 1", "not available"),
    ("import subprocess\noutput = 1", "not available"),
    ("import socket\noutput = 1", "not available"),
    ("import ctypes\noutput = 1", "not available"),
    ("import importlib\noutput = 1", "not available"),
    ("import builtins\noutput = 1", "not available"),
    ("output = __import__('os').getcwd()", "not available"),
    ("from urllib import request\noutput = 1", "not available"),
    ("output = open('/etc/passwd').read()", "name 'open' is not defined"),
    ("from os import path\noutput = 1", "not available"),
])
async def test_python_blocked_imports_and_open(code, needle):
    with pytest.raises(sk.StepFailed) as e:
        await run("python", {"code": code}, ctx())
    assert needle in str(e.value)
    assert "python step raised" in str(e.value)


async def test_python_exception_gives_traceback_tail_without_runner_frames():
    with pytest.raises(sk.StepFailed) as e:
        await run("python", {"code": "x = 1\ny = x / 0\noutput = y"}, ctx())
    msg = str(e.value)
    assert "ZeroDivisionError" in msg and 'line 2' in msg
    assert "_python_step_runner" not in msg


async def test_python_missing_output_is_an_error():
    with pytest.raises(sk.StepFailed, match="assign a value to `output`"):
        await run("python", {"code": "x = 1"}, ctx())


async def test_python_timeout_kills_the_subprocess():
    started = asyncio.get_event_loop().time()
    with pytest.raises(sk.StepFailed, match="exceeded its 1s timeout"):
        await run("python", {"code": "while True: pass", "timeout_seconds": 1}, ctx())
    assert asyncio.get_event_loop().time() - started < 5


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the cap is a POSIX rlimit applied through preexec_fn; Windows has neither",
)
async def test_python_memory_cap():
    with pytest.raises(sk.StepFailed, match="MemoryError"):
        await run("python", {"code": "x = bytearray(600 * 1024 * 1024)\noutput = 'no'"}, ctx())


async def test_python_output_is_capped():
    out = await run("python", {"code": "output = 'x' * 200000"}, ctx())
    assert len(out.output) <= sk.PYTHON_STEP_OUTPUT_CAP + 20
    assert out.output.endswith("[truncated]")


async def test_python_prints_do_not_corrupt_the_result():
    out = await run("python", {"code": "print('noise')\nprint('{\"ok\": false}')\noutput = 'clean'"}, ctx())
    assert out.output == "clean"


def test_python_env_is_minimal_and_carries_no_secrets(monkeypatch):
    monkeypatch.setenv("CANARY_SECRET", "s3cret-value")
    monkeypatch.setenv("VCAP_SERVICES", '{"x": 1}')
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    env = sk.python_step_env()
    assert env == {"PATH": "/usr/bin:/bin"}
    # The runner itself, started the way the step starts it, sees no canary:
    # os is importable here because we call it directly, not through the
    # guarded __import__ the step code gets.
    probe = (
        "import os, sys, json\n"
        "sys.stdout.write(json.dumps({'env': sorted(os.environ), 'cwd': os.getcwd()}))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-E", "-c", probe],
        env=env, capture_output=True, text=True, timeout=20, cwd="/",
    )
    seen = json.loads(result.stdout)
    assert "CANARY_SECRET" not in seen["env"] and "VCAP_SERVICES" not in seen["env"]


def test_python_env_carries_the_library_path_and_nothing_else(monkeypatch):
    """The CF Python buildpack links the interpreter against a libpython under
    /home/vcap/deps, found only through LD_LIBRARY_PATH: without it every python
    step dies with exit code 127 before running a line."""
    monkeypatch.setenv("LD_LIBRARY_PATH", "/home/vcap/deps/0/python/lib")
    monkeypatch.setenv("CANARY_SECRET", "s3cret-value")
    monkeypatch.setenv("VCAP_SERVICES", '{"x": 1}')
    env = sk.python_step_env()
    assert env == {"PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": "/home/vcap/deps/0/python/lib"}


async def test_python_subprocess_is_started_with_the_minimal_env(monkeypatch):
    monkeypatch.setenv("CANARY_SECRET", "s3cret-value")
    # CI runners (setup-python) export LD_LIBRARY_PATH, which the step passes
    # through by design; pin it off here so the env is exactly PATH.
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    captured = {}
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    out = await run("python", {"code": "output = 'ok'"}, ctx())
    assert out.output == "ok"
    assert captured["kwargs"]["env"] == {"PATH": "/usr/bin:/bin"}
    assert captured["args"][1:4] == ("-I", "-S", "-E")
    assert captured["args"][4].endswith("_python_step_runner.py")
    assert captured["kwargs"]["cwd"] != os.getcwd()
    if sys.platform != "win32":
        assert callable(captured["kwargs"]["preexec_fn"])


def test_python_config_requires_compilable_code_and_bounded_timeout():
    with pytest.raises(ValueError, match="compile"):
        sk.parse_step_config("python", {"code": "def ("})
    with pytest.raises(ValueError, match="code"):
        sk.parse_step_config("python", {"code": "   "})
    with pytest.raises(ValueError, match="timeout_seconds"):
        sk.parse_step_config("python", {"code": "output = 1", "timeout_seconds": 61})
    with pytest.raises(ValueError, match="timeout_seconds"):
        sk.parse_step_config("python", {"code": "output = 1", "timeout_seconds": 0})


# ---------------------------------------------------------------------------
# dispatch and labels
# ---------------------------------------------------------------------------
async def test_execute_step_rejects_agent_and_unknown_kinds():
    with pytest.raises(ValueError):
        await sk.execute_step("agent", {}, ctx())
    with pytest.raises(ValueError, match="unknown step kind"):
        await sk.execute_step("shell", {}, ctx())


def test_validate_step_config_is_a_no_op_for_agents_and_labels():
    sk.validate_step_config("agent", {"anything": 1})
    assert sk.config_to_json("agent", {"x": 1}) is None
    assert sk.step_label("transform", 3) == "transform#3"
    assert sk.summarize_config("http", {"destination": "jira", "path": "/x"}) == "GET jira/x"
    assert sk.summarize_config("condition", {"rules": [rule({"op": "not_empty"})]}) == "1 rule"
    assert sk.summarize_config("transform", {}) == "pass-through"
    assert sk.summarize_config("python", {"code": "\n\noutput = text"}) == "output = text"
    assert sk.summarize_config("python", {"code": "def ("}) == "python"
