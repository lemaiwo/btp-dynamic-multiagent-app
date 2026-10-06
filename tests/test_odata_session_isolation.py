"""CSRF tokens and SAP session cookies stay with the user they belong to.

The store under test (``agents.odata.session.CsrfSessionStore``) is the only
place a SAP session cookie may live: the HTTP client of a service is shared
by every user of the agent and keeps no cookie at all. These tests drive the
store the way the write path will -- fetch a token, then send a modifying
request with the token and an explicit ``Cookie`` header -- against a SAP
double that knows which token and cookie it handed to which user, with runs
of different users overlapping on one store and one HTTP client.

No network, no database.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
for _v in (
    "DESTINATION_CLIENT_ID", "DESTINATION_CLIENT_SECRET", "DESTINATION_URI",
    "DESTINATION_TOKEN_URL", "DESTINATION_UAA_URL", "CONNECTIVITY_CLIENT_ID",
    "CONNECTIVITY_CLIENT_SECRET", "CONNECTIVITY_TOKEN_URL", "CONNECTIVITY_PROXY_HOST",
    "CONNECTIVITY_PROXY_PORT", "CONNECTIVITY_PP_MODE",
):
    os.environ.pop(_v, None)

from agents import auth  # noqa: E402
from agents.destination import DestinationError  # noqa: E402
from agents.destination_auth import DestinationUserRequired  # noqa: E402
from agents.odata import session as session_mod  # noqa: E402
from agents.odata.session import (  # noqa: E402
    SESSION_TTL_SECONDS,
    TECHNICAL,
    CsrfSession,
    CsrfSessionStore,
    NoCookieJar,
    SessionIdentityError,
    cookies_from_response,
)

ALICE = "alice@example.com"
BOB = "bob@example.com"
USER_DEST = "S4_ODATA_USER"
TECH_DEST = "S4_ODATA_TECH"
BASE = "https://s4.internal:44300/sap/opu/odata/sap/SRV"
COOKIE_NAME = "SAP_SESSIONID_XXX_100"


class Sap:
    """SAP as far as CSRF goes: one session (token + cookie) per caller.

    The caller is whoever the ``Authorization`` header names -- the header a
    destination would have produced for that user. A modifying request is
    accepted only with the token *and* the cookie SAP handed to that caller;
    anything else is a 403, and is recorded as a crossing when the token or
    cookie belongs to somebody else.
    """

    def __init__(self, *, parallel_sessions: bool = False) -> None:
        # parallel_sessions: an earlier session of a caller stays valid next to
        # a newer one (real SAP allows several); off, only the latest counts.
        self.parallel_sessions = parallel_sessions
        self.history: dict[str, set[tuple[str, str]]] = {}
        self.issued: dict[str, tuple[str, str]] = {}   # caller -> (token, cookie value)
        self.fetches: dict[str, int] = {}
        self.writes: list[tuple[str, str, str]] = []   # (caller, token, cookie header)
        self.crossed: list[tuple[str, str, str]] = []
        self.generation: dict[str, int] = {}
        self.transport = httpx.MockTransport(self._handle)

    def expire(self, caller: str) -> None:
        """SAP forgets this caller's session (a timeout on the back end)."""
        self.issued.pop(caller, None)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        caller = request.headers.get("Authorization", "").removeprefix("Bearer ")
        await asyncio.sleep(0)   # let other runs in, as a real round trip would
        if request.headers.get("X-CSRF-Token") == "Fetch":
            n = self.generation[caller] = self.generation.get(caller, 0) + 1
            self.fetches[caller] = self.fetches.get(caller, 0) + 1
            token, cookie = f"T-{caller}-{n}", f"S-{caller}-{n}"
            self.issued[caller] = (token, cookie)
            self.history.setdefault(caller, set()).add((token, cookie))
            return httpx.Response(
                200,
                headers=[
                    ("X-CSRF-Token", token),
                    ("Set-Cookie", f"{COOKIE_NAME}={cookie}; path=/; secure; HttpOnly"),
                    ("Set-Cookie", "sap-usercontext=sap-client=100; path=/"),
                ],
            )
        if request.method == "GET":
            return httpx.Response(200, headers={"Set-Cookie": f"{COOKIE_NAME}=S-stray; path=/"})
        token = request.headers.get("X-CSRF-Token", "")
        cookie = request.headers.get("Cookie", "")
        self.writes.append((caller, token, cookie))
        expected = self.issued.get(caller)
        if expected and token == expected[0] and f"{COOKIE_NAME}={expected[1]}" in cookie:
            return httpx.Response(201, json={"d": {}})
        if self.parallel_sessions and any(
            token == t and f"{COOKIE_NAME}={c};" in f"{cookie};"
            for t, c in self.history.get(caller, ())
        ):
            return httpx.Response(201, json={"d": {}})
        if any(
            other != caller and (token == t or c in cookie)
            for other, (t, c) in self.issued.items()
        ):
            self.crossed.append((caller, token, cookie))
        return httpx.Response(403, headers={"X-CSRF-Token": "Required"})


class as_user:
    """Bind a signed-in user for the current task, as the JWT middleware does."""

    def __init__(self, principal: str | None, jwt: str | None = None) -> None:
        self.principal = principal
        self.jwt = jwt if jwt is not None else f"jwt-of-{principal}"

    def __enter__(self) -> None:
        self._jwt = auth.current_jwt.set(self.jwt)
        self._principal = auth.current_principal.set(self.principal)

    def __exit__(self, *exc: object) -> None:
        auth.current_principal.reset(self._principal)
        auth.current_jwt.reset(self._jwt)


def fetcher(http: httpx.AsyncClient, caller: str):
    """The token fetch the write path does, as ``caller`` (the destination's header)."""

    async def fetch() -> CsrfSession:
        response = await http.get(
            f"{BASE}/", headers={"Authorization": f"Bearer {caller}", "X-CSRF-Token": "Fetch"}
        )
        return CsrfSession.fresh(response.headers["X-CSRF-Token"], cookies_from_response(response))

    return fetch


async def write_as(
    store: CsrfSessionStore,
    http: httpx.AsyncClient,
    principal: str,
    *,
    destination: str = USER_DEST,
    pause: int = 1,
) -> int:
    """One modifying call of one user, retried once after a 403 like the client will."""
    with as_user(principal):
        key = store.key(destination, True)
        for _ in range(2):
            current = await store.get(key, fetcher(http, principal))
            for _ in range(pause):           # another user's run gets in between fetch and use
                await asyncio.sleep(0)
            response = await http.post(
                f"{BASE}/Items",
                json={},
                headers={
                    "Authorization": f"Bearer {principal}",
                    "X-CSRF-Token": current.token,
                    "Cookie": current.cookie_header(),
                },
            )
            if response.status_code != 403:
                break
            store.drop(key)
        return response.status_code


def client(sap: Sap) -> httpx.AsyncClient:
    return httpx.AsyncClient(cookies=NoCookieJar(), transport=sap.transport)


# -- per-user sessions ------------------------------------------------------

async def test_two_users_get_their_own_token_and_cookies():
    sap, store = Sap(), CsrfSessionStore()
    async with client(sap) as http:
        with as_user(ALICE):
            alice_key = store.key(USER_DEST, True)
            alice = await store.get(alice_key, fetcher(http, ALICE))
        with as_user(BOB):
            bob_key = store.key(USER_DEST, True)
            bob = await store.get(bob_key, fetcher(http, BOB))

    assert alice_key != bob_key
    assert alice_key[0] == bob_key[0] == USER_DEST
    assert alice.token == f"T-{ALICE}-1" and bob.token == f"T-{BOB}-1"
    assert alice.cookies == {COOKIE_NAME: f"S-{ALICE}-1", "sap-usercontext": "sap-client=100"}
    assert bob.cookies[COOKIE_NAME] == f"S-{BOB}-1"
    assert alice.cookie_header() == f"{COOKIE_NAME}=S-{ALICE}-1; sap-usercontext=sap-client=100"
    assert len(store) == 2


async def test_one_fetch_per_key_under_concurrent_gets():
    sap, store = Sap(), CsrfSessionStore()
    async with client(sap) as http:
        with as_user(ALICE):
            key = store.key(USER_DEST, True)
            got = await asyncio.gather(*(store.get(key, fetcher(http, ALICE)) for _ in range(10)))
    assert sap.fetches == {ALICE: 1}
    assert {s.token for s in got} == {f"T-{ALICE}-1"}


async def test_overlapping_runs_do_not_cross():
    sap, store = Sap(), CsrfSessionStore()
    async with client(sap) as http:
        rounds = []
        for i in range(20):
            rounds.append(write_as(store, http, ALICE, pause=1 + i % 3))
            rounds.append(write_as(store, http, BOB, pause=1 + (i + 1) % 3))
        statuses = await asyncio.gather(*rounds)
        assert len(http.cookies.jar) == 0

    assert statuses == [201] * 40
    assert sap.crossed == []
    assert len(sap.writes) == 40
    for caller, token, cookie in sap.writes:
        assert token == f"T-{caller}-1"
        assert cookie.startswith(f"{COOKIE_NAME}=S-{caller}-1")
        other = BOB if caller == ALICE else ALICE
        assert other not in token and other not in cookie
    assert sap.fetches == {ALICE: 1, BOB: 1}


async def test_overlapping_runs_with_expiring_sessions_do_not_cross():
    """SAP drops a session mid-flight; the 403 retry of one user stays that user's."""
    sap, store = Sap(), CsrfSessionStore()

    async def saboteur() -> None:
        for _ in range(6):
            for _ in range(7):
                await asyncio.sleep(0)
            sap.expire(ALICE)

    async with client(sap) as http:
        rounds = [saboteur()]
        for i in range(20):
            rounds.append(write_as(store, http, ALICE, pause=1 + i % 4))
            rounds.append(write_as(store, http, BOB, pause=1 + (i + 2) % 4))
        results = await asyncio.gather(*rounds)

    assert sap.crossed == []
    for caller, token, cookie in sap.writes:
        assert token.startswith(f"T-{caller}-")
        assert cookie.startswith(f"{COOKIE_NAME}=S-{caller}-")
    assert sap.fetches[BOB] == 1          # nothing alice went through touched bob's entry
    assert sap.fetches[ALICE] > 1         # the sabotage did happen
    assert all(status == 201 for status in results[2::2])   # every one of bob's writes


async def test_a_403_invalidation_drops_only_that_users_entry():
    sap, store = Sap(), CsrfSessionStore()
    async with client(sap) as http:
        assert await write_as(store, http, ALICE) == 201
        assert await write_as(store, http, BOB) == 201
        with as_user(BOB):
            bob_key = store.key(USER_DEST, True)
            bob_before = await store.get(bob_key, fetcher(http, BOB))

        sap.expire(ALICE)
        assert await write_as(store, http, ALICE) == 201      # 403, drop, fetch again, 201

        with as_user(BOB):
            bob_after = await store.get(bob_key, fetcher(http, BOB))
        assert await write_as(store, http, BOB) == 201

    assert sap.fetches == {ALICE: 2, BOB: 1}
    assert bob_after.token == bob_before.token == f"T-{BOB}-1"
    assert bob_after.cookies == bob_before.cookies
    assert sap.crossed == []


async def test_dropping_an_unknown_key_is_harmless():
    store = CsrfSessionStore()
    store.drop((USER_DEST, "user:nobody"))
    assert len(store) == 0


async def test_a_returned_session_is_a_private_copy():
    """A caller that edits what it got does not change what the next call gets."""
    sap, store = Sap(), CsrfSessionStore()
    async with client(sap) as http:
        with as_user(ALICE):
            key = store.key(USER_DEST, True)
            first = await store.get(key, fetcher(http, ALICE))
            first.cookies[COOKIE_NAME] = "tampered"
            first.cookies["extra"] = "x"
            second = await store.get(key, fetcher(http, ALICE))
    assert second.cookies == {COOKIE_NAME: f"S-{ALICE}-1", "sap-usercontext": "sap-client=100"}


async def test_a_failed_fetch_is_not_cached_and_does_not_block_the_key():
    store = CsrfSessionStore()
    calls = 0

    async def failing() -> CsrfSession:
        nonlocal calls
        calls += 1
        raise RuntimeError("no token today")

    async def working() -> CsrfSession:
        return CsrfSession.fresh("T-ok", {})

    with as_user(ALICE):
        key = store.key(USER_DEST, True)
        with pytest.raises(RuntimeError):
            await store.get(key, failing)
        assert len(store) == 0
        assert (await asyncio.wait_for(store.get(key, working), 1)).token == "T-ok"
    assert calls == 1


# -- the shared client keeps no cookie --------------------------------------

async def test_client_cookie_jar_stays_empty():
    sap = Sap()
    async with httpx.AsyncClient(cookies=NoCookieJar(), transport=sap.transport) as http:
        first = await http.get("https://s4.example/x")
        assert first.headers["Set-Cookie"].startswith(COOKIE_NAME)     # SAP did try
        assert len(http.cookies.jar) == 0
        assert isinstance(http.cookies.jar, NoCookieJar)               # httpx kept our jar
        second = await http.get("https://s4.example/x")
        assert "Cookie" not in second.request.headers
        http.cookies.set(COOKIE_NAME, "S-forced", domain="s4.example")  # even on purpose
        assert len(http.cookies.jar) == 0
        third = await http.get("https://s4.example/x")
        assert "Cookie" not in third.request.headers


async def test_an_explicit_cookie_header_is_the_only_cookie_sent():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Cookie", ""))
        return httpx.Response(200, headers={"Set-Cookie": f"{COOKIE_NAME}=S-from-sap; path=/"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(cookies=NoCookieJar(), transport=transport) as http:
        await http.get("https://s4.example/x", headers={"Cookie": "a=1"})
        await http.get("https://s4.example/x")
    assert seen == ["a=1", ""]


def test_cookies_from_response_keeps_name_and_value_only():
    response = httpx.Response(
        200,
        headers=[
            ("Set-Cookie", "a=1; Path=/; Secure; HttpOnly"),
            ("Set-Cookie", "b=x=y; Domain=s4.example"),
            ("Set-Cookie", 'bad name=1'),
            ("Set-Cookie", "inject=1\r\nX-Evil: 1"),
            ("Set-Cookie", "gone=; Max-Age=0"),
            ("Set-Cookie", "novalue"),
        ],
    )
    assert cookies_from_response(response) == {"a": "1", "b": "x=y"}


def test_cookie_header_never_carries_a_separator_or_a_line_break():
    odd = CsrfSession.fresh("T", {"ok": "1", "bad;name": "2", "split": "a; b=c", "nl": "a\r\nX: y"})
    assert odd.cookie_header() == "ok=1"
    assert CsrfSession.fresh("T", {}).cookie_header() == ""


# -- technical vs user ------------------------------------------------------

async def test_technical_service_has_one_entry_and_user_service_needs_a_user():
    store = CsrfSessionStore()
    fetches = 0

    async def fetch() -> CsrfSession:
        nonlocal fetches
        fetches += 1
        return CsrfSession.fresh(f"T-tech-{fetches}", {COOKIE_NAME: "S-tech"})

    # no user bound: a scheduled run
    technical = store.key(TECH_DEST, False)
    assert technical == (TECH_DEST, TECHNICAL)
    first = await store.get(technical, fetch)
    # the same entry whoever is signed in
    with as_user(ALICE):
        assert store.key(TECH_DEST, False) == technical
        second = await store.get(store.key(TECH_DEST, False), fetch)
    with as_user(BOB):
        third = await store.get(store.key(TECH_DEST, False), fetch)
    assert fetches == 1 and len(store) == 1
    assert first.token == second.token == third.token == "T-tech-1"

    # a user-context service without a user is refused, never the technical entry
    with pytest.raises(DestinationUserRequired) as refused:
        store.key(TECH_DEST, True)
    assert refused.value.destination == TECH_DEST
    assert isinstance(refused.value, DestinationError)
    with as_user(None, jwt=""):
        with pytest.raises(DestinationUserRequired):
            store.key(TECH_DEST, True)


async def test_a_technical_entry_is_never_served_to_a_user_request_or_the_reverse():
    store = CsrfSessionStore()

    def fetch_of(token: str):
        async def fetch() -> CsrfSession:
            return CsrfSession.fresh(token, {COOKIE_NAME: f"S-{token}"})
        return fetch

    # one destination, used by a technical service and by a user-context service
    technical = store.key(USER_DEST, False)
    await store.get(technical, fetch_of("tech"))
    with as_user(ALICE):
        alice_key = store.key(USER_DEST, True)
        assert alice_key != technical
        assert (await store.get(alice_key, fetch_of("alice"))).token == "alice"
        assert (await store.get(technical, fetch_of("never"))).token == "tech"
    assert len(store) == 2

    # a user whose principal is literally the technical marker is still a user
    with as_user(TECHNICAL):
        odd_key = store.key(USER_DEST, True)
        assert odd_key != technical
        assert (await store.get(odd_key, fetch_of("odd"))).token == "odd"
    assert (await store.get(technical, fetch_of("never"))).token == "tech"

    # dropping the user's entry leaves the technical one, and the reverse
    store.drop(alice_key)
    assert (await store.get(technical, fetch_of("never"))).token == "tech"
    store.drop(technical)
    with as_user(TECHNICAL):
        assert (await store.get(odd_key, fetch_of("never"))).token == "odd"


async def test_a_user_key_is_only_served_to_the_user_it_was_made_for():
    """The key comes from the request context; a key carried into another user's
    request (a bug in a caller) is refused rather than served."""
    store = CsrfSessionStore()
    fetched: list[str] = []

    async def fetch() -> CsrfSession:
        fetched.append("x")
        return CsrfSession.fresh("T-alice", {COOKIE_NAME: "S-alice"})

    with as_user(ALICE):
        alice_key = store.key(USER_DEST, True)
        await store.get(alice_key, fetch)
    with as_user(BOB):
        with pytest.raises(SessionIdentityError) as refused:
            await store.get(alice_key, fetch)
        assert ALICE not in str(refused.value) and BOB not in str(refused.value)
    # no user at all (a scheduled run that got hold of a user key)
    with pytest.raises(DestinationUserRequired):
        await store.get(alice_key, fetch)
    assert fetched == ["x"]


async def test_users_without_a_principal_are_told_apart_by_their_token():
    """No principal could be derived: the key is a digest of the JWT, never a shared slot."""
    store = CsrfSessionStore()
    with as_user(None, jwt="opaque-token-one"):
        one = store.key(USER_DEST, True)
    with as_user(None, jwt="opaque-token-two"):
        two = store.key(USER_DEST, True)
    with as_user(None, jwt="opaque-token-one"):
        again = store.key(USER_DEST, True)
    assert one != two and one == again
    assert "opaque-token-one" not in one[1] and "opaque-token-two" not in two[1]
    assert TECHNICAL not in (one[1], two[1])


async def test_two_destinations_never_share_an_entry_for_the_same_user():
    sap, store = Sap(), CsrfSessionStore()
    async with client(sap) as http:
        with as_user(ALICE):
            first_key = store.key(USER_DEST, True)
            second_key = store.key("S4_ODATA_OTHER", True)
            assert first_key != second_key and first_key[1] == second_key[1]
            first = await store.get(first_key, fetcher(http, ALICE))
            second = await store.get(second_key, fetcher(http, ALICE))
            assert first.token != second.token
            assert first.cookies[COOKIE_NAME] != second.cookies[COOKIE_NAME]
            store.drop(first_key)
            assert (await store.get(second_key, fetcher(http, ALICE))).token == second.token
    assert sap.fetches == {ALICE: 2}
    assert len(store) == 1


# -- lifetime ---------------------------------------------------------------

async def test_entries_expire_and_the_cache_is_bounded(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(session_mod, "_now", lambda: now[0])
    monkeypatch.setattr(session_mod, "SESSION_CACHE_MAX", 3)
    store = CsrfSessionStore()
    fetched: list[str] = []

    def fetch_of(name: str):
        async def fetch() -> CsrfSession:
            fetched.append(name)
            return CsrfSession.fresh(f"T-{name}-{len(fetched)}", {})
        return fetch

    async def get(name: str) -> CsrfSession:
        with as_user(name):
            return await store.get(store.key(USER_DEST, True), fetch_of(name))

    first = await get("u1")
    assert first.expires_at == 1000.0 + SESSION_TTL_SECONDS
    now[0] += SESSION_TTL_SECONDS - 1
    assert (await get("u1")).token == first.token          # still live
    now[0] += 1
    assert (await get("u1")).token != first.token          # expired: fetched again
    assert fetched == ["u1", "u1"]

    # bounded: the least recently used entry goes first
    await get("u2")
    await get("u3")
    await get("u1")            # u1 is now the most recent
    await get("u4")            # evicts u2
    assert len(store) == 3
    before = len(fetched)
    await get("u1")
    await get("u3")
    await get("u4")
    assert len(fetched) == before
    await get("u2")
    assert len(fetched) == before + 1

    # expired entries do not linger until the bound is hit
    now[0] += SESSION_TTL_SECONDS
    await get("u9")
    assert len(store) == 1


async def test_a_fetch_cannot_outlive_the_ttl(monkeypatch):
    """Whatever lifetime a fetch claims, the store caps it at the TTL."""
    now = [50.0]
    monkeypatch.setattr(session_mod, "_now", lambda: now[0])
    store = CsrfSessionStore()

    async def immortal() -> CsrfSession:
        return CsrfSession(token="T", cookies={}, expires_at=float("inf"))

    key = store.key(TECH_DEST, False)
    assert (await store.get(key, immortal)).expires_at == 50.0 + SESSION_TTL_SECONDS


async def test_a_fetch_must_return_a_session():
    store = CsrfSessionStore()

    async def wrong() -> CsrfSession:
        return {"token": "T"}  # type: ignore[return-value]

    with pytest.raises(TypeError):
        await store.get(store.key(TECH_DEST, False), wrong)
    assert len(store) == 0


# -- nothing secret in repr, logs or errors ---------------------------------

async def test_no_token_or_cookie_value_in_repr_logs_or_errors(caplog):
    secret_token = "CSRF-SECRET-7f3a91"
    secret_cookie = "COOKIE-SECRET-9b1c44"
    secret_jwt = "JWT-SECRET-55d0e2"
    store = CsrfSessionStore()

    async def fetch() -> CsrfSession:
        return CsrfSession.fresh(secret_token, {COOKIE_NAME: secret_cookie})

    caplog.set_level(logging.DEBUG)
    errors: list[str] = []
    with as_user(ALICE, jwt=secret_jwt):
        key = store.key(USER_DEST, True)
        held = await store.get(key, fetch)
        await store.get(key, fetch)
    with as_user(None, jwt=secret_jwt):
        anonymous = store.key(USER_DEST, True)
        await store.get(anonymous, fetch)
    with as_user(BOB):
        try:
            await store.get(key, fetch)
        except DestinationError as exc:
            errors.append(f"{exc} {exc!r}")
    try:
        await store.get(key, fetch)
    except DestinationError as exc:
        errors.append(f"{exc} {exc!r}")
    store.drop(key)
    store.drop(anonymous)

    assert len(errors) == 2
    text = "\n".join(
        [repr(held), str(held), repr(store), str(store), repr(key), repr(anonymous)]
        + [*errors, caplog.text]
        + [record.getMessage() for record in caplog.records]
    )
    for secret in (secret_token, secret_cookie, secret_jwt):
        assert secret not in text
    # still usable
    assert held.token == secret_token and held.cookies[COOKIE_NAME] == secret_cookie
    assert "CsrfSession" in repr(held) and "CsrfSessionStore" in repr(store)


# -- fix round 1: the key follows the credential that is sent ----------------

def jwt_digest(jwt: str) -> str:
    return hashlib.sha256(jwt.encode("utf-8")).hexdigest()


async def test_the_key_is_the_principal_and_a_digest_of_the_bound_token():
    store = CsrfSessionStore()
    with as_user(ALICE, jwt="token-a"):
        assert store.key(USER_DEST, True) == (USER_DEST, f"user:{ALICE}:{jwt_digest('token-a')}")
    with as_user(None, jwt="token-a"):
        assert store.key(USER_DEST, True) == (USER_DEST, f"token:{jwt_digest('token-a')}")
    # a refreshed token of the same user is a new entry
    with as_user(ALICE, jwt="token-a2"):
        assert store.key(USER_DEST, True) == (USER_DEST, f"user:{ALICE}:{jwt_digest('token-a2')}")


async def test_a_run_as_principal_with_the_triggers_token_never_meets_either_user():
    """A "Run now" job keeps the trigger's JWT while ``run_as`` rebinds the
    principal. SAP then issues the session of the JWT's owner; it must not be
    filed where the run-as user -- or anybody but that exact pair -- finds it."""
    sap, store = Sap(), CsrfSessionStore()
    async with client(sap) as http:
        # admin alice triggers; the run is bound to bob, the token stays alice's
        with as_user(BOB, jwt=f"jwt-of-{ALICE}"):
            run_key = store.key(USER_DEST, True)
            run_session = await store.get(run_key, fetcher(http, ALICE))   # SAP sees alice
        assert run_session.token == f"T-{ALICE}-1"

        # bob's own request: his token, his principal
        with as_user(BOB):
            bob_key = store.key(USER_DEST, True)
            assert bob_key != run_key
            bob = await store.get(bob_key, fetcher(http, BOB))
            with pytest.raises(SessionIdentityError):
                await store.get(run_key, fetcher(http, BOB))
        assert bob.token == f"T-{BOB}-1"
        assert bob.cookies[COOKIE_NAME] == f"S-{BOB}-1"

        # alice's own request: same token as the run, but another principal
        with as_user(ALICE):
            alice_key = store.key(USER_DEST, True)
            assert alice_key not in (run_key, bob_key)
            alice = await store.get(alice_key, fetcher(http, ALICE))
            with pytest.raises(SessionIdentityError):
                await store.get(run_key, fetcher(http, ALICE))
        assert alice.token == f"T-{ALICE}-2"       # fetched anew, not the run's entry

        # the same pair again is served the run's entry
        with as_user(BOB, jwt=f"jwt-of-{ALICE}"):
            assert store.key(USER_DEST, True) == run_key
            assert (await store.get(run_key, fetcher(http, ALICE))).token == run_session.token
    assert sap.fetches == {ALICE: 2, BOB: 1}
    assert len(store) == 3


async def test_a_refreshed_token_is_not_served_the_old_tokens_entry():
    store = CsrfSessionStore()
    n = 0

    async def fetch() -> CsrfSession:
        nonlocal n
        n += 1
        return CsrfSession.fresh(f"T-{n}", {})

    with as_user(ALICE, jwt="first"):
        old_key = store.key(USER_DEST, True)
        assert (await store.get(old_key, fetch)).token == "T-1"
    with as_user(ALICE, jwt="second"):
        assert (await store.get(store.key(USER_DEST, True), fetch)).token == "T-2"
        with pytest.raises(SessionIdentityError):
            await store.get(old_key, fetch)


async def test_no_token_validation_happens_in_the_store(monkeypatch):
    """Keys come from what the middleware already bound; nothing here validates
    a JWT (a blocking JWKS fetch on the event loop) to find a principal."""
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("the store must not validate tokens")

    monkeypatch.setattr(auth, "principal_from_token", boom)
    monkeypatch.setattr(auth, "get_validator", boom)
    store = CsrfSessionStore()

    async def fetch() -> CsrfSession:
        return CsrfSession.fresh("T", {})

    with as_user(None, jwt="some-token"):
        key = store.key(USER_DEST, True)
        await store.get(key, fetch)
    with as_user(ALICE):
        await store.get(store.key(USER_DEST, True), fetch)


@pytest.mark.parametrize(
    "principal",
    [TECHNICAL, "token:" + "a" * 64, "user:" + ALICE, f"{ALICE}:" + "b" * 64, "token:"],
)
async def test_odd_principals_never_reach_another_entry(principal):
    store = CsrfSessionStore()

    def fetch_of(token: str):
        async def fetch() -> CsrfSession:
            return CsrfSession.fresh(token, {})
        return fetch

    jwt = "x"
    technical = store.key(USER_DEST, False)
    await store.get(technical, fetch_of("tech"))
    with as_user(None, jwt=jwt):
        anonymous = store.key(USER_DEST, True)
        await store.get(anonymous, fetch_of("anon"))
    with as_user(ALICE, jwt=jwt):
        alice = store.key(USER_DEST, True)
        await store.get(alice, fetch_of("alice"))
    with as_user(principal, jwt=jwt):
        odd = store.key(USER_DEST, True)
        assert odd not in (technical, anonymous, alice)
        assert (await store.get(odd, fetch_of("odd"))).token == "odd"
        for foreign in (anonymous, alice):
            with pytest.raises(SessionIdentityError):
                await store.get(foreign, fetch_of("never"))
    assert len(store) == 4


# -- fix round 1: concurrency ------------------------------------------------

async def test_a_slow_fetch_of_one_user_does_not_block_another():
    store = CsrfSessionStore()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow() -> CsrfSession:
        started.set()
        await release.wait()
        return CsrfSession.fresh("T-alice", {})

    async def quick() -> CsrfSession:
        return CsrfSession.fresh("T-bob", {})

    async def alice_run() -> CsrfSession:
        with as_user(ALICE):
            return await store.get(store.key(USER_DEST, True), slow)

    task = asyncio.create_task(alice_run())
    await started.wait()
    with as_user(BOB):
        bob = await asyncio.wait_for(store.get(store.key(USER_DEST, True), quick), 1)
    technical = await asyncio.wait_for(store.get(store.key(USER_DEST, False), quick), 1)
    assert bob.token == technical.token == "T-bob"
    assert not task.done()
    release.set()
    assert (await task).token == "T-alice"


async def test_drop_while_a_fetch_is_in_flight_keeps_the_fresh_session():
    """The drop meant the old session; the one being fetched is newer."""
    store = CsrfSessionStore()
    started, release = asyncio.Event(), asyncio.Event()
    fetches = 0

    async def slow() -> CsrfSession:
        nonlocal fetches
        fetches += 1
        started.set()
        await release.wait()
        return CsrfSession.fresh(f"T-{fetches}", {})

    with as_user(ALICE):
        key = store.key(USER_DEST, True)
        task = asyncio.create_task(store.get(key, slow))
        await started.wait()
        store.drop(key)
        release.set()
        assert (await task).token == "T-1"
        assert (await store.get(key, slow)).token == "T-1"
    assert fetches == 1


async def test_a_cancelled_fetch_releases_the_key_and_stores_nothing():
    store = CsrfSessionStore()
    started = asyncio.Event()

    async def hangs() -> CsrfSession:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def works() -> CsrfSession:
        return CsrfSession.fresh("T-ok", {})

    with as_user(ALICE):
        key = store.key(USER_DEST, True)
        first = asyncio.create_task(store.get(key, hangs))
        await started.wait()
        waiter = asyncio.create_task(store.get(key, works))   # queued behind the fetch
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await asyncio.wait_for(waiter, 1)).token == "T-ok"
        assert len(store) == 1


async def test_eviction_during_overlapping_runs_never_crosses(monkeypatch):
    """A cache too small for the users in flight costs fetches, never a crossing."""
    monkeypatch.setattr(session_mod, "SESSION_CACHE_MAX", 1)
    sap, store = Sap(parallel_sessions=True), CsrfSessionStore()
    carol = "carol@example.com"

    async with client(sap) as http:
        rounds = [
            write_as(store, http, user, pause=1 + (i + j) % 3)
            for i in range(10)
            for j, user in enumerate((ALICE, BOB, carol))
        ]
        statuses = await asyncio.gather(*rounds)

    assert statuses == [201] * 30
    assert sap.crossed == []
    for caller, token, cookie in sap.writes:
        assert token.startswith(f"T-{caller}-")
        assert cookie.startswith(f"{COOKIE_NAME}=S-{caller}-")
    assert len(store) <= 1
    assert sum(sap.fetches.values()) > 3      # eviction did happen


async def test_a_key_is_served_in_a_child_task_and_refused_without_a_token():
    store = CsrfSessionStore()

    async def fetch() -> CsrfSession:
        return CsrfSession.fresh("T-alice", {})

    with as_user(ALICE):
        key = store.key(USER_DEST, True)
        child = asyncio.create_task(store.get(key, fetch))            # copies the context
        bare = asyncio.create_task(store.get(key, fetch), context=contextvars.Context())
        assert (await child).token == "T-alice"
        with pytest.raises(DestinationUserRequired):
            await bare
    assert len(store) == 1


# -- fix round 1: drop(stale) and update --------------------------------------

async def test_drop_with_a_stale_copy_spares_a_newer_session():
    store = CsrfSessionStore()
    n = 0

    async def fetch() -> CsrfSession:
        nonlocal n
        n += 1
        return CsrfSession.fresh(f"T-{n}", {})

    with as_user(ALICE):
        key = store.key(USER_DEST, True)
        stale = await store.get(key, fetch)
        # five overlapping calls saw the same stale copy and were all refused by SAP
        store.drop(key, stale)
        fresh = await store.get(key, fetch)
        for _ in range(4):
            store.drop(key, stale)                     # the fresh entry stays
        assert (await store.get(key, fetch)).token == fresh.token == "T-2"
        assert n == 2
        store.drop(key, fresh)
        assert len(store) == 0
        store.drop(key, fresh)                         # nothing there: harmless
        await store.get(key, fetch)
        store.drop(key)                                # the plain form still drops
        assert len(store) == 0


async def test_update_swaps_cookies_of_the_session_the_caller_saw(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(session_mod, "_now", lambda: now[0])
    store = CsrfSessionStore()
    n = 0

    async def fetch() -> CsrfSession:
        nonlocal n
        n += 1
        return CsrfSession.fresh(f"T-{n}", {COOKIE_NAME: f"S-{n}"})

    with as_user(ALICE):
        key = store.key(USER_DEST, True)
        seen = await store.get(key, fetch)
        now[0] += 100
        rotated = {COOKIE_NAME: "S-rotated", "bad name": "x", "nl": "a\r\nb"}
        assert store.update(key, seen, rotated) is True
        rotated[COOKIE_NAME] = "changed-after-the-call"
        after = await store.get(key, fetch)
        assert after.token == "T-1"
        assert after.cookies == {COOKIE_NAME: "S-rotated"}       # unsafe pairs are not kept
        assert after.expires_at == seen.expires_at               # no new lifetime

        # a copy of an older session changes nothing
        store.drop(key)
        newer = await store.get(key, fetch)
        assert store.update(key, seen, {COOKIE_NAME: "S-old-writer"}) is False
        assert (await store.get(key, fetch)).cookies == newer.cookies
        # nothing stored, or expired: nothing to update
        store.drop(key)
        assert store.update(key, newer, {COOKIE_NAME: "x"}) is False
        assert len(store) == 0
        current = await store.get(key, fetch)
        now[0] += SESSION_TTL_SECONDS
        assert store.update(key, current, {COOKIE_NAME: "x"}) is False

    # only the owner may update
    with as_user(ALICE):
        mine = await store.get(key, fetch)
    with as_user(BOB):
        with pytest.raises(SessionIdentityError):
            store.update(key, mine, {COOKIE_NAME: "S-bob"})
    with pytest.raises(DestinationUserRequired):
        store.update(key, mine, {COOKIE_NAME: "S-nobody"})
    with as_user(ALICE):
        assert (await store.get(key, fetch)).cookies == mine.cookies


def test_a_deletion_cookie_with_a_value_is_not_stored():
    response = httpx.Response(
        200,
        headers=[
            ("Set-Cookie", "keep=1; Max-Age=3600; Path=/"),
            ("Set-Cookie", "later=1; Expires=Fri, 01 Jan 2100 00:00:00 GMT"),
            ("Set-Cookie", "odd=1; Expires=not-a-date; Max-Age=soon"),
            ("Set-Cookie", "zero=deleted; Max-Age=0"),
            ("Set-Cookie", "negative=deleted; max-age=-1; Path=/"),
            ("Set-Cookie", "past=deleted; expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/"),
            # Max-Age wins over Expires (RFC 6265)
            ("Set-Cookie", "both=deleted; Expires=Fri, 01 Jan 2100 00:00:00 GMT; Max-Age=0"),
        ],
    )
    assert cookies_from_response(response) == {"keep": "1", "later": "1", "odd": "1"}
