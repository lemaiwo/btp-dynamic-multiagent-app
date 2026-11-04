"""The one reading of a ``builtin:git`` entry. Standard library only.

The save-time gate (``agents/admin.py``), storage (``agents/db.py``,
``_clean_git_entry``) and the toolset (``agents/git_tools.py``) all call this
module, so none accepts what another refuses. A value is accepted only in
exactly the form that is stored: nothing here trims, lower-cases or converts.
A refusal names the field and the rule, never the value, and never the name
of a key it does not know.

The entry::

    {"destination": "<name>", "provider": "github" | "bitbucket" | "gitlab",
     "repositories": ["owner/repo", ...], "base_branch": "main",
     "branch_prefix": "agent/", "allow_push": true, "allow_pull_request": true}

The credential is the destination's (its URL is the provider's API base);
the technical user commits, the chat user is named in the commit message, so
``user_context: true`` is refused.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

BUILTIN_GIT_URL = "builtin:git"
PROVIDERS = ("github", "bitbucket", "gitlab")
ENTRY_KEYS = ("destination", "provider", "repositories", "base_branch",
              "branch_prefix", "allow_push", "allow_pull_request")
DEFAULT_BASE_BRANCH = "main"
DEFAULT_BRANCH_PREFIX = "agent/"
MAX_REPOSITORIES = 50
MAX_PATH_CHARS = 1000

_DESTINATION_RE = re.compile(r"[A-Za-z0-9_.-]{1,200}")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_PREFIX_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}/")
# The part of a branch name a tool call gives; the prefix is put in front.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,80}")
_HASH_RE = re.compile(r"[0-9a-f]{7,64}")
_GITHUB_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_BITBUCKET_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,61}/[a-z0-9_][a-z0-9._-]{0,61}")
_GITLAB_SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")
_SWITCHES = ("allow_push", "allow_pull_request")
# Echoed by the API's own answers (`_redact_servers` in agents/db.py) or sent
# with their default by a client that serialises every field. Tolerated only
# when they say nothing; never stored.
_ECHOED = ("has_client_secret", "user_context")
_REPOSITORY_RULES = {
    "github": "owner/name (GitHub)",
    "bitbucket": "workspace/slug in lower case (Bitbucket)",
    "gitlab": "group[/subgroup]/project (GitLab)",
}
# What a pipeline reads: a commit must not define the CI that holds the
# deploy credentials. Compared without case: a checkout on a case-folding
# file system reads `.GitHub/Workflows` as the same directory.
_PIPELINE_FILES = ("bitbucket-pipelines.yml", ".gitlab-ci.yml")
_PIPELINE_DIRS = (".github/workflows/", ".gitlab/")


class GitConfigError(ValueError):
    """A refused entry. The text names the field and the rule only."""


@dataclass(frozen=True)
class Pins:
    """What the toolset is built with."""

    provider: str
    repositories: tuple[str, ...]
    base_branch: str
    branch_prefix: str
    allow_push: bool
    allow_pull_request: bool


def _is(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def branch_ok(value: Any) -> bool:
    """A branch name as git accepts it, in the subset this toolset uses."""
    return (_is(_BRANCH_RE, value) and ".." not in value and "//" not in value
            and not value.endswith(("/", ".", ".lock")) and "/." not in value)


def name_ok(value: Any) -> bool:
    """The part of a new branch's name that a tool call gives."""
    return (_is(_NAME_RE, value) and ".." not in value and "//" not in value
            and not value.endswith(("/", ".", ".lock")) and "/." not in value)


def ref_ok(value: Any) -> bool:
    """A branch name or a commit hash, as a read may name it."""
    return branch_ok(value) or _is(_HASH_RE, value)


def is_hash(value: Any) -> bool:
    return _is(_HASH_RE, value)


def repository_ok(provider: str, value: Any) -> bool:
    if provider == "github":
        return _is(_GITHUB_RE, value) and value.split("/")[1] not in (".", "..")
    if provider == "bitbucket":
        return _is(_BITBUCKET_RE, value)
    if provider == "gitlab":
        if not isinstance(value, str) or len(value) > 255:
            return False
        parts = value.split("/")
        return 2 <= len(parts) <= 20 and all(
            _is(_GITLAB_SEGMENT_RE, p) and not p.endswith((".git", ".atom", "."))
            and ".." not in p for p in parts)
    return False


def check_entry(cfg: Any) -> None:
    """Raises :class:`GitConfigError` unless ``cfg`` is a valid block."""
    if not isinstance(cfg, dict):
        raise GitConfigError(
            f"oauth: a {BUILTIN_GIT_URL} entry needs a config object with destination, "
            "provider and repositories")
    if cfg.get("user_context") is True:
        raise GitConfigError(
            f"oauth.user_context: {BUILTIN_GIT_URL} has no signed-in user to act as; "
            "it commits as the technical user of the destination")
    present = {k for k, v in cfg.items() if v is not None}
    stray = present - set(ENTRY_KEYS) - {k for k in _ECHOED if cfg.get(k) is False}
    if stray:
        # Not named: a key outside the closed set is the client's own text.
        raise GitConfigError(
            f"oauth: a {BUILTIN_GIT_URL} entry holds only {', '.join(ENTRY_KEYS)}; "
            "remove every other key")
    if not _is(_DESTINATION_RE, cfg.get("destination")):
        raise GitConfigError(
            "oauth.destination: required, a destination name of 1-200 letters, "
            "digits, '_', '.' or '-' without surrounding spaces")
    provider = cfg.get("provider")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise GitConfigError(
            f"oauth.provider: required, one of {', '.join(PROVIDERS)}")
    repositories = cfg.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise GitConfigError(
            "oauth.repositories: required, a list of 1 to 50 repository paths")
    if len(repositories) > MAX_REPOSITORIES:
        raise GitConfigError(
            f"oauth.repositories: at most {MAX_REPOSITORIES} repositories per entry")
    if not all(repository_ok(provider, r) for r in repositories):
        raise GitConfigError(
            f"oauth.repositories: each entry is one repository path, "
            f"{_REPOSITORY_RULES[provider]}, without surrounding spaces")
    if len(set(repositories)) != len(repositories):
        raise GitConfigError("oauth.repositories: a repository is listed twice")
    if cfg.get("base_branch") is not None and not branch_ok(cfg["base_branch"]):
        raise GitConfigError(
            "oauth.base_branch: a branch name of letters, digits, '.', '_', '-' and '/' "
            "(no '..', no '//', not ending in '/', '.' or '.lock')")
    prefix = cfg.get("branch_prefix")
    if prefix is not None and not _is(_PREFIX_RE, prefix):
        raise GitConfigError(
            "oauth.branch_prefix: 1-40 letters, digits, '.', '_' or '-' followed by "
            "one '/', starting with a letter or digit (default agent/)")
    base = cfg.get("base_branch", DEFAULT_BASE_BRANCH) or DEFAULT_BASE_BRANCH
    if base.startswith(prefix if prefix is not None else DEFAULT_BRANCH_PREFIX):
        raise GitConfigError(
            "oauth.base_branch: must not carry the branch prefix; the agent pushes "
            "only to branches with the prefix")
    for switch in _SWITCHES:
        if cfg.get(switch) is not None and not isinstance(cfg[switch], bool):
            raise GitConfigError(
                f"oauth.{switch}: must be the JSON boolean true or false; a string "
                "or a number does not set it")


def clean_entry(cfg: Any) -> dict[str, Any]:
    """The block as it is stored: checked, its own keys only, fixed order.
    A default is not written out; a switch only as ``true``."""
    check_entry(cfg)
    cleaned: dict[str, Any] = {"destination": cfg["destination"],
                               "provider": cfg["provider"],
                               "repositories": list(cfg["repositories"])}
    if cfg.get("base_branch") is not None:
        cleaned["base_branch"] = cfg["base_branch"]
    if cfg.get("branch_prefix") is not None:
        cleaned["branch_prefix"] = cfg["branch_prefix"]
    for switch in _SWITCHES:
        if cfg.get(switch) is True:
            cleaned[switch] = True
    return cleaned


def pins_of(cfg: Any) -> Pins:
    """The checked block as the toolset reads it."""
    cleaned = clean_entry(cfg)
    return Pins(
        provider=cleaned["provider"],
        repositories=tuple(cleaned["repositories"]),
        base_branch=cleaned.get("base_branch", DEFAULT_BASE_BRANCH),
        branch_prefix=cleaned.get("branch_prefix", DEFAULT_BRANCH_PREFIX),
        allow_push=cleaned.get("allow_push") is True,
        allow_pull_request=cleaned.get("allow_pull_request") is True,
    )


def confine_path(path: Any, *, allow_root: bool = False) -> str:
    """A path below the repository root, or a ``ValueError`` with one fixed
    text. No leading or trailing '/', no empty, '.' or '..' segment, no
    backslash, no control or format character; ``allow_root`` lets the empty
    path name the root (a directory listing). Each segment is percent-encoded
    by the adapter, so '%', '?' and '#' are refused as well: a file name that
    holds one is rare, and one rule for every provider is easier to trust."""
    if allow_root and path == "":
        return ""
    if (not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS
            or any(c in path for c in "\\%?#")
            or any(unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp")
                   for c in path)
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise ValueError("the path is not a path below the repository root")
    return path


def is_pipeline_file(path: str) -> bool:
    """Whether a commit to ``path`` would change what a CI pipeline runs."""
    folded = path.casefold()
    return folded in _PIPELINE_FILES or folded.startswith(_PIPELINE_DIRS)
