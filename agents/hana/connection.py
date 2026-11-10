"""The connection layer of ``builtin:hana``: one ``hdbcli`` connection per
call, through the connectivity service's SOCKS5 proxy and the Cloud
Connector, inside a read-only transaction that is always rolled back.

**Why the transaction matters.** The database user of a system is the
destination's technical user, and on the first landscape that user has wide
privileges by the customer's decision. So the barrier against a write is
here, not in the database: autocommit off, ``SET TRANSACTION READ ONLY``
before anything else, a query timeout, and ``rollback()`` plus ``close()``
in ``finally`` on every path. Nothing here ever commits.

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
import logging
import re
import urllib.parse
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from agents.hana import BUILTIN_HANA_URL
from agents.hana.errors import HanaError

logger = logging.getLogger(__name__)

# probe 2026-11-10: SIB refuses TLS on its SQL ports; its destinations set hana.encrypt=false
DEFAULT_ENCRYPT = True
CONNECT_TIMEOUT_MS = 15000
QUERY_TIMEOUT_SECONDS = 20
CALL_DEADLINE_SECONDS = 45
"""How long a caller waits, for a free slot and its connection thread
together. Above connectTimeout (15 s) plus the query timeout (20 s); on
expiry the caller gets ``timeout`` while a thread that was started keeps its
slot until it really ends."""
MAX_CONCURRENT = 4
"""Connections at a time per process; a thread that runs on after its caller
was cancelled keeps its slot until it ends."""

READ_ONLY_STATEMENT = "SET TRANSACTION READ ONLY"
# The SELECT guard allows unqualified M_* names and does not check unqualified
# function calls; HANA resolves both in the session's current schema first,
# and the destination's user may have a schema of its own with look-alikes.
# Pinning SYS makes an unqualified name mean the monitoring view. Run after
# SET TRANSACTION READ ONLY (SET SCHEMA is a session setting, not a write):
# unverified on the landscape.
SCHEMA_STATEMENT = "SET SCHEMA SYS"
# hana.* destination properties this layer reads. hana.database is accepted
# and ignored: the first system's destination carries it, and a tenant is
# reached on its own mapped SQL port, so no databaseName is sent.
_HANA_PROPERTIES = frozenset({"hana.encrypt", "hana.sslHostNameInCertificate",
                              "hana.database"})

_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_MAX_LOCATION_ID = 100
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
        try:
            return await self._resolver.resolve_properties()
        except Exception as exc:  # noqa: BLE001 - mapped, class logged
            failure = type(exc).__name__
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


def _execute(system: str, kwargs: dict[str, Any], sql: str, binds: tuple,
             max_rows: int) -> tuple[list[str], list[tuple], bool]:
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
        step = "read_only"
        cursor.execute(READ_ONLY_STATEMENT)
        step = "set_schema"
        cursor.execute(SCHEMA_STATEMENT)
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
        rows = [tuple(r) for r in fetched]
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
    return columns, rows[:max_rows], len(rows) > max_rows


async def _in_slot(system: str, kwargs: dict[str, Any], sql: str, binds: tuple,
                   max_rows: int) -> tuple[list[str], list[tuple], bool]:
    slot = _semaphore()
    future: asyncio.Future | None = None

    def _done(fut: asyncio.Future) -> None:
        slot.release()
        if not fut.cancelled():
            fut.exception()  # retrieved: no "never retrieved" warning

    # One deadline for the wait for a free slot and the run together.
    try:
        async with asyncio.timeout(CALL_DEADLINE_SECONDS) as deadline:
            # Cancelled while queued, asyncio.Semaphore hands a slot it was
            # about to give on to the next waiter: nothing is taken or leaked.
            await slot.acquire()
            try:
                future = asyncio.ensure_future(
                    asyncio.to_thread(_execute, system, kwargs, sql, binds, max_rows))
            except BaseException:
                slot.release()
                raise
            future.add_done_callback(_done)
            # The thread cannot be cancelled: a cancelled or timed-out caller
            # leaves it running in its slot, so the cap counts connections,
            # not callers.
            return await asyncio.shield(future)
    except TimeoutError:
        if not deadline.expired():
            # Not this deadline. The thread wraps every failure in _Failed,
            # so no TimeoutError of its own can arrive here.
            raise
        if future is None:
            logger.warning("hana: no free connection within the call deadline "
                           "for system %s", system)
            raise HanaError("timeout") from None
        if future.done():
            # The thread ended in the same stretch as the deadline: its own
            # outcome stands (a _Failed propagates and is mapped by run_query).
            return future.result()
        logger.warning("hana: call deadline passed for system %s", system)
        raise HanaError("timeout") from None


async def run_query(system: HanaSystem, sql: str, binds: Sequence[Any],
                    max_rows: int) -> tuple[list[str], list[tuple], bool]:
    """Run one read on ``system``: ``(columns, rows, truncated)``.

    ``rows`` holds at most ``max_rows``; ``truncated`` says one more was
    there. The SQL is checked by the caller (the guard); this layer only
    makes sure it runs read-only and never commits.
    """
    if not (isinstance(max_rows, int) and not isinstance(max_rows, bool)
            and max_rows >= 1):
        raise HanaError("invalid_argument")
    if not isinstance(sql, str) or not sql or not isinstance(binds, (list, tuple)):
        raise HanaError("invalid_argument")
    props = await system.properties()
    target = settings_from_properties(props, system.name)
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
        return await _in_slot(system.name, kwargs, sql, tuple(binds), max_rows)
    except _Failed as failed:
        caught: _Failed | None = failed
    error = map_driver_error(caught.cause)
    logger.warning("hana: %s failed for system %s (%s -> %s)", caught.step,
                   system.name, _detail(caught.cause), error.code)
    raise error


__all__ = [
    "CALL_DEADLINE_SECONDS", "CONNECT_TIMEOUT_MS", "DEFAULT_ENCRYPT", "MAX_CONCURRENT",
    "QUERY_TIMEOUT_SECONDS",
    "HanaSystem", "HanaTarget", "Socks5Proxy", "connect_kwargs", "map_driver_error",
    "run_query", "settings_from_properties",
]
