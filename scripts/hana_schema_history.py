"""Pin the digest of the current SAP HANA artifact set in
``agents/hana_schema_history.json``.

On HANA the models are the schema: ``agents/hana_hdi.py`` generates the HDI
design-time artifacts from them at every start. An HDI container remembers
the *schema generation* it was last deployed with, and an app version of a
lower generation leaves it alone (otherwise a rollback would drop the tables
and columns the newer version added). That only works when every change of
the artifact set gets a new generation, so the history pins one SHA-256 per
generation and ``tests/test_hana_hdi.py`` fails when the models no longer
produce the pinned digest.

After changing a model (or the generator):

1. raise ``HANA_SCHEMA_GENERATION`` in ``agents/hana_hdi.py`` by one;
2. run this script; it adds the new generation's digest;
3. commit both files together.

The script only ever adds a generation. It refuses to change a digest that
is already pinned: some container may hold that generation.

Run:  .venv/bin/python scripts/hana_schema_history.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Only the models are needed; no database is opened.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import agents.ide.models  # noqa: E402,F401  (registers the IDE tables)
from agents.db import Base  # noqa: E402
from agents.hana_hdi import (  # noqa: E402
    HANA_SCHEMA_GENERATION,
    HISTORY_FILE,
    artifacts,
    schema_digest,
)


def main() -> int:
    history: dict[str, str] = (
        json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else {}
    )
    digest = schema_digest(artifacts(Base.metadata))
    key = str(HANA_SCHEMA_GENERATION)
    pinned = history.get(key)
    if pinned == digest:
        print(f"generation {key} is pinned already; nothing to do")
        return 0
    if pinned is not None:
        print(
            f"The models no longer produce the artifacts pinned for generation {key}.\n"
            "Raise HANA_SCHEMA_GENERATION in agents/hana_hdi.py by one and run this "
            "script again; a pinned digest is never changed.",
            file=sys.stderr,
        )
        return 1
    highest = max((int(g) for g in history), default=0)
    if HANA_SCHEMA_GENERATION != highest + 1:
        print(
            f"HANA_SCHEMA_GENERATION is {key}, the history ends at {highest}: "
            "generations go up by one.",
            file=sys.stderr,
        )
        return 1
    history[key] = digest
    ordered = {g: history[g] for g in sorted(history, key=int)}
    HISTORY_FILE.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")
    print(f"pinned generation {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
