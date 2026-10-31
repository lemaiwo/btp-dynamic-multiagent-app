"""``agents/git_config.py``: the one reading of a ``builtin:git`` entry
(the gate, the storage cleaner and the toolset all call it)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from agents.git_config import (  # noqa: E402
    GitConfigError,
    branch_ok,
    check_entry,
    clean_entry,
    confine_path,
    is_pipeline_file,
    name_ok,
    pins_of,
    repository_ok,
)

SECRET = "Zx9-secret"
BASE = {"destination": "GITHUB", "provider": "github", "repositories": ["acme/web"]}
FULL = {**BASE, "repositories": ["acme/web", "acme/api"], "base_branch": "develop",
        "branch_prefix": "bot/", "allow_push": True, "allow_pull_request": True}


def _refused(cfg) -> str:
    with pytest.raises(GitConfigError) as info:
        check_entry(cfg)
    return str(info.value)


def test_a_full_entry_is_stored_as_checked_in_a_fixed_order():
    stored = clean_entry(dict(reversed(list(FULL.items()))))
    assert stored == FULL and list(stored) == list(FULL)


def test_defaults_are_not_written_and_pins_fill_them():
    assert clean_entry(BASE) == BASE
    pins = pins_of(BASE)
    assert (pins.provider, pins.repositories, pins.base_branch, pins.branch_prefix,
            pins.allow_push, pins.allow_pull_request) == (
        "github", ("acme/web",), "main", "agent/", False, False)


def test_switches_are_stored_only_as_true():
    stored = clean_entry({**BASE, "allow_push": False, "allow_pull_request": False})
    assert stored == BASE
    for switch in ("allow_push", "allow_pull_request"):
        for value in ("true", 1, "yes", [True]):
            assert f"oauth.{switch}: must be the JSON boolean" in _refused(
                {**BASE, switch: value})


def test_echoed_keys_are_tolerated_only_when_they_say_nothing_and_never_stored():
    assert clean_entry({**BASE, "has_client_secret": False, "user_context": False}) == BASE
    assert "holds only" in _refused({**BASE, "has_client_secret": True})


def test_user_context_is_refused():
    text = _refused({**BASE, "user_context": True})
    assert text.startswith("oauth.user_context:") and "technical user" in text


def test_an_unknown_key_is_refused_without_its_name():
    text = _refused({**BASE, SECRET: "x"})
    assert "holds only" in text and SECRET not in text
    assert "holds only" in _refused({**BASE, "client_secret": SECRET})
    assert "holds only" in _refused({**BASE, "workspace": "acme"})


@pytest.mark.parametrize("cfg", [None, [], "builtin:git", 1])
def test_a_block_that_is_no_object_is_refused(cfg):
    assert _refused(cfg).startswith("oauth: a builtin:git entry needs a config object")


@pytest.mark.parametrize("change, field", [
    ({"destination": None}, "oauth.destination"),
    ({"destination": f" {SECRET}"}, "oauth.destination"),
    ({"destination": "a/b"}, "oauth.destination"),
    ({"provider": None}, "oauth.provider"),
    ({"provider": "GitHub"}, "oauth.provider"),
    ({"provider": SECRET}, "oauth.provider"),
    ({"repositories": None}, "oauth.repositories"),
    ({"repositories": []}, "oauth.repositories"),
    ({"repositories": "acme/web"}, "oauth.repositories"),
    ({"repositories": ["acme/web", "acme/web"]}, "oauth.repositories"),
    ({"repositories": [f"acme/{i}" for i in range(51)]}, "oauth.repositories"),
    ({"repositories": [f" acme/{SECRET}"]}, "oauth.repositories"),
    ({"repositories": ["acme"]}, "oauth.repositories"),
    ({"repositories": ["acme/web/x"]}, "oauth.repositories"),
    ({"repositories": ["acme/.."]}, "oauth.repositories"),
    ({"repositories": [1]}, "oauth.repositories"),
    ({"base_branch": "a..b"}, "oauth.base_branch"),
    ({"base_branch": "main/"}, "oauth.base_branch"),
    ({"base_branch": f"{SECRET} x"}, "oauth.base_branch"),
    ({"base_branch": "agent/main"}, "oauth.base_branch"),
    ({"branch_prefix": "agent"}, "oauth.branch_prefix"),
    ({"branch_prefix": "/agent/"}, "oauth.branch_prefix"),
    ({"branch_prefix": "a/b/"}, "oauth.branch_prefix"),
    ({"branch_prefix": "x" * 41 + "/"}, "oauth.branch_prefix"),
    ({"branch_prefix": f"{SECRET} /"}, "oauth.branch_prefix"),
])
def test_every_rule_names_the_field_never_the_value(change, field):
    text = _refused({**BASE, **change})
    assert text.startswith(field + ":") and SECRET not in text


@pytest.mark.parametrize("provider, good, bad", [
    ("github", ["acme/web", "A-1/x.y_z", "o/.github"], ["acme", "-a/b", "a/b/c", "a/..",
                                                         "acme/we b"]),
    ("bitbucket", ["acme-ws/svc-a", "w_1/r.x"], ["Acme/svc", "acme/Svc", "acme", "a/b/c",
                                                 "acme/.x"]),
    ("gitlab", ["group/project", "g/sub/p", "G_1/Sub.x/p-2"],
     ["group", "g//p", "g/p.git", "g/../p", "/g/p", "g/p/", "g/-p"]),
])
def test_repository_paths_follow_the_provider(provider, good, bad):
    assert all(repository_ok(provider, r) for r in good)
    assert not any(repository_ok(provider, r) for r in bad)
    cfg = {"destination": "D", "provider": provider, "repositories": good}
    assert clean_entry(cfg)["repositories"] == good
    assert "oauth.repositories: each entry" in _refused({**cfg, "repositories": bad[:1]})


def test_a_path_of_another_provider_is_refused():
    assert "GitHub" in _refused({**BASE, "provider": "github", "repositories": ["g/s/p"]})
    assert "Bitbucket" in _refused({**BASE, "provider": "bitbucket",
                                    "repositories": ["Acme/Web"]})


def test_branch_names():
    assert all(branch_ok(b) for b in ["main", "agent/fix-1", "release/2.0", "a_b.c"])
    assert not any(branch_ok(b) for b in ["", "-x", "a..b", "a//b", "a/", "a.", "x.lock",
                                          "a/.b", "a b", "ä", None, 1])
    assert name_ok("fix/label") and not name_ok("x" * 82) and name_ok("x" * 81)


@pytest.mark.parametrize("path", ["src/app.js", "a", "dir/sub/file.txt", "Ünïcode/ok.md"])
def test_a_path_below_the_root_is_accepted(path):
    assert confine_path(path) == path


@pytest.mark.parametrize("path", ["", "/abs", "a/", "a//b", "../x", "a/../b", "./a",
                                  "a\\b", "a%2Fb", "a?b", "a#b", "a\x00b", "a\nb",
                                  "a‮b", "x" * 1001, None, 3])
def test_a_path_that_leaves_the_root_is_refused_with_one_text(path):
    with pytest.raises(ValueError) as info:
        confine_path(path)
    assert str(info.value) == "the path is not a path below the repository root"


def test_the_root_is_a_path_only_for_a_listing():
    assert confine_path("", allow_root=True) == ""


@pytest.mark.parametrize("path", [".github/workflows/deploy.yml", ".GitHub/Workflows/x.yml",
                                  "bitbucket-pipelines.yml", ".gitlab-ci.yml",
                                  ".gitlab/ci/build.yml", ".GITLAB-CI.YML"])
def test_pipeline_files(path):
    assert is_pipeline_file(path)


@pytest.mark.parametrize("path", ["src/.gitlab-ci.yml", ".github/CODEOWNERS",
                                  "docs/bitbucket-pipelines.yml", "gitlab/x"])
def test_not_pipeline_files(path):
    assert not is_pipeline_file(path)
