"""Deterministic workflow step kinds: ``condition``, ``transform``, ``http``, ``python``.

A workflow step is an ``agent`` by default. The four kinds here run without a
model: they take the previous step's text (and, inside a branch, the fan-out
item) and hand plain text on, exactly as an agent step would. The runner
dispatches on ``WorkflowStep.kind`` and calls :func:`execute_step`; everything
about *what* a kind does lives in this module so the runner stays a scheduler.

Config shapes (``WorkflowStep.config_json``), validated by the pydantic models
below at save time (``validate_step_config``) and again when a run reaches the
step::

    condition  {"rules": [{"when": {"source": "text|item|json", "field": "a.b[0]",
                                    "op": "contains|not_contains|equals|not_equals|
                                           matches|not_matches|gt|lt|is_empty|not_empty",
                                    "value": "...", "case_sensitive": false},
                           "then": {"action": "continue|stop", "output": "<template>"}}],
                "else": {"action": "continue|stop", "output": ""}}
    transform  {"extract_json": "path or empty", "regex": {"pattern": "", "replace": "",
                "flags": "i"} | null, "template": "...", "truncate": int | null}
    http       {"destination": "name", "method": "GET", "path": "/relative", "query": {},
                "headers": {}, "body": "<template>", "content_type": "application/json",
                "timeout_seconds": 30, "expect_status": [200, ..., 299]}
    python     {"code": "...", "timeout_seconds": 10}

Templates (``transform.template``, ``condition.then.output``, ``http.path``,
``http.query`` values and ``http.body``) know four placeholders:
``{{text}}``, ``{{item.<path>}}``, ``{{json.<path>}}`` and
``{{source.<name>}}``. Unknown placeholders render empty. A template is
substituted, never evaluated: there is no expression language.

``stop`` from a condition is delivered to the runner as
``StepOutcome.action == "stop"``; what it ends (the run, or one item's branch,
or the join line) is the runner's decision, because only the runner knows
where the step sits.

The ``python`` kind runs admin-authored code in a subprocess; see
:mod:`agents._python_step_runner` for what that sandbox does and, more
importantly, what it does not guarantee.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from agents.loc_fields import loc_field

logger = logging.getLogger(__name__)

AGENT_KIND = "agent"
STEP_KINDS: tuple[str, ...] = ("agent", "condition", "transform", "http", "python")
DETERMINISTIC_KINDS: tuple[str, ...] = tuple(k for k in STEP_KINDS if k != AGENT_KIND)

RUNNER_PATH = Path(__file__).resolve().parent / "_python_step_runner.py"

# The python subprocess sees exactly this environment: no VCAP_SERVICES, no
# AICORE_*, no DATABASE_URL. PATH alone, so the interpreter can start.
PYTHON_STEP_ENV: dict[str, str] = {"PATH": "/usr/bin:/bin"}
# ...plus these, copied from the parent when set. On CF the buildpack's
# interpreter finds its libpython only through LD_LIBRARY_PATH; without it the
# child exits 127 before running a line. A library path, never a credential.
PYTHON_STEP_PASSTHROUGH: tuple[str, ...] = ("LD_LIBRARY_PATH",)
PYTHON_STEP_MEMORY_BYTES = 256 * 1024 * 1024
PYTHON_STEP_OUTPUT_CAP = 64 * 1024
HTTP_BODY_EXCERPT = 500
# The most bytes of a response body an http step reads and hands on. Larger is
# refused, not cut: a JSON document that ends early is not one, and the next
# step would read a part as the whole (the rule of `builtin:sharepoint` and
# `builtin:bitbucket`). The same size the python step hands on.
HTTP_STEP_RESPONSE_CAP = PYTHON_STEP_OUTPUT_CAP
_TRUNCATED = "…[truncated]"

# Regular expressions are admin-authored and run on text a previous step (an
# agent, an http response) produced. `re` backtracks exponentially on patterns
# such as `(a+)+$` and holds the GIL while it matches, so neither the event
# loop nor a worker thread is safe: a thread stalls every request just the
# same, and `asyncio.wait_for` cannot interrupt it. Every match therefore runs
# in a child process (the python step's sandbox: isolated interpreter, minimal
# environment, memory cap) that is killed after REGEX_TIMEOUT_SECONDS.
REGEX_TIMEOUT_SECONDS: float = 5
# The longest text a regex is applied to, in characters. Longer is refused,
# not cut: a condition over a cut text could take the other branch, and a
# replace would silently drop the tail.
REGEX_OPERAND_CAP = 64 * 1024
# The longest text a regex replace may produce (a replacement can repeat the
# match): four times the operand cap.
REGEX_RESULT_CAP = 4 * REGEX_OPERAND_CAP
_REGEX_CHILD = (
    "import json,re,sys\n"
    "d=json.loads(sys.stdin.buffer.read())\n"
    "try:\n"
    " p=re.compile(d['pattern'],d['flags'])\n"
    " if d['mode']=='search':\n"
    "  o={'ok':True,'hit':p.search(d['text']) is not None}\n"
    " else:\n"
    "  t=p.sub(d['replace'],d['text'])\n"
    "  o={'ok':True,'text':t} if len(t)<=d['cap'] else {'ok':False,'error':'too_large'}\n"
    "except re.error as e:\n"
    " o={'ok':False,'error':'re','message':str(e)}\n"
    "sys.stdout.write(json.dumps(o))\n"
)


class StepFailed(Exception):
    """A deterministic step could not produce an output.

    Carries the operator-facing message; the runner wraps it into its own
    step failure exactly as it does an agent exception.
    """


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------
_OPS = (
    "contains", "not_contains", "equals", "not_equals", "matches",
    "not_matches", "gt", "lt", "is_empty", "not_empty",
)


def _check_compiles(pattern: str, field_name: str) -> None:
    """ValueError naming ``field_name`` when ``pattern`` is no regex.

    Neither the pattern nor `re`'s text, which quotes parts of it (a group
    name, an escape): this is the answer to a refused save.
    """
    try:
        re.compile(pattern)
    except re.error as e:
        where = f" at position {e.pos}" if e.pos is not None else ""
        raise ValueError(
            f"{field_name} is not a valid regular expression{where}"
        ) from None


class ConditionWhen(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["text", "item", "json"] = "text"
    # Dotted path with [n] indexes into the item or the parsed JSON. Ignored
    # for source "text", where the whole previous text is the operand.
    field: str = ""
    op: Literal[_OPS] = "contains"  # type: ignore[valid-type]
    value: str = ""
    case_sensitive: bool = False

    @field_validator("value", mode="before")
    @classmethod
    def _value_to_str(cls, v: Any) -> Any:
        # A number typed into a UI arrives as a number; compare as text.
        return "" if v is None else (v if isinstance(v, str) else str(v))

    @model_validator(mode="after")
    def _regex_compiles(self) -> ConditionWhen:
        # A `matches` value that does not compile used to be "no match" at run
        # time, silently taking the other branch; refuse it at save instead.
        if self.op in ("matches", "not_matches"):
            _check_compiles(self.value, "value")
        return self


class ConditionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["continue", "stop"] = "continue"
    # A template; empty passes the incoming text through unchanged.
    output: str = ""


class ConditionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: ConditionWhen
    then: ConditionAction = Field(default_factory=ConditionAction)


class ConditionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    rules: list[ConditionRule] = Field(default_factory=list)
    else_: ConditionAction = Field(default_factory=ConditionAction, alias="else")


class RegexSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1)
    replace: str = ""
    # Any of i, m, s, x.
    flags: str = ""

    @field_validator("flags")
    @classmethod
    def _known_flags(cls, v: str) -> str:
        bad = [c for c in v if c not in "imsx"]
        if bad:
            # Not the characters themselves: this text is the answer to a
            # refused save, and the field holds whatever was pasted into it.
            raise ValueError(f"{len(bad)} unknown regex flag(s); use i, m, s or x")
        return v

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        _check_compiles(v, "pattern")
        return v


class TransformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extract_json: str = ""
    regex: RegexSpec | None = None
    template: str = ""
    truncate: int | None = Field(default=None, ge=1)


_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


class HttpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str = Field(min_length=1)
    method: Literal[_METHODS] = "GET"  # type: ignore[valid-type]
    path: str = "/"
    query: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    content_type: str = "application/json"
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    expect_status: list[int] = Field(default_factory=lambda: list(range(200, 300)))

    @field_validator("method", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v

    @field_validator("destination")
    @classmethod
    def _strip_destination(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("an http step needs a destination name")
        return v

    @field_validator("path")
    @classmethod
    def _confined_path(cls, v: str) -> str:
        # Templates are rendered before the request, so the rendered path is
        # confined again at run time; this catches the static shape early.
        try:
            return confine_path(v, allow_placeholders=True)
        except ValueError:
            # Save time only: `confine_path` quotes the path, which is right
            # for a run record and wrong for the answer to a refused save --
            # `/v1/items?api_key=<token>` is exactly the mistake it catches.
            raise ValueError(
                "path must be a path starting with '/', without a host, a "
                "query string (use `query`), a fragment, a backslash, '.' or "
                "'..' segments or an empty segment ('//'): the host comes from "
                "the destination"
            ) from None

    @field_validator("query", "headers", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> Any:
        if v is None:
            return {}
        if isinstance(v, dict):
            return {str(k): ("" if val is None else str(val)) for k, val in v.items()}
        return v

    @field_validator("expect_status")
    @classmethod
    def _status_range(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("expect_status must list at least one status code")
        for code in v:
            if not 100 <= code <= 599:
                raise ValueError(f"{code} is not an HTTP status code")
        return v


class PythonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    timeout_seconds: int = Field(default=10, ge=1, le=60)

    @field_validator("code")
    @classmethod
    def _compiles(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("code must not be empty")
        try:
            compile(v, "<python step>", "exec")
        except SyntaxError as e:
            raise ValueError(
                f"code does not compile: {e.msg} (line {e.lineno})"
            ) from None
        return v


CONFIG_MODELS: dict[str, type[BaseModel]] = {
    "condition": ConditionConfig,
    "transform": TransformConfig,
    "http": HttpConfig,
    "python": PythonConfig,
}


def _loc_part(part: Any) -> str:
    if isinstance(part, int) and not isinstance(part, bool):
        return str(part)
    return loc_field(part)


def _describe_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        # A key of the config is the client's own text (an unknown key
        # arrives here as a location): named only when it looks like a field.
        loc = ".".join(_loc_part(p) for p in err.get("loc", ()) if p != "else_")
        msg = err.get("msg", "invalid")
        # pydantic prefixes its own ValueError messages with "Value error, ".
        msg = msg.removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def parse_step_config(kind: str, config: Any) -> BaseModel:
    """The typed config for ``kind``, or ValueError naming what is wrong."""
    model = CONFIG_MODELS.get(kind)
    if model is None:
        raise ValueError(
            f"unknown step kind {kind!r}; expected one of {', '.join(STEP_KINDS)}"
        )
    if config is None:
        config = {}
    if isinstance(config, str):
        try:
            config = json.loads(config) if config.strip() else {}
        except json.JSONDecodeError as e:
            raise ValueError(f"config is not valid JSON: {e}") from None
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    try:
        return model.model_validate(config)
    except ValidationError as e:
        raise ValueError(_describe_validation_error(e)) from None


def validate_step_config(kind: str, config: Any) -> None:
    """Save-time gate for one step's kind + config. Raises ValueError."""
    if kind == AGENT_KIND:
        return
    parse_step_config(kind, config)


def config_to_json(kind: str, config: Any) -> str | None:
    """What goes into ``WorkflowStep.config_json``: normalised, or None for agents."""
    if kind == AGENT_KIND:
        return None
    parsed = parse_step_config(kind, config)
    return json.dumps(parsed.model_dump(by_alias=True), ensure_ascii=False)


def config_from_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        logger.warning("Malformed step config_json; treating as empty")
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Paths, templates, context
# ---------------------------------------------------------------------------
_PATH_TOKEN = re.compile(r"\[(-?\d+)\]|([^.\[\]]+)")


def get_path(data: Any, path: str) -> tuple[bool, Any]:
    """Resolve ``a.b[0].c`` against nested dicts/lists.

    Returns ``(found, value)`` so a missing key and a present ``null`` can be
    told apart. An empty path is the whole document.
    """
    path = (path or "").strip()
    if not path:
        return True, data
    current = data
    for match in _PATH_TOKEN.finditer(path):
        index, key = match.group(1), match.group(2)
        if index is not None:
            if not isinstance(current, list):
                return False, None
            i = int(index)
            if not -len(current) <= i < len(current):
                return False, None
            current = current[i]
        else:
            key = key.strip()
            if isinstance(current, dict):
                if key not in current:
                    return False, None
                current = current[key]
            elif isinstance(current, list) and key.isdigit():
                i = int(key)
                if i >= len(current):
                    return False, None
                current = current[i]
            else:
                return False, None
    return True, current


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


@dataclass
class StepContext:
    """What a deterministic step can see.

    ``text`` is the previous step's output; at a join with several sources it
    is their ``## From`` blocks concatenated, so a condition can still read
    everything. ``item`` is the fan-out WorkItem (as a dict) inside a branch
    or on the join line, else None. ``sources`` maps each predecessor's name
    to its text for ``{{source.<name>}}``.
    """

    text: str = ""
    item: dict[str, Any] | None = None
    sources: dict[str, str] = field(default_factory=dict)
    _json: Any = field(default=None, init=False, repr=False)
    _json_state: str = field(default="unparsed", init=False, repr=False)

    @property
    def json_data(self) -> Any:
        """The text parsed as JSON, or None (see :meth:`json_ok`)."""
        self._parse()
        return self._json

    def json_ok(self) -> bool:
        self._parse()
        return self._json_state == "ok"

    def _parse(self) -> None:
        if self._json_state != "unparsed":
            return
        candidate = (self.text or "").strip()
        # Agents often wrap JSON in a code fence; unwrap it once.
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", candidate, re.DOTALL)
        if fence:
            candidate = fence.group(1)
        try:
            self._json = json.loads(candidate) if candidate else None
            self._json_state = "ok" if candidate else "empty"
        except Exception:  # noqa: BLE001
            self._json, self._json_state = None, "invalid"


def context_from_sources(
    sources: list[tuple[str, str]], item: Any = None
) -> StepContext:
    """Build a context from the runner's ``(name, text)`` predecessor list."""
    if not sources:
        text = ""
    elif len(sources) == 1:
        text = sources[0][1] or ""
    else:
        text = "\n\n".join(f"## From {n}\n{(t or '').strip()}" for n, t in sources)
    item_dict = None
    if item is not None:
        item_dict = item if isinstance(item, dict) else item.model_dump()
    return StepContext(
        text=text, item=item_dict, sources={n: (t or "") for n, t in sources},
    )


_PLACEHOLDER = re.compile(r"\{\{\s*(.+?)\s*\}\}")


def render_template(template: str, ctx: StepContext, text: str | None = None) -> str:
    """Substitute placeholders; unknown ones render empty. Never evaluates."""
    if not template:
        return ""
    current = ctx.text if text is None else text

    def lookup(expr: str) -> str:
        root, _, rest = expr.partition(".")
        root = root.strip()
        if root == "text":
            return current
        if root == "item":
            if ctx.item is None:
                return ""
            found, value = get_path(ctx.item, rest)
            return _to_text(value) if found else ""
        if root == "json":
            if not ctx.json_ok():
                return ""
            found, value = get_path(ctx.json_data, rest)
            return _to_text(value) if found else ""
        if root == "source":
            return ctx.sources.get(rest.strip(), "")
        return ""

    return _PLACEHOLDER.sub(lambda m: lookup(m.group(1)), template)


@dataclass(frozen=True)
class StepOutcome:
    output: str
    action: Literal["continue", "stop"] = "continue"
    # Which condition rule fired (1-based), for the step run's record.
    detail: str = ""


# ---------------------------------------------------------------------------
# condition
# ---------------------------------------------------------------------------
def _operand(when: ConditionWhen, ctx: StepContext) -> tuple[bool, Any]:
    if when.source == "text":
        return True, ctx.text
    if when.source == "item":
        if ctx.item is None:
            return False, None
        return get_path(ctx.item, when.field)
    if not ctx.json_ok():
        return False, None
    return get_path(ctx.json_data, when.field)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


async def _run_regex(request: dict[str, Any]) -> dict[str, Any]:
    """Run one regex search or replace in a killed-on-timeout child process.

    ``request`` is ``{mode: "search" | "sub", pattern, flags, text, replace?}``.
    Raises StepFailed with a fixed text (never the pattern or the text) when
    the operand is over REGEX_OPERAND_CAP, the match takes longer than
    REGEX_TIMEOUT_SECONDS or the child gives no result. A cancellation (the
    step's own timeout, shutdown) kills the child before it propagates.
    """
    text = request["text"]
    if len(text) > REGEX_OPERAND_CAP:
        raise StepFailed(
            f"the text a regular expression is applied to is {len(text)} "
            f"characters, over the limit of {REGEX_OPERAND_CAP}; refused, not cut"
        )
    payload = json.dumps(
        {**request, "cap": REGEX_RESULT_CAP}, ensure_ascii=False,
    ).encode("utf-8")
    limit = REGEX_TIMEOUT_SECONDS
    kwargs: dict[str, Any] = dict(
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=python_step_env(),
    )
    if sys.platform != "win32":
        kwargs["preexec_fn"] = _limits(max(1, int(limit)))
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-S", "-E", "-c", _REGEX_CHILD, **kwargs,
        )
    except Exception as e:  # noqa: BLE001
        raise StepFailed(
            f"could not start the regular expression check: {type(e).__name__}"
        ) from None
    deadline = asyncio.timeout(limit)
    try:
        async with deadline:
            stdout, _ = await proc.communicate(payload)
    except TimeoutError:
        await _kill(proc)
        if deadline.expired():
            raise StepFailed(
                f"a regular expression took longer than {limit:g}s and was "
                "stopped; simplify the pattern (a nested quantifier such as "
                "(x+)+ backtracks exponentially)"
            ) from None
        raise
    except asyncio.CancelledError:
        await _kill(proc)
        raise
    try:
        result = json.loads(stdout.decode("utf-8", "replace")) if stdout.strip() else None
    except json.JSONDecodeError:
        result = None
    if not isinstance(result, dict):
        raise StepFailed(
            "a regular expression ended without a result "
            f"(exit code {proc.returncode})"
        )
    return result


async def _regex_hit(pattern: str, text: str, flags: int) -> bool:
    result = await _run_regex(
        {"mode": "search", "pattern": pattern, "flags": flags, "text": text},
    )
    # A pattern that does not compile is refused at save; one stored before
    # that check keeps its old meaning, "no match".
    return bool(result.get("ok") and result.get("hit"))


async def evaluate_rule(when: ConditionWhen, ctx: StepContext) -> bool:
    """One rule's truth. Missing operands (no item, unparseable JSON) are false."""
    found, actual = _operand(when, ctx)
    op = when.op
    if op == "is_empty":
        return (not found) or _is_empty(actual)
    if op == "not_empty":
        return found and not _is_empty(actual)
    if not found:
        return False

    expected = when.value
    fold = (lambda s: s) if when.case_sensitive else (lambda s: s.casefold())

    if op in ("contains", "not_contains"):
        if isinstance(actual, list):
            hit = any(fold(_to_text(x)) == fold(expected) for x in actual)
        elif isinstance(actual, dict):
            hit = any(fold(str(k)) == fold(expected) for k in actual)
        else:
            hit = fold(expected) in fold(_to_text(actual))
        return hit if op == "contains" else not hit
    if op in ("equals", "not_equals"):
        hit = fold(_to_text(actual)) == fold(expected)
        return hit if op == "equals" else not hit
    if op in ("matches", "not_matches"):
        flags = 0 if when.case_sensitive else int(re.IGNORECASE)
        hit = await _regex_hit(expected, _to_text(actual), flags)
        return hit if op == "matches" else not hit
    if op in ("gt", "lt"):
        a, b = _number(actual), _number(expected)
        if a is None or b is None:
            # Lexical fallback keeps "2026-01-02" > "2025-12-31" meaningful.
            a_s, b_s = fold(_to_text(actual)), fold(expected)
            return a_s > b_s if op == "gt" else a_s < b_s
        return a > b if op == "gt" else a < b
    return False


async def run_condition(cfg: ConditionConfig, ctx: StepContext) -> StepOutcome:
    for index, rule in enumerate(cfg.rules, start=1):
        try:
            fired = await evaluate_rule(rule.when, ctx)
        except StepFailed as e:
            raise StepFailed(f"rule {index}: {e}") from None
        if fired:
            chosen, detail = rule.then, f"rule {index}"
            break
    else:
        chosen, detail = cfg.else_, "else"
    output = render_template(chosen.output, ctx) if chosen.output else ctx.text
    return StepOutcome(output=output, action=chosen.action, detail=detail)


# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------
_RE_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}


async def run_transform(cfg: TransformConfig, ctx: StepContext) -> StepOutcome:
    text = ctx.text
    if cfg.extract_json:
        if not ctx.json_ok():
            raise StepFailed(
                "extract_json needs the previous step's output to be JSON, and it "
                f"is not: {_excerpt(ctx.text, 120)!r}"
            )
        found, value = get_path(ctx.json_data, cfg.extract_json)
        if not found:
            raise StepFailed(
                f"extract_json path {cfg.extract_json!r} is not present in the "
                "previous step's JSON"
            )
        text = _to_text(value)
    if cfg.regex is not None:
        flags = 0
        for c in cfg.regex.flags:
            flags |= int(_RE_FLAGS[c])
        result = await _run_regex({
            "mode": "sub", "pattern": cfg.regex.pattern, "flags": flags,
            "text": text, "replace": cfg.regex.replace,
        })
        if result.get("ok"):
            text = str(result.get("text", ""))
        elif result.get("error") == "too_large":
            raise StepFailed(
                "regex replace produced more than "
                f"{REGEX_RESULT_CAP} characters; refused, not cut"
            )
        else:
            # A bad group reference in `replace` (not checked at save).
            raise StepFailed(f"regex replace failed: {result.get('message', 'error')}")
    if cfg.template:
        text = render_template(cfg.template, ctx, text=text)
    if cfg.truncate is not None and len(text) > cfg.truncate:
        text = text[:cfg.truncate] + _TRUNCATED
    return StepOutcome(output=text)


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------
def confine_path(path: str, *, allow_placeholders: bool = False) -> str:
    """A relative path that stays under the destination's URL, or ValueError.

    Same reasoning as ``jira_tools.normalize_api_base``: the host comes from
    the destination, and a scheme, a protocol-relative ``//`` or a ``..``
    segment would let a config edit point the destination's credential at
    another host or another API.
    """
    text = str(path or "").strip()
    if not text:
        return "/"
    probe = _PLACEHOLDER.sub("x", text) if allow_placeholders else text
    if "://" in probe or probe.startswith("//") or "\\" in probe:
        raise ValueError(
            f"invalid path {text!r}; it is a path, not a URL -- the host comes "
            "from the destination"
        )
    if not probe.startswith("/"):
        raise ValueError(f"invalid path {text!r}; it must start with '/'")
    if "?" in probe or "#" in probe:
        raise ValueError(
            f"invalid path {text!r}; put query parameters in `query`, not in the path"
        )
    if any(segment in ("..", ".") for segment in probe.split("/")):
        raise ValueError(
            f"invalid path {text!r}; '.' and '..' segments would reach endpoints "
            "outside the destination"
        )
    if "//" in probe:
        raise ValueError(f"invalid path {text!r}; empty segments ('//') are not allowed")
    return text


def _excerpt(text: str, limit: int = HTTP_BODY_EXCERPT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + _TRUNCATED


_resolvers: dict[str, Any] = {}


def build_resolver(destination: str) -> Any:
    """One cached DestinationResolver per destination name.

    App-level destination only, exactly as jira_tools resolves it: the
    destination holds the credential and the token is reused until it nears
    expiry. Raises DestinationError when the app has no destination binding.
    """
    resolver = _resolvers.get(destination)
    if resolver is None:
        from agents.destination import (  # noqa: PLC0415
            MISSING_BINDING_MESSAGE,
            DestinationError,
            DestinationResolver,
            config_from_environment,
        )

        config = config_from_environment(os.environ)
        if config is None:
            raise DestinationError(f"http step: {MISSING_BINDING_MESSAGE}")
        resolver = DestinationResolver(destination, config)
        _resolvers[destination] = resolver
    return resolver


def _is_json_content(content_type: str) -> bool:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return media.endswith("/json") or media.endswith("+json")


async def run_http(
    cfg: HttpConfig,
    ctx: StepContext,
    *,
    resolver: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> StepOutcome:
    try:
        path = confine_path(render_template(cfg.path, ctx) if cfg.path else "/")
    except ValueError as e:
        raise StepFailed(str(e)) from None
    query = {k: render_template(v, ctx) for k, v in cfg.query.items()}
    body = render_template(cfg.body, ctx) if cfg.body else ""

    if resolver is None:
        try:
            resolver = build_resolver(cfg.destination)
        except Exception as e:  # noqa: BLE001
            raise StepFailed(f"destination {cfg.destination!r}: {e}") from None

    async def send(http: httpx.AsyncClient) -> httpx.Response:
        destination = await resolver.resolve()
        if str(getattr(destination, "proxy_type", "") or "").casefold() == "onpremise":
            # An OnPremise URL is a virtual host behind the Cloud Connector,
            # reachable only through the connectivity proxy, which this step
            # does not speak; sent directly, the destination's credential
            # would go to whatever public DNS answers for that name.
            raise StepFailed(
                f"destination {cfg.destination!r} is an OnPremise destination; "
                "the http step reaches Internet destinations only"
            )
        headers = {**destination.headers, **cfg.headers}
        content = None
        if body and cfg.method != "GET":
            headers.setdefault("Content-Type", cfg.content_type)
            content = body.encode("utf-8")
        # The destination's `URL.queries.*` (sap-client, ...) go with every
        # request; a parameter the step names itself wins.
        params = {**(getattr(destination, "queries", None) or {}), **query}
        request = http.build_request(
            cfg.method,
            f"{destination.url.rstrip('/')}{path}",
            params=params or None,
            headers=headers,
            content=content,
        )
        return await http.send(request, stream=True)

    async def read_capped(response: httpx.Response) -> tuple[bytes, bool]:
        """At most HTTP_STEP_RESPONSE_CAP bytes, and whether there was more."""
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > HTTP_STEP_RESPONSE_CAP:
                chunks.append(chunk[: len(chunk) - (size - HTTP_STEP_RESPONSE_CAP)])
                return b"".join(chunks), True
            chunks.append(chunk)
        return b"".join(chunks), False

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(float(cfg.timeout_seconds)), transport=transport,
        ) as http:
            response = await send(http)
            try:
                if response.status_code == 401 and hasattr(resolver, "invalidate"):
                    # The destination's cached token aged out; refresh once.
                    await response.aclose()
                    resolver.invalidate()
                    response = await send(http)
                raw, over = await read_capped(response)
            finally:
                await response.aclose()
    except StepFailed:
        raise
    except httpx.HTTPError as e:
        raise StepFailed(
            f"{cfg.method} {path} via destination {cfg.destination!r} failed: "
            f"{type(e).__name__}: {e}"
        ) from None
    except Exception as e:  # noqa: BLE001
        raise StepFailed(
            f"destination {cfg.destination!r}: {type(e).__name__}: {e}"
        ) from None

    try:
        text = raw.decode(response.charset_encoding or "utf-8", "replace")
    except LookupError:  # a charset Python does not know
        text = raw.decode("utf-8", "replace")
    if response.status_code not in cfg.expect_status:
        # Only an excerpt is shown, so a body over the cap needs no refusal.
        raise StepFailed(
            f"{cfg.method} {path} returned HTTP {response.status_code} "
            f"(expected {_describe_statuses(cfg.expect_status)}): {_excerpt(text)}"
        )
    if over:
        raise StepFailed(
            f"{cfg.method} {path} answered with a body over the "
            f"{HTTP_STEP_RESPONSE_CAP} bytes an http step hands on; refused, not "
            "cut (narrow the request with `query`)"
        )
    if _is_json_content(response.headers.get("content-type", "")) and text.strip():
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass
    return StepOutcome(output=text, detail=f"HTTP {response.status_code}")


def _describe_statuses(codes: list[int]) -> str:
    if codes == list(range(200, 300)):
        return "2xx"
    return ", ".join(str(c) for c in codes[:8]) + ("…" if len(codes) > 8 else "")


# ---------------------------------------------------------------------------
# python
# ---------------------------------------------------------------------------
def python_step_env() -> dict[str, str]:
    """The child's whole environment: a fixed PATH plus an allowlist copied
    from os.environ. Never os.environ itself."""
    env = dict(PYTHON_STEP_ENV)
    for name in PYTHON_STEP_PASSTHROUGH:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _limits(cpu_seconds: int):
    """preexec_fn: cap address space and CPU time (Linux/POSIX only)."""
    def apply() -> None:
        try:
            import resource  # noqa: PLC0415

            resource.setrlimit(
                resource.RLIMIT_AS, (PYTHON_STEP_MEMORY_BYTES, PYTHON_STEP_MEMORY_BYTES)
            )
            # A little above the wall-clock timeout, so a busy loop is ended by
            # the parent's kill (with its clear message) and the CPU limit only
            # backstops a parent that never gets to it.
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds + 2, cpu_seconds + 3))
        except Exception:  # noqa: BLE001 - no `resource` on this platform
            pass
    return apply


def python_step_command() -> list[str]:
    return [sys.executable, "-I", "-S", "-E", str(RUNNER_PATH)]


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill the child if it is still there; it may have died on its own."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


async def run_python(cfg: PythonConfig, ctx: StepContext) -> StepOutcome:
    payload = json.dumps({
        "code": cfg.code,
        "text": ctx.text,
        "item": ctx.item,
        "sources": ctx.sources,
        "json": ctx.json_data if ctx.json_ok() else None,
    }, ensure_ascii=False).encode("utf-8")

    with tempfile.TemporaryDirectory(prefix="pystep-") as workdir:
        kwargs: dict[str, Any] = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=python_step_env(),
            cwd=workdir,
        )
        if sys.platform != "win32":
            kwargs["preexec_fn"] = _limits(cfg.timeout_seconds)
        try:
            proc = await asyncio.create_subprocess_exec(*python_step_command(), **kwargs)
        except Exception as e:  # noqa: BLE001
            raise StepFailed(f"could not start the python step: {type(e).__name__}: {e}") from None
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload), timeout=cfg.timeout_seconds,
            )
        except asyncio.TimeoutError:
            await _kill(proc)
            raise StepFailed(
                f"python step exceeded its {cfg.timeout_seconds}s timeout and was killed"
            ) from None
        except asyncio.CancelledError:
            await _kill(proc)
            raise

    raw = stdout[:PYTHON_STEP_OUTPUT_CAP + 4096].decode("utf-8", "replace")
    try:
        result = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        result = None
    if not isinstance(result, dict):
        err = stderr.decode("utf-8", "replace").strip()
        tail = err[-2000:] if err else (raw.strip()[-500:] or "no output")
        raise StepFailed(
            f"python step exited with code {proc.returncode} without a result: {tail}"
        )
    if not result.get("ok"):
        raise StepFailed(f"python step raised:\n{str(result.get('error') or '').strip()}")
    output = str(result.get("output") or "")
    if len(output.encode("utf-8")) > PYTHON_STEP_OUTPUT_CAP:
        output = output.encode("utf-8")[:PYTHON_STEP_OUTPUT_CAP].decode("utf-8", "ignore")
        output += _TRUNCATED
    elif result.get("truncated"):
        output += _TRUNCATED
    return StepOutcome(output=output)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
async def execute_step(
    kind: str,
    config: Any,
    ctx: StepContext,
    *,
    http_resolver: Any = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> StepOutcome:
    """Run one deterministic step. Raises StepFailed (or ValueError on bad config)."""
    if kind == AGENT_KIND:
        raise ValueError("agent steps are run by the workflow runner, not execute_step")
    cfg = parse_step_config(kind, config)
    if kind == "condition":
        return await run_condition(cfg, ctx)  # type: ignore[arg-type]
    if kind == "transform":
        return await run_transform(cfg, ctx)  # type: ignore[arg-type]
    if kind == "http":
        return await run_http(  # type: ignore[arg-type]
            cfg, ctx, resolver=http_resolver, transport=http_transport,
        )
    if kind == "python":
        return await run_python(cfg, ctx)  # type: ignore[arg-type]
    raise ValueError(f"unknown step kind {kind!r}")


def step_label(kind: str, position: int) -> str:
    """The ``## From`` name of a deterministic step's output, e.g. ``transform#3``."""
    return f"{kind}#{position}"


def summarize_config(kind: str, config: Any) -> str:
    """A one-line, human summary for lists and flow diagrams."""
    try:
        cfg = parse_step_config(kind, config)
    except ValueError:
        return kind
    if isinstance(cfg, ConditionConfig):
        n = len(cfg.rules)
        return f"{n} rule{'s' if n != 1 else ''}"
    if isinstance(cfg, TransformConfig):
        bits = []
        if cfg.extract_json:
            bits.append(f"json {cfg.extract_json}")
        if cfg.regex:
            bits.append("regex")
        if cfg.template:
            bits.append("template")
        if cfg.truncate:
            bits.append(f"≤{cfg.truncate}")
        return ", ".join(bits) or "pass-through"
    if isinstance(cfg, HttpConfig):
        return f"{cfg.method} {cfg.destination}{cfg.path}"
    if isinstance(cfg, PythonConfig):
        first = next((ln.strip() for ln in cfg.code.splitlines() if ln.strip()), "")
        return _excerpt(first, 40)
    return kind
