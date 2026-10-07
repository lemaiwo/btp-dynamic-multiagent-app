"""The spreadsheet reader of ``builtin:sharepoint`` is pinned and parses no DTD."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_openpyxl_and_defusedxml_are_pinned():
    pins = (ROOT / "requirements.txt").read_text().splitlines()
    assert "openpyxl==3.1.5" in pins
    assert "defusedxml==0.7.1" in pins


def test_openpyxl_uses_the_defused_parser():
    import openpyxl

    assert openpyxl.__version__ == "3.1.5"
    assert openpyxl.DEFUSEDXML is True
