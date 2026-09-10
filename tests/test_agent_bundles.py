"""Validation of the shipped agent config bundles in docs/*.config.json.

Those files are pasted into /admin -> Import, where a typo surfaces as a 422
against a live landscape. Validating them against the very models the import
endpoint uses catches it here instead. The secret check is the other half: a
bundle is a committed file, so it must never carry a real client_secret.

Run:  python tests/test_agent_bundles.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests/_test_bundles.db")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
# A developer .env may set this. Set it EMPTY rather than popping it: app.py
# calls load_dotenv(), which fills in vars that are absent but never overrides
# ones already present. Empty means "no allowlist", i.e. the default rule.
os.environ["MCP_URL_ALLOWLIST"] = ""

from agents.admin import ImportPayload  # noqa: E402

FAILED = 0
PASSED = 0

# Secret-shaped oauth keys that must be blank in a committed bundle.
SECRET_KEYS = ("client_secret",)


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if ok:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")


def check_bundle(path: Path, shared_skills: set[str]) -> None:
    """Validate one bundle.

    ``shared_skills`` are the skills defined by any bundle in docs/:
    outlook-sap-assistant deliberately ships none of its own and reuses
    sap-mail-triage from the Gmail bundle, so a reference resolves as long as
    some bundle defines it. A typo still fails.
    """
    print(f"\n{path.name}")
    raw = json.loads(path.read_text(encoding="utf-8"))

    try:
        payload = ImportPayload.model_validate(raw)
        check("validates against ImportPayload", True)
    except Exception as e:  # noqa: BLE001 - the message is the test output
        check("validates against ImportPayload", False, str(e))
        return

    skill_names = {s.name for s in payload.skills} | shared_skills
    slugs: list[str] = []
    for agent in payload.agents:
        missing = [s for s in agent.skills if s not in skill_names]
        check(
            f"{agent.name}: skills resolve across docs/ bundles",
            not missing,
            f"unknown: {missing}",
        )
        check(f"{agent.name}: has a run_prompt", bool(agent.run_prompt.strip()))
        if agent.api_slug:
            slugs.append(agent.api_slug)
        for server in agent.mcp_servers:
            cfg = server.oauth.to_config() if server.oauth else {}
            leaked = [k for k in SECRET_KEYS if str(cfg.get(k) or "").strip()]
            check(
                f"{agent.name}: no secret committed for {server.url}",
                not leaked,
                f"non-empty: {leaked}",
            )
    check("api slugs unique within the bundle", len(slugs) == len(set(slugs)))


def main() -> None:
    bundles = sorted((ROOT / "docs").glob("*.config.json"))
    check("bundles found in docs/", bool(bundles))
    shared_skills: set[str] = set()
    for path in bundles:
        raw = json.loads(path.read_text(encoding="utf-8"))
        shared_skills |= {str(s.get("name", "")) for s in raw.get("skills") or []}
    for path in bundles:
        check_bundle(path, shared_skills)
    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()


def test_sap_security_notes_bundle_is_importable():
    """The shipped bundle must satisfy the same validation as an API import."""
    import json
    from pathlib import Path

    import pytest

    from agents.admin import ImportPayload

    path = Path("docs/sap-security-notes-workflow.config.json")
    if not path.exists():
        pytest.skip("landscape bundle is gitignored; present only locally")

    raw = json.loads(path.read_text(encoding="utf-8"))
    payload = ImportPayload.model_validate(raw)
    names = {a.name for a in payload.agents}
    assert names == {"sapnotes-fetcher", "sapnotes-system-analyst", "sapnotes-digest-writer"}
    workflow = payload.workflows[0]
    assert workflow.api_slug == "sap-security-notes"
    # No fan-out: a digest step after one would fire per item, not per run.
    assert all(not s.fan_out for s in workflow.steps)
