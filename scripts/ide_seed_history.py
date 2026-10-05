"""Add the hashes of the shipped IDE seed texts to ``seed_history.json``.

``agents.ide.seed.refresh_ide_seed`` updates a stored agent prompt or skill
only when its text hashes to a version this repository shipped; any other
text is an admin's edit and is kept. So every shipped text must be in the
history *before* it is replaced: after editing ``agents/ide/seed.ide.json``
run this script and commit both files together
(``tests/test_ide_seed_refresh.py::test_history_holds_the_shipped_texts``
fails until then).

The script only ever adds hashes; a hash once listed stays, because some
landscape may still hold that text. Extra seed files (an older version, e.g.
``git show <commit>:agents/ide/seed.ide.json > /tmp/old.json``) can be
passed as arguments to add their texts too.

Run:  .venv/bin/python scripts/ide_seed_history.py [older-seed.json ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ide.seed import HISTORY_FILE_NAME, IDE_SEED_FILE, text_hash  # noqa: E402

HISTORY = IDE_SEED_FILE.with_name(HISTORY_FILE_NAME)


def add_seed(history: dict, seed: dict) -> int:
    """Add the hashes of one seed file's texts; returns how many were new."""
    added = 0
    for kind, field in (("agents", "instructions"), ("skills", "content")):
        table = history.setdefault(kind, {})
        for entry in seed.get(kind, []):
            hashes = table.setdefault(entry["name"], [])
            digest = text_hash(entry[field])
            if digest not in hashes:
                hashes.append(digest)
                added += 1
    return added


def main(extra: list[str]) -> None:
    history = (
        json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists()
        else {"agents": {}, "skills": {}}
    )
    added = 0
    for name in [*extra, str(IDE_SEED_FILE)]:
        added += add_seed(history, json.loads(Path(name).read_text(encoding="utf-8")))
    ordered = {kind: dict(sorted(history.get(kind, {}).items()))
               for kind in ("agents", "skills")}
    HISTORY.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")
    print(f"{HISTORY.relative_to(ROOT)}: {added} hash(es) added")


if __name__ == "__main__":
    main(sys.argv[1:])
