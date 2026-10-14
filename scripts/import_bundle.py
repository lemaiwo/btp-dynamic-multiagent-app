"""Import an agent config bundle (docs/*.config.json) into the registry DB.

The committed bundles carry no ``client_secret`` -- see tests/test_agent_bundles.py
-- so this fills the blanks from a credentials file before importing, and never
prints a secret. Two modes:

    python scripts/import_bundle.py docs/gmail-sap-arc1-assistant.config.json
        Import into the DB named by DATABASE_URL (default: ./agents_registry.db).

    python scripts/import_bundle.py <bundle> --out filled.json
        Write the same bundle with secrets filled in and import nothing. Use it
        to feed /admin -> Import on a landscape this machine cannot reach
        directly, then delete the file.

Agents are written one by one, each in its own commit, through the storage
gate (``upsert_agent``). A refused agent is rolled back, reported as
``FAIL   agent <name>: <rule>`` (the rule names the field, never the value) and
skipped; the others are still imported, and the script exits 1. That differs
from ``POST /admin/api/import``, which writes nothing at all when any entry is
refused (one transaction, all-or-nothing). ``expose_invoke`` must be a JSON
boolean here: the HTTP import's lax boolean reads the string "true" as true,
so the script refuses any other value rather than silently storing "off".

Secrets come from --secrets (a Google client_secret_*.json, or a flat
{"<url>": {"client_secret": "..."}} map). Without it, the repo root is searched
for a client_secret_*.json, which covers builtin:gmail.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _google_secret(path: Path) -> str:
    """The client_secret out of a Google OAuth client download."""
    data = json.loads(path.read_text(encoding="utf-8"))
    section = data.get("web") or data.get("installed") or data
    return str(section.get("client_secret") or "")


def _secret_for(url: str, secrets_file: Path | None) -> str:
    if secrets_file is None:
        return ""
    data = json.loads(secrets_file.read_text(encoding="utf-8"))
    entry = data.get(url)
    if isinstance(entry, dict) and entry.get("client_secret"):
        return str(entry["client_secret"])
    # Not a per-URL map: treat it as a Google client download.
    return _google_secret(secrets_file)


def fill_secrets(bundle: dict, secrets_file: Path | None) -> list[str]:
    """Fill blank client_secrets in place. Returns the URLs still missing one."""
    missing: list[str] = []
    for agent in bundle.get("agents") or []:
        for server in agent.get("mcp_servers") or []:
            oauth = server.get("oauth")
            if not isinstance(oauth, dict) or oauth.get("dcr"):
                continue
            if "client_id" not in oauth or oauth.get("client_secret"):
                continue
            secret = _secret_for(str(server.get("url", "")), secrets_file)
            if secret:
                oauth["client_secret"] = secret
            else:
                missing.append(f"{agent.get('name')} -> {server.get('url')}")
    return missing


EXPOSE_INVOKE_NOT_BOOLEAN = "expose_invoke: must be a JSON boolean (true or false)"


async def import_bundle(bundle: dict) -> list[str]:
    """Import the bundle; answers the names of the agents that were refused.

    Every agent is written (and committed) on its own through
    ``upsert_agent``, the storage gate: a refused agent (a ``ValueError``, for
    example an invoke ``result_schema`` outside the supported subset) is
    rolled back, reported with the gate's text (which names the field and the
    rule, never the value) and skipped; the others are still imported.
    """
    from agents.db import (  # noqa: PLC0415 - after sys.path setup
        SessionLocal,
        init_db,
        upsert_agent,
        upsert_skill,
    )

    await init_db()
    failed: list[str] = []
    async with SessionLocal() as session:
        for skill in bundle.get("skills") or []:
            await upsert_skill(
                session,
                name=skill["name"],
                description=skill["description"],
                content=skill["content"],
            )
            print(f"  skill  {skill['name']}")
        for agent in bundle.get("agents") or []:
            if "expose_invoke" in agent and not isinstance(agent["expose_invoke"], bool):
                failed.append(agent["name"])
                print(f"  FAIL   agent {agent['name']}: {EXPOSE_INVOKE_NOT_BOOLEAN}")
                continue
            try:
                await upsert_agent(
                    session,
                    name=agent["name"],
                    description=agent["description"],
                    instructions=agent["instructions"],
                    mcp_servers=agent["mcp_servers"],
                    skills=agent.get("skills") or [],
                    enabled=agent.get("enabled", True),
                    expose_chat=agent.get("expose_chat", True),
                    expose_api=agent.get("expose_api", False),
                    api_slug=agent.get("api_slug") or None,
                    run_prompt=agent.get("run_prompt") or None,
                    run_timeout_seconds=agent.get("run_timeout_seconds", 1800),
                    # Invoke API settings: a bundle without the key keeps what
                    # is stored (an older export carries none of them).
                    # `upsert_agent` checks schema and limits and stores the
                    # switch only for the JSON boolean true.
                    **{key: agent[key] for key in (
                        "expose_invoke", "result_schema", "invoke_limits",
                    ) if key in agent},
                )
            except ValueError as e:
                await session.rollback()
                failed.append(agent["name"])
                print(f"  FAIL   agent {agent['name']}: {e}")
                continue
            print(f"  agent  {agent['name']}")
        await session.commit()
    return failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--secrets", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None, help="write filled bundle, do not import")
    args = ap.parse_args()

    secrets = args.secrets
    if secrets is None:
        found = sorted(ROOT.glob("client_secret_*.json"))
        secrets = found[0] if found else None

    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    missing = fill_secrets(bundle, secrets)
    for entry in missing:
        print(f"  WARN   no client_secret for {entry}", file=sys.stderr)

    if args.out:
        args.out.write_text(
            json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"  wrote  {args.out}  (contains secrets -- delete after use)")
        return 0

    from sqlalchemy.engine import make_url  # noqa: PLC0415

    from agents.db import DATABASE_URL  # noqa: PLC0415 - after sys.path setup

    # The URL holds the database password; this output ends up in terminals
    # and CI logs.
    print(f"  db     {make_url(DATABASE_URL).render_as_string(hide_password=True)}")
    failed = asyncio.run(import_bundle(bundle))
    print("  done   restart or reload the app to pick this up")
    return 1 if missing or failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
