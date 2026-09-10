# SAP note detail (`builtin:sapnotedetail`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `builtin:sapnotedetail` toolset that returns the fixing
support-package level for a list of SAP note numbers, so the monthly digest can
say MISSING or IMPLEMENTED where it currently says UNKNOWN.

**Architecture:** An in-process toolset calls the private
`https://me.sap.com/backend/raw/sapnotes/Detail` endpoint with a session cookie
a human obtained in a browser. A new `auth_mode="session"` carries that
credential in the existing `mcp_oauth_tokens` table, so the admin credentials
panel shows it going stale before a run depends on it. Playwright runs on the
operator's machine only; the platform sees a cookie string.

**Tech Stack:** Python 3.11, `httpx`, `pydantic-ai` `FunctionToolset`,
SQLAlchemy async, FastAPI, pytest, Playwright (ops-only), SAPUI5/TypeScript.

**Spec:** `docs/superpowers/specs/2026-09-10-sap-note-detail-design.md`

## Global Constraints

- **This is a private API.** Parsing must degrade to `unavailable` on an
  unexpected shape, never raise. A format change costs a thin digest, not a
  failed run.
- **Never guess the response shape.** Task 1 captures a real response to a
  fixture; every later task reads that file for its expected values.
- **No secret in a server config block.** The cookie lives in
  `mcp_oauth_tokens.access_token`, matching how OAuth access tokens are already
  stored. Nothing about the credential goes in `oauth`.
- **The cookie is never logged**, never echoed by an endpoint, never printed by
  a script.
- **Playwright is an ops dependency.** It must not enter `requirements.txt` or
  the Cloud Foundry buildpack, the same way the UI5 npm toolchain stays out of
  the Python dependencies.
- **`auth_mode="session"` is a two-way rule.** Only `builtin:sapnotedetail`
  may use it, and it may use nothing else.
- Python: 4-space indent, `from __future__ import annotations`, double quotes,
  module docstrings explaining *why*.
- **The test runner is per-file.** Script-style files run as
  `python tests/test_x.py`; pytest-style files run under pytest. `pytest tests/`
  alone is NOT this project's runner and reports pre-existing failures.
- **Known pre-existing failure:** `tests/test_agent_bundles.py` fails 9 checks
  because agents in other `docs/` bundles lack a `run_prompt`. Not caused by
  this work; do not "fix" it.

---

### Task 1: Capture a real `Detail` response

**BLOCKER — read before starting.** This task needs a **dialog** S-user or SAP
Universal ID with a password. It cannot use `SAP_NOTES_USER` from `.env`: that
is a *technical communication user*, which by definition cannot log on in
dialog mode, and this is a browser form login. If no dialog credential is
available, **stop here and report** — Tasks 2 and 3 cannot be written without
the fixture this produces, though Tasks 4-6 could proceed independently.

**Files:**
- Create: `scripts/sap_session.py`
- Create: `tests/fixtures/sapnote_detail.json` (produced by running the script)
- Modify: `.env.example`

**Interfaces:**
- Produces:
  - `async login(user: str, pwd: str, *, headful: bool = False, timeout_s: int = 180) -> str`
    returning a serialized `Cookie` header
  - `async fetch_detail_raw(cookie: str, note: str) -> tuple[int, str, bytes]`
    returning `(status, content_type, body)`
  - `tests/fixtures/sapnote_detail.json` — one real response body

- [x] **Step 1: Add the dialog credential to `.env.example`**

Append:

```bash
# Dialog S-user / SAP Universal ID used ONLY to obtain a me.sap.com browser
# session for builtin:sapnotedetail. NOT the technical communication user in
# SAP_NOTES_USER -- a technical user cannot log on in dialog mode, and this is
# a form login. Used by scripts/sap_session.py on an operator machine; never
# deployed.
SAP_DIALOG_USER=
SAP_DIALOG_PWD=
```

- [x] **Step 2: Install Playwright locally (ops only)**

Run:

```bash
.venv/Scripts/python.exe -m pip install playwright
.venv/Scripts/python.exe -m playwright install chromium
```

Do NOT add `playwright` to `requirements.txt`. It is an operator tool; adding
it would pull Chromium into the Cloud Foundry buildpack.

- [x] **Step 3: Write the session helper**

**SUPERSEDED — see `scripts/sap_session.py` as committed.** The listing that
stood here was written before the flow had ever been run and carried three
defects, all found by running it:

1. `wait_until="commit"` returned before me.sap.com finished redirecting
   through XSUAA, so DOM queries raced the navigation and died with
   "Execution context was destroyed".
2. accounts.sap.com is a **two-step** form: page one has only `#j_username`
   and a Continue button. Filling a password there did nothing, and submitting
   the username alone parked on step two forever.
3. Cookies were filtered by `"sap.com" in domain`, but the jar holds two
   cookies named `JSESSIONID` — one for me.sap.com, one for accounts.sap.com.
   Sending both handed me.sap.com the identity provider's session, which
   returned the anonymous bootstrap page and looked like an auth failure.

Verify the file imports cleanly before running it:
`.venv/Scripts/python.exe -c "import ast,pathlib;ast.parse(pathlib.Path('scripts/sap_session.py').read_text())"`

- [x] **Step 4: Capture the fixture**

Run:

```bash
.venv/Scripts/python.exe scripts/sap_session.py capture 3771065 --headful
```

Expected: `status 200  type application/json`, and
`tests/fixtures/sapnote_detail.json` written.

If it exits with "Not JSON — the session did not take", the login did not
complete. Re-run with `--headful` and finish any MFA challenge by hand.

- [x] **Step 5: Record the real shape**

Run:

```bash
.venv/Scripts/python.exe -c "import json;d=json.load(open('tests/fixtures/sapnote_detail.json'));print(json.dumps(d,indent=1)[:3000])"
```

Write the answers to these three questions into the spec's "Risks" section,
replacing risk 1:

1. What are the top-level keys, and which one holds the note record?
2. What are the literal key names for validity, support packages, support
   package patches, component and priority?
3. What does one support-package entry look like — which key holds the
   software component, and which holds the level?

Every later task depends on these answers being written down.

- [x] **Step 6: Commit**

The fixture is a real SAP response. Check it carries no personal data (an
S-user id, a name, an email) before committing; if it does, redact those values
in the file, keeping the structure.

```bash
git add scripts/sap_session.py .env.example tests/fixtures/sapnote_detail.json docs/superpowers/specs/2026-09-10-sap-note-detail-design.md
git commit -m "feat(sapnotedetail): capture a real me.sap.com Detail response"
```

---

### Task 2: `SapNoteDetailClient` — one note, parsed

**Files:**
- Create: `agents/sapnotedetail_tools.py`
- Create: `tests/test_sapnotedetail_tools.py`

**Interfaces:**
- Consumes: `tests/fixtures/sapnote_detail.json` (Task 1)
- Produces:
  - `BUILTIN_SAPNOTEDETAIL_URL: str = "builtin:sapnotedetail"`
  - `parse_detail(raw: Any, note: str) -> dict[str, Any]`
  - `SapNoteDetailClient(http, cookie, *, max_concurrency=5)` with
    `async get_detail(note: str) -> dict[str, Any]`

- [ ] **Step 1: Write the failing parsing tests**

Create `tests/test_sapnotedetail_tools.py`. **Fill the three marked expected
values by reading `tests/fixtures/sapnote_detail.json`** — they are real values
from the captured note, not invented ones:

```python
"""Tests for the me.sap.com note-detail toolset."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import httpx
import pytest

from agents.sapnotedetail_tools import (
    BUILTIN_SAPNOTEDETAIL_URL,
    SapNoteDetailClient,
    parse_detail,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sapnote_detail.json"
NOTE = "3771065"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_builtin_url_is_stable():
    assert BUILTIN_SAPNOTEDETAIL_URL == "builtin:sapnotedetail"


def test_parse_detail_reports_ok_and_echoes_the_note():
    parsed = parse_detail(load_fixture(), NOTE)
    assert parsed["note"] == NOTE
    assert parsed["status"] == "ok"


def test_parse_detail_extracts_the_header():
    parsed = parse_detail(load_fixture(), NOTE)
    assert parsed["note_type"] == "SAP Security Note"
    assert "CVE-2018-2424" in parsed["title"]


def test_parse_detail_extracts_validity():
    parsed = parse_detail(load_fixture(), NOTE)
    entry = parsed["validity"][0]
    assert set(entry) == {"software_component", "from", "to"}
    assert entry["software_component"] == "HDB"
    assert entry["from"] == "1.00"


def test_parse_detail_reads_support_packages_from_the_patch_list():
    """`SupportPackage` is empty on every note sampled; the level is in
    `SupportPackagePatch`. Reading the wrong one returns nothing for every
    note while looking like it works."""
    parsed = parse_detail(load_fixture(), NOTE)
    packages = parsed["support_packages"]
    assert packages, "the fixture note must carry at least one fixing level"
    first = packages[0]
    assert set(first) == {"component_version", "support_package", "patch"}
    assert first["component_version"] == "SAPUI5 CLIENT RT AS JAVA 7.40"
    assert first["support_package"] == "SP010"
    assert first["patch"] == "000014"


def test_parse_detail_degrades_on_an_unexpected_shape():
    """A private API can change without notice; a shape change must not raise."""
    parsed = parse_detail({"something": "else"}, NOTE)
    assert parsed["status"] == "unavailable"
    assert parsed["note"] == NOTE
    assert parsed["support_packages"] == []


def test_parse_detail_degrades_on_garbage():
    for junk in (None, [], "text", 42):
        parsed = parse_detail(junk, NOTE)
        assert parsed["status"] == "unavailable"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_sapnotedetail_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.sapnotedetail_tools'`

- [ ] **Step 3: Write the module and the parser**

Create `agents/sapnotedetail_tools.py`. **The `_KEYS` mapping below must be
corrected against the real fixture** — the names here are the TypeScript
interface names from `marianfoo/sap-mcp-servers`, which describe the parsed
model, not necessarily the raw JSON keys:

```python
"""SAP note detail from the private me.sap.com backend.

NVD gives a note number and a CVSS score but never says which support package
fixes it, so half of every digest lands in an UNKNOWN bucket. That field lives
in SAP's backbone and has no supported API: two SAP Community requests for one
went unanswered, and the Support Portal OData services do not carry it.

What does carry it is `me.sap.com/backend/raw/sapnotes/Detail`, a private
endpoint behind XSUAA/SAML. It ignores HTTP Basic outright -- probed, it
returns a byte-identical bootstrap page with credentials and without -- so the
only usable credential is a session cookie a human obtained in a browser. See
docs/superpowers/specs/2026-09-10-sap-note-detail-design.md, and note the
upstream project's warning that this is a private API subject to SAP's ToS.

Everything here degrades rather than raises. A private API can change shape
without notice, and a thin digest that says so is worth more than a run that
dies.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from pydantic_ai.toolsets import FunctionToolset

logger = logging.getLogger(__name__)

BUILTIN_SAPNOTEDETAIL_URL = "builtin:sapnotedetail"

DETAIL_URL = "https://me.sap.com/backend/raw/sapnotes/Detail"

DEFAULT_MAX_CONCURRENCY = 5

# Confirmed against two live responses on 2026-09-10; see the spec's
# "Confirmed API shape". Everything of interest hangs off Response.SAPNote,
# and each table is an object with an `Items` list, not a bare list.
_KEY_HEADER = "Header"
_KEY_TITLE = "Title"
_KEY_VALIDITY = "Validity"
# The fixing level lives here, NOT in `SupportPackage`, which was empty on
# every one of ten sampled notes.
_KEY_SUPPORT_PACKAGE_PATCH = "SupportPackagePatch"


def _text(value: Any) -> str:
    """A scalar as text, unwrapping the {"value": ...} envelope SAP uses."""
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return ""
    return str(value).strip()


def unavailable(note: str, reason: str) -> dict[str, Any]:
    """The shape every failure returns. Never raises, always answers."""
    return {
        "note": note,
        "status": "unavailable",
        "reason": reason,
        "title": "",
        "note_type": "",
        "version": "",
        "validity": [],
        "support_packages": [],
    }


def _items(node: Any) -> list[dict[str, Any]]:
    """The `Items` list of a SAP table node, or empty."""
    if not isinstance(node, dict):
        return []
    items = node.get("Items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _packages(node: Any) -> list[dict[str, str]]:
    """The fixing support-package levels."""
    out: list[dict[str, str]] = []
    for entry in _items(node):
        row = {
            "component_version": _text(entry.get("SoftwareComponentVersion")),
            "support_package": _text(entry.get("SupportPackage")),
            "patch": _text(entry.get("SupportPackagePatch")),
        }
        if any(row.values()):
            out.append(row)
    return out


def _validity(node: Any) -> list[dict[str, str]]:
    """Which components and release ranges the note applies to.

    The only place a software component appears: the response carries no
    top-level component or priority field.
    """
    out: list[dict[str, str]] = []
    for entry in _items(node):
        out.append({
            "software_component": _text(entry.get("SoftwareComponent")),
            "from": _text(entry.get("From")),
            "to": _text(entry.get("To")),
        })
    return out


def parse_detail(raw: Any, note: str) -> dict[str, Any]:
    """One `Detail` response, normalized. Returns `unavailable` on any surprise."""
    if not isinstance(raw, dict):
        return unavailable(note, "response was not a JSON object")

    # Confirmed envelope: {"Request": {...}, "Response": {"SAPNote": {...}}}.
    record = raw.get("Response")
    record = record.get("SAPNote") if isinstance(record, dict) else None
    if not isinstance(record, dict):
        return unavailable(note, "response carried no Response.SAPNote")

    known = {_KEY_HEADER, _KEY_TITLE, _KEY_VALIDITY, _KEY_SUPPORT_PACKAGE_PATCH}
    if not known & set(record):
        return unavailable(note, "response carried none of the expected fields")

    header = record.get(_KEY_HEADER) or {}
    header = header if isinstance(header, dict) else {}
    return {
        "note": note,
        "status": "ok",
        "title": _text(record.get(_KEY_TITLE)),
        "note_type": _text(header.get("Type")),
        "version": _text(header.get("Version")),
        "validity": _validity(record.get(_KEY_VALIDITY)),
        "support_packages": _packages(record.get(_KEY_SUPPORT_PACKAGE_PATCH)),
    }


class SessionExpired(RuntimeError):
    """The me.sap.com session is no longer usable. Only a human can fix it."""


class SapNoteDetailClient:
    """Fetches note detail with a browser-obtained session cookie.

    Takes an ``httpx.AsyncClient`` so tests can inject a mock transport. The
    cookie is sent as a header rather than a cookie jar because it arrives as
    one opaque serialized string from the operator's browser.

    ``cookie`` is either the string itself or an async callable returning it.
    The callable form exists because ``build_builtin_toolset`` passes only
    ``(oauth, server_key, auth_mode)`` -- there is no principal at build time
    to resolve a credential for -- so production resolves it per call, the
    same way the forwarded JWT is read from a contextvar rather than captured
    when the registry is built.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        cookie: str | Callable[[], Awaitable[str]] = "",
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        self._http = http
        self._cookie = cookie
        self.max_concurrency = max(1, int(max_concurrency))

    async def _cookie_header(self) -> str:
        value = self._cookie
        if callable(value):
            value = await value()
        return (value or "").strip()

    async def get_detail(self, note: str) -> dict[str, Any]:
        """One note. Auth failures raise SessionExpired; nothing else raises."""
        cookie = await self._cookie_header()
        if not cookie:
            # No stored session at all reads the same as a dead one: a human
            # has to open a browser either way.
            raise SessionExpired(f"note {note}: no session cookie is stored")
        try:
            r = await self._http.get(
                DETAIL_URL,
                params={"q": note, "t": "E"},
                headers={"Cookie": cookie, "Accept": "application/json"},
                follow_redirects=False,
            )
        except Exception as e:  # noqa: BLE001 - a flaky note must not end a run
            logger.warning("note %s: transport error %s", note, type(e).__name__)
            return unavailable(note, f"transport error: {type(e).__name__}")

        if r.status_code in (401, 403):
            raise SessionExpired(f"note {note}: HTTP {r.status_code}")
        # The tell for a dead session is an HTML bootstrap page with a 200,
        # not a 401 -- that is exactly how this endpoint refuses anonymous
        # callers, so it must be treated as an auth failure, not a bad note.
        if "json" not in (r.headers.get("content-type") or "").lower():
            raise SessionExpired(f"note {note}: got HTML, the session is not valid")
        if r.status_code >= 400:
            return unavailable(note, f"HTTP {r.status_code}")

        try:
            return parse_detail(r.json(), note)
        except ValueError:
            return unavailable(note, "response was not valid JSON")
```

Add `from collections.abc import Awaitable, Callable` to the imports.

Then correct `_KEY_*` and the `_packages` / `_validity` key names against the
fixture until the tests pass. This is the only guessing-allowed moment in the
plan, and it is guessing *checked against real data* — the fixture is the
oracle, so a wrong mapping fails a test rather than shipping.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_sapnotedetail_tools.py -v`
Expected: PASS — 7 tests

- [ ] **Step 5: Commit**

```bash
git add agents/sapnotedetail_tools.py tests/test_sapnotedetail_tools.py
git commit -m "feat(sapnotedetail): parse note detail from the me.sap.com backend"
```

---

### Task 3: Batching, concurrency, and the failure envelope

**Files:**
- Modify: `agents/sapnotedetail_tools.py`
- Modify: `tests/test_sapnotedetail_tools.py`

**Interfaces:**
- Consumes: `SapNoteDetailClient.get_detail`, `SessionExpired`, `unavailable` (Task 2)
- Produces:
  - `SapNoteDetailClient.get_details(notes: list[str]) -> dict[str, Any]`
  - `sapnotedetail_toolset(oauth, *, http=None, cookie=None, server_key=..., auth_mode=None) -> FunctionToolset`
    exposing one tool, `get_note_details(notes: list[str])`

- [ ] **Step 1: Write the failing batch tests**

Append to `tests/test_sapnotedetail_tools.py`:

```python
def _json_handler(payload: dict, seen: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.params["q"])
        return httpx.Response(200, json=payload)
    return handler


def _client(handler, **kw) -> SapNoteDetailClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SapNoteDetailClient(http, "SESSION=x", **kw)


@pytest.mark.asyncio
async def test_get_details_returns_one_entry_per_note():
    seen: list[str] = []
    client = _client(_json_handler(load_fixture(), seen))
    result = await client.get_details(["3771065", "3747649"])
    assert result["count"] == 2
    assert sorted(seen) == ["3747649", "3771065"]
    assert [n["note"] for n in result["notes"]] == ["3771065", "3747649"]


@pytest.mark.asyncio
async def test_get_details_reports_an_empty_list_without_calling_out():
    seen: list[str] = []
    client = _client(_json_handler(load_fixture(), seen))
    result = await client.get_details([])
    assert result == {"notes": [], "count": 0, "unavailable": 0, "session_expired": False}
    assert seen == []


@pytest.mark.asyncio
async def test_get_details_never_exceeds_the_concurrency_cap():
    live = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return httpx.Response(200, json=load_fixture())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SapNoteDetailClient(http, "SESSION=x", max_concurrency=3)
    await client.get_details([str(n) for n in range(3000000, 3000012)])
    assert peak <= 3, f"ran {peak} concurrent requests against a cap of 3"


@pytest.mark.asyncio
async def test_a_dead_session_stops_the_batch_and_keeps_earlier_results():
    """100 real answers beat throwing the run away, but do not retry a corpse."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(200, json=load_fixture())
        return httpx.Response(403)

    client = _client(handler, max_concurrency=1)
    result = await client.get_details(["1000001", "1000002", "1000003", "1000004"])

    assert result["session_expired"] is True
    assert sum(1 for n in result["notes"] if n["status"] == "ok") == 2
    # The two after the failure are reported, not silently dropped.
    assert result["count"] == 4
    assert result["unavailable"] == 2
    # And no further requests were made once the session was known dead.
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_an_html_body_counts_as_an_expired_session():
    """This endpoint refuses an anonymous caller with 200 + HTML, not a 401."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>bootstrap</html>",
                              headers={"content-type": "text/html"})

    result = await _client(handler, max_concurrency=1).get_details(["1000001"])
    assert result["session_expired"] is True


@pytest.mark.asyncio
async def test_one_flaky_note_does_not_end_the_batch():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["q"] == "1000002":
            return httpx.Response(500, json={})
        return httpx.Response(200, json=load_fixture())

    result = await _client(handler).get_details(["1000001", "1000002", "1000003"])
    assert result["session_expired"] is False
    assert result["unavailable"] == 1
    assert sum(1 for n in result["notes"] if n["status"] == "ok") == 2


def test_toolset_exposes_one_batched_tool():
    from agents.sapnotedetail_tools import sapnotedetail_toolset

    http = httpx.AsyncClient(transport=httpx.MockTransport(_json_handler({})))
    toolset = sapnotedetail_toolset({}, http=http, cookie="SESSION=x")
    assert sorted(toolset.tools) == ["get_note_details"]


def test_toolset_takes_a_list_not_a_single_note():
    """129 notes must be one tool call, not 129 LLM turns."""
    import inspect

    from agents.sapnotedetail_tools import sapnotedetail_toolset

    http = httpx.AsyncClient(transport=httpx.MockTransport(_json_handler({})))
    toolset = sapnotedetail_toolset({}, http=http, cookie="SESSION=x")
    params = inspect.signature(toolset.tools["get_note_details"].function).parameters
    assert list(params) == ["notes"]
```

Add `import asyncio` to the test file's imports.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_sapnotedetail_tools.py -v -k "get_details or toolset or session"`
Expected: FAIL — `AttributeError: 'SapNoteDetailClient' object has no attribute 'get_details'`

- [ ] **Step 3: Implement the batch and the toolset**

Append to `agents/sapnotedetail_tools.py`:

```python
    async def get_details(self, notes: list[str]) -> dict[str, Any]:
        """Many notes, fetched concurrently behind a cap.

        One call per note would be one LLM turn per note -- ~129 for a monthly
        run -- so the whole list is fetched here instead.

        A dead session stops further fetching: retrying a hundred times
        against a corpse is noise, and the answer will not change until a
        human opens a browser. Notes already fetched are kept, and the ones
        never attempted are reported as unavailable rather than dropped, so
        the count always matches what was asked for.
        """
        wanted = [str(n).strip() for n in (notes or []) if str(n).strip()]
        if not wanted:
            return {"notes": [], "count": 0, "unavailable": 0, "session_expired": False}

        results: dict[str, dict[str, Any]] = {}
        session_expired = False
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def one(note: str) -> None:
            nonlocal session_expired
            if session_expired:
                return
            async with semaphore:
                if session_expired:
                    return
                try:
                    results[note] = await self.get_detail(note)
                except SessionExpired as e:
                    logger.warning("me.sap.com session is dead: %s", e)
                    session_expired = True

        await asyncio.gather(*(one(n) for n in wanted))

        ordered = [
            results.get(n) or unavailable(
                n, "session expired" if session_expired else "not fetched"
            )
            for n in wanted
        ]
        return {
            "notes": ordered,
            "count": len(ordered),
            "unavailable": sum(1 for n in ordered if n["status"] != "ok"),
            "session_expired": session_expired,
        }


def sapnotedetail_toolset(
    oauth: dict[str, Any],
    *,
    http: httpx.AsyncClient | None = None,
    cookie: str | None = None,
    server_key: str = BUILTIN_SAPNOTEDETAIL_URL,
    auth_mode: str | None = None,
) -> FunctionToolset:
    """The note-detail toolset for one agent.

    The cookie is NOT read from ``oauth``: it is a credential, and credentials
    live in ``mcp_oauth_tokens``, never in a server config block. Passing it
    explicitly is the test path; Task 5 replaces the default with a resolver
    that reads the current principal's stored session at call time, because
    the registry has no principal to resolve one for at build time.
    """
    session = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    client = SapNoteDetailClient(session, cookie or "")
    toolset = FunctionToolset()
    # The registry closes `http_client` on old toolsets when it swaps a build.
    toolset.http_client = session  # type: ignore[attr-defined]

    @toolset.tool
    async def get_note_details(notes: list[str]) -> dict[str, Any]:
        """Look up SAP note detail, including which support package fixes each.

        Pass every note number you need in ONE call — the list is fetched
        concurrently. Do not call this once per note.

        Each entry carries `support_packages` (the fixing level per software
        component) and `validity` (which releases the note applies to). An
        entry with `status` of `unavailable` could not be read; report it as
        unknown, never as "not affected".

        If `session_expired` is true, the SAP session died and the remaining
        notes were never checked. Say so prominently — the report is
        incomplete, not clean.

        Args:
            notes: SAP note numbers, e.g. ["3771065", "3747649"].
        """
        return await client.get_details(notes)

    return toolset
```

- [ ] **Step 4: Run the full file**

Run: `.venv/Scripts/python.exe -m pytest tests/test_sapnotedetail_tools.py -v`
Expected: PASS — 15 tests

- [ ] **Step 5: Commit**

```bash
git add agents/sapnotedetail_tools.py tests/test_sapnotedetail_tools.py
git commit -m "feat(sapnotedetail): batched lookup with a concurrency cap and session-expiry handling"
```

---

### Task 4: `auth_mode="session"` and registration

**Files:**
- Modify: `agents/db.py` (auth-mode constants, ~line 35-60)
- Modify: `agents/builtins.py` (imports and `_FACTORIES`)
- Modify: `agents/admin.py` (`McpServerPayload._validate_oauth`)
- Modify: `tests/test_builtins.py`
- Modify: `tests/test_db_config.py`

**Interfaces:**
- Consumes: `BUILTIN_SAPNOTEDETAIL_URL`, `sapnotedetail_toolset` (Tasks 2-3)
- Produces: `AUTH_MODE_SESSION: str = "session"` in `agents.db`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_builtins.py`:

```python
def test_sapnotedetail_is_a_known_builtin():
    from agents.builtins import BUILTIN_URLS, is_builtin_url

    assert "builtin:sapnotedetail" in BUILTIN_URLS
    assert is_builtin_url("builtin:sapnotedetail")


def test_build_builtin_toolset_constructs_sapnotedetail():
    from pydantic_ai.toolsets import FunctionToolset

    from agents.builtins import build_builtin_toolset

    toolset = build_builtin_toolset("builtin:sapnotedetail", {}, "session")
    assert isinstance(toolset, FunctionToolset)


def test_session_mode_is_rejected_on_any_other_url():
    from pydantic import ValidationError

    from agents.admin import McpServerPayload

    with pytest.raises(ValidationError, match="builtin:sapnotedetail"):
        McpServerPayload(url="builtin:gmail", auth_mode="session")


def test_sapnotedetail_is_rejected_under_any_other_mode():
    from pydantic import ValidationError

    from agents.admin import McpServerPayload

    with pytest.raises(ValidationError, match="auth_mode=session"):
        McpServerPayload(url="builtin:sapnotedetail", auth_mode="none")


def test_sapnotedetail_accepts_session_mode():
    from agents.admin import McpServerPayload

    payload = McpServerPayload(url="builtin:sapnotedetail", auth_mode="session")
    assert payload.auth_mode == "session"
```

Append to `tests/test_db_config.py`:

```python
def test_session_mode_carries_no_config_block():
    """The cookie is a credential and lives in mcp_oauth_tokens, not here."""
    assert _clean_oauth({"cookie": "x"}, "session", None, url="builtin:sapnotedetail") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_builtins.py tests/test_db_config.py -v`
Expected: FAIL — `assert 'builtin:sapnotedetail' in BUILTIN_URLS`

- [ ] **Step 3: Add the auth mode**

In `agents/db.py`, next to `AUTH_MODE_DESTINATION` (~line 52):

```python
# A browser session cookie a human obtained and pasted in. Distinct from
# oauth2 because there is no authorize endpoint and nothing to refresh with:
# only a person with a browser can renew it, which is exactly what the
# credentials panel needs to be able to say.
AUTH_MODE_SESSION = "session"
```

Add it to `VALID_AUTH_MODES` only. Do NOT add it to `OAUTH_CONFIG_MODES`: this
mode carries no config block, and `_clean_oauth` already returns None for modes
it does not recognize, which is what the Step 1 test asserts.

- [ ] **Step 4: Register the factory**

In `agents/builtins.py`, add the import after the sapnotes one:

```python
from agents.sapnotedetail_tools import BUILTIN_SAPNOTEDETAIL_URL, sapnotedetail_toolset
```

and the entry in `_FACTORIES`:

```python
    BUILTIN_SAPNOTEDETAIL_URL: sapnotedetail_toolset,
```

- [ ] **Step 5: Add the two-way validation rule**

In `agents/admin.py`, import `AUTH_MODE_SESSION` from `agents.db`, and add to
`McpServerPayload._validate_oauth`, immediately after the existing
`BUILTIN_JIRA_URL` guard:

```python
        is_note_detail = (
            str(self.url or "").strip().rstrip("/").lower() == BUILTIN_SAPNOTEDETAIL_URL
        )
        if is_note_detail and self.auth_mode != AUTH_MODE_SESSION:
            # Caught here rather than at reload for the same reason the Jira
            # rule is: the toolset has no other way to authenticate, so a
            # server saved under another mode builds fine and then fails
            # mid-run, with the agent still looking configured in the UI.
            raise ValueError(
                f"{BUILTIN_SAPNOTEDETAIL_URL} requires auth_mode=session: it "
                "authenticates with a browser session cookie refreshed by a "
                "human, and holds no credential of its own"
            )
        if self.auth_mode == AUTH_MODE_SESSION and not is_note_detail:
            raise ValueError(
                "auth_mode=session is only supported for "
                f"{BUILTIN_SAPNOTEDETAIL_URL}; no other server reads a "
                "browser session cookie"
            )
```

Import `BUILTIN_SAPNOTEDETAIL_URL` from `agents.sapnotedetail_tools` alongside
the existing `BUILTIN_JIRA_URL` import.

- [ ] **Step 6: Run to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_builtins.py tests/test_db_config.py -v`
Expected: PASS

- [ ] **Step 7: Check for regressions**

Run each affected script-style suite:

```bash
.venv/Scripts/python.exe tests/test_admin_api.py
.venv/Scripts/python.exe tests/test_outlook_tools.py
.venv/Scripts/python.exe tests/test_client_credentials.py
.venv/Scripts/python.exe tests/test_jira_tools.py
```

Expected: `0 failed` for each. If a test pins the exact set of built-in URLs,
change it to a subset assertion — `tests/test_builtins.py` owns the full set.

- [ ] **Step 8: Commit**

```bash
git add agents/db.py agents/builtins.py agents/admin.py tests/test_builtins.py tests/test_db_config.py
git commit -m "feat(sapnotedetail): add auth_mode=session and register the builtin"
```

---

### Task 5: Session storage and the admin endpoint

**Files:**
- Modify: `agents/oauth2.py` (a setter next to the existing token helpers)
- Modify: `agents/admin.py` (new endpoint; credential-status recognition)
- Modify: `agents/sapnotedetail_tools.py` (resolve the cookie per call — Step 6)
- Test: `tests/test_admin_api.py`

`agents/registry.py` is deliberately NOT modified: resolving the cookie at
call time rather than at build time is what avoids touching it.

**Interfaces:**
- Consumes: `AUTH_MODE_SESSION` (Task 4), `sapnotedetail_toolset(cookie=...)` (Task 3)
- Produces:
  - `async store_session_cookie(user_id: str, server_key: str, cookie: str, ttl_hours: int) -> datetime`
  - `POST /admin/api/sessions/{server_key}`

- [ ] **Step 1: Write the failing endpoint tests**

Append to `tests/test_admin_api.py`:

```python
def test_session_payload_rejects_a_blank_cookie():
    import pytest
    from pydantic import ValidationError

    from agents.admin import SessionPayload

    with pytest.raises(ValidationError):
        SessionPayload(cookie="   ")


def test_session_payload_bounds_the_ttl():
    import pytest
    from pydantic import ValidationError

    from agents.admin import SessionPayload

    # A week-long TTL would claim a session is healthy long after SAP dropped
    # it, which is worse than no status at all.
    with pytest.raises(ValidationError):
        SessionPayload(cookie="SESSION=x", expires_in_hours=999)


def test_session_payload_defaults_to_twelve_hours():
    from agents.admin import SessionPayload

    assert SessionPayload(cookie="SESSION=x").expires_in_hours == 12
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_admin_api.py -v -k session`
Expected: FAIL — `ImportError: cannot import name 'SessionPayload'`

- [ ] **Step 3: Add the storage helper**

In `agents/oauth2.py`, next to the other token helpers:

```python
async def store_session_cookie(
    user_id: str, server_key: str, cookie: str, ttl_hours: int = 12
) -> datetime:
    """Persist a browser session cookie as this principal's credential.

    Stored in ``mcp_oauth_tokens`` rather than a table of its own: it is the
    same kind of thing as an access token, and reusing the row means
    ``token_status`` and the admin credentials panel report it with no changes.

    ``refresh_token`` stays NULL on purpose. There is nothing to refresh with,
    so the status goes straight from `valid` to `expired` -- which is the
    truth, because only a human with a browser can renew it.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    async with SessionLocal() as session:
        await upsert_user_token(
            session,
            user_id=user_id,
            server_key=server_key,
            access_token=cookie,
            refresh_token=None,
            token_type="Cookie",
            scope=None,
            expires_at=expires_at,
        )
        await session.commit()
    # The cookie itself is never logged.
    logger.info(
        "stored a session cookie for %s on %s, valid until %s",
        user_id, server_key, expires_at.isoformat(),
    )
    return expires_at
```

Check the real signature of the existing upsert helper with
`grep -n "async def upsert_user_token" -A 12 agents/oauth2.py` and match it;
adapt the call if the parameter names differ.

- [ ] **Step 4: Add the endpoint**

In `agents/admin.py`, add the payload next to the other schemas:

```python
class SessionPayload(BaseModel):
    """A browser session cookie for a `session`-mode server."""

    cookie: str = Field(min_length=1, max_length=16384)
    # 12h matches the upstream project's cache TTL. Bounded because an
    # over-long TTL makes the credentials panel claim a session is healthy
    # long after SAP dropped it, which is worse than showing nothing.
    expires_in_hours: int = Field(default=12, ge=1, le=48)
    # Defaults to the caller. Set it when connecting a service identity that
    # is not you -- the same value the "Use my principal" button fills in.
    principal: str = Field(default="", max_length=255)

    @field_validator("cookie")
    @classmethod
    def _cookie_is_not_blank(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            raise ValueError("cookie must not be blank")
        return text
```

and the route:

```python
@router.post("/api/sessions/{server_key:path}", dependencies=[Depends(require_admin)])
async def api_store_session(server_key: str, payload: SessionPayload) -> dict[str, Any]:
    """Store a browser session cookie for a `session`-mode server.

    The response never echoes the cookie: it is a live credential, and an
    admin API that reflects one back has widened where it can leak to.
    """
    from agents.oauth2 import store_session_cookie
    from agents.sapnotedetail_tools import BUILTIN_SAPNOTEDETAIL_URL

    key = server_key.strip().lower()
    if key != BUILTIN_SAPNOTEDETAIL_URL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{server_key!r} does not use a browser session",
        )

    who = (payload.principal or "").strip() or (current_principal() or "")
    if not who:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no principal: pass one, or call this with a user token",
        )

    expires_at = await store_session_cookie(
        who, key, payload.cookie, payload.expires_in_hours
    )
    return {"server_key": key, "principal": who, "expires_at": expires_at.isoformat()}
```

- [ ] **Step 5: Report the mode as needing a token**

`session` must count as a mode that needs a user token, so the panel shows its
state and the pre-flight check covers it.

Run `grep -n "needs_token" agents/admin.py` and include `AUTH_MODE_SESSION`
wherever `AUTH_MODE_OAUTH2` makes `needs_token` true. Do NOT add it to the
`no_user_token` tuple alongside `AUTH_MODE_APP_ONLY` / `AUTH_MODE_DESTINATION`:
that tuple means "connected by configuration, nobody signs in", and this mode
is the opposite — a human is exactly what it needs.

Then confirm the pre-flight path covers it:
`grep -n "has_usable_token\|needs_token" agents/workflow_runner.py`. If that
check keys off the same helper, nothing more is required; if it has its own
mode list, add `AUTH_MODE_SESSION` there too.

- [ ] **Step 6: Resolve the cookie per call**

`build_builtin_toolset` passes only `(oauth, server_key, auth_mode)` — there is
no principal at build time — so the cookie cannot be captured when the registry
builds. Pass a resolver instead, which the client awaits per call (Task 2's
`_cookie_header`).

In `agents/sapnotedetail_tools.py`, change the factory's default so that a
`cookie` of `None` becomes a resolver rather than an empty string:

```python
    async def _stored_cookie() -> str:
        """The current principal's stored session, read at call time."""
        from agents.auth import current_principal
        from agents.db import SessionLocal
        from agents.oauth2 import get_user_token

        who = current_principal() or ""
        if not who:
            return ""
        async with SessionLocal() as db:
            row = await get_user_token(db, who, server_key)
        return (row.access_token if row else "") or ""

    client = SapNoteDetailClient(session, cookie if cookie is not None else _stored_cookie)
```

Check `get_user_token`'s real signature first
(`grep -n "async def get_user_token" -A 8 agents/oauth2.py`) and match it.

No change to `agents/registry.py` or `agents/builtins.py` is needed — which is
the point of resolving lazily.

- [ ] **Step 7: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_admin_api.py -v -k session`
Expected: PASS — 3 tests

Run: `.venv/Scripts/python.exe tests/test_admin_api.py`
Expected: `0 failed`

Run: `.venv/Scripts/python.exe tests/test_oauth2.py`
Expected: `0 failed`

- [ ] **Step 8: Commit**

```bash
git add agents/oauth2.py agents/admin.py agents/registry.py tests/test_admin_api.py
git commit -m "feat(sapnotedetail): store the session cookie and serve it to the toolset"
```

---

### Task 6: Both admin UIs

**Files:**
- Modify: `ui5-admin/webapp/model/validators.ts`
- Modify: `ui5-admin/webapp/service/types.ts`
- Modify: `ui5-admin/webapp/fragment/McpServerDialog.fragment.xml`
- Modify: `ui5-admin/webapp/i18n/i18n.properties`
- Modify: `templates/admin.html`
- Test: `ui5-admin/webapp/test/unit/validators.qunit.ts`

**Interfaces:**
- Consumes: `builtin:sapnotedetail`, `auth_mode="session"` (Task 4)

- [ ] **Step 1: Write the failing QUnit cases**

Append to `ui5-admin/webapp/test/unit/validators.qunit.ts`:

```typescript
QUnit.test("accepts builtin:sapnotedetail", function (assert) {
    assert.strictEqual(validators.validateServerUrl("builtin:sapnotedetail", "session"), "");
});

QUnit.test("session mode carries no oauth config", function (assert) {
    assert.strictEqual(
        validators.validateOAuth(undefined, "session", "builtin:sapnotedetail"), ""
    );
});

QUnit.test("session mode rejects an oauth config block", function (assert) {
    assert.notStrictEqual(
        validators.validateOAuth({ client_id: "x" }, "session", "builtin:sapnotedetail"), ""
    );
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ui5-admin && npm test`
Expected: FAIL — `builtin:sapnotedetail` is not in `BUILTIN_URLS`

- [ ] **Step 3: Update the validator and types**

In `ui5-admin/webapp/model/validators.ts`, add `"builtin:sapnotedetail"` to
`BUILTIN_URLS`.

In `ui5-admin/webapp/service/types.ts`, extend the `AuthMode` union:

```typescript
export type AuthMode = "jwt" | "none" | "oauth2" | "app_only" | "destination" | "session";
```

`validateOAuth`'s existing `authMode !== "oauth2"` branch already rejects a
config block for any unknown mode, so `session` needs no new branch there —
confirm the Step 1 tests pass on that behaviour rather than adding one.

- [ ] **Step 4: Add the dialog option and labels**

In `ui5-admin/webapp/i18n/i18n.properties`, next to the other auth-mode labels:

```properties
#XSEL: Browser session cookie auth mode
authSession=Browser session (refreshed by hand)
#XFLD: Explains that this credential is not entered in this dialog
sessionHint=This server uses a session cookie refreshed with scripts/sap_session.py. There is nothing to enter here.
```

In `ui5-admin/webapp/fragment/McpServerDialog.fragment.xml`, add the option to
the auth-mode `Select`:

```xml
                <core:Item key="session" text="{i18n>authSession}" />
```

and a hint above the credential fields, so the empty dialog does not read as
broken:

```xml
            <Label text="" visible="{= ${server>/auth_mode} === 'session' }" />
            <Text text="{i18n>sessionHint}"
                  visible="{= ${server>/auth_mode} === 'session' }" />
```

- [ ] **Step 5: Mirror in the legacy admin**

In `templates/admin.html`, find the auth-mode `<select>` and add:

```html
<option value="session">Browser session (refreshed by hand)</option>
```

Grep for the other mode values first (`grep -n 'value="destination"' templates/admin.html`)
and follow whatever shape that file uses; do not restructure it.

- [ ] **Step 6: Run both suites**

Run: `cd ui5-admin && npm test`
Expected: PASS — all existing tests plus 3 new

Run: `.venv/Scripts/python.exe tests/test_admin_ui.py`
Expected: `0 failed`

- [ ] **Step 7: Commit**

```bash
git add ui5-admin/webapp templates/admin.html
git commit -m "feat(sapnotedetail): expose auth_mode=session in both admin UIs"
```

---

### Task 7: The refresh script and the operator guide

**Files:**
- Modify: `scripts/sap_session.py` (add the `refresh` command)
- Create: `docs/SAP_NOTE_DETAIL.md`
- Modify: `docs/SAP_SECURITY_NOTES.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `login()` (Task 1), `POST /admin/api/sessions/{server_key}` (Task 5)

- [ ] **Step 1: Add the refresh command**

In `scripts/sap_session.py`, add a second subcommand that logs in and POSTs the
cookie to a running app:

```python
async def refresh(base_url: str, token: str, *, headful: bool = False) -> None:
    """Log in and hand the resulting cookie to the app.

    The cookie is written to the app over HTTPS and never printed here.
    """
    user = os.environ.get("SAP_DIALOG_USER", "").strip()
    pwd = os.environ.get("SAP_DIALOG_PWD", "").strip()
    if not user or not pwd:
        raise SystemExit("SAP_DIALOG_USER / SAP_DIALOG_PWD are not set in .env")

    cookie = await login(user, pwd, headful=headful)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{base_url.rstrip('/')}/admin/api/sessions/builtin:sapnotedetail",
            json={"cookie": cookie, "expires_in_hours": 12},
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code >= 400:
        raise SystemExit(f"app refused the session: {r.status_code} {r.text[:200]}")
    print(f"session stored, valid until {r.json().get('expires_at')}")
```

Wire it into `main()` as `refresh --base-url ... --token ...`, reading the
token from `ADMIN_TOKEN` when the flag is absent.

- [ ] **Step 2: Write the operator guide**

Create `docs/SAP_NOTE_DETAIL.md` covering, in this order:

1. **What this adds** — the fixing support-package level per note, which is
   what moves notes out of the UNKNOWN bucket.
2. **The credential.** A *dialog* S-user or Universal ID, NOT the technical
   communication user in `SAP_NOTES_USER`. State plainly that a technical user
   cannot log on in dialog mode and will fail.
3. **Why runs are attended.** The session lasts hours and the job is monthly,
   so the session is always stale when a run is due: refresh, then trigger.
4. **One-time setup:** `pip install playwright`, `playwright install chromium`,
   set `SAP_DIALOG_USER` / `SAP_DIALOG_PWD` in `.env`. Note explicitly that
   Playwright is deliberately absent from `requirements.txt` so Chromium stays
   out of the Cloud Foundry buildpack.
5. **Refreshing:**
   `python scripts/sap_session.py refresh --base-url https://... --token ...`,
   with `--headful` when MFA is expected.
6. **Checking it took:** the agent's credentials panel shows `valid` and an
   expiry. If it shows `expired`, the run will refuse to start.
7. **Known limits:** private API subject to SAP's ToS, may change without
   notice; `unavailable` notes are reported as unknown, never as "not
   affected"; a session that dies mid-run truncates the report and the digest
   says so.

- [ ] **Step 3: Cross-link from the Phase 1 guide**

In `docs/SAP_SECURITY_NOTES.md`, replace the Phase 2 bullet under "Not in
Phase 1" with a pointer to `docs/SAP_NOTE_DETAIL.md`, and update the "roughly
half of critical notes land in UNKNOWN" limitation to say that note detail now
narrows it.

- [ ] **Step 4: Update CLAUDE.md**

Add after the `agents/sapnotes_tools.py` entry:

```markdown
- `agents/sapnotedetail_tools.py` — SAP note detail from the private
  `me.sap.com` backend (`builtin:sapnotedetail`), giving the support-package
  level that fixes each note. Authenticates with a browser session cookie
  under `auth_mode="session"`, stored in `mcp_oauth_tokens` and refreshed by
  hand with `scripts/sap_session.py`; Playwright stays off the platform.
  See `docs/SAP_NOTE_DETAIL.md`
```

Also add `session` to the `auth_mode` list in the `agents/db.py` bullet.

- [ ] **Step 5: Run everything**

Run every script-style suite and the pytest-style files:

```bash
for f in tests/test_*.py; do echo "== $f"; .venv/Scripts/python.exe "$f" 2>&1 | tail -1; done
.venv/Scripts/python.exe -m pytest tests/test_sapnotedetail_tools.py tests/test_sapnotes_tools.py tests/test_builtins.py tests/test_db_config.py tests/test_admin_api.py -q
cd ui5-admin && npm test
```

Expected: every suite `0 failed` except `test_agent_bundles.py`, which fails 9
pre-existing checks unrelated to this work.

- [ ] **Step 6: Commit**

```bash
git add scripts/sap_session.py docs/SAP_NOTE_DETAIL.md docs/SAP_SECURITY_NOTES.md CLAUDE.md
git commit -m "docs(sapnotedetail): operator guide and the session refresh command"
```

---

## Out of scope

- **Rewiring the workflow.** Feeding `support_packages` into the analyst prompt
  and reshaping the digest is a separate plan; this delivers the toolset.
- **Unattended runs.** Needs a credential SAP does not offer for this endpoint.
- **Note text, corrections, TCI transports.**
- **Replacing the ARC-1 analyst.** CVERS says what is installed; this says what
  fixes the note. Both are needed.

## Verification

Done when:

1. Every test suite is green by the project's runner (per-file scripts plus
   pytest over the pytest-style files), and `npm test` passes in `ui5-admin/`.
2. An agent configured with `builtin:sapnotedetail` under `auth_mode="session"`
   saves, and the credentials panel shows `expired` before a session is stored
   and `valid` after `scripts/sap_session.py refresh`.
3. `get_note_details` against a real session returns `support_packages` for a
   known ABAP note.
4. With the session deliberately expired, the workflow refuses to start and the
   run view shows a failed step naming the server.
