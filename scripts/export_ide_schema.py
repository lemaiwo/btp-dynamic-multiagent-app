"""Write the JSON Schema of the IDE API for the UI's contract tests.

``agents/ide/schemas.py`` holds the response and request models of
``/ide/api``. The UI's fake backend (``ui5-ide/webapp/test``) is checked
against the same models through this file, so the two cannot drift apart
silently: ``tests/test_ide_schemas.py::test_schema_file_is_current`` fails
until this script is rerun after a model change, and the regenerated file
goes into the same commit.

Run:  .venv/bin/python scripts/export_ide_schema.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ide.schemas import render_schema  # noqa: E402

TARGET = ROOT / "ui5-ide" / "webapp" / "test" / "contract" / "ide-api.schema.json"


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(render_schema(), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
