"""mta.yaml: the IDE env vars are strings.

CF passes every env var as a string anyway, but an unquoted ``90`` is an int
in the MTA descriptor, and an ``.mtaext`` override or a tool that compares
values then sees a different type than the app reads. Quoting keeps the
descriptor and the runtime the same.

Run:  python -m pytest tests/test_ide_mta.py -q
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _app_properties() -> dict:
    mta = yaml.safe_load((ROOT / "mta.yaml").read_text())
    merged: dict = {}
    for module in mta.get("modules", []):
        merged.update(module.get("properties") or {})
    return merged


def test_ide_env_vars_are_quoted_strings():
    props = {k: v for k, v in _app_properties().items() if k.startswith("IDE_")}
    assert props, "no IDE_* properties in mta.yaml"
    for key, value in props.items():
        assert isinstance(value, str), (key, value)


def test_mta_version_is_2_19_0():
    mta = yaml.safe_load((ROOT / "mta.yaml").read_text())
    assert mta["version"] == "2.19.0"


def test_every_ide_property_is_read_by_the_app():
    """A descriptor property nobody reads is a setting that silently does nothing."""
    names = [k for k in _app_properties() if k.startswith("IDE_")]
    sources = [ROOT / "app.py", *(ROOT / "agents").rglob("*.py")]
    code = "\n".join(p.read_text() for p in sources)
    unread = [n for n in names if n not in code]
    assert not unread, unread


def test_python_module_archive_excludes_local_only_folders():
    """docs/, memory/, .sdd/ and tests/ hold landscape- and customer-specific
    material (docs/ and memory/ are gitignored); a local ``mbt build`` must not
    pack them into the app droplet."""
    mta = yaml.safe_load((ROOT / "mta.yaml").read_text())
    module = next(m for m in mta["modules"] if m["type"] == "python")
    ignore = module["build-parameters"]["ignore"]
    # Makefile_*.mta: mbt's temporary makefile, written into the module path
    # (".") during a build; it holds a local absolute path.
    for entry in ("docs/", "memory/", ".sdd/", "tests/", "CLAUDE.local.md",
                  "Makefile_*.mta"):
        assert entry in ignore, entry
