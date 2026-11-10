"""A fake ``hdbcli`` for the ``builtin:hana`` tests.

``install(monkeypatch)`` puts a fake ``hdbcli`` package and its ``dbapi``
module into ``sys.modules``, so ``from hdbcli import dbapi`` inside
``agents/hana/connection.py`` gets the fake. The fake records every call
on the connection and the cursor; ``FakeDbapi.fail_at`` scripts an error at
one step, ``FakeDbapi.rows`` / ``description`` the result, and
``FakeDbapi.gate`` (a ``threading.Event``) holds ``connect`` until it is set.

Every error text the fake raises carries ``MARKER``: a test asserts the
marker never reaches an answer or a log record.
"""

from __future__ import annotations

import sys
import threading
import types
from dataclasses import dataclass, field
from typing import Any

MARKER = "DRIVER-TEXT-7f3a9c"
HOST = "vhost-db-7f3a9c.internal"
USER = "DBUSER_7F3A9C"
PASSWORD = "pw-7f3a9c-secret"
TOKEN = "tok-7f3a9c-connectivity"


class Error(Exception):
    """Like ``hdbcli.dbapi.Error``: ``args == (errorcode, errortext)``."""

    def __init__(self, errorcode: Any, errortext: str = "") -> None:
        super().__init__(errorcode, errortext)
        self.errorcode = errorcode
        self.errortext = errortext


class DatabaseError(Error):
    pass


class OperationalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


def driver_error(code: Any, text: str = "", cls: type[Error] = DatabaseError) -> Error:
    """An error whose text holds the host, the user and the marker."""
    return cls(code, f"{text} {HOST} {USER} {MARKER}".strip())


class FakeLob:
    """Like an hdbcli LOB: ``read(size)`` answers at most ``size`` characters
    (or bytes) of ``content``, and only while the connection is open; ``error``
    makes ``read`` fail with a text that holds the marker. ``reads`` records
    the sizes asked for."""

    def __init__(self, content: str | bytes, *, error: BaseException | None = None,
                 db: FakeDbapi | None = None) -> None:
        self.content = content
        self.error = error
        self.db = db
        self.reads: list[Any] = []

    def read(self, size: int = -1, position: int = 0) -> str | bytes:
        self.reads.append(size)
        if self.db is not None:
            self.db.calls.append(("lob_read",))
            if self.db.closed:
                raise driver_error(-10807, "LOB read after close")
        if self.error is not None:
            raise self.error
        return self.content if size is None or size < 0 else self.content[:size]


class FakeCursor:
    def __init__(self, db: FakeDbapi, conn: FakeConnection) -> None:
        self.db = db
        self.conn = conn
        self.description: tuple | None = None

    def _step(self, name: str, *args: Any) -> None:
        self.db.calls.append((name, *args))
        failure = self.db.fail_at.get(name)
        if failure is not None:
            raise failure

    def execute(self, sql: str, params: Any = None) -> None:
        if sql == "SET TRANSACTION READ ONLY":
            self._step("read_only")
            return
        if sql == "SET SCHEMA SYS":
            self._step("set_schema")
            return
        if params is None:
            self._step("execute", sql)
        else:
            self._step("execute", sql, params)
        self.description = self.db.description

    def setquerytimeout(self, seconds: int) -> None:
        self._step("setquerytimeout", seconds)

    def fetchmany(self, size: int) -> list[tuple]:
        self._step("fetchmany", size)
        return [tuple(r) for r in self.db.rows[:size]]

    def close(self) -> None:
        self._step("cursor_close")


class FakeConnection:
    def __init__(self, db: FakeDbapi) -> None:
        self.db = db

    def _step(self, name: str, *args: Any) -> None:
        self.db.calls.append((name, *args))
        failure = self.db.fail_at.get(name)
        if failure is not None:
            raise failure

    def setautocommit(self, value: bool) -> None:
        self._step("setautocommit", value)

    def cursor(self) -> FakeCursor:
        self._step("cursor")
        return FakeCursor(self.db, self)

    def rollback(self) -> None:
        self._step("rollback")

    def commit(self) -> None:  # never called by the code under test
        self._step("commit")

    def close(self) -> None:
        self.db.closed = True
        self._step("close")


@dataclass
class FakeDbapi:
    rows: list[tuple] = field(default_factory=list)
    description: tuple | None = (("A",), ("B",))
    fail_at: dict[str, BaseException] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)
    connect_kwargs: list[dict] = field(default_factory=list)
    gate: threading.Event | None = None
    inside: int = 0
    peak: int = 0
    closed: bool = False
    #: Passwords the fake database refuses with SQL code 10 (a wrong password).
    refuse_passwords: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def connect(self, **kwargs: Any) -> FakeConnection:
        with self._lock:
            self.inside += 1
            self.peak = max(self.peak, self.inside)
        try:
            self.connect_kwargs.append(dict(kwargs))
            self.calls.append(("connect",))
            if self.gate is not None:
                self.gate.wait(5)
            failure = self.fail_at.get("connect")
            if failure is not None:
                raise failure
            if kwargs.get("password") in self.refuse_passwords:
                raise driver_error(10, "authentication failed")
            self.closed = False
            return FakeConnection(self)
        finally:
            with self._lock:
                self.inside -= 1

    def steps(self) -> list[str]:
        return [c[0] for c in self.calls]


def fresh_login_state(monkeypatch: Any) -> None:
    """Give the test a login state of its own (the connection layer keeps it
    per process, keyed by database account): no breaker or confirmed login of an
    earlier test reaches it, and the old state is back after the test."""
    from agents.hana import connection

    monkeypatch.setattr(connection, "_logins", {})
    monkeypatch.setattr(connection, "_accounts", {})
    monkeypatch.setattr(connection, "_generation", 0)


def install(monkeypatch: Any, db: FakeDbapi | None = None) -> FakeDbapi:
    """Inject a fake ``hdbcli`` package backed by ``db``, with a fresh login
    state (:func:`fresh_login_state`)."""
    fresh_login_state(monkeypatch)
    db = db or FakeDbapi()
    dbapi = types.ModuleType("hdbcli.dbapi")
    dbapi.Error = Error
    dbapi.DatabaseError = DatabaseError
    dbapi.OperationalError = OperationalError
    dbapi.ProgrammingError = ProgrammingError
    dbapi.connect = db.connect
    package = types.ModuleType("hdbcli")
    package.dbapi = dbapi
    monkeypatch.setitem(sys.modules, "hdbcli", package)
    monkeypatch.setitem(sys.modules, "hdbcli.dbapi", dbapi)
    return db


def http_props(**overrides: Any) -> dict[str, str]:
    """The shape the landscape uses: Type HTTP, an http:// carrier URL, Basic."""
    props = {
        "Name": "hana-test",
        "Type": "HTTP",
        "URL": f"http://{HOST}:30015",
        "ProxyType": "OnPremise",
        "Authentication": "BasicAuthentication",
        "User": USER,
        "Password": PASSWORD,
    }
    props.update(overrides)
    return {k: v for k, v in props.items() if v is not None}


def tcp_props(**overrides: Any) -> dict[str, str]:
    props = {
        "Name": "hana-test",
        "Type": "TCP",
        "Address": f"{HOST}:30013",
        "ProxyType": "OnPremise",
        "User": USER,
        "Password": PASSWORD,
    }
    props.update(overrides)
    return {k: v for k, v in props.items() if v is not None}


class FakeResolver:
    """``resolve_properties()`` answering ``props`` or raising ``error``."""

    def __init__(self, props: dict[str, str] | None = None,
                 error: BaseException | None = None) -> None:
        self.props = props if props is not None else http_props()
        self.error = error
        self.calls = 0
        self.forced: list[bool] = []

    async def resolve_properties(self, *, force: bool = False) -> dict[str, str]:
        self.calls += 1
        self.forced.append(force)
        if self.error is not None:
            raise self.error
        return self.props


@dataclass
class _Config:
    proxy_host: str = "connectivity-proxy.internal"
    proxy_port: int = 20003
    socks5_port: int | None = 20004


class FakeTokens:
    """``ConnectivityTokens`` stand-in: ``app_token()`` and ``.config``."""

    def __init__(self, *, socks5_port: int | None = 20004,
                 error: BaseException | None = None) -> None:
        self.config = _Config(socks5_port=socks5_port)
        self.error = error

    async def app_token(self) -> str:
        if self.error is not None:
            raise self.error
        return TOKEN
