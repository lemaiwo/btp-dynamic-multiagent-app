# Per-Agent Model and Peer Delegation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each agent run on its own LLM, and let an agent consult another agent directly instead of every hop routing through the single orchestrator.

**Architecture:** Two new nullable columns on `agent_configs` (`model_name`, `peers_json`) plus a second pass in `build_orchestrator()` that attaches the existing `_attach_delegation_tool` to specialists rather than only to the orchestrator. Recursion is bounded at run time by a `ContextVar` delegation stack — a depth cap and a re-entry guard — because mutual peers are a legitimate configuration and must not be rejected at save time.

**Tech Stack:** Python 3.13, SQLAlchemy async, FastAPI, pydantic-ai, vanilla-JS admin template.

**Spec:** `docs/superpowers/specs/2026-08-31-agent-workflows-design.md` (components 1 and 2, increments 1 and 2). Component 3–5 — the workflow engine — is a separate plan.

## Global Constraints

- **Tests are standalone scripts, not pytest.** Every suite is `python tests/test_x.py`: module-level env setup, a `check(label, condition, detail)` helper, `async def main()`, `asyncio.run(main())`, and `sys.exit(1 if FAILED else 0)`. Copy the shape from `tests/test_job_runs.py`. Do not add pytest.
- **Env must be set before importing `agents.db`**, because the module resolves `DATABASE_URL` at import. Every new suite sets `DATABASE_URL` to its own SQLite file, pops `VCAP_SERVICES` and `VCAP_APPLICATION`, and sets `MCP_URL_ALLOWLIST=""` (empty, not popped — `load_dotenv()` fills absent vars but never overrides present ones).
- **`AICORE_AVAILABLE_MODELS` must be set in any suite that touches models.** `agents.shared.available_models()` otherwise queries SAP AI Core over the network.
- **`agents.registry.get_model` must be patched in any suite that builds the registry.** The real one needs AI Core credentials. Replace it with a function returning `pydantic_ai.models.test.TestModel()`; a plain stub object fails, because `Agent()` calls `models.infer_model()` on it.
- **Schema changes use `_ensure_column`** in `agents/db.py:init_db`. `create_all` does not alter existing tables.
- **New columns are nullable with no default**, so existing rows and existing exports keep working untouched.
- **Peers and skills are referenced by name, never by id** (`AgentConfig.skills` is the precedent), so exports and `agents.seed.json` stay portable across landscapes.
- **A malformed JSON column must never break the build.** Log a warning and return `[]`, exactly as `AgentConfig.skills` does.

---

### Task 1: Per-agent model override

**Files:**
- Modify: `agents/db.py` — `AgentConfig` column, `to_dict`, `to_export`, `init_db`, `upsert_agent`
- Modify: `agents/admin.py` — `AgentPayload.model_name`, `api_create_agent`, `api_update_agent`
- Modify: `agents/registry.py` — per-row model resolution in `build_orchestrator`
- Test: `tests/test_agent_model.py` (create)

**Interfaces:**
- Consumes: `agents.shared.get_model(name)`, `agents.shared.default_model_name()`, `agents.shared.available_models()` — all already exist.
- Produces:
  - `AgentConfig.model_name: str | None`
  - `upsert_agent(..., model_name: str | None = None)`
  - `registry._model_for(row, *, default_model, default_name, cache) -> Model`
  - `to_dict()["model_name"]` and `to_export()["model_name"]`, both `""` when unset.

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_model.py`:

```python
"""Per-agent model override: storage, export, and registry resolution.

Run:  python tests/test_agent_model.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_agent_model.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
# A developer .env may set this. Set it EMPTY rather than popping it: app.py
# calls load_dotenv(), which fills in vars that are absent but never overrides
# ones already present. Empty means "no allowlist", i.e. the default rule.
os.environ["MCP_URL_ALLOWLIST"] = ""
# available_models() would otherwise query AI Core over the network.
os.environ["AICORE_AVAILABLE_MODELS"] = "gpt-4o,gpt-4o-mini"
os.environ["AICORE_MODEL"] = "gpt-4o"

from pydantic_ai.models.test import TestModel  # noqa: E402

import agents.registry as registry_module  # noqa: E402
from agents.db import (  # noqa: E402
    SessionLocal,
    get_agent_by_name,
    init_db,
    upsert_agent,
)

FAILED = 0
PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}   {detail}")


def servers(host: str) -> list[dict[str, str]]:
    return [{"url": f"https://{host}.example.com/mcp", "auth_mode": "none"}]


async def main() -> None:
    await init_db()

    print("\n== storage ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="small", description="cheap reader", instructions="read",
            mcp_servers=servers("small"), model_name="gpt-4o-mini",
        )
        await upsert_agent(
            s, name="plain", description="no override", instructions="work",
            mcp_servers=servers("plain"),
        )
        await upsert_agent(
            s, name="broken", description="bad model", instructions="work",
            mcp_servers=servers("broken"), model_name="no-such-model",
        )
        small = await get_agent_by_name(s, "small")
        plain = await get_agent_by_name(s, "plain")
        check("override stored", small.model_name == "gpt-4o-mini", small.model_name)
        check("absent override is None", plain.model_name is None, plain.model_name)
        check("to_dict carries it", small.to_dict()["model_name"] == "gpt-4o-mini")
        check("to_dict blank when unset", plain.to_dict()["model_name"] == "")
        check("to_export carries it", small.to_export()["model_name"] == "gpt-4o-mini")

    print("\n== blank is normalized to None ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="small", description="cheap reader", instructions="read",
            mcp_servers=servers("small"), model_name="   ",
        )
        blanked = await get_agent_by_name(s, "small")
        check("whitespace override becomes None", blanked.model_name is None,
              repr(blanked.model_name))
        # Put it back for the build tests below.
        await upsert_agent(
            s, name="small", description="cheap reader", instructions="read",
            mcp_servers=servers("small"), model_name="gpt-4o-mini",
        )

    print("\n== registry resolution ==")
    asked: list[str | None] = []
    instances: dict[str | None, TestModel] = {}

    def fake_get_model(name: str | None = None):
        asked.append(name)
        if name == "no-such-model":
            raise RuntimeError("deployment not found")
        if name not in instances:
            instances[name] = TestModel()
        return instances[name]

    real_get_model = registry_module.get_model
    registry_module.get_model = fake_get_model
    try:
        build = await registry_module.build_orchestrator()
    finally:
        registry_module.get_model = real_get_model

    default_model = instances["gpt-4o"]
    check("override model was requested", "gpt-4o-mini" in asked, str(asked))
    check("overriding agent got its own model",
          build.specialists["small"].model is instances["gpt-4o-mini"])
    check("plain agent got the global model",
          build.specialists["plain"].model is default_model)
    check("orchestrator keeps the global model",
          build.orchestrator.model is default_model)

    print("\n== unresolvable override falls back ==")
    check("agent with a bad model is still built",
          "broken" in build.specialists, str(sorted(build.specialists)))
    check("bad override falls back to the global model",
          build.specialists["broken"].model is default_model)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_agent_model.py`
Expected: FAIL — `upsert_agent() got an unexpected keyword argument 'model_name'`.

- [ ] **Step 3: Add the column and storage plumbing**

In `agents/db.py`, add to `AgentConfig` immediately after the `run_timeout_seconds` column:

```python
    # Overrides the globally active model for this agent. Null means "use the
    # global one" — which is what every agent did before workflows needed a
    # cheap reader and an expensive specialist in the same chain.
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
```

In `AgentConfig.to_dict`, after `"run_timeout_seconds": self.run_timeout_seconds,`:

```python
            "model_name": self.model_name or "",
```

In `AgentConfig.to_export`, after `"run_timeout_seconds": self.run_timeout_seconds,`:

```python
            "model_name": self.model_name or "",
```

In `init_db`, after the `expected_sections_json` call:

```python
        await _ensure_column(
            conn, "agent_configs", "model_name", "VARCHAR(128)"
        )
```

In `upsert_agent`, add the keyword parameter after `run_timeout_seconds: int = 1800,`:

```python
    model_name: str | None = None,
```

and set it on both branches. In the `existing is None` branch, after
`row.run_timeout_seconds = int(run_timeout_seconds)`:

```python
        row.model_name = (model_name or "").strip() or None
```

In the `else` branch, after `existing.run_timeout_seconds = int(run_timeout_seconds)`:

```python
        existing.model_name = (model_name or "").strip() or None
```

- [ ] **Step 4: Add the registry resolution**

In `agents/registry.py`, add this helper just above `build_orchestrator`:

```python
def _model_for(row: AgentConfig, *, default_model, default_name: str, cache: dict):
    """The model this agent should run on.

    Null or blank ``model_name`` means "use the globally active model", which
    is what every agent did before per-agent models existed. An override that
    cannot be loaded falls back to the global model with a warning rather than
    failing the build: one bad value must not take the whole registry down,
    for the same reason the global resolution already falls back.
    """
    name = (row.model_name or "").strip()
    if not name or name == default_name:
        return default_model
    if name in cache:
        return cache[name]
    try:
        model = get_model(name)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Agent %s requests model %r, which could not be loaded; using the "
            "active model %r instead. Pick a valid model in /admin to clear this.",
            row.name, name, default_name, exc_info=True,
        )
        model = default_model
    cache[name] = model
    return model
```

In `build_orchestrator`, add a cache alongside the existing `specialists` and
`mcp_clients` declarations:

```python
    model_cache: dict = {}
```

and change the specialist construction from `Agent(model, ...)` to:

```python
        specialist = Agent(
            _model_for(row, default_model=model, default_name=model_name,
                       cache=model_cache),
            instructions=specialist_instructions,
            toolsets=servers,
            retries=_TOOL_RETRIES,
        )
```

Leave the `orchestrator = Agent(model, ...)` line unchanged — the orchestrator
keeps the global model.

- [ ] **Step 5: Add the admin API field**

In `agents/admin.py`, add to `AgentPayload` after the `skills` field:

```python
    model_name: str = Field(default="", max_length=128)

    @field_validator("model_name")
    @classmethod
    def _validate_model_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            return ""
        allowed = available_models()
        if v not in allowed:
            raise ValueError(f"model_name must be one of {allowed}")
        return v
```

In `api_create_agent`, add to the `upsert_agent(...)` call after
`run_timeout_seconds=payload.run_timeout_seconds,`:

```python
                model_name=payload.model_name,
```

In `api_update_agent`, after `row.run_timeout_seconds = payload.run_timeout_seconds`:

```python
        row.model_name = payload.model_name.strip() or None
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_agent_model.py`
Expected: PASS, `0 failed`.

- [ ] **Step 7: Run the suites this task touches**

Run:
```bash
./.venv/Scripts/python.exe tests/test_admin_api.py
./.venv/Scripts/python.exe tests/test_job_runs.py
./.venv/Scripts/python.exe tests/test_agent_bundles.py
```
Expected: all PASS. `test_agent_bundles.py` exercises export/import, which now
carries `model_name`.

- [ ] **Step 8: Commit**

```bash
git add agents/db.py agents/admin.py agents/registry.py tests/test_agent_model.py
git commit -m "feat: per-agent model override"
```

---

### Task 2: Store the peer list

**Files:**
- Modify: `agents/db.py` — `AgentConfig.peers_json` column, `peers` property, `to_dict`, `to_export`, `init_db`, `upsert_agent`
- Modify: `agents/admin.py` — `AgentPayload.peers`, `api_create_agent`, `api_update_agent`
- Test: `tests/test_peer_delegation.py` (create)

**Interfaces:**
- Consumes: `AgentConfig.skills` as the pattern to mirror.
- Produces:
  - `AgentConfig.peers_json: str | None` and `AgentConfig.peers -> list[str]`
  - `upsert_agent(..., peers: list[str] | None = None)`
  - `to_dict()["peers"]` and `to_export()["peers"]`, both `list[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_peer_delegation.py` with this content. Later tasks append to
the same file, so keep the helper block intact:

```python
"""Agent-to-agent peer delegation: storage, build wiring, and loop guards.

Run:  python tests/test_peer_delegation.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_peer_delegation.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
# A developer .env may set this. Set it EMPTY rather than popping it: app.py
# calls load_dotenv(), which fills in vars that are absent but never overrides
# ones already present. Empty means "no allowlist", i.e. the default rule.
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ["AICORE_AVAILABLE_MODELS"] = "gpt-4o"
os.environ["AICORE_MODEL"] = "gpt-4o"

from pydantic_ai.models.test import TestModel  # noqa: E402

import agents.registry as registry_module  # noqa: E402
from agents.db import (  # noqa: E402
    SessionLocal,
    get_agent_by_name,
    init_db,
    upsert_agent,
)

FAILED = 0
PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}   {detail}")


def servers(host: str) -> list[dict[str, str]]:
    return [{"url": f"https://{host}.example.com/mcp", "auth_mode": "none"}]


def tool_names(agent) -> list[str]:
    """The function tools registered on a pydantic-ai Agent.

    `_function_toolset` is private, but it is the only handle on the registered
    tool set; there is no public accessor. If a pydantic-ai upgrade moves it,
    this helper is the single place to fix.
    """
    toolset = getattr(agent, "_function_toolset", None)
    return sorted(getattr(toolset, "tools", {}).keys())


async def build_with_test_model():
    """Build the registry with a fake model so no AI Core credentials are needed."""
    real = registry_module.get_model
    registry_module.get_model = lambda name=None: TestModel()
    try:
        return await registry_module.build_orchestrator()
    finally:
        registry_module.get_model = real


async def main() -> None:
    await init_db()

    print("\n== peer storage ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="abap", description="ABAP specialist", instructions="abap",
            mcp_servers=servers("abap"),
        )
        await upsert_agent(
            s, name="fiori", description="Fiori specialist", instructions="fiori",
            mcp_servers=servers("fiori"), peers=["abap"],
        )
        fiori = await get_agent_by_name(s, "fiori")
        abap = await get_agent_by_name(s, "abap")
        check("peers stored", fiori.peers == ["abap"], str(fiori.peers))
        check("no peers is an empty list", abap.peers == [], str(abap.peers))
        check("to_dict carries peers", fiori.to_dict()["peers"] == ["abap"])
        check("to_export carries peers", fiori.to_export()["peers"] == ["abap"])

    print("\n== peer list is cleaned ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="fiori", description="Fiori specialist", instructions="fiori",
            mcp_servers=servers("fiori"), peers=["abap", "  ", "abap", ""],
        )
        fiori = await get_agent_by_name(s, "fiori")
        check("blanks dropped and duplicates collapsed",
              fiori.peers == ["abap"], str(fiori.peers))

    print("\n== malformed json degrades quietly ==")
    async with SessionLocal() as s:
        row = await get_agent_by_name(s, "fiori")
        row.peers_json = "{not json"
        await s.commit()
        row = await get_agent_by_name(s, "fiori")
        check("malformed peers_json yields []", row.peers == [], str(row.peers))
        row.peers_json = '{"a": 1}'
        await s.commit()
        row = await get_agent_by_name(s, "fiori")
        check("non-list peers_json yields []", row.peers == [], str(row.peers))
        # Restore for the build tests in later tasks.
        row.peers_json = '["abap"]'
        await s.commit()

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_peer_delegation.py`
Expected: FAIL — `upsert_agent() got an unexpected keyword argument 'peers'`.

- [ ] **Step 3: Add the column, property and storage plumbing**

In `agents/db.py`, add to `AgentConfig` immediately after the `skills_json` column:

```python
    # JSON-encoded list of agent names this agent may consult directly. Peers
    # become delegation tools on this agent, so a chain can run specialist to
    # specialist instead of routing every hop through the orchestrator.
    # Referenced by name, like skills, so exports stay portable.
    peers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Add this property immediately after the `skills` property:

```python
    @property
    def peers(self) -> list[str]:
        """Names of the agents this agent may consult (may be empty)."""
        if not self.peers_json:
            return []
        try:
            data = json.loads(self.peers_json)
        except Exception:
            logger.warning("Malformed peers_json on agent %s", self.name)
            return []
        if not isinstance(data, list):
            return []
        return [str(p) for p in data if isinstance(p, str) and p.strip()]
```

In `to_dict`, after `"skills": self.skills,`:

```python
            "peers": self.peers,
```

In `to_export`, after `"skills": self.skills,`:

```python
            "peers": self.peers,
```

In `init_db`, after the `model_name` call added in Task 1:

```python
        await _ensure_column(conn, "agent_configs", "peers_json", "TEXT")
```

In `upsert_agent`, add the keyword parameter after `model_name: str | None = None,`:

```python
    peers: list[str] | None = None,
```

and just below the existing `skills_json = await normalize_skills_json(...)` line:

```python
    # Order-preserving de-duplication: the same peer listed twice would
    # otherwise register the same delegation tool twice on the same agent.
    seen: set[str] = set()
    cleaned_peers: list[str] = []
    for p in peers or []:
        p = str(p).strip()
        if p and p not in seen:
            seen.add(p)
            cleaned_peers.append(p)
    peers_json = json.dumps(cleaned_peers) if cleaned_peers else None
```

Set it on both branches. In the `existing is None` branch, after
`row.model_name = ...`:

```python
        row.peers_json = peers_json
```

In the `else` branch, after `existing.model_name = ...`:

```python
        existing.peers_json = peers_json
```

- [ ] **Step 4: Add the admin API field**

In `agents/admin.py`, add to `AgentPayload` after the `skills` field and its
validator:

```python
    peers: list[str] = Field(default_factory=list)

    @field_validator("peers")
    @classmethod
    def _clean_peers(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for p in v:
            p = str(p).strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out
```

In `api_create_agent`, add to the `upsert_agent(...)` call after
`model_name=payload.model_name,`:

```python
                peers=payload.peers,
```

In `api_update_agent`, after `row.model_name = payload.model_name.strip() or None`:

```python
        row.peers_json = json.dumps(payload.peers) if payload.peers else None
```

If `json` is not already imported in `agents/admin.py`, add `import json` to the
standard-library imports at the top.

- [ ] **Step 5: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_peer_delegation.py`
Expected: PASS, `0 failed`.

- [ ] **Step 6: Run the suites this task touches**

Run:
```bash
./.venv/Scripts/python.exe tests/test_admin_api.py
./.venv/Scripts/python.exe tests/test_agent_bundles.py
```
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add agents/db.py agents/admin.py tests/test_peer_delegation.py
git commit -m "feat: store the per-agent peer list"
```

---

### Task 3: Attach peer delegation tools in a second build pass

**Files:**
- Modify: `agents/registry.py` — `build_orchestrator` second pass
- Test: `tests/test_peer_delegation.py` (extend)

**Interfaces:**
- Consumes: `AgentConfig.peers` (Task 2), `_attach_delegation_tool(parent, specialist, row)` — already exists and already takes the parent agent as its first argument, so its body needs no change.
- Produces: specialists carrying `delegate_<peer>` tools, named by the existing `_sanitize_tool_name`.

- [ ] **Step 1: Write the failing test**

In `tests/test_peer_delegation.py`, insert this block immediately before the
final `print(f"\n==== {PASSED} passed...")` line:

```python
    print("\n== second pass attaches peer tools ==")
    async with SessionLocal() as s:
        # "zulu" sorts after "abap", and list_agents() orders by name, so zulu's
        # peer is built BEFORE zulu itself — the case a single-pass build gets
        # wrong. "alpha" sorts first and consults zulu, covering the opposite
        # order in the same build.
        await upsert_agent(
            s, name="zulu", description="Z specialist", instructions="z",
            mcp_servers=servers("zulu"), peers=["abap"],
        )
        await upsert_agent(
            s, name="alpha", description="A specialist", instructions="a",
            mcp_servers=servers("alpha"), peers=["zulu"],
        )
        await upsert_agent(
            s, name="lonely", description="no peers", instructions="l",
            mcp_servers=servers("lonely"),
        )

    build = await build_with_test_model()
    check("peer built before its consumer is attached",
          "delegate_abap" in tool_names(build.specialists["zulu"]),
          str(tool_names(build.specialists["zulu"])))
    check("peer built after its consumer is attached",
          "delegate_zulu" in tool_names(build.specialists["alpha"]),
          str(tool_names(build.specialists["alpha"])))
    check("an agent without peers gets no delegation tools",
          tool_names(build.specialists["lonely"]) == [],
          str(tool_names(build.specialists["lonely"])))
    check("the orchestrator still delegates to every chat agent",
          "delegate_abap" in tool_names(build.orchestrator)
          and "delegate_lonely" in tool_names(build.orchestrator),
          str(tool_names(build.orchestrator)))

    print("\n== unusable peer names are skipped ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="picky", description="odd peers", instructions="p",
            mcp_servers=servers("picky"),
            peers=["picky", "ghost", "disabled-one", "abap"],
        )
        await upsert_agent(
            s, name="disabled-one", description="off", instructions="d",
            mcp_servers=servers("disabled"), enabled=False,
        )

    build = await build_with_test_model()
    picky_tools = tool_names(build.specialists["picky"])
    check("self-reference skipped", "delegate_picky" not in picky_tools, str(picky_tools))
    check("unknown peer skipped", "delegate_ghost" not in picky_tools, str(picky_tools))
    check("disabled peer skipped",
          "delegate_disabled_one" not in picky_tools, str(picky_tools))
    check("valid peer still attached", "delegate_abap" in picky_tools, str(picky_tools))
    check("build survives bad peers", "picky" in build.specialists)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_peer_delegation.py`
Expected: FAIL — `peer built before its consumer is attached`, because no peer
tools are attached at all yet (the specialist tool list is empty).

- [ ] **Step 3: Add the second pass**

In `agents/registry.py`, in `build_orchestrator`, add this block immediately
before the closing `return BuildResult(...)`:

```python
    # Second pass: attach peer delegation tools. This cannot be folded into the
    # loop above, because a peer may be built after the agent that consults it
    # — the build order follows list_agents() (alphabetical), not the peer
    # graph. Peers are looked up by name, like skills, so a name that no longer
    # resolves is skipped with a warning rather than failing the build.
    rows_by_name = {r.name: r for r in enabled_rows}
    for row in enabled_rows:
        parent = specialists.get(row.name)
        if parent is None:
            continue  # agent had no usable MCP servers; already warned above
        for peer_name in row.peers:
            if peer_name == row.name:
                logger.warning("Agent %s lists itself as a peer; skipping", row.name)
                continue
            peer_specialist = specialists.get(peer_name)
            peer_row = rows_by_name.get(peer_name)
            if peer_specialist is None or peer_row is None:
                logger.warning(
                    "Agent %s references unknown or unbuilt peer %r; skipping it",
                    row.name, peer_name,
                )
                continue
            _attach_delegation_tool(parent, peer_specialist, peer_row)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_peer_delegation.py`
Expected: PASS, `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add agents/registry.py tests/test_peer_delegation.py
git commit -m "feat: attach peer delegation tools in a second build pass"
```

---

### Task 4: Bound peer recursion with a depth cap and a re-entry guard

**Files:**
- Modify: `agents/registry.py` — module constants and the `_delegate` body inside `_attach_delegation_tool`
- Test: `tests/test_peer_delegation.py` (extend)

**Interfaces:**
- Consumes: `_attach_delegation_tool` (Task 3).
- Produces:
  - `registry._delegation_stack: ContextVar[tuple[str, ...]]`
  - `registry._MAX_DELEGATION_DEPTH: int` from `AGENT_DELEGATION_MAX_DEPTH`, default 3
  - `registry._depth_message(name) -> str`, `registry._reentry_message(name) -> str`

Both guards **return a string** to the calling model rather than raising: a
delegation tool that raises surfaces to the user as a failed turn, whereas a
message lets the model answer with what it already has.

- [ ] **Step 1: Write the failing test**

In `tests/test_peer_delegation.py`, insert this block immediately before the
final `print(f"\n==== {PASSED} passed...")` line:

```python
    print("\n== recursion guards ==")
    from pydantic_ai import RunContext  # noqa: PLC0415

    # A→B→A must not recurse. Build a mutual pair and drive the tool directly:
    # the guards live in the tool body, so calling it is the honest test.
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="ping", description="P", instructions="p",
            mcp_servers=servers("ping"), peers=["pong"],
        )
        await upsert_agent(
            s, name="pong", description="Q", instructions="q",
            mcp_servers=servers("pong"), peers=["ping"],
        )

    build = await build_with_test_model()
    check("mutual peers are both wired",
          "delegate_pong" in tool_names(build.specialists["ping"])
          and "delegate_ping" in tool_names(build.specialists["pong"]))

    stack_var = registry_module._delegation_stack
    ping_tool = build.specialists["ping"]._function_toolset.tools["delegate_pong"]

    async def call_tool(tool, query: str) -> str:
        """Invoke a registered delegation tool the way the agent runtime does."""
        return await tool.function(None, query)

    token = stack_var.set(("pong",))
    try:
        out = await call_tool(ping_tool, "hello")
    finally:
        stack_var.reset(token)
    check("re-entry into an agent already on the stack is refused",
          "already" in out.lower() and "pong" in out, out[:160])

    token = stack_var.set(tuple(f"a{i}" for i in range(registry_module._MAX_DELEGATION_DEPTH)))
    try:
        out = await call_tool(ping_tool, "hello")
    finally:
        stack_var.reset(token)
    check("depth cap is refused", "depth" in out.lower() or "too many" in out.lower(),
          out[:160])

    check("stack is reset after a call", stack_var.get() == ())

    print("\n== nested usage is forwarded to the parent run ==")

    # Token usage from a peer must aggregate into the run that triggered it, or
    # a chain's cost is invisible. _delegate already forwards usage=ctx.usage;
    # this pins that it keeps doing so now that peers can trigger it.
    class FakeUsage:
        pass

    class FakeCtx:
        def __init__(self, usage):
            self.usage = usage

    class FakeResult:
        output = "done"

    sentinel = FakeUsage()
    recorded: dict = {}
    pong_agent = build.specialists["pong"]
    real_run = pong_agent.run

    async def recording_run(query, **kwargs):
        recorded.update(kwargs)
        return FakeResult()

    pong_agent.run = recording_run
    try:
        out = await ping_tool.function(FakeCtx(sentinel), "hi")
    finally:
        pong_agent.run = real_run
    check("nested run receives the parent's usage object",
          recorded.get("usage") is sentinel, str(list(recorded)))
    check("delegation returns the specialist's output", out == "done", out[:80])

    print("\n== guards return text, never raise ==")
    token = stack_var.set(("pong",))
    try:
        raised = False
        try:
            await call_tool(ping_tool, "hello")
        except Exception:
            raised = True
    finally:
        stack_var.reset(token)
    check("re-entry does not raise", not raised)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_peer_delegation.py`
Expected: FAIL — `AttributeError: module 'agents.registry' has no attribute
'_delegation_stack'`.

- [ ] **Step 3: Add the guards**

In `agents/registry.py`, add near the other module constants at the top, after
`_MAX_AUTH_ROUNDS`:

```python
# Peer delegation lets an agent consult another agent, so a chain can run
# specialist to specialist. Mutual peers (A lists B, B lists A) are a
# legitimate configuration — two specialists that can each ask the other a
# question — so cycles are not rejected at save time. They are bounded here
# instead.
_MAX_DELEGATION_DEPTH = int(os.environ.get("AGENT_DELEGATION_MAX_DEPTH", "3"))
_delegation_stack: ContextVar[tuple[str, ...]] = ContextVar(
    "delegation_stack", default=()
)


def _depth_message(agent_name: str) -> str:
    return (
        f"Cannot consult **{agent_name}**: the delegation chain already reached "
        f"its depth limit of {_MAX_DELEGATION_DEPTH}. Answer with the "
        "information you already have, and say what is still missing."
    )


def _reentry_message(agent_name: str) -> str:
    return (
        f"Cannot consult **{agent_name}**: it is already working on this "
        "request further up the chain, so consulting it again would loop. "
        "Answer with the information you already have."
    )
```

Add the import at the top of the file, next to the other standard-library
imports:

```python
from contextvars import ContextVar
```

In `_attach_delegation_tool`, at the very start of the `_delegate` body — before
the existing `logger.info("[delegate] %s START | ...")` line — add:

```python
        stack = _delegation_stack.get()
        if row.name in stack:
            logger.info("[delegate] %s refused: already on the stack %s", row.name, stack)
            return _reentry_message(row.name)
        if len(stack) >= _MAX_DELEGATION_DEPTH:
            logger.info(
                "[delegate] %s refused: depth %d reached", row.name, len(stack)
            )
            return _depth_message(row.name)
        stack_token = _delegation_stack.set(stack + (row.name,))
```

and reset it in the existing `finally:` block, before
`report_delegation_end(row.name)`:

```python
            _delegation_stack.reset(stack_token)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_peer_delegation.py`
Expected: PASS, `0 failed`.

- [ ] **Step 5: Verify chat delegation still works**

Run:
```bash
./.venv/Scripts/python.exe tests/test_chat_progress_ui.py
./.venv/Scripts/python.exe tests/test_signin_dedup.py
./.venv/Scripts/python.exe tests/test_tool_resilience.py
```
Expected: all PASS. These drive the orchestrator's delegation tool, which now
pushes and pops the stack on every call.

- [ ] **Step 6: Commit**

```bash
git add agents/registry.py tests/test_peer_delegation.py
git commit -m "feat: bound peer delegation with a depth cap and re-entry guard"
```

---

### Task 5: Admin UI for the model override and peers

**Files:**
- Modify: `templates/admin.html` — agent form fields, render/collect helpers, save payload, load path
- Test: `tests/test_admin_ui.py` (extend)

**Interfaces:**
- Consumes: `GET /admin/api/model` (returns `{"model_name", "available", "default"}` — already exists), `GET /admin/api/agents` (now returns `peers` and `model_name`), `POST`/`PUT /admin/api/agents` (now accept them).
- Produces: form controls `#agent-model-name` and `#agent-peers`, and JS helpers `renderAgentPeerChecks(selected, currentName)` and `collectAgentPeers()`.

- [ ] **Step 1: Write the failing test**

In `tests/test_admin_ui.py` (around line 237), add `"agent-model-name"` to the
form-field id tuple so it reads:

```python
        for fid in ("agent-id", "agent-name", "agent-description",
                    "agent-instructions", "agent-enabled", "agent-run-prompt",
                    "agent-model-name",
                    "skill-id", "skill-name", "skill-description", "skill-content"):
```

and add `"agent-peers"` to the container-div tuple just below it:

```python
        for did in ("agent-mcp-servers", "agent-skills", "agent-peers"):
```

Then add this immediately after that container loop:

```python
        check(
            "model override is a select, not a free-text input",
            any(e[0] == "select" and e[1].get("id") == "agent-model-name"
                for e in coll.elements),
        )
```

And add these to the JavaScript section, alongside the other assertions made
against the extracted inline script (the variable holding it is `js`):

```python
        check("peers are collected on save", "collectAgentPeers()" in js)
        check("peer checkboxes are rendered", "renderAgentPeerChecks" in js)
        check(
            "an agent is not offered itself as a peer",
            "a.name !== currentName" in js,
        )
        check("model options are rendered", "renderAgentModelOptions" in js)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_admin_ui.py`
Expected: FAIL — `form field #agent-peers` and `form field #agent-model-name`
not found.

- [ ] **Step 3: Add the form controls**

In `templates/admin.html`, immediately after the existing Skills `<label
class="full">` block (the one containing `<div id="agent-skills" ...>`), add:

```html
            <label class="full">
                Can consult <span style="color:var(--muted);font-weight:normal">(optional — other agents this one may ask directly, without going through the orchestrator)</span>
                <div id="agent-peers" style="border:1px solid var(--border);border-radius:6px;padding:8px;max-height:160px;overflow-y:auto"></div>
            </label>
            <label class="full">
                Model <span style="color:var(--muted);font-weight:normal">(optional — leave on "Use the active model" unless this agent needs its own, e.g. a cheaper one for simple triage)</span>
                <select id="agent-model-name">
                    <option value="">Use the active model</option>
                </select>
            </label>
```

- [ ] **Step 4: Add the JS helpers**

In `templates/admin.html`, add these functions immediately after the existing
`collectAgentSkills()` function:

```javascript
function renderAgentPeerChecks(selected, currentName) {
    const container = document.getElementById('agent-peers');
    // An agent consulting itself would be an immediate loop, so it is never
    // offered. The backend skips it too; this just avoids showing a dead option.
    const others = allAgents.filter(a => a.name !== currentName);
    if (others.length === 0) {
        container.innerHTML = '<span style="color:var(--muted)">No other agents defined yet.</span>';
        return;
    }
    container.innerHTML = others.map(a => `
        <label style="margin:2px 0;font-weight:normal;display:flex;gap:6px;align-items:baseline">
            <input type="checkbox" class="agent-peer-check" value="${escapeHtml(a.name)}"
                   ${selected.includes(a.name) ? 'checked' : ''}>
            <span><strong>${escapeHtml(a.name)}</strong>
                <span style="color:var(--muted)"> — ${escapeHtml(a.description)}</span></span>
        </label>
    `).join('');
}

function collectAgentPeers() {
    return Array.from(document.querySelectorAll('#agent-peers .agent-peer-check'))
        .filter(cb => cb.checked)
        .map(cb => cb.value);
}

async function renderAgentModelOptions(selected) {
    const sel = document.getElementById('agent-model-name');
    const res = await api('/model');
    let available = [];
    if (res.ok) {
        const data = await res.json();
        available = data.available || [];
    }
    sel.innerHTML = '<option value="">Use the active model</option>' +
        available.map(m =>
            `<option value="${escapeHtml(m)}" ${m === selected ? 'selected' : ''}>${escapeHtml(m)}</option>`
        ).join('');
    sel.value = selected || '';
}
```

`allAgents` (declared at `templates/admin.html:405`, refreshed at line 411) is
the module-level array the agents table already renders from. Reuse it — do not
introduce a second source of truth.

- [ ] **Step 5: Wire the controls into open, load and save**

In `openAgentModal()` (`templates/admin.html:698`), after the existing
`renderAgentSkillChecks([]);` call at line 705, add:

```javascript
    renderAgentPeerChecks([], '');
    await renderAgentModelOptions('');
```

and change the declaration to `async function openAgentModal() {`, since
`renderAgentModelOptions` is awaited. Its `onclick` callers need no change —
the browser ignores the returned promise.

In `editAgent(id)` (`templates/admin.html:808`, already `async`), after the
existing `renderAgentSkillChecks(a.skills || []);` call at line 821, add:

```javascript
    renderAgentPeerChecks(a.peers || [], a.name);
    await renderAgentModelOptions(a.model_name || '');
```

In `saveAgent()` (`templates/admin.html:834`), add to the JSON body next to
`skills: collectAgentSkills(),`:

```javascript
        peers: collectAgentPeers(),
        model_name: document.getElementById('agent-model-name').value,
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_admin_ui.py`
Expected: PASS, `0 failed`. This suite also runs `node --check` on the inline
script, so a JS syntax error fails here rather than in a browser.

- [ ] **Step 7: Run the whole Python suite**

Run:
```bash
for t in tests/test_*.py; do echo "== $t"; ./.venv/Scripts/python.exe "$t" || echo "FAILED: $t"; done
```
Expected: every suite reports `0 failed`.

- [ ] **Step 8: Update the project docs**

In `CLAUDE.md`, under **Key files**, extend the `agents/registry.py` bullet with
a sentence describing the second pass:

```
  Agents may list `peers` (other agents' names); a second build pass attaches a
  delegation tool per peer to that agent, so a chain can run specialist to
  specialist instead of through the orchestrator. Recursion is bounded by
  `AGENT_DELEGATION_MAX_DEPTH` and a re-entry guard. `AgentConfig.model_name`
  overrides the globally active model per agent.
```

- [ ] **Step 9: Commit**

```bash
git add templates/admin.html tests/test_admin_ui.py CLAUDE.md
git commit -m "feat: admin UI for the model override and agent peers"
```

---

## Verification

After Task 5, the feature is complete when all of the following hold:

1. Every suite in `tests/` reports `0 failed`.
2. Creating two agents in `/admin`, listing one as the other's peer, saving, and
   pressing **Reload** produces a `delegate_<peer>` tool on the consulting
   agent — visible in the app log line `[delegate] <peer> START` when a chat
   request exercises the chain.
3. Setting an agent's Model to a different deployment and reloading logs no
   warning; setting it to a model that is not deployed logs the fallback
   warning and the agent still answers.
4. An existing deployment upgrades cleanly: `_ensure_column` adds `model_name`
   and `peers_json` to the live `agent_configs` table, and agents with neither
   set behave exactly as before.
