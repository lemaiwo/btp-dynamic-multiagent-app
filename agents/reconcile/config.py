"""The config block of a ``builtin:reconcile`` entry (``auth_mode="none"``).

The entry holds no credential: ECC is read through a sibling entry of the
same agent, the ARC-1 MCP server named in ``arc1_server``, whose own auth
(``oauth2``) carries the signed-in user. What the block holds is the link
to that sibling plus the run's knobs. The admin gate (``agents.admin``),
storage (``agents.db._clean_builtin_public``) and the factory all call
:func:`validate_reconcile_block`, so none accepts what another refuses.

Standard library only: ``agents.db`` imports this module, so it must not
import ``agents.db`` or ``agents.admin`` back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

RECONCILE_KEYS = ("arc1_server", "target", "batch_size", "mismatch_cap")
# ARC-1's SAPQuery takes at most 10,000 rows per call (maxRows), and a batch
# asks for one row more than it has keys (arc1_reader.MAX_BATCH_KEYS).
DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE = 200, 9_999
DEFAULT_MISMATCH_CAP, MAX_MISMATCH_CAP = 50_000, 200_000
TARGET_RE = re.compile(r"^[A-Za-z0-9_./-]{1,64}\Z")
# The sibling must act as the signed-in tester: per-user OAuth or the
# forwarded JWT. A technical identity (app_only, destination) or none would
# read the source system as somebody else.
ARC1_SIBLING_MODES = ("oauth2", "jwt")
ARC1_SIBLING_MESSAGE = (
    "oauth.arc1_server: must name an ARC-1 server entry of this agent "
    "with auth_mode oauth2 or jwt"
)
# Two entries would share the server key and their tools' names.
RECONCILE_SINGLE_ENTRY_MESSAGE = (
    "an agent may have at most one builtin:reconcile entry; it reads one "
    "ARC-1 server entry, named in oauth.arc1_server"
)
_BUILTIN_RECONCILE_URL = "builtin:reconcile"
_DIGITS_RE = re.compile(r"^[0-9]{1,7}$")


@dataclass(frozen=True)
class ReconcileConfig:
    """The checked block. Blank values are the defaults: ``arc1_server`` and
    ``target`` empty (no sibling named, the ARC-1 server's own target),
    the numbers their ``DEFAULT_*``."""

    arc1_server: str
    target: str
    batch_size: int
    mismatch_cap: int

    @classmethod
    def from_oauth(cls, oauth: dict[str, Any] | None) -> "ReconcileConfig":
        """The config of a stored or posted block; ``ValueError`` (naming the
        key, never the value) for a block the gate would refuse."""
        block = oauth if isinstance(oauth, dict) else {}
        validate_reconcile_block(block)
        return cls(
            arc1_server=_text(block.get("arc1_server")),
            target=_text(block.get("target")),
            batch_size=_number(block.get("batch_size"), DEFAULT_BATCH_SIZE),
            mismatch_cap=_number(block.get("mismatch_cap"), DEFAULT_MISMATCH_CAP),
        )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any, default: int) -> int:
    text = _text(value)
    return int(text) if text else default


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _check_number(key: str, value: Any, maximum: int) -> None:
    if _blank(value):
        return
    # A string only: the gate and storage hand strings on; a JSON number
    # here was not posted through either.
    text = value.strip() if isinstance(value, str) else None
    if text is None or not _DIGITS_RE.match(text) or not 1 <= int(text) <= maximum:
        raise ValueError(f"oauth.{key}: a whole number from 1 to {maximum}")


def _check_arc1_server(value: Any) -> None:
    if _blank(value):
        return
    rule = ("oauth.arc1_server: the https URL of this agent's ARC-1 server entry, "
            "with a host and without credentials, query or fragment")
    if not isinstance(value, str):
        raise ValueError(rule)
    text = value.strip()
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise ValueError(rule) from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or "?" in text
        or "#" in text
    ):
        raise ValueError(rule)


def validate_reconcile_block(cfg: dict[str, Any]) -> None:
    """Refuse a block that cannot be used. The text names the key and the
    rule, never the value (a URL could carry a token, a key name could be a
    pasted secret, so an unknown key is not named either)."""
    if not isinstance(cfg, dict):
        raise ValueError("oauth: a builtin:reconcile entry needs a config object")
    if set(cfg) - set(RECONCILE_KEYS):
        raise ValueError(
            "oauth: a builtin:reconcile entry holds only "
            f"{', '.join(RECONCILE_KEYS)}"
        )
    _check_arc1_server(cfg.get("arc1_server"))
    target = cfg.get("target")
    if not _blank(target) and not (
        isinstance(target, str) and TARGET_RE.fullmatch(target.strip())
    ):
        raise ValueError(
            "oauth.target: 1 to 64 letters, digits, '_', '.', '-' or '/'"
        )
    _check_number("batch_size", cfg.get("batch_size"), MAX_BATCH_SIZE)
    _check_number("mismatch_cap", cfg.get("mismatch_cap"), MAX_MISMATCH_CAP)


def endpoint_key(url: Any) -> str:
    """The form two spellings of one MCP endpoint share: trimmed, no trailing
    ``/``, ``/mcp`` added to a bare host (the rule of
    ``agents.shared.mcp_endpoint_url``), scheme and host lower-cased (they are
    case-insensitive; the path is not)."""
    text = str(url or "").strip().rstrip("/")
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme or not parts.netloc:
        return text
    path = parts.path or "/mcp"
    netloc = parts.netloc
    if "@" not in netloc:
        netloc = netloc.lower()
    return parts._replace(scheme=parts.scheme.lower(), netloc=netloc, path=path).geturl()


def _is_reconcile(entry: Any) -> bool:
    return isinstance(entry, dict) and (
        str(entry.get("url") or "").strip().rstrip("/").lower() == _BUILTIN_RECONCILE_URL)


def check_reconcile_entries(servers: Any) -> None:
    """The rules of an agent's server list that one entry alone cannot say:
    at most one ``builtin:reconcile`` entry, and its ``arc1_server`` (when
    set) names a remote entry of the SAME list whose ``auth_mode`` acts as
    the signed-in tester (:data:`ARC1_SIBLING_MODES`). The admin gate and
    storage (``agents.db.prepare_servers``) both call it; the texts are
    fixed and name no URL."""
    entries = [s for s in servers or [] if isinstance(s, dict)]
    reconcile = [s for s in entries if _is_reconcile(s)]
    if len(reconcile) > 1:
        raise ValueError(RECONCILE_SINGLE_ENTRY_MESSAGE)
    if not reconcile:
        return
    block = reconcile[0].get("oauth")
    wanted = _text(block.get("arc1_server")) if isinstance(block, dict) else ""
    if not wanted:
        return  # the tools answer arc1_not_configured
    key = endpoint_key(wanted)
    for entry in entries:
        url = str(entry.get("url") or "").strip()
        if url.lower().startswith("builtin") or endpoint_key(url) != key:
            continue
        mode = str(entry.get("auth_mode") or "jwt").strip().lower()
        if mode in ARC1_SIBLING_MODES:
            return
    raise ValueError(ARC1_SIBLING_MESSAGE)
