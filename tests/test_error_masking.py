"""Exception text that reaches the model, a tool card or a stored run row.

An exception's text can carry the request URL as it was sent (httpx puts it
in every ``HTTPStatusError``), and with it a credential that a destination
appends as ``URL.queries.*`` or a ``?token=`` in a configured MCP URL. The
class name and the rest of the message stay: admins debug from them.
Every credential below is made up.

Run:  python -m pytest tests/test_error_masking.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

from sqlalchemy.engine import make_url  # noqa: E402

from agents import db as agents_db  # noqa: E402
from agents.errors import describe_exception, exception_class, mask_text  # noqa: E402

SECRET = "s3cr3t-Query-Value"


def _status_error() -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST", f"https://mcp.example.internal/mcp?sap-client=100&apikey={SECRET}"
    )
    response = httpx.Response(401, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("raise_for_status did not raise")


# --- the helper -------------------------------------------------------------


def test_an_http_status_error_keeps_class_and_status_but_not_the_url():
    text = describe_exception(_status_error())
    assert text.startswith("HTTPStatusError: ")
    assert "401" in text and "Unauthorized" in text
    assert SECRET not in text and "mcp.example.internal" not in text
    assert "[url]" in text


@pytest.mark.parametrize("raw", [
    f"Authorization: Bearer {SECRET}",
    f"authorization={SECRET}",
    f"sent Bearer {SECRET} to the server",
    f"retry with token={SECRET}&x=1",
    f"login failed password={SECRET}",
    f"client_secret={SECRET}",
    f"GET /api/items?{SECRET}=1 failed",
    f"GET /api/items?filter={SECRET} failed",
    f"postgres connection user:{SECRET}@db.internal refused",
    f"//other.host/path?{SECRET}",
    "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz here",
])
def test_credentials_are_masked(raw):
    out = mask_text(raw)
    assert SECRET not in out and "c2lnbmF0dXJlMTIz" not in out, out


@pytest.mark.parametrize("plain", [
    "Run exceeded its 10:25 window",
    "step 3 at 10:25:01 failed: division by zero",
    "Peer Tool failed: mcp down",
    "Object ZCL_X does not exist",
    "ratio 3/4 and a=b without a query",
    "Is it ready? yes",
])
def test_ordinary_text_is_left_alone(plain):
    assert mask_text(plain) == plain


def test_the_message_is_cut():
    out = describe_exception(RuntimeError("x" * 5000))
    assert out.startswith("RuntimeError: ")
    assert len(out) <= 420


def test_a_group_is_unwrapped_and_each_member_masked():
    group = ExceptionGroup("tasks", [RuntimeError("boom"), _status_error()])
    out = describe_exception(group)
    assert "RuntimeError: boom" in out and "HTTPStatusError" in out
    assert SECRET not in out


def test_exception_class_names_the_class_only():
    assert exception_class(_status_error()) == "HTTPStatusError"


# --- the model-facing sites ---------------------------------------------------


async def test_resilient_tool_call_masks_the_url():
    from agents.shared import _resilient_tool_call

    class Ctx:
        retry = 0
        max_retries = 3

    async def failing(name, args, metadata):
        raise _status_error()

    out = await _resilient_tool_call(Ctx(), failing, "t", {})
    assert "HTTPStatusError" in out and "401" in out
    assert SECRET not in out and "mcp.example.internal" not in out


def test_delegation_error_text_masks_the_url():
    from agents.registry import _format_error

    out = _format_error(ExceptionGroup("tg", [_status_error()]))
    assert "HTTPStatusError" in out
    assert SECRET not in out


# --- B-core-2: the Postgres URL from a binding --------------------------------


@pytest.mark.parametrize("user,password", [
    ("pg@user", "p@ss"),
    ("u/s:er%", "p/a:s%s@w?o#rd"),
    ("plain", "pg-s3cret"),
])
def test_postgres_binding_with_special_characters_round_trips(user, password):
    target = agents_db._postgres_target({
        "hostname": "pg.example.internal", "port": 5432, "username": user,
        "password": password, "dbname": "appdb", "sslrootcert": "PGCA",
    })
    url = make_url(target.url)
    assert url.drivername == "postgresql+asyncpg"
    assert (url.username, url.password) == (user, password)
    assert (url.host, url.port, url.database) == ("pg.example.internal", 5432, "appdb")
    assert dict(url.query) == {"ssl": "require"}
    engine_url, connect_args = agents_db._engine_settings(target)
    parsed = make_url(engine_url)
    assert (parsed.username, parsed.password, parsed.host) == (
        user, password, "pg.example.internal"
    )
    assert not parsed.query and set(connect_args) == {"ssl"}


def test_postgres_port_given_as_text_is_accepted():
    target = agents_db._postgres_target({
        "host": "h", "port": "6543", "username": "u", "password": "p", "database": "d",
    })
    assert make_url(target.url).port == 6543
