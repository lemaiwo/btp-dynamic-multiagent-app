"""A refused IDE request body is a 422 that never echoes the input.

FastAPI's default 422 carries each error's ``input``. A lone surrogate
(half a UTF-16 pair, e.g. a field cut inside an emoji) in that input cannot
be encoded as UTF-8, so the error answer itself failed: a 500. The IDE
routers answer with ``loc``/``msg``/``type`` only -- the shape the UI reads
field errors from (``IdeService.toError``). The session title is cleaned
like other plain text: control characters (NUL included), format
characters and lone surrogates are dropped.

Run:  python -m pytest tests/test_ide_validation_errors.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

(ROOT / "tests" / "_test_ide_validation_errors.db").unlink(missing_ok=True)
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_validation_errors.db'}",
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from agents.auth import require_developer  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide.models import IdeConventions, IdeSession  # noqa: E402
from agents.ide.review_routes import router as review_router  # noqa: E402
from agents.ide.routes import install_validation_handler  # noqa: E402
from agents.ide.routes import router as ide_router  # noqa: E402
from agents.ide.store import upsert_conventions  # noqa: E402

JSON = {"x-test-user": "alice", "content-type": "application/json"}


def _dev(request: Request) -> dict:
    return {"user_name": request.headers.get("x-test-user", "alice"),
            "scope": ["developer"]}


class _Other(BaseModel):
    name: str


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(review_router)
    app.include_router(ide_router)

    @app.post("/other")
    async def other(body: _Other) -> dict:  # not an IDE route
        return {"name": body.name}

    install_validation_handler(app)
    app.dependency_overrides[require_developer] = _dev
    return app


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeSession, IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
        await upsert_conventions(db, "T1", label="Target one", namespace="Z")
    yield


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_app()),
                           base_url="http://test") as c:
        yield c


@pytest.fixture
async def sid(client) -> str:
    r = await client.post("/ide/api/sessions", json={"title": "t", "target": "T1"},
                          headers=JSON)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _assert_plain_422(r) -> None:
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail
    for item in detail:
        assert set(item) == {"loc", "msg", "type"}, item
        assert "input" not in item


async def test_surrogate_in_patch_title_is_422_or_cleaned(client, sid):
    r = await client.patch(f"/ide/api/sessions/{sid}",
                           content=b'{"title":"\\ud83d"}', headers=JSON)
    # Only half a pair: nothing is left of the title.
    _assert_plain_422(r)
    assert r.json()["detail"][0]["loc"] == ["body", "title"]


async def test_surrogate_in_open_name_is_422(client, sid):
    r = await client.post(f"/ide/api/sessions/{sid}/open",
                          content=b'{"type":"CLAS","name":"\\ud83d"}', headers=JSON)
    _assert_plain_422(r)
    assert r.json()["detail"][0]["loc"] == ["body", "name"]


async def test_surrogate_in_create_title_is_dropped(client):
    r = await client.post("/ide/api/sessions",
                          content=b'{"title":"ab\\ud83dc","target":"T1"}',
                          headers=JSON)
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "abc"


async def test_refusal_keeps_loc_msg_type_for_the_ui(client):
    r = await client.post("/ide/api/sessions",
                          json={"title": "t", "target": "not a target!"},
                          headers=JSON)
    _assert_plain_422(r)
    assert r.json()["detail"][0]["loc"] == ["body", "target"]
    assert r.json()["detail"][0]["msg"]


@pytest.mark.parametrize("title, expected", [
    ("a\x00b", "ab"),
    ("\x00My\x07 session\x1b", "My session"),
    ("one​two‮", "onetwo"),
    ("line\nbreak\ttab", "line break tab"),
])
async def test_title_is_plain_text(client, sid, title, expected):
    r = await client.patch(f"/ide/api/sessions/{sid}", json={"title": title},
                           headers=JSON)
    assert r.status_code == 200, r.text
    assert r.json()["title"] == expected
    r = await client.post("/ide/api/sessions", json={"title": title, "target": "T1"},
                          headers=JSON)
    assert r.status_code == 201, r.text
    assert r.json()["title"] == expected


async def test_title_of_only_control_characters_is_422(client, sid):
    r = await client.patch(f"/ide/api/sessions/{sid}", json={"title": "\x00\x01"},
                           headers=JSON)
    _assert_plain_422(r)


async def test_other_routes_keep_fastapi_default(client):
    r = await client.post("/other", json={})
    assert r.status_code == 422
    assert "input" in r.json()["detail"][0]


def test_app_installs_the_handler():
    import app as app_module

    handler = app_module.app.exception_handlers.get(RequestValidationError)
    assert handler is not None
    assert getattr(handler, "__module__", "") == "agents.ide.routes"
