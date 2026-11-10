"""An agent save that changes its ``builtin:hana`` entry reaches the running
build at once: ``_entry_signature`` (agents/admin.py) changes with the entry
(Allow SELECT, a system added or removed, a destination), not with its
wording or the order of its systems; an agent that is not enabled has none."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from agents.admin import _entry_signature  # noqa: E402

PRD = {"name": "prd", "destination": "HANA_PRD"}
SYS = {"name": "sysdb", "destination": "HANA_SYS"}
BLOCK = {"systems": [PRD, SYS], "allow_select": True}


def entry(block, auth_mode="destination", url="builtin:hana"):
    return [{"url": url, "auth_mode": auth_mode, "oauth": dict(block)}]


def test_a_hana_entry_is_part_of_the_signature():
    assert _entry_signature(entry(BLOCK)) != []
    assert _entry_signature([]) == []


def test_allow_select_and_the_systems_change_it():
    base = _entry_signature(entry(BLOCK))
    assert _entry_signature(entry({**BLOCK, "allow_select": False})) != base
    without = {k: v for k, v in BLOCK.items() if k != "allow_select"}
    assert _entry_signature(entry(without)) != base
    assert _entry_signature(entry({**BLOCK, "systems": [PRD]})) != base
    third = {"name": "qas", "destination": "HANA_QAS"}
    assert _entry_signature(entry({**BLOCK, "systems": [PRD, SYS, third]})) != base
    moved = {**BLOCK, "systems": [PRD, {"name": "sysdb", "destination": "HANA_OTHER"}]}
    assert _entry_signature(entry(moved)) != base


def test_spelling_out_the_default_or_reordering_is_no_change():
    without = {"systems": [PRD, SYS]}
    assert _entry_signature(entry({**without, "allow_select": False})) == _entry_signature(
        entry(without))
    assert _entry_signature(entry({**BLOCK, "systems": [SYS, PRD]})) == _entry_signature(
        entry(BLOCK))
    assert _entry_signature(entry(BLOCK, url="Builtin:HANA/")) == _entry_signature(entry(BLOCK))


def test_a_refused_block_is_compared_as_it_stands():
    bad = _entry_signature(entry({"systems": []}))
    assert bad != [] and bad != _entry_signature(entry(BLOCK))
    assert _entry_signature(entry(BLOCK, auth_mode="jwt")) != _entry_signature(entry(BLOCK))


def test_a_disabled_agent_has_no_signature():
    assert _entry_signature(entry(BLOCK), enabled=False) == []


def test_it_is_apart_from_the_catalogue_part():
    from agents.admin import _catalogue_part

    assert _catalogue_part(_entry_signature(entry(BLOCK))) == []


# --- the bundle import ---------------------------------------------------------

def test_the_import_reload_names_every_entry_kind():
    import inspect

    import agents.admin as admin

    assert "HANA" in inspect.getdoc(admin._entry_signatures)
    source = inspect.getsource(admin)
    assert "changed OData or Bitbucket entry of an agent" not in source
