"""The SAP session of the InA client: CSRF token, cookies, whose they are.

``GetServerInfo`` with ``X-CSRF-Token: Fetch`` hands out the token and the
session cookie; every ``GetResponse`` sends them back. That pair is a SAP
session of whoever the destination signed in as, so it is kept per
destination and per signed-in user in the ``InaSessionStore``, never in the
shared HTTP client. Covered: one fetch per (destination, user) and its token
reused, by the describe and by every request of a run (VariableSubmit, read,
Close); two destinations, and the technical and a user entry of one
destination, never share a session; a cookie SAP rotates on a JSON answer
replaces the stored one, while one from a sign-in page is not stored; a 403
re-fetches once, then ``csrf_failed`` or ``forbidden``; a GetServerInfo that
hands out no token or no Analytics service, or is refused, sends no POST.
The run's sequence itself is ``tests/test_ina_run.py``.

The doubles of ``tests/ina_helpers.py`` (real ``destination_http_client``
over ``httpx.MockTransport``).

Run:  python -m pytest tests/test_ina_sessions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ina.client import InaSessionStore  # noqa: E402
from tests.ina_helpers import (  # noqa: E402
    ALICE,
    BOB,
    DESTINATION,
    INFO_PATH,
    RESPONSE_PATH,
    RUN,
    Sap,
    definition,
    fixture,
    make,
    refused,
    signed_in,
)

# --------------------------------------------------------------- CSRF and session


async def test_server_info_is_fetched_once_per_destination_and_user_and_its_token_reused():
    sap = Sap()
    sap.answers = [fixture("describe_ok.json"), fixture("run_ok.json")]
    store = InaSessionStore()
    await make(sap, store=store).describe()
    await make(sap, store=store, known=definition()).run(**RUN)
    (get,) = sap.gets
    assert get.url.path == INFO_PATH and get.headers["X-CSRF-Token"] == "Fetch"
    assert "cookie" not in get.headers
    assert sap.kinds == ["GET", "describe", "submit", "read", "close"]
    for post in sap.posts:
        assert post.url.path == RESPONSE_PATH
        assert post.headers["X-CSRF-Token"] == "csrf-1"
        assert post.headers["Cookie"] == "SAP_SESSIONID_XYZ_100=sess-1"
        assert post.headers["Content-Type"] == "application/json"
        # The destination's credential travels as for any destination request.
        assert post.headers["Authorization"] == "Bearer dest-token"
        assert post.url.host == "sia.example.test"


async def test_each_signed_in_user_has_a_session_of_their_own():
    sap = Sap()
    sap.answers = [fixture("run_ok.json")]
    store = InaSessionStore()
    with signed_in(ALICE):
        await make(sap, user_context=True, store=store, known=definition()).run(**RUN)
        await make(sap, user_context=True, store=store, known=definition()).run(**RUN)
    with signed_in(BOB):
        await make(sap, user_context=True, store=store, known=definition()).run(**RUN)
    assert len(sap.gets) == 2
    # Each run is submit, read and Close: six requests of Alice's, three of Bob's.
    tokens = [p.headers["X-CSRF-Token"] for p in sap.posts]
    assert tokens == ["csrf-1"] * 6 + ["csrf-2"] * 3
    assert [p.headers["Authorization"] for p in sap.posts] == [
        f"Bearer user-token-of-{ALICE}"
    ] * 6 + [f"Bearer user-token-of-{BOB}"] * 3


async def test_the_shared_http_client_keeps_no_cookie():
    sap = Sap()
    sap.answers = [fixture("describe_ok.json")]
    client = make(sap)
    await client.describe()
    assert len(client._http.cookies.jar) == 0


async def test_two_destinations_on_one_store_never_share_a_session():
    sap = Sap()
    sap.answers = [fixture("run_ok.json")]
    store = InaSessionStore()
    for name in (DESTINATION, "SIA_INA_2", DESTINATION, "SIA_INA_2"):
        await make(sap, destination=name, store=store, known=definition()).run(**RUN)
    assert len(sap.gets) == 2
    sent = [(p.headers["X-CSRF-Token"], p.headers["Cookie"]) for p in sap.posts]
    first = [("csrf-1", "SAP_SESSIONID_XYZ_100=sess-1")] * 3
    second = [("csrf-2", "SAP_SESSIONID_XYZ_100=sess-2")] * 3
    assert sent == first + second + first + second


async def test_the_technical_and_a_user_session_of_one_destination_are_apart():
    sap = Sap()
    sap.answers = [fixture("run_ok.json")]
    store = InaSessionStore()
    await make(sap, store=store, known=definition()).run(**RUN)
    with signed_in(ALICE):
        await make(sap, user_context=True, store=store, known=definition()).run(**RUN)
    await make(sap, store=store, known=definition()).run(**RUN)
    assert len(sap.gets) == 2
    assert [p.headers["X-CSRF-Token"] for p in sap.reads] == ["csrf-1", "csrf-2", "csrf-1"]
    assert [p.headers["Authorization"] for p in sap.reads] == [
        "Bearer dest-token",
        f"Bearer user-token-of-{ALICE}",
        "Bearer dest-token",
    ]
    assert [p.headers["X-CSRF-Token"] for p in sap.posts] == (
        ["csrf-1"] * 3 + ["csrf-2"] * 3 + ["csrf-1"] * 3
    )


def _with_cookie(response: httpx.Response, cookie: str) -> httpx.Response:
    response.headers["set-cookie"] = cookie
    return response


async def test_a_cookie_sap_rotates_on_an_answer_replaces_the_stored_one():
    sap = Sap()
    sap.answers = [
        _with_cookie(
            httpx.Response(200, json=fixture("run_ok.json")),
            "SAP_SESSIONID_XYZ_100=sess-rotated; path=/",
        ),
        fixture("run_ok.json"),
    ]
    store = InaSessionStore()
    await make(sap, store=store, known=definition()).run(**RUN)
    await make(sap, store=store, known=definition()).run(**RUN)
    assert len(sap.gets) == 1
    # Rotated on the first read: its Close and the whole second run send it.
    assert [p.headers["Cookie"] for p in sap.posts] == [
        "SAP_SESSIONID_XYZ_100=sess-1",
        "SAP_SESSIONID_XYZ_100=sess-1",
        *["SAP_SESSIONID_XYZ_100=sess-rotated"] * 4,
    ]
    assert {p.headers["X-CSRF-Token"] for p in sap.posts} == {"csrf-1"}


async def test_a_cookie_of_a_sign_in_page_is_not_stored():
    sap = Sap()
    sap.answers = [
        _with_cookie(
            httpx.Response(200, text="<html>Logon</html>", headers={"content-type": "text/html"}),
            "SAP_SESSIONID_XYZ_100=from-the-logon-page; path=/",
        ),
        fixture("run_ok.json"),
    ]
    store = InaSessionStore()
    error = await refused(make(sap, store=store, known=definition()).run(**RUN))
    assert error.code == "not_json"
    await make(sap, store=store, known=definition()).run(**RUN)
    assert sap.kinds == ["GET", *["submit", "read", "close"] * 2]
    assert {p.headers["Cookie"] for p in sap.posts} == {"SAP_SESSIONID_XYZ_100=sess-1"}


async def test_a_403_on_the_post_fetches_the_token_again_once():
    sap = Sap()
    sap.answers = [
        httpx.Response(403, headers={"x-csrf-token": "Required"}, text="CSRF validation failed"),
        fixture("run_ok.json"),
    ]
    out = await make(sap, known=definition()).run(**RUN)
    assert out["row_count"] == 3
    # The read was refused: the whole run again, in a new session.
    assert sap.kinds == ["GET", "submit", "read", "GET", "submit", "read", "close"]
    assert [p.headers["X-CSRF-Token"] for p in sap.reads] == ["csrf-1", "csrf-2"]


async def test_a_second_403_is_csrf_failed():
    sap = Sap()
    sap.answers = [httpx.Response(403, headers={"x-csrf-token": "Required"})]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "csrf_failed"
    assert sap.kinds == ["GET", "submit", "read", "GET", "submit", "read"]


async def test_a_403_that_is_no_csrf_refusal_is_forbidden_after_the_one_retry():
    sap = Sap()
    sap.answers = [httpx.Response(403, text="<html>no authorisation</html>")]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "forbidden"
    assert "html" not in error.message and "authorisation" not in error.message
    assert len(sap.reads) == 2 and sap.closes == []


async def test_server_info_without_a_token_is_csrf_failed():
    sap = Sap()
    sap.info_answer = lambda request: httpx.Response(200, json=fixture("server_info.json"))
    error = await refused(make(sap).describe())
    assert error.code == "csrf_failed"
    assert sap.posts == []


async def test_server_info_without_the_analytics_service_is_sap_error():
    sap = Sap()
    sap.server_info = {"Services": [{"Service": "Catalog", "Capabilities": []}]}
    error = await refused(make(sap).describe())
    assert error.code == "sap_error"
    assert sap.posts == []


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "unauthorized"), (403, "forbidden"), (404, "sap_error"), (500, "sap_error")],
)
async def test_a_refused_server_info_maps_to_its_code(status, code):
    sap = Sap()
    sap.info_answer = lambda request: httpx.Response(status, text="<html>secret page</html>")
    error = await refused(make(sap).describe())
    assert error.code == code
    assert "secret" not in error.message
    assert sap.posts == []


async def test_a_redirect_is_not_followed():
    sap = Sap()
    sap.info_answer = lambda request: httpx.Response(
        302, headers={"location": "https://idp.example.test/login?token=abc"}
    )
    error = await refused(make(sap).describe())
    assert error.code == "sap_error"
    assert len(sap.requests) == 1
    assert "idp" not in error.message and "abc" not in error.message
