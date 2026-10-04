"""The ABAP Assistant's entries in ``xs-security.json``.

The product is called "ABAP Assistant" in what an administrator reads (scope,
role template and role collection descriptions), while the NAMES stay as they
were: ``AbapIdeDeveloper`` and ``ABAP IDE Developer`` carry role assignments
on every landscape, and renaming them would drop those assignments.

Run:  python -m pytest tests/test_ide_xs_security.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
XS = json.loads((ROOT / "xs-security.json").read_text(encoding="utf-8"))


def _by_name(section: str) -> dict[str, dict]:
    return {entry["name"]: entry for entry in XS[section]}


def test_developer_names_are_unchanged():
    scopes = _by_name("scopes")
    templates = _by_name("role-templates")
    collections = _by_name("role-collections")
    assert "$XSAPPNAME.developer" in scopes
    assert templates["AbapIdeDeveloper"]["scope-references"] == [
        "$XSAPPNAME.user", "$XSAPPNAME.developer"]
    assert collections["ABAP IDE Developer"]["role-template-references"] == [
        "$XSAPPNAME.AbapIdeDeveloper"]


def test_developer_descriptions_say_abap_assistant():
    descriptions = [
        _by_name("scopes")["$XSAPPNAME.developer"]["description"],
        _by_name("role-templates")["AbapIdeDeveloper"]["description"],
        _by_name("role-collections")["ABAP IDE Developer"]["description"],
    ]
    for text in descriptions:
        assert "ABAP Assistant" in text, text
        assert "ABAP IDE" not in text, text


def test_mta_comments_do_not_say_abap_ide():
    mta = (ROOT / "mta.yaml").read_text(encoding="utf-8")
    assert "ABAP IDE" not in mta
