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
