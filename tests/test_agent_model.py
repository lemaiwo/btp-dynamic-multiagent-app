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
