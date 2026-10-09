"""The one reading of a ``builtin:bitbucket`` entry. Standard library only.

The save-time gate (``agents/admin.py``), storage (``agents/db.py``) and the
toolset (``agents/bitbucket_tools.py``) all call this module, so none accepts
what another refuses. A value is accepted only in exactly the form that is
stored: nothing here trims, lower-cases or converts. A refusal names the field
and the rule, never the value, and never the name of a key it does not know.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

BUILTIN_BITBUCKET_URL = "builtin:bitbucket"
ENTRY_KEYS = ("destination", "workspace", "repositories", "branch",
              "allow_comment", "allow_approve", "require_green_builds")
DEFAULT_BRANCH = "main"
MAX_REPOSITORIES = 50
MAX_PATH_CHARS = 1000

_DESTINATION_RE = re.compile(r"[A-Za-z0-9_.-]{1,200}")
_WORKSPACE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,61}")
_REPOSITORY_RE = re.compile(r"[a-z0-9_][a-z0-9._-]{0,61}")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_SWITCHES = ("allow_comment", "allow_approve", "require_green_builds")
# Echoed by the API's own answers (`_redact_servers` in agents/db.py) or sent
# with their default by a client that serialises every field. Tolerated only
# when they say nothing; never stored.
_ECHOED = ("has_client_secret", "user_context")
_PATH_REFUSAL = "the path is not a file path below the repository root"


class BitbucketConfigError(ValueError):
    """A refused entry. The text names the field and the rule only."""


@dataclass(frozen=True)
class Pins:
    """What the toolset is built with. ``repositories`` is ``None`` for the
    whole workspace."""

    workspace: str
    repositories: tuple[str, ...] | None
    branch: str
    allow_comment: bool
    allow_approve: bool
    require_green_builds: bool


def _is(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _branch_ok(value: Any) -> bool:
    return (_is(_BRANCH_RE, value) and ".." not in value and "//" not in value
            and not value.endswith(("/", ".", ".lock")))


def check_entry(cfg: Any) -> None:
    """Raises :class:`BitbucketConfigError` unless ``cfg`` is a valid block."""
    if not isinstance(cfg, dict):
        raise BitbucketConfigError(
            f"oauth: a {BUILTIN_BITBUCKET_URL} entry needs a config object with "
            "destination and workspace")
    if cfg.get("user_context") is True:
        raise BitbucketConfigError(
            f"oauth.user_context: {BUILTIN_BITBUCKET_URL} has no signed-in user to act "
            "as; it reviews as the technical user of the destination")
    present = {k for k, v in cfg.items() if v is not None}
    stray = present - set(ENTRY_KEYS) - {k for k in _ECHOED if cfg.get(k) is False}
    if stray:
        # Not named: a key outside the closed set is the client's own text.
        raise BitbucketConfigError(
            f"oauth: a {BUILTIN_BITBUCKET_URL} entry holds only "
            f"{', '.join(ENTRY_KEYS)}; remove every other key")
    if not _is(_DESTINATION_RE, cfg.get("destination")):
        raise BitbucketConfigError(
            "oauth.destination: required, a destination name of 1-200 letters, "
            "digits, '_', '.' or '-' without surrounding spaces")
    if not _is(_WORKSPACE_RE, cfg.get("workspace")):
        raise BitbucketConfigError(
            "oauth.workspace: required, one workspace slug (lower-case letters, "
            "digits, '_' and '-', at most 62 characters)")
    repositories = cfg.get("repositories")
    if repositories is not None:
        if not isinstance(repositories, list) or not repositories:
            raise BitbucketConfigError(
                "oauth.repositories: a list of 1 to 50 repository slugs; leave the "
                "key out to review every repository of the workspace")
        if len(repositories) > MAX_REPOSITORIES:
            raise BitbucketConfigError(
                f"oauth.repositories: at most {MAX_REPOSITORIES} repositories per entry")
        if not all(_is(_REPOSITORY_RE, name) for name in repositories):
            raise BitbucketConfigError(
                "oauth.repositories: each entry is one repository slug (lower-case "
                "letters, digits, '_', '-' and '.', not starting with '.', at most "
                "62 characters)")
        if len(set(repositories)) != len(repositories):
            raise BitbucketConfigError("oauth.repositories: a repository is listed twice")
    if cfg.get("branch") is not None and not _branch_ok(cfg["branch"]):
        raise BitbucketConfigError(
            "oauth.branch: a branch name of letters, digits, '.', '_', '-' and '/' "
            "(no '..', no '//', not ending in '/', '.' or '.lock')")
    for switch in _SWITCHES:
        if cfg.get(switch) is not None and not isinstance(cfg[switch], bool):
            raise BitbucketConfigError(
                f"oauth.{switch}: must be the JSON boolean true or false; a string "
                "or a number does not set it")
    if cfg.get("allow_approve") is True and cfg.get("allow_comment") is not True:
        raise BitbucketConfigError(
            "oauth.allow_approve: requires oauth.allow_comment; an approval is only "
            "sent after the review comment was posted")


def asks_to_approve(cfg: Any) -> bool:
    """Whether a block asks for approving, checked or not: ``allow_approve``
    holds anything but ``false`` / nothing.

    For the rules about who may reach an approving agent (no chat exposure,
    never a peer), which also judge rows no gate has seen. Wider than
    :func:`pins_of` on purpose: a hand-written ``"true"`` gives no toolset at
    all, and still must not read as "does not approve" to those rules."""
    return isinstance(cfg, dict) and cfg.get("allow_approve") not in (None, False)


def clean_entry(cfg: Any) -> dict[str, Any]:
    """The block as it is stored: checked, its own keys only, fixed order."""
    check_entry(cfg)
    cleaned: dict[str, Any] = {"destination": cfg["destination"],
                               "workspace": cfg["workspace"]}
    if cfg.get("repositories") is not None:
        cleaned["repositories"] = list(cfg["repositories"])
    if cfg.get("branch") is not None:
        cleaned["branch"] = cfg["branch"]
    if cfg.get("allow_comment") is True:
        cleaned["allow_comment"] = True
    if cfg.get("allow_approve") is True:
        cleaned["allow_approve"] = True
    if cfg.get("require_green_builds") is False:
        cleaned["require_green_builds"] = False
    return cleaned


def pins_of(cfg: Any) -> Pins:
    """The checked block as the toolset reads it."""
    cleaned = clean_entry(cfg)
    repositories = cleaned.get("repositories")
    return Pins(
        workspace=cleaned["workspace"],
        repositories=tuple(repositories) if repositories is not None else None,
        branch=cleaned.get("branch", DEFAULT_BRANCH),
        allow_comment=cleaned.get("allow_comment") is True,
        allow_approve=cleaned.get("allow_approve") is True,
        require_green_builds=cleaned.get("require_green_builds") is not False,
    )


def repository_allowed(pins: Pins, name: Any) -> bool:
    """Whether a tool argument names a repository this entry may touch: one of
    the pinned slugs, or without a list anything that has the form of a slug."""
    if not _is(_REPOSITORY_RE, name):
        return False
    return pins.repositories is None or name in pins.repositories


def confine_path(path: Any) -> str:
    """A file path below the repository root, or a ``ValueError`` with one
    fixed text. No leading or trailing '/', no empty, '.' or '..' segment, no
    backslash, '%', '?', '#' and no control character: the value is put into a
    URL path segment by segment."""
    if (not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS
            or any(c in path for c in "\\%?#")
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise ValueError(_PATH_REFUSAL)
    return path
