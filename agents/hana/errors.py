"""The closed error codes of ``builtin:hana``. Standard library only.

Every refusal and every failure of the toolset is a :class:`HanaError` with
one of :data:`ERROR_CODES` and the fixed text of :data:`MESSAGES`. Nothing a
driver, the destination service or the database says is ever part of it: a
driver's error text names the database host, the user and sometimes the
statement, so only the class of the failure and, for ``sql_error``, the
integer SQL code leave.
"""

from __future__ import annotations

from typing import Any

MESSAGES: dict[str, str] = {
    "destination_error": (
        "The system's destination could not be used: it must be an OnPremise "
        "destination with a user and a password, and the app needs its "
        "connectivity binding with a SOCKS5 port."),
    "proxy_refused": (
        "The connectivity proxy or the Cloud Connector refused the connection. "
        "Nothing was read; tell the user instead of retrying."),
    "unreachable": "The database could not be reached through the Cloud Connector.",
    "login_failed": (
        "The database refused the login of the destination's user. Nothing was "
        "read; tell the user instead of retrying."),
    "not_authorized": "The database user may not read this view.",
    "timeout": "The database did not answer in time.",
    "unknown_system": "No system of that name is configured for this agent.",
    "unknown_check": "No check of that name exists.",
    "invalid_argument": "An argument of the call is not valid.",
    "select_refused": "The statement is not a read of the allowed monitoring views.",
    "result_too_large": "The result is too large to return; narrow the query.",
    "sql_error": "The database refused the statement.",
    "call_failed": "The call could not be completed. Tell the user instead of retrying.",
}

ERROR_CODES: frozenset[str] = frozenset(MESSAGES)


class HanaError(Exception):
    """A failure of ``builtin:hana`` with a code of :data:`ERROR_CODES`.

    ``str()`` and ``.message`` are the fixed text of the code. ``sql_code``
    is kept only when it is a real integer (a HANA error code), never text.
    """

    def __init__(self, code: str, *, sql_code: Any = None) -> None:
        if code not in MESSAGES:
            raise ValueError("unknown builtin:hana error code")
        self.code = code
        self.message = MESSAGES[code]
        self.sql_code = (
            sql_code if isinstance(sql_code, int) and not isinstance(sql_code, bool)
            else None)
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.sql_code is not None:
            out["sql_code"] = self.sql_code
        return out
