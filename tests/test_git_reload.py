"""An agent save that changes its ``builtin:git`` entry reaches the running
build at once: ``_entry_signature`` (agents/admin.py) changes with the entry,
not with its wording (addendum h, like the Bitbucket entry)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from agents.admin import _entry_signature  # noqa: E402

BLOCK = {"destination": "github-x", "provider": "github",
         "repositories": ["acme/orders"], "allow_push": True}


def entry(block, auth_mode="destination"):
    return [{"url": "builtin:git", "auth_mode": auth_mode, "oauth": dict(block)}]


def test_a_git_entry_is_part_of_the_signature():
    assert _entry_signature(entry(BLOCK)) != []
    assert _entry_signature([]) == []


def test_the_switches_and_pins_change_it():
    base = _entry_signature(entry(BLOCK))
    assert _entry_signature(entry({**BLOCK, "allow_push": False})) != base
    assert _entry_signature(entry({**BLOCK, "allow_pull_request": True})) != base
    assert _entry_signature(entry({**BLOCK, "repositories": ["acme/orders", "acme/x"]})) != base
    assert _entry_signature(entry({**BLOCK, "base_branch": "develop"})) != base


def test_spelling_out_the_defaults_is_no_change():
    base = _entry_signature(entry(BLOCK))
    spelled = {**BLOCK, "base_branch": "main", "branch_prefix": "agent/",
               "allow_pull_request": False}
    assert _entry_signature(entry(spelled)) == base


def test_a_refused_block_is_compared_as_it_stands():
    bad = _entry_signature(entry({"provider": "svn"}))
    assert bad != [] and bad != _entry_signature(entry(BLOCK))
    assert _entry_signature(entry(BLOCK, auth_mode="jwt")) != _entry_signature(entry(BLOCK))


def test_a_disabled_agent_has_no_signature():
    assert _entry_signature(entry(BLOCK), enabled=False) == []
