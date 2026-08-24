"""Tool-prefix tests: prefixed MCP tool names must fit the 64-char limit.

When an agent binds multiple MCP servers, the registry prefixes each server's
tools. The prefix must be short or the resulting `{prefix}_{tool}` name exceeds
OpenAI/Azure's 64-char function-name cap and the LLM call is rejected.

Run:  python tests/test_tool_prefixes.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests/_test_prefixes.db")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
# A developer .env may set this. Set it EMPTY rather than popping it: app.py
# calls load_dotenv(), which fills in vars that are absent but never overrides
# ones already present. Empty means "no allowlist", i.e. the default rule.
os.environ["MCP_URL_ALLOWLIST"] = ""

from agents.registry import _MAX_PREFIX_LEN, _compute_tool_prefixes  # noqa: E402

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


# The real-world case that broke: a long BTP hostname + a public docs server.
ARC1 = "https://infrabel-app-acc-cf-ai-arc1-mcp-server.cfapps.eu20-001.hana.ondemand.com/mcp"
SAPDOCS = "http://mcp-sap-docs.marianzeis.de/mcp"
# Longest tool names actually exposed by these servers.
LONG_TOOLS = ["SAPTransport", "SAPContext", "sap_community_search"]


def main() -> None:
    prefixes = _compute_tool_prefixes([ARC1, SAPDOCS])
    print("prefixes:", prefixes)
    check("one prefix per server", len(prefixes) == 2)
    check("prefixes are unique", len(set(prefixes)) == 2)
    for p in prefixes:
        check(f"prefix '{p}' within max len", len(p) <= _MAX_PREFIX_LEN + 2)
        for tool in LONG_TOOLS:
            name = f"{p}_{tool}"
            check(f"'{name}' <= 64 chars ({len(name)})", len(name) <= 64)

    # Collision: two servers whose first DNS label is identical get suffixed.
    collide = _compute_tool_prefixes(
        ["https://same-host.example.com/a/mcp", "https://same-host.example.org/b/mcp"]
    )
    check("colliding first-labels disambiguated", collide[0] != collide[1])

    # builtin: URLs have no hostname. Falling back to the path keeps them
    # distinguishable; without it every hostless entry slugs to "mcp" and two
    # built-ins on one agent would collide into mcp_0 / mcp_1.
    builtin = _compute_tool_prefixes([ARC1, "builtin:gmail"])
    check("builtin prefixes from its path", builtin[1] == "gmail", f"got {builtin[1]!r}")
    check("builtin does not collide with a real host", builtin[0] != builtin[1])

    # Sanity: the OLD full-hostname slug would have failed.
    full = "infrabel_app_acc_cf_ai_arc1_mcp_server_cfapps_eu20_001_hana_ondemand_com"
    check("old full-hostname prefix would exceed 64", len(f"{full}_SAPContext") > 64)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
