"""The destinations an OData catalogue service can name, for a dropdown.

``GET /admin/api/odata/destinations`` asks the BTP destination service what
exists at the two levels this app can see -- the service instance it is
bound to and the subaccount -- and answers a short, fixed description of
each. It lives under ``agents/odata`` rather than next to
``agents/destination.py`` because what it decides is the catalogue's own
question: can an OData service use this destination today.

**What leaves this module is an allowlist.** A destination configuration is
where the credential is: ``Password``, ``clientSecret``, a
``URL.headers.Authorization`` value, a certificate, and the ``URL`` itself
(an internal host). ``_reduce`` reads exactly five properties of an entry
(``Name``, ``Description``, ``Type``, ``ProxyType``, ``Authentication`` and
nothing else) and builds a new object from them; the entry itself is never
stored, returned, logged or put into an exception. Each of the strings is
then cleaned on its own: cut, one line, URLs masked, capped, and the three
type fields only as a known value or a short identifier.

``description`` is the admin's free text and is returned as such: one line,
capped, with URLs masked -- and that is all. Nothing recognises a password
or a key somebody typed into a description, so it is not a scrubbed field.
It is in the answer because whoever may call this route may already name,
and call through, any of these destinations.

**Bounds.** One token request and one ``GET`` per level, no retry, no
redirect followed, ``Accept-Encoding: identity`` (a compressed answer is not
read), ``MAX_LIST_BYTES`` read per level and ``MAX_ENTRIES`` returned, all
inside ``LIST_BUDGET_SECONDS``. An answer cut at the byte cap still yields
the entries that arrived whole. Nothing is cached: an admin who just created
a destination expects to see it, and the per-destination resolvers and their
caches are not involved at all.

**Errors.** Fixed texts and a stable code only (``ListError``). The log
line of a failure (WARNING) carries a reason code, the HTTP status and the
class of the exception, never its text and never a body: an HTTP client's
error texts quote bytes of the answer, which here are destination
configurations or the service token. One level failing is a ``warnings``
entry next to the other level's list; see ``list_destinations`` for the
contract.

Nothing here changes any state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterator

import httpx

from agents.destination import (
    PROXY_TYPE_ON_PREMISE,
    USER_PROPAGATING_AUTH_TYPES,
    DestinationError,
    DestinationServiceConfig,
    config_from_environment,
    fetch_service_token,
)
from agents.odata.models import DESTINATION_NAME_RE

logger = logging.getLogger(__name__)

# UNVERIFIED until the first landscape run: these are the well-known paths of
# the Destination Configuration API (v1) for "all destinations of the service
# instance" and "all destinations of the subaccount", each expected to answer
# a JSON array of flat destination configuration objects. Neither the paths
# nor the answer shape were checked against SAP's documentation or a real
# service when this was written. A wrong path shows up as a `level_unavailable`
# warning with `reason: http_error` and the status; a wrong shape as
# `reason: not_json`.
INSTANCE_DESTINATIONS_PATH = "/destination-configuration/v1/instanceDestinations"
SUBACCOUNT_DESTINATIONS_PATH = "/destination-configuration/v1/subaccountDestinations"

LEVEL_INSTANCE = "instance"
LEVEL_SUBACCOUNT = "subaccount"

# Token and both listings. The approuter gives a request about 30 seconds;
# an admin waiting for a dropdown gives it fewer.
LIST_BUDGET_SECONDS = 15.0
# Read per level. A destination configuration is a few hundred bytes to a
# few kilobytes (more with an inline certificate), so this is thousands of
# destinations; beyond it the answer is cut and says so.
MAX_LIST_BYTES = 2 * 1024 * 1024
# Returned in all, and reduced per level. A dropdown of more is of no use.
MAX_ENTRIES = 1000
MAX_NAME_CHARS = 200  # what DESTINATION_NAME_RE allows
MAX_DESCRIPTION_CHARS = 300
_MAX_LOG_CHARS = 300

OTHER = "other"
_TYPES = frozenset({"HTTP", "RFC", "MAIL", "LDAP", "TCP"})
_PROXY_TYPES = frozenset({"Internet", PROXY_TYPE_ON_PREMISE, "PrivateLink"})
# An authentication type is an identifier such as `OAuth2ClientCredentials`.
# The list grows on SAP's side, so the form is checked rather than the value.
_AUTHENTICATION_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}", re.ASCII)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")

REASON_NOT_HTTP = "not_http"
REASON_INVALID_NAME = "invalid_name"
NOTE_ON_PREMISE = "on_premise"

NO_BINDING_TEXT = (
    "no destination service is bound to this application, so its destinations "
    "cannot be listed; type the destination name instead"
)
TOKEN_FAILED_TEXT = (
    "the destination service did not issue a token, so its destinations cannot "
    "be listed; type the destination name instead"
)
LIST_FAILED_TEXT = (
    "the destination service did not list its destinations; type the "
    "destination name instead"
)
TIMEOUT_TEXT = (
    "the destination service did not answer in time; type the destination name instead"
)
_FAILED_TEXT = "the destinations could not be listed; type the destination name instead"


class ListError(Exception):
    """No list at all: the status, a stable code and a fixed text."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


class _LevelFailed(Exception):
    """One level could not be listed. ``reason`` is a code, never a text."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class _Level:
    """What one level contributed: reduced entries only, by stored name."""

    level: str
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped: int = 0
    truncated: bool = False
    failed: _LevelFailed | None = None


def _config() -> DestinationServiceConfig | None:
    """The destination service binding, read per call. A seam for tests."""
    return config_from_environment(os.environ)


def _transport() -> httpx.AsyncBaseTransport | None:
    """The transport of the requests; ``None`` = httpx's own. A seam for tests."""
    return None


def _plain(text: object, limit: int) -> str:
    """One line of printable text, URLs masked, at most ``limit`` characters.

    The rule of ``agents.odata.preview.plain``, kept here so that this module
    does not import the preview (and with it the metadata parser) for five
    lines.

    The text is cut BEFORE it is cleaned. The URL mask backtracks over a long
    run of scheme characters that never reaches ``://`` (quadratic), and it
    runs in the event loop where no timeout can stop it, so it must never
    see a property of megabytes. Four times the limit leaves room for what
    cleaning removes; a URL that the cut falls into is still masked.
    """
    if not isinstance(text, str):
        return ""
    head = text[: limit * 4]
    line = " ".join("".join(ch if ch.isprintable() else " " for ch in head).split())
    return _URL.sub("<url>", line)[:limit]


# ---------------------------------------------------------------- the allowlist


def _enumerated(value: object, known: frozenset[str]) -> str:
    """``""`` when the property is absent, the value when it is a known one,
    else the fixed ``other`` -- never a text of the service's choosing."""
    if value is None:
        return ""
    return value if isinstance(value, str) and value in known else OTHER


def _authentication(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and _AUTHENTICATION_RE.fullmatch(value):
        return value
    return OTHER


def _reduce(entry: object, level: str) -> tuple[str, dict[str, Any]] | None:
    """One listed entry as ``(stored name, what may be shown)``, or ``None``
    for an entry without a plain name.

    THE place a destination configuration is read. Five properties are looked
    at; everything else in ``entry`` -- the URL, users, passwords, client
    secrets, token service settings, static headers and queries,
    certificates -- is never touched, and ``entry`` is not kept.
    """
    if not isinstance(entry, dict):
        return None
    stored = entry.get("Name")
    if not isinstance(stored, str):
        return None
    name = _plain(stored, MAX_NAME_CHARS)
    if not name:
        return None
    kind = _enumerated(entry.get("Type"), _TYPES)
    proxy_type = _enumerated(entry.get("ProxyType"), _PROXY_TYPES)
    authentication = _authentication(entry.get("Authentication"))
    # Judged on the name as stored: the cut or cleaned form that is shown
    # could match where the real one does not.
    if not re.fullmatch(DESTINATION_NAME_RE, stored):
        reason: str | None = REASON_INVALID_NAME
    elif kind != "HTTP":
        reason = REASON_NOT_HTTP
    else:
        reason = None
    return stored, {
        "name": name,
        "description": _plain(entry.get("Description"), MAX_DESCRIPTION_CHARS),
        "type": kind,
        "proxy_type": proxy_type,
        "authentication": authentication,
        "level": level,
        "user_propagating": authentication in USER_PROPAGATING_AUTH_TYPES,
        "usable": reason is None,
        "reason": reason,
        "notes": [NOTE_ON_PREMISE] if proxy_type == PROXY_TYPE_ON_PREMISE else [],
        "shadows_subaccount": False,
    }


def _elements(text: str, cut: bool) -> Iterator[Any]:
    """The elements of the JSON array ``text``, one at a time.

    One at a time so that a listing is never held as a whole list of raw
    configurations. With ``cut`` (the read stopped at the byte cap) the walk
    ends quietly at the first element that is not whole; without it anything
    but a complete array is ``not_json``.
    """
    decoder = json.JSONDecoder()
    size = len(text)

    def skip(at: int) -> int:
        while at < size and text[at] in " \t\r\n":
            at += 1
        return at

    at = skip(0)
    if text[at : at + 1] != "[":
        raise _LevelFailed("not_json")
    at = skip(at + 1)
    if text[at : at + 1] == "]":
        at += 1
    else:
        while True:
            broken = False
            try:
                element, at = decoder.raw_decode(text, at)
            except (ValueError, RecursionError):
                broken = True
            if broken:
                if cut:
                    return
                # Raised outside the `except`: a JSONDecodeError holds the
                # whole listing (`.doc`), and `from None` would only hide it,
                # not let go of it (`__context__`).
                raise _LevelFailed("not_json")
            yield element
            del element
            at = skip(at)
            mark = text[at : at + 1]
            if mark == ",":
                at = skip(at + 1)
                continue
            if mark == "]":
                at += 1
                break
            if cut:
                return
            raise _LevelFailed("not_json")
    if not cut and skip(at) != size:
        raise _LevelFailed("not_json")


def _reduce_listing(raw: bytes, cut: bool, level: str) -> _Level:
    """The answer of one level, reduced. Raw configurations do not leave here."""
    text: str | None = None
    try:
        text = raw.decode("utf-8-sig", errors="ignore" if cut else "strict")
    except UnicodeDecodeError:
        pass
    if text is None:
        # Outside the `except`: a UnicodeDecodeError holds the raw bytes.
        raise _LevelFailed("not_json")
    result = _Level(level, truncated=cut)
    for element in _elements(text, cut):
        reduced = _reduce(element, level)
        if reduced is None:
            result.skipped += 1
            continue
        stored, item = reduced
        if stored in result.entries:
            continue  # a name twice at one level: the first stands
        if len(result.entries) >= MAX_ENTRIES:
            result.truncated = True
            break
        result.entries[stored] = item
    return result


# -------------------------------------------------------------------- the fetch


async def _read(http: httpx.AsyncClient, url: str, token: str) -> tuple[bytes, bool]:
    """One ``GET``: the body, at most ``MAX_LIST_BYTES``, and whether it was cut."""
    limit = MAX_LIST_BYTES
    async with http.stream(
        "GET",
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
        follow_redirects=False,
    ) as response:
        status = response.status_code
        if 300 <= status < 400:
            # The next hop would be chosen by the answer and get the token.
            raise _LevelFailed("redirect", status)
        if not 200 <= status < 300:
            # The body is not read: nothing in it is needed, and an error
            # page is somebody else's text.
            raise _LevelFailed("http_error", status)
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            # `identity` was asked for; inflating is what the cap must not allow.
            raise _LevelFailed("not_json")
        body = bytearray()
        # Raw: the bytes of the wire, never a decoded form of them.
        async for chunk in response.aiter_raw():
            body += chunk
            if len(body) > limit:
                return bytes(body[:limit]), True  # the rest is never read
    return bytes(body), False


async def _list_level(
    http: httpx.AsyncClient,
    config: DestinationServiceConfig,
    token: str,
    level: str,
    path: str,
    deadline: float,
) -> _Level:
    """One level, listed and reduced. Never raises (but for a cancel): a
    failure is the ``failed`` of the result, so the other level still counts."""
    failed: _LevelFailed
    detail = ""
    try:
        async with asyncio.timeout_at(deadline) as budget:
            raw, cut = await _read(http, f"{config.api_url}{path}", token)
            return _reduce_listing(raw, cut, level)
    except _LevelFailed as exc:
        # A new object: the raised one carries a traceback whose frames hold
        # the raw listing, and it is kept in the result.
        failed = _LevelFailed(exc.reason, exc.status)
    except TimeoutError:
        failed = _LevelFailed("timeout" if budget.expired() else "unreachable")
    except httpx.TimeoutException:
        failed = _LevelFailed("timeout")
    except httpx.HTTPError as exc:
        # The class only. h11 quotes bytes of the answer in its texts
        # ("illegal chunk header: ...", "Illegal header value ..."), and the
        # answer is the destination configurations.
        failed = _LevelFailed("unreachable")
        detail = type(exc).__name__
    except Exception as exc:  # a defect; its text may quote a configuration
        failed = _LevelFailed("failed")
        detail = type(exc).__name__
    logger.warning(
        "odata destinations: %s level not listed (reason=%s status=%s) %s",
        level,
        failed.reason,
        failed.status if failed.status is not None else "-",
        detail,
    )
    return _Level(level, failed=failed)


# The two texts of ``agents.destination``'s token request that carry more
# than a fixed sentence: the class of the httpx error followed by its text,
# and the status followed by the body. Only the class and the status are for
# the log.
_TOKEN_UNREACHABLE_RE = re.compile(
    r"could not reach the destination service token endpoint: ([A-Za-z_][A-Za-z0-9_]{0,63}):"
)
_TOKEN_STATUS_RE = re.compile(
    r"destination service token request returned ([0-9]{3})(?:$| \()"
)


def _token_failure_code(text: str) -> str:
    """What the log may say about a failed token request.

    Recognised positively, anything else is ``failed``: the text of a
    ``DestinationError`` can end in the token endpoint's body or in an httpx
    error text that quotes the answer -- and that answer is the service token.
    """
    found = _TOKEN_UNREACHABLE_RE.match(text)
    if found:
        return f"unreachable {found.group(1)}"
    found = _TOKEN_STATUS_RE.match(text)
    if found:
        return f"status {found.group(1)}"
    if text.endswith("is not JSON"):
        return "not_json"
    if text.endswith("carried no access_token"):
        return "no_access_token"
    return "failed"


_FIND_STATUS_RE = re.compile(r"destination service returned ([0-9]{3}) for ")
# code -> the fixed text an admin answer carries. Positive recognition of
# the texts `agents.destination` raises; anything else is `failed`.
_FAILURE_TEXTS = MappingProxyType({
    "not_found": "the destination does not exist in the subaccount of this app's "
    "destination service",
    "unreachable": "the destination service could not be reached",
    "token_unreachable": "the destination service's token endpoint could not be reached",
    "no_credential": "the destination returned no credential: check its Authentication type",
    "token_error": "the destination could not obtain a token from its target",
    "needs_user": "the destination propagates the signed-in user and has no credential "
    "of its own",
    "no_url": "the destination has no URL",
    "not_json": "the destination service's answer could not be read",
    "no_access_token": "the destination service's token endpoint returned no token",
    "failed": "the destination could not be resolved",
})


def destination_failure(text: str) -> tuple[str, str]:
    """``(code, fixed text)`` for the text of a ``DestinationError``.

    For an admin answer: the error's own text can quote the destination
    service's answer or a URL, so only a code that was recognised
    positively and a text written here leave the app. The text ends in
    ``(<code>)``; the detail belongs in the log.
    """
    token = _token_failure_code(text)
    found = _FIND_STATUS_RE.match(text)
    if token.startswith("unreachable"):
        code = "token_unreachable"
    elif token.startswith("status "):
        code = f"token_status_{token.removeprefix('status ')}"
    elif "token" in text and token in ("not_json", "no_access_token"):
        code = token
    elif found:
        code = f"status_{found.group(1)}"
    elif text.startswith("could not reach the destination service"):
        code = "unreachable"
    elif " does not exist in the subaccount" in text:
        code = "not_found"
    elif " returned no authentication token" in text:
        code = "no_credential"
    elif " could not obtain a token " in text:
        code = "token_error"
    elif " uses PrincipalPropagation" in text:
        code = "needs_user"
    elif text.endswith("has no URL configured"):
        code = "no_url"
    elif text.endswith("is not JSON"):
        code = "not_json"
    else:
        code = "failed"
    if code.startswith("token_status_"):
        message = "the destination service's token endpoint refused this app"
    elif code.startswith("status_"):
        message = "the destination service answered with an error"
    else:
        message = _FAILURE_TEXTS[code]
    return code, f"{message} ({code})"


def _level_text(level: str) -> str:
    return f"the {level} destinations could not be listed"


def _warning(result: _Level) -> dict[str, Any]:
    assert result.failed is not None
    return {
        "code": "level_unavailable",
        "level": result.level,
        "reason": result.failed.reason,
        "status": result.failed.status,
        "message": _level_text(result.level),
    }


async def list_destinations() -> dict[str, Any]:
    """The destinations of the instance and the subaccount, for a dropdown.

    Answer::

        {"items": [item], "truncated": bool, "skipped": int, "warnings": [w]}

    ``item``: ``name``, ``description``, ``type`` (``HTTP``, ``RFC``,
    ``MAIL``, ``LDAP``, ``TCP``, ``other``, or ``""`` when absent),
    ``proxy_type`` (``Internet``, ``OnPremise``, ``PrivateLink``, ``other``,
    ``""``), ``authentication`` (the type's identifier, ``other``, ``""``),
    ``level`` (``instance`` | ``subaccount``), ``user_propagating``,
    ``usable`` with ``reason`` (``null`` when usable, else ``invalid_name``
    or ``not_http``, the name first), ``notes`` (``["on_premise"]`` for a
    destination behind the Cloud Connector: selectable, but requests through
    the connectivity proxy are not possible yet) and ``shadows_subaccount``
    (an instance destination whose name also exists in the subaccount; the
    instance one is what "find destination" resolves, so it is the one
    listed). Sorted by name, case-insensitively.

    ``truncated``: a level's answer was cut at ``MAX_LIST_BYTES`` or more
    than ``MAX_ENTRIES`` destinations exist. ``skipped``: entries that had
    no plain name. ``warnings``: one ``level_unavailable`` entry (``level``,
    ``reason``, ``status``, ``message``) per level that could not be listed
    while the other could.

    :class:`ListError` when there is no list at all: 503
    ``no_destination_service``, 502 ``token_failed``, 502 ``list_failed``
    (both levels), 504 ``timeout``.
    """
    config = _config()
    if config is None:
        raise ListError(503, "no_destination_service", NO_BINDING_TEXT)
    budget = LIST_BUDGET_SECONDS
    deadline = asyncio.get_running_loop().time() + budget
    token_failure: str | None = None
    timed_out = False
    results: list[_Level] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(budget), transport=_transport(), follow_redirects=False
    ) as http:
        token = ""
        try:
            async with asyncio.timeout_at(deadline) as scope:
                token = await fetch_service_token(config, http)
        except DestinationError as exc:
            token_failure = _token_failure_code(str(exc)[:_MAX_LOG_CHARS])
        except TimeoutError:
            timed_out = scope.expired()
            token_failure = "timeout"
        if token_failure is None:
            results = list(
                await asyncio.gather(
                    _list_level(
                        http, config, token, LEVEL_INSTANCE, INSTANCE_DESTINATIONS_PATH, deadline
                    ),
                    _list_level(
                        http,
                        config,
                        token,
                        LEVEL_SUBACCOUNT,
                        SUBACCOUNT_DESTINATIONS_PATH,
                        deadline,
                    ),
                )
            )
    if token_failure is not None:
        # Raised outside the except blocks: no chained, unscrubbed error.
        logger.warning("odata destinations: no service token (%s)", token_failure)
        if timed_out:
            raise ListError(504, "timeout", TIMEOUT_TEXT)
        raise ListError(502, "token_failed", TOKEN_FAILED_TEXT)

    listed = [r for r in results if r.failed is None]
    if not listed:
        if all(r.failed is not None and r.failed.reason == "timeout" for r in results):
            raise ListError(504, "timeout", TIMEOUT_TEXT)
        if any(r.failed is not None and r.failed.reason == "failed" for r in results):
            raise ListError(500, "list_failed", _FAILED_TEXT)
        raise ListError(502, "list_failed", LIST_FAILED_TEXT)

    merged: dict[str, dict[str, Any]] = {}
    for result in results:  # the instance first: its name wins
        for stored, item in result.entries.items():
            if stored in merged:
                merged[stored]["shadows_subaccount"] = True
            else:
                merged[stored] = item
    items = sorted(merged.values(), key=lambda item: (item["name"].casefold(), item["name"]))
    truncated = any(r.truncated for r in listed) or len(items) > MAX_ENTRIES
    answer = {
        "items": items[:MAX_ENTRIES],
        "truncated": truncated,
        "skipped": sum(r.skipped for r in listed),
        "warnings": [_warning(r) for r in results if r.failed is not None],
    }
    logger.info(
        "odata destinations: listed (items=%d skipped=%d truncated=%s warnings=%d)",
        len(answer["items"]),
        answer["skipped"],
        truncated,
        len(answer["warnings"]),
    )
    return answer
