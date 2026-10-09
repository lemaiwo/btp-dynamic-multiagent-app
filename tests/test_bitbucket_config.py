"""The one reading of a ``builtin:bitbucket`` entry: what is accepted, what is
stored, and that a refusal never repeats what the admin typed."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from agents.bitbucket_config import (  # noqa: E402
    DEFAULT_BRANCH,
    ENTRY_KEYS,
    BitbucketConfigError,
    Pins,
    check_entry,
    clean_entry,
    confine_path,
    pins_of,
    repository_allowed,
)

# Never part of a refusal: upper case and punctuation keep it from being a
# legal slug, branch or destination name.
SECRET = "S3cr3t Zx9!tok"
BASE = {"destination": "BITBUCKET", "workspace": "acme-ws"}


def test_the_minimal_entry_is_read_only_on_main_with_green_builds_required():
    check_entry(BASE)
    assert clean_entry(BASE) == BASE
    assert pins_of(BASE) == Pins(
        workspace="acme-ws", repositories=None, branch=DEFAULT_BRANCH,
        allow_comment=False, allow_approve=False, require_green_builds=True)


def test_the_full_entry_is_stored_key_by_key_in_a_fixed_order():
    entry = {"require_green_builds": False, "allow_approve": True, "allow_comment": True,
             "branch": "release/2.x", "repositories": ["svc-a", "svc_b.c"], **BASE}
    cleaned = clean_entry(entry)
    assert cleaned == entry
    assert list(cleaned) == ["destination", "workspace", "repositories", "branch",
                             "allow_comment", "allow_approve", "require_green_builds"]
    assert cleaned["repositories"] is not entry["repositories"]  # a copy
    assert pins_of(entry).repositories == ("svc-a", "svc_b.c")


@pytest.mark.parametrize("extra", [
    {"allow_comment": False}, {"allow_approve": False}, {"require_green_builds": True},
    {"has_client_secret": False}, {"user_context": False}, {"user_context": None},
    {"repositories": None}, {"branch": None},
])
def test_a_default_or_echoed_value_is_accepted_and_not_stored(extra):
    assert clean_entry({**BASE, **extra}) == BASE


@pytest.mark.parametrize("change, field", [
    ({"destination": ""}, "oauth.destination"),
    ({"destination": " BITBUCKET"}, "oauth.destination"),
    ({"destination": SECRET}, "oauth.destination"),
    ({"workspace": ""}, "oauth.workspace"),
    ({"workspace": "Acme"}, "oauth.workspace"),
    ({"workspace": "acme-ws "}, "oauth.workspace"),
    ({"workspace": "acme/" + SECRET}, "oauth.workspace"),
    ({"workspace": 7}, "oauth.workspace"),
    ({"repositories": []}, "oauth.repositories"),
    ({"repositories": "svc-a"}, "oauth.repositories"),
    ({"repositories": ["svc-a", "svc-a"]}, "oauth.repositories"),
    ({"repositories": ["svc-a", SECRET]}, "oauth.repositories"),
    ({"repositories": ["../" + SECRET]}, "oauth.repositories"),
    ({"repositories": [".hidden"]}, "oauth.repositories"),
    ({"repositories": [7]}, "oauth.repositories"),
    ({"repositories": [f"r{i}" for i in range(51)]}, "oauth.repositories"),
    ({"branch": ""}, "oauth.branch"),
    ({"branch": "main "}, "oauth.branch"),
    ({"branch": 'main" OR state="MERGED'}, "oauth.branch"),
    ({"branch": "a..b"}, "oauth.branch"),
    ({"branch": "a//b"}, "oauth.branch"),
    ({"branch": "feature/"}, "oauth.branch"),
    ({"branch": "x.lock"}, "oauth.branch"),
    ({"allow_comment": "true"}, "oauth.allow_comment"),
    ({"allow_approve": 1}, "oauth.allow_approve"),
    ({"require_green_builds": "false"}, "oauth.require_green_builds"),
    ({"allow_approve": True}, "oauth.allow_approve"),            # needs allow_comment
    ({"user_context": True}, "oauth.user_context"),
    ({"has_client_secret": True}, "oauth"),
    ({SECRET: 1}, "oauth"),
    ({"project": "ABC"}, "oauth"),                               # another built-in's key
    ({"client_secret": SECRET}, "oauth"),
])
def test_a_refusal_names_the_field_and_the_rule_never_the_value(change, field):
    for reader in (check_entry, clean_entry, pins_of):
        with pytest.raises(BitbucketConfigError) as refused:
            reader({**BASE, **change})
        text = str(refused.value)
        assert text.startswith(field + ":"), text
        assert SECRET not in text and "Zx9" not in text and "MERGED" not in text


@pytest.mark.parametrize("block", [None, [], "x", 7])
def test_a_block_that_is_no_object_is_refused(block):
    with pytest.raises(BitbucketConfigError, match="^oauth:"):
        check_entry(block)


def test_the_entry_keys_are_a_closed_set():
    assert ENTRY_KEYS == ("destination", "workspace", "repositories", "branch",
                          "allow_comment", "allow_approve", "require_green_builds")


def test_a_repository_is_allowed_only_inside_the_pin():
    pinned = pins_of({**BASE, "repositories": ["svc-a"]})
    assert repository_allowed(pinned, "svc-a")
    assert not repository_allowed(pinned, "svc-b")
    assert not repository_allowed(pinned, "SVC-A")
    free = pins_of(BASE)
    assert repository_allowed(free, "anything_1.x")
    for bad in ("", "..", "a/b", "a%2Fb", "a?b", "A", 7, None, "x" * 63):
        assert not repository_allowed(free, bad)


@pytest.mark.parametrize("path", ["src/main.py", "a b/ü.txt", "x" * 1000])
def test_a_file_path_below_the_repository_root_is_confined(path):
    assert confine_path(path) == path


@pytest.mark.parametrize("path", [
    "", "/etc/passwd", "a/../b", "..", "a/./b", "a//b", "a\\b", "a%2e%2e/b", "a?b", "a#b",
    "a\nb", "a\x00b", "a/", "x" * 1001, 7, None,
])
def test_a_path_that_could_leave_the_file_endpoint_is_refused(path):
    with pytest.raises(ValueError) as refused:
        confine_path(path)
    assert str(refused.value) == "the path is not a file path below the repository root"
