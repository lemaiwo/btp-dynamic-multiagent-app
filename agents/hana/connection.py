"""The connection layer of ``builtin:hana``: one ``hdbcli`` connection per
call, through the connectivity service's SOCKS5 proxy and the Cloud
Connector, inside a read-only transaction that is always rolled back.

**Why the transaction matters.** The database user of a system is the
destination's technical user, and on the first landscape that user has wide
privileges by the customer's decision. So the barrier against a write is
here, not in the database: autocommit off, ``SET SCHEMA SYS``, then ``SET
TRANSACTION READ ONLY`` as the last statement before the query (so no session
statement can end or change that transaction after it), a query timeout, and
``rollback()`` plus ``close()`` in ``finally`` on every path. Nothing here
ever commits.

**The login protection.** Every call is one login, and HANA locks a user
after a few invalid attempts (reset only by a successful login); the user may
be a person's. The state is per process and keyed by the DATABASE ACCOUNT
(:func:`account_key`: a digest of location id, host, port and user), shared
by every :class:`HanaSystem` and every destination that resolves to that
account (:func:`_in_slot` describes the gate): until a login with the
current credential succeeded only ONE login is in flight; a login failure
opens a breaker that answers ``login_failed`` without connecting for
:data:`LOGIN_BREAKER_SECONDS`, and after it the destination is read fresh
(once per window) and no credential that failed on the account (the last
:data:`FAILED_CREDENTIALS_KEPT`, by :func:`credential_digest`) is tried
again; such a caller is answered without opening the window again, so
another destination of the account with the correct password still
connects. A credential evicted from those 8 could be tried once more. Two
virtual hosts mapped to one backend cannot be told apart and are counted as
two accounts: keep one destination per database user.

Two resets. :func:`soft_reset_login_state`, called by every registry reload
(a model change, an agent or catalogue save, a bundle import...), keeps the
failed credential and drops only the window and the confirmations, so a
corrected password is tried at once and the unchanged wrong one costs no
login. :func:`reset_login_state` forgets everything; only the admin's Reload
and Restart call it ("I fixed it", also the way out after an unlock in the
database with the same password). Worst case per account and app instance:
each distinct wrong credential costs 1 failed login (nothing confirmed), a
confirmed credential going stale in the database at most
:data:`MAX_CONCURRENT`; +1 per explicit Reload, Restart or app start; other
reloads add none.

**LOB values** (``NCLOB`` statement texts, ``BLOB``) are read in the worker
thread before the connection closes, each up to a cap and all of a call
within a total budget (:class:`_LobReader`).

**The destination** (resolved with the app's token, cached by
:class:`agents.destination.DestinationResolver`) carries the virtual host,
the SQL port and the credential, in one of two shapes: ``tcp`` (``Type
TCP``, ``Address <host>:<port>``) or ``http`` (``Type HTTP``, ``URL
http://<host>:<port>``, ``Authentication BasicAuthentication``; the URL is
only a carrier for host and port, so ``https`` or anything after the port is
refused). Both need ``ProxyType OnPremise``, ``User`` and ``Password``.
``hana.encrypt`` is exactly ``true`` or ``false`` (absent:
:data:`DEFAULT_ENCRYPT`); an encrypted login always validates the
certificate, there is no insecure switch. ``hana.sslHostNameInCertificate``
names the host the certificate is checked against.

**The proxy keywords** are the variant ``props`` that passed the landscape
probe (``scripts/probe_hana_connectivity.py``, gate G0): ``proxyHostname`` /
``proxyPort`` (the binding's SOCKS5 port), ``proxyUserName`` = the
application's connectivity token and, only with a Cloud Connector location
id, ``proxyPassword`` = ``"1." + base64(location id)``.

**What leaves.** Every failure is a :class:`agents.hana.errors.HanaError`
with a fixed text. A log line names the step, the system's slug and the
exception class or the integer SQL code: never the driver's text, the host,
the user or a row. ``hdbcli`` is imported inside the call, so an app that
never builds this toolset never loads it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import time
import urllib.parse
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from agents.hana import BUILTIN_HANA_URL
from agents.hana.errors import HanaError

logger = logging.getLogger(__name__)

# landscape probe 2026-11-10: the first on-premise system refuses TLS on its SQL ports;
# its destinations set hana.encrypt=false
DEFAULT_ENCRYPT = True
CONNECT_TIMEOUT_MS = 15000
# hdbcli's communicationTimeout (ms): a socket that stalls after the login
# fails the call instead of holding its slot until a restart. Unverified on the
# landscape: the keyword set the probe proved (gate G0) did not have it.
COMMUNICATION_TIMEOUT_MS = 30000
QUERY_TIMEOUT_SECONDS = 20
CALL_DEADLINE_SECONDS = 45
"""How long a caller waits, for a free slot and its connection thread
together. Above connectTimeout (15 s) plus the query timeout (20 s); on
expiry the caller gets ``timeout`` while a thread that was started keeps its
slot until it really ends."""
MAX_CONCURRENT = 4
"""Connections at a time per process; a thread that runs on after its caller
was cancelled keeps its slot until it ends."""
LOGIN_BREAKER_SECONDS = 900
"""How long a system answers ``login_failed`` without connecting after a
login failed (see the module docstring)."""
FAILED_CREDENTIALS_KEPT = 8
"""How many failed credential digests an account remembers (oldest out
first): two destinations of one account, or an old and a new password, must
not take turns overwriting one remembered failure."""
LOB_MAX_CHARS = 65536
"""A text LOB longer than this is cut, with :data:`LOB_CUT_MARKER`."""
LOB_MAX_BYTES = 65536
"""A binary LOB is read up to this many bytes (the answer shows its size only)."""
LOB_BUDGET_CHARS = 60_001
"""What the LOB values of one call may read together when the caller names no
budget: one more than the toolset's answer cap (``MAX_RESULT_CHARS`` of
``agents/hana/tools.py``), since an answer that holds more is refused anyway."""
LOB_CUT_MARKER = " …[cut]"
LOB_UNREADABLE = "<unreadable LOB>"

READ_ONLY_STATEMENT = "SET TRANSACTION READ ONLY"
# The SELECT guard allows unqualified M_* names and does not check unqualified
# function calls; HANA resolves both in the session's current schema first,
# and the destination's user may have a schema of its own with look-alikes.
# Pinning SYS makes an unqualified name mean the monitoring view. Run BEFORE
# SET TRANSACTION READ ONLY: should a session statement end the transaction or
# change its access mode, the read-only statement still comes last.
SCHEMA_STATEMENT = "SET SCHEMA SYS"
# hana.* destination properties this layer reads. hana.database is accepted
# and ignored: the first system's destination carries it, and a tenant is
# reached on its own mapped SQL port, so no databaseName is sent.
_HANA_PROPERTIES = frozenset({"hana.encrypt", "hana.sslHostNameInCertificate",
                              "hana.database"})

_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_MAX_LOCATION_ID = 100
# 10 is an invalid user or password. 414 and 663 open the breaker too, the
# safe direction: their meaning on HANA 2.0 SPS07 (taken here as "password
# must be changed" and "user is locked") is unverified on the landscape.
_LOGIN_CODES = frozenset({10, 414, 663})
_NOT_AUTHORIZED_CODES = frozenset({258})
_TIMEOUT_CODES = frozenset({1024, 613})
# hdbcli's client-side code of a connection that could not be made. The
# probe saw it for a refused CONNECT at the Cloud Connector and for a TLS
# login against a port without TLS (that one stays `unreachable`). Which
# words its text holds per cause was never recorded (texts are not printed),
# so the word classification in map_driver_error is not observed on a
# landscape; nor is any other negative code, which stays `sql_error` with
# its code (e.g. -10807).
_CONNECT_CODES = frozenset({-10709})


@dataclass(frozen=True, repr=False)
class HanaTarget:
    """Where and as whom to connect. ``repr`` shows the port and the TLS
    switch only: the host, the user, the password and the location id are
    never printed."""

    host: str
    port: int
    user: str
    password: str = field(compare=False)
    location_id: str | None = None
    encrypt: bool = DEFAULT_ENCRYPT
    ssl_host: str | None = None

    def __repr__(self) -> str:
        return f"HanaTarget(port={self.port}, encrypt={self.encrypt})"

    __str__ = __repr__


class Socks5Proxy(NamedTuple):
    """The connectivity binding's proxy host and SOCKS5 port."""

    host: str
    port: int


def _refuse(system: str, rule: str) -> HanaError:
    # The rule is fixed text of this module; the system is a checked slug.
    logger.warning("hana: destination of system %s refused: %s", system, rule)
    return HanaError("destination_error")


def _port(value: str) -> int | None:
    if not value.isdigit() or len(value) > 5:
        return None
    number = int(value)
    return number if 0 < number < 65536 else None


def _host_and_port(props: Mapping[str, Any], name: str) -> tuple[str, int]:
    kind = str(props.get("Type") or "")
    if kind == "TCP":
        host, sep, port = str(props.get("Address") or "").rpartition(":")
        number = _port(port)
        if not sep or number is None or not _HOST_RE.fullmatch(host):
            raise _refuse(name, "Address is not <host>:<port>")
        return host, number
    if kind == "HTTP":
        if props.get("Authentication") != "BasicAuthentication":
            raise _refuse(name, "an HTTP carrier needs Authentication BasicAuthentication")
        try:
            parts = urllib.parse.urlsplit(str(props.get("URL") or ""))
            number = parts.port
        except ValueError:
            raise _refuse(name, "URL is not http://<host>:<port>") from None
        if (parts.scheme != "http" or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or parts.path not in ("", "/") or not number
                or not parts.hostname or not _HOST_RE.fullmatch(parts.hostname)):
            raise _refuse(name, "URL is not http://<host>:<port>")
        return parts.hostname, number
    raise _refuse(name, "Type is neither TCP nor HTTP")


def settings_from_properties(props: Mapping[str, Any], name: str) -> HanaTarget:
    """The target of one system from its destination's raw properties.

    ``name`` is the system's slug, used in a log line only. Raises
    ``HanaError("destination_error")``; the log names the rule, never a
    value.
    """
    if props.get("ProxyType") != "OnPremise":
        raise _refuse(name, "ProxyType is not OnPremise")
    host, port = _host_and_port(props, name)
    user, password = props.get("User"), props.get("Password")
    if not (isinstance(user, str) and user and isinstance(password, str) and password):
        raise _refuse(name, "User and Password are required")
    location = props.get("CloudConnectorLocationId") or None
    if location is not None and (
            not isinstance(location, str) or len(location) > _MAX_LOCATION_ID):
        raise _refuse(name, "CloudConnectorLocationId is not a location id")
    if any(isinstance(k, str) and k.startswith("hana.") and k not in _HANA_PROPERTIES
           for k in props):
        raise _refuse(name, "a hana.* property other than hana.encrypt, "
                            "hana.sslHostNameInCertificate or hana.database")
    raw_encrypt = props.get("hana.encrypt")
    if raw_encrypt is None:
        encrypt = DEFAULT_ENCRYPT
    elif raw_encrypt in ("true", "false"):
        encrypt = raw_encrypt == "true"
    else:
        raise _refuse(name, "hana.encrypt is neither true nor false")
    ssl_host = props.get("hana.sslHostNameInCertificate")
    if ssl_host is not None and not (
            isinstance(ssl_host, str) and _HOST_RE.fullmatch(ssl_host)):
        raise _refuse(name, "hana.sslHostNameInCertificate is not a host name")
    return HanaTarget(host=host, port=port, user=user, password=password,
                      location_id=location, encrypt=encrypt, ssl_host=ssl_host)


def connect_kwargs(target: HanaTarget, token: str, socks5: Socks5Proxy) -> dict[str, Any]:
    """The keywords of ``hdbcli.dbapi.connect``: the probe's variant ``props``.

    Holds the password and the token: never printed or logged.
    """
    kwargs: dict[str, Any] = {
        "address": target.host,
        "port": target.port,
        "user": target.user,
        "password": target.password,
        "proxyHostname": socks5.host,
        "proxyPort": str(socks5.port),
        "proxyUserName": token,
        "connectTimeout": str(CONNECT_TIMEOUT_MS),
        # Not in the keyword set the landscape probe proved: unverified there.
        "communicationTimeout": str(COMMUNICATION_TIMEOUT_MS),
        # Always sent, as in the probe's passing login: never left to the
        # driver's default.
        "encrypt": "true" if target.encrypt else "false",
    }
    if target.location_id:
        kwargs["proxyPassword"] = "1." + base64.b64encode(
            target.location_id.encode()).decode()
    if target.encrypt:
        kwargs["sslValidateCertificate"] = "true"
        if target.ssl_host:
            kwargs["sslHostNameInCertificate"] = target.ssl_host
    return kwargs


def _driver_error_class() -> type[BaseException] | None:
    try:
        from hdbcli import dbapi
    except ImportError:
        return None
    return getattr(dbapi, "Error", None)


def _int_code(exc: BaseException) -> int | None:
    if isinstance(exc, OSError):
        return None  # an errno is no SQL code
    code = getattr(exc, "errorcode", None)
    if code is None and exc.args:
        code = exc.args[0]
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def map_driver_error(exc: BaseException) -> HanaError:
    """The closed code for a failure of the driver or the socket.

    The driver's text is read here only to tell a refused connection from a
    timeout; it never leaves this function.
    """
    driver_error = _driver_error_class()
    if driver_error is not None and isinstance(exc, driver_error):
        code = _int_code(exc)
        if code in _LOGIN_CODES:
            return HanaError("login_failed")
        if code in _NOT_AUTHORIZED_CODES:
            return HanaError("not_authorized")
        if code in _TIMEOUT_CODES:
            return HanaError("timeout")
        if code in _CONNECT_CODES:
            text = str(getattr(exc, "errortext", "") or "").lower()
            if "timed out" in text or "timeout" in text:
                return HanaError("timeout")
            if "refused" in text:
                return HanaError("proxy_refused")
            return HanaError("unreachable")
        return HanaError("sql_error", sql_code=code)
    if isinstance(exc, ConnectionRefusedError):
        return HanaError("proxy_refused")
    if isinstance(exc, TimeoutError):
        return HanaError("timeout")
    if isinstance(exc, OSError):
        return HanaError("unreachable")
    return HanaError("sql_error")


def _detail(exc: BaseException) -> str:
    """What a log line may say about a failure: the SQL code or the class."""
    code = _int_code(exc)
    return f"sql_code={code}" if code is not None else type(exc).__name__


def account_key(target: HanaTarget) -> str:
    """The database account a target logs in as: SHA-256 of location id,
    host, port and user. Kept in memory only, never logged or returned; the
    login state and the login lock are keyed by it, so two destinations of one
    account share one breaker. The password is not part of it (that is
    :func:`credential_digest`)."""
    text = "\0".join((target.location_id or "", target.host, str(target.port), target.user))
    return hashlib.sha256(text.encode()).hexdigest()


class HanaSystem:
    """One configured system: its slug, its destination and the clients that
    resolve it. The resolver and the connectivity tokens are built from the
    environment on first use when not given (tests pass fakes)."""

    def __init__(self, name: str, destination: str, *, resolver: Any = None,
                 tokens: Any = None) -> None:
        self.name = name
        self.destination = destination
        self._resolver = resolver
        self._tokens = tokens
        # The login state's epoch this object last read its destination at:
        # each failed login and each reset moves it on, and every system
        # object of that account then reads its destination fresh once (its
        # own resolver may cache the old password).
        self._read_epoch: tuple[int, int] = (0, 0)

    def __repr__(self) -> str:
        return f"HanaSystem({self.name})"

    async def properties(self) -> Mapping[str, Any]:
        from agents import destination as dest

        if self._resolver is None:
            try:
                self._resolver = dest.resolver_from_environment(
                    self.destination, server_key=BUILTIN_HANA_URL)
            except Exception as exc:  # noqa: BLE001 - mapped, class logged
                failure = type(exc).__name__
            else:
                failure = None
            if failure is not None:
                logger.warning("hana: destination binding for system %s failed (%s)",
                               self.name, failure)
                raise HanaError("destination_error")
        epoch = _read_epoch(self.destination)
        try:
            props = await self._resolver.resolve_properties(force=epoch != self._read_epoch)
        except Exception as exc:  # noqa: BLE001 - mapped, class logged
            failure = type(exc).__name__
        else:
            self._read_epoch = epoch
            return props
        logger.warning("hana: resolve destination of system %s failed (%s)",
                       self.name, failure)
        raise HanaError("destination_error")

    def tokens(self) -> Any:
        if self._tokens is None:
            from agents import destination as dest

            self._tokens = dest.connectivity_from_environment()
            if self._tokens is None:
                logger.warning("hana: no connectivity binding for system %s", self.name)
                raise HanaError("destination_error")
        return self._tokens


def _clock() -> float:
    """The breaker's clock (a seam for the tests)."""
    return time.monotonic()


class _Login:
    """What this process knows about the logins through one destination.

    Read and written on the event loop only, never in a worker thread. The
    credential is held as :func:`credential_digest` only, and ``repr`` shows
    none of it."""

    __slots__ = ("blocked_until", "confirmed", "epoch", "failed", "recheck_at")

    def __init__(self) -> None:
        self.confirmed: str | None = None
        # The digests that failed, oldest first, at most FAILED_CREDENTIALS_KEPT.
        self.failed: list[str] = []
        self.blocked_until: float | None = None
        # When a caller next makes every system of the account read its
        # destination fresh (once per window, without closing the window).
        self.recheck_at: float | None = None
        self.epoch = 0

    def __repr__(self) -> str:
        return (f"_Login(confirmed={self.confirmed is not None}, "
                f"failed={len(self.failed)}, epoch={self.epoch})")

    __str__ = __repr__


_logins: dict[str, _Login] = {}
"""Login state by :func:`account_key`."""
_accounts: dict[str, str] = {}
"""Destination name -> the account key it resolved to last: lets a call
answer ``login_failed`` while the window is open without reading the
destination. Before a destination's first resolve there is no entry (and
nothing to protect yet)."""
_generation = 0
"""Moved on by each reset: every system object reads its destination fresh."""
_login_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = \
    weakref.WeakKeyDictionary()


def credential_digest(user: str, password: str) -> str:
    """SHA-256 of ``user`` NUL ``password``: how the breaker tells one
    credential from another without keeping it. Never logged or returned."""
    return hashlib.sha256(f"{user}\0{password}".encode()).hexdigest()


def _login(account: str) -> _Login:
    state = _logins.get(account)
    if state is None:
        state = _logins[account] = _Login()
    return state


def _read_epoch(destination: str) -> tuple[int, int]:
    account = _accounts.get(destination)
    state = _logins.get(account) if account is not None else None
    return _generation, state.epoch if state is not None else 0


def _login_lock(account: str) -> asyncio.Lock:
    """The lock that keeps one login in flight per database account, per
    event loop (like the slots)."""
    loop = asyncio.get_running_loop()
    locks = _login_locks.get(loop)
    if locks is None:
        locks = _login_locks[loop] = {}
    lock = locks.get(account)
    if lock is None:
        lock = locks[account] = asyncio.Lock()
    return lock


def soft_reset_login_state() -> None:
    """What every registry reload does: drop each breaker window and each
    confirmed login, KEEP each failed credential, and have every system read
    its destination fresh. A corrected password is then tried at once (its
    digest differs), the unchanged wrong one is answered ``login_failed``
    without a login. Reloads happen for many reasons that have nothing to do
    with the database (a model change, an agent save), so forgetting the
    failure here would spend a login of the person's account at each.

    The states are changed in place and the locks stay: a login in flight
    keeps its lock, and its outcome lands in the same state."""
    global _generation
    _generation += 1
    for state in _logins.values():
        state.blocked_until = None
        state.confirmed = None
        state.epoch += 1


def reset_login_state() -> None:
    """Forget every breaker, failed credential and confirmed login. Only the
    admin's explicit Reload and Restart call this ("I fixed it": also the
    way out after the user was unlocked in the database with the same
    password); each costs at most one more login per stale credential.

    The locks stay: a login still in flight keeps its account's lock until
    its thread ends, so no second login starts beside it."""
    global _generation
    _generation += 1
    _logins.clear()
    _accounts.clear()


def _blocked(state: _Login) -> bool:
    until = state.blocked_until
    return until is not None and _clock() < until


def _gate_closed(account: str, digest: str) -> bool:
    """No login may be tried: the breaker's window is open, or this very
    credential is the one that failed."""
    state = _login(account)
    return _blocked(state) or digest in state.failed


def _confirmed(account: str, digest: str) -> bool:
    state = _login(account)
    return state.confirmed == digest and digest not in state.failed


def _close_gate(state: _Login) -> None:
    """(Re)open the breaker's window; every system object of the
    account reads its destination fresh on its next attempt."""
    state.blocked_until = state.recheck_at = _clock() + LOGIN_BREAKER_SECONDS
    state.epoch += 1


def _recheck_if_due(state: _Login) -> None:
    """Once per window after a failure, have every system of the account
    read its destination fresh (a corrected password is picked up even
    when the resolver caches the old one), without blocking anybody."""
    due = state.recheck_at
    if due is not None and _clock() >= due:
        state.recheck_at = _clock() + LOGIN_BREAKER_SECONDS
        state.epoch += 1


def _note_login_failed(account: str, digest: str, system: str) -> None:
    state = _login(account)
    if not _blocked(state):
        logger.warning("hana: login failed for system %s; no login is tried for "
                       "%d s, nor with this password again (login breaker; an admin's "
                       "Reload resets it)", system, LOGIN_BREAKER_SECONDS)
    if digest in state.failed:
        state.failed.remove(digest)
    state.failed.append(digest)
    del state.failed[:-FAILED_CREDENTIALS_KEPT]
    # Whichever credential was confirmed: the gate closes for all of them, so
    # the next login is one at a time again.
    state.confirmed = None
    _close_gate(state)


def _note_login_ok(account: str, digest: str) -> None:
    state = _login(account)
    # A login of a thread that started before this credential failed does
    # not lift the failure (the failure is the newer fact): only a full reset
    # does. Nor does any success touch the OTHER failed credentials.
    if digest not in state.failed:
        state.confirmed = digest


def _settle(account: str, digest: str, system: str, fut: asyncio.Future) -> None:
    """What a finished thread says about the login, on the event loop. Runs
    also for a thread whose caller left (deadline, cancellation)."""
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is None or (isinstance(exc, _Failed) and exc.step != "connect"):
        _note_login_ok(account, digest)  # connect returned: the login held
    elif isinstance(exc, _Failed) and map_driver_error(exc.cause).code == "login_failed":
        _note_login_failed(account, digest, system)


_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = \
    weakref.WeakKeyDictionary()


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(MAX_CONCURRENT)
    return slot


class _Failed(Exception):
    """A failure in the worker thread and the step it happened at. Never
    logged or returned: run_query maps it."""

    def __init__(self, step: str, cause: BaseException) -> None:
        super().__init__(step)
        self.step = step
        self.cause = cause


def _quiet(system: str, step: str, action: Any) -> None:
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - logged by class only
        logger.warning("hana: %s failed for system %s (%s)", step, system, _detail(exc))


class _LobBudget(Exception):
    """The LOB values of one call passed the call's budget (no text)."""


class _LobReader:
    """Reads the LOB values of one call within a total budget.

    hdbcli hands ``NCLOB`` / ``CLOB`` / ``BLOB`` back as objects whose
    ``read`` works only while the connection is open (``STATEMENT_STRING`` of
    ``M_EXPENSIVE_STATEMENTS`` / ``M_ACTIVE_STATEMENTS`` is ``NCLOB``), so
    this runs in the worker thread before ``rollback`` and ``close``. Each
    value is read up to ``per_value`` characters (bytes for a ``BLOB``; the
    answer shows a binary value's size only) and a longer text is cut with
    :data:`LOB_CUT_MARKER`. All values of the call together may take
    ``budget`` characters: a value that would pass it, or any further value
    once it is used up, raises :class:`_LobBudget` without another read (no
    more round trips; the caller answers ``result_too_large``). Why a total:
    ``top`` 500 rows of a SELECT that repeats a LOB column would otherwise
    read gigabytes before the answer's cap refuses it. A failing read is the
    fixed :data:`LOB_UNREADABLE`, never the driver's text. Unverified on the
    real driver: ``read(size)`` as the first positional argument and the
    type it returns.
    """

    def __init__(self, per_value: int, budget: int) -> None:
        self.per_value = per_value
        self.left = budget

    def value(self, value: Any) -> Any:
        read = getattr(value, "read", None)
        if not callable(read):
            return value
        if self.left <= 0:
            raise _LobBudget()
        try:
            content = read(min(self.per_value, self.left) + 1)
        except Exception:  # noqa: BLE001 - the driver's text never leaves
            return LOB_UNREADABLE
        if not isinstance(content, (str, bytes, bytearray, memoryview)):
            return LOB_UNREADABLE
        if len(content) > self.left:
            raise _LobBudget()
        kept = content[:self.per_value]
        self.left -= len(kept)
        if isinstance(content, str):
            return kept + LOB_CUT_MARKER if len(content) > self.per_value else kept
        return bytes(kept)


def _execute(system: str, kwargs: dict[str, Any], sql: str, binds: tuple,
             max_rows: int, lob: tuple[int, int] = (LOB_MAX_CHARS, LOB_BUDGET_CHARS)
             ) -> tuple[list[str], list[tuple], bool]:
    """The blocking part, in a worker thread. Rollback and close always."""
    from hdbcli import dbapi

    step = "connect"
    try:
        conn = dbapi.connect(**kwargs)
    except Exception as exc:  # noqa: BLE001 - mapped by the caller
        raise _Failed(step, exc) from None
    cursor = None
    try:
        step = "setautocommit"
        conn.setautocommit(False)
        step = "cursor"
        cursor = conn.cursor()
        step = "set_schema"
        cursor.execute(SCHEMA_STATEMENT)
        step = "read_only"
        cursor.execute(READ_ONLY_STATEMENT)
        step = "setquerytimeout"
        cursor.setquerytimeout(QUERY_TIMEOUT_SECONDS)
        step = "execute"
        if binds:
            cursor.execute(sql, binds)
        else:
            cursor.execute(sql)
        columns = [str(d[0]) for d in (cursor.description or ())]
        step = "fetch"
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        step = "lob_read"
        # Before rollback and close: a LOB cannot be read after them.
        lobs = _LobReader(*lob)
        rows = [tuple(lobs.value(v) for v in r) for r in fetched[:max_rows]]
    except Exception as exc:  # noqa: BLE001 - mapped by the caller
        failure: _Failed | None = _Failed(step, exc)
    else:
        failure = None
    finally:
        _quiet(system, "rollback", conn.rollback)
        if cursor is not None:
            _quiet(system, "cursor close", cursor.close)
        _quiet(system, "close", conn.close)
    if failure is not None:
        raise failure
    return columns, rows, truncated


def _spawn(coro: Any) -> asyncio.Future:
    """Schedule the task that starts the worker thread (a seam for the
    tests: what may happen between the slot check and the task's first
    step)."""
    return asyncio.ensure_future(coro)


async def _in_slot(system: str, account: str, digest: str, kwargs: dict[str, Any],
                   sql: str, binds: tuple, max_rows: int, lob: tuple[int, int]
                   ) -> tuple[list[str], list[tuple], bool]:
    """Wait for the login turn and a free slot, then run in a worker thread.

    **The login gate.** While no login with this credential has succeeded
    (:func:`_confirmed`), a caller first takes the account's lock, so only
    ONE such login is in flight; a caller that got the lock looks at the
    breaker again (the holder before it may have failed). A thread that
    starts while holding the lock keeps it until the thread has ended (its
    login may still be in flight after its caller left), and :func:`_settle`
    records the outcome before the lock and the slot are given back. A
    confirmed caller skips the lock and checks again after it got a slot.
    The task that starts the thread runs a loop step later, so it looks at
    the breaker once more right before ``to_thread`` (no ``await`` between
    that check and the thread's submission) and answers ``login_failed``
    without connecting when a failure was recorded meanwhile; slot and lock
    then go back through the same done path. What bounds the failures of a
    stale confirmed credential is the slots: only threads already running
    when the first failure is recorded can still fail, at most
    :data:`MAX_CONCURRENT`. The lock is always taken before a slot: a caller
    holding a slot never waits for the lock.
    """
    slot = _semaphore()
    lock = _login_lock(account)
    future: asyncio.Future | None = None

    async def _thread() -> tuple[list[str], list[tuple], bool]:
        if _gate_closed(account, digest):
            raise HanaError("login_failed")  # recorded since the slot check
        return await asyncio.to_thread(_execute, system, kwargs, sql, binds, max_rows, lob)

    def _start(holds_lock: bool) -> asyncio.Future:
        def _done(fut: asyncio.Future) -> None:
            try:
                _settle(account, digest, system, fut)
            finally:
                slot.release()
                if holds_lock:
                    lock.release()

        coro = _thread()
        try:
            started = _spawn(coro)
        except BaseException:
            coro.close()
            raise
        started.add_done_callback(_done)
        return started

    # One deadline for the login turn, the wait for a free slot and the run.
    try:
        async with asyncio.timeout(CALL_DEADLINE_SECONDS) as deadline:
            while future is None:
                holds_lock = False
                try:
                    if not _confirmed(account, digest):
                        # Cancelled or timed out while waiting, asyncio.Lock
                        # is left as it was: nothing is taken or leaked.
                        await lock.acquire()
                        holds_lock = True
                        if _confirmed(account, digest):
                            # A login succeeded meanwhile: run beside the others.
                            lock.release()
                            holds_lock = False
                    if _gate_closed(account, digest):
                        raise HanaError("login_failed")
                    # Cancelled while queued, asyncio.Semaphore hands a slot it
                    # was about to give on to the next waiter.
                    await slot.acquire()
                    if _gate_closed(account, digest) or not (
                            holds_lock or _confirmed(account, digest)):
                        # The credential failed, or its confirmation was lost,
                        # while this caller waited for the slot.
                        slot.release()
                        if _gate_closed(account, digest):
                            raise HanaError("login_failed")
                        continue
                    try:
                        future = _start(holds_lock)
                    except BaseException:
                        slot.release()
                        raise
                except BaseException:
                    if holds_lock and future is None:
                        lock.release()
                    raise
            # The thread cannot be cancelled: a cancelled or timed-out caller
            # leaves it running in its slot (and with the login lock), so the
            # cap counts connections, not callers.
            return await asyncio.shield(future)
    except TimeoutError:
        if not deadline.expired():
            # Not this deadline. The thread wraps every failure in _Failed,
            # so no TimeoutError of its own can arrive here.
            raise
        if future is None:
            logger.warning("hana: no login turn or free connection within the call "
                           "deadline for system %s", system)
            raise HanaError("timeout") from None
        if future.done():
            # The thread ended in the same stretch as the deadline: its own
            # outcome stands (a _Failed propagates and is mapped by run_query).
            return future.result()
        logger.warning("hana: call deadline passed for system %s", system)
        raise HanaError("timeout") from None


async def run_query(system: HanaSystem, sql: str, binds: Sequence[Any],
                    max_rows: int, *, lob_chars: int = LOB_MAX_CHARS,
                    lob_budget: int = LOB_BUDGET_CHARS
                    ) -> tuple[list[str], list[tuple], bool]:
    """Run one read on ``system``: ``(columns, rows, truncated)``.

    ``rows`` holds at most ``max_rows``; ``truncated`` says one more was
    there. The SQL is checked by the caller (the guard); this layer only
    makes sure it runs read-only and never commits. ``lob_chars`` caps each
    LOB value and ``lob_budget`` all of them together (:class:`_LobReader`);
    a call over the budget is ``result_too_large``.
    """
    if not (isinstance(max_rows, int) and not isinstance(max_rows, bool)
            and max_rows >= 1):
        raise HanaError("invalid_argument")
    if not isinstance(sql, str) or not sql or not isinstance(binds, (list, tuple)):
        raise HanaError("invalid_argument")
    known = _accounts.get(system.destination)
    if known is not None:
        if _blocked(_login(known)):
            raise HanaError("login_failed")  # the window is open: nothing is read
        _recheck_if_due(_login(known))
    props = await system.properties()
    target = settings_from_properties(props, system.name)
    digest = credential_digest(target.user, target.password)
    account = account_key(target)
    _accounts[system.destination] = account
    state = _login(account)  # a reset may have replaced it meanwhile
    if digest in state.failed:
        # The destination still holds a credential that failed on this
        # account: no login. The window is NOT opened again: it is shared by
        # the account, and another destination of it may hold the correct
        # password.
        logger.warning("hana: the destination of system %s still holds the password "
                       "that failed; no login tried (login breaker)", system.name)
        raise HanaError("login_failed")
    tokens = system.tokens()
    config = getattr(tokens, "config", None)
    socks5_port = getattr(config, "socks5_port", None)
    if not socks5_port:
        logger.warning("hana: connectivity binding has no SOCKS5 port (system %s)",
                       system.name)
        raise HanaError("destination_error")
    try:
        token = await tokens.app_token()
    except Exception as exc:  # noqa: BLE001 - mapped, class logged
        failure = type(exc).__name__
    else:
        failure = None
    if failure is not None:
        logger.warning("hana: connectivity token for system %s failed (%s)",
                       system.name, failure)
        raise HanaError("proxy_refused")
    kwargs = connect_kwargs(target, token, Socks5Proxy(config.proxy_host, socks5_port))
    try:
        return await _in_slot(system.name, account, digest, kwargs, sql,
                              tuple(binds), max_rows, (lob_chars, lob_budget))
    except _Failed as failed:
        caught: _Failed | None = failed
    if isinstance(caught.cause, _LobBudget):
        logger.warning("hana: LOB values of system %s passed the call's budget; "
                       "reading stopped (result_too_large)", system.name)
        raise HanaError("result_too_large")
    error = map_driver_error(caught.cause)
    logger.warning("hana: %s failed for system %s (%s -> %s)", caught.step,
                   system.name, _detail(caught.cause), error.code)
    # A login failure was recorded by _settle when the thread ended.
    raise error


__all__ = [
    "CALL_DEADLINE_SECONDS", "COMMUNICATION_TIMEOUT_MS", "CONNECT_TIMEOUT_MS",
    "DEFAULT_ENCRYPT", "LOB_BUDGET_CHARS", "LOB_MAX_BYTES", "LOB_MAX_CHARS",
    "FAILED_CREDENTIALS_KEPT", "LOGIN_BREAKER_SECONDS",
    "MAX_CONCURRENT", "QUERY_TIMEOUT_SECONDS",
    "HanaSystem", "HanaTarget", "Socks5Proxy", "account_key", "connect_kwargs",
    "credential_digest", "map_driver_error", "reset_login_state", "run_query",
    "settings_from_properties", "soft_reset_login_state",
]
