"""The transport of ``builtin:bitbucket``: where a request may go, how a
failure is said, and that nothing remote ends up in a message or a log."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import httpx  # noqa: E402
import pytest  # noqa: E402

from agents import bitbucket_tools  # noqa: E402
from agents.bitbucket_config import pins_of  # noqa: E402
from agents.bitbucket_tools import (  # noqa: E402
    BACKOFF_SECONDS,
    BitbucketClient,
    Refused,
    build_http_client,
)
from agents.destination import DestinationError  # noqa: E402
from tests.bitbucket_helpers import (  # noqa: E402
    BASE_CFG,
    HOST,
    TOKEN,
    WS,
    FakeBitbucket,
    FakeResolver,
)

PINS = pins_of(BASE_CFG)
PR = f"/2.0/repositories/{WS}/svc-a/pullrequests/7"
MARK = "PLANTED-ERROR-BODY"


def _client(fake, **kw):
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    return BitbucketClient(fake.client(), PINS, sleep=sleep, **kw), slept


def _through_destination(fake, url=f"https://{HOST}"):
    http = build_http_client(BASE_CFG, "builtin:bitbucket", transport=fake.transport(),
                             resolver=FakeResolver(url))
    return BitbucketClient(http, PINS)


@pytest.mark.parametrize("dest_url", [f"https://{HOST}", f"https://{HOST}/", f"https://{HOST}/2.0",
                                      f"https://{HOST}/2.0/"])
async def test_the_api_root_is_sent_once_whatever_the_destination_url_ends_in(dest_url):
    fake = FakeBitbucket()
    client = _through_destination(fake, dest_url)
    body = client._json(await client._send("GET", "/2.0/user"))
    assert body["account_id"] == "acc-1"
    sent = fake.requests[0]
    assert (sent.url.host, sent.url.raw_path) == (HOST, b"/2.0/user")
    assert sent.headers["authorization"] == TOKEN


async def test_a_proxy_destination_keeps_its_own_prefix():
    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(200, json={"ok": True})
    client = _through_destination(fake, "https://apim.example.test/bitbucket")
    await client._send("GET", "/2.0/user")
    assert (fake.requests[0].url.host, fake.requests[0].url.raw_path) == (
        "apim.example.test", b"/bitbucket/2.0/user")


async def test_the_diff_redirect_is_followed_once_with_the_location_as_given():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 7, head="aaaaaaaaaaaa")
    fake.diffs[("svc-a", 7)] = "diff --git a/x b/x\n"
    client = _through_destination(fake)
    answer = await client._get_following(PR + "/diff")
    assert answer.status_code == 200 and answer.text.startswith("diff --git")
    assert fake.paths() == [
        f"GET {PR}/diff",
        f"GET /2.0/repositories/{WS}/svc-a/diff/{WS}/svc-a:aaaaaaaaaaaa%0Dbbbbbbbbbbbb"
        "?from_pullrequest_id=7",
    ]
    assert all(r.headers["authorization"] == TOKEN for r in fake.requests)


@pytest.mark.parametrize("location", [
    "https://evil.example.test/2.0/x", "http://api.bitbucket.org/2.0/x",
    "https://api.bitbucket.org:8443/2.0/x", "https://user:pw@api.bitbucket.org/2.0/x",
    "//evil.example.test/x", "", None, "https://api.bitbucket.org/2.0/x y",
])
async def test_a_redirect_elsewhere_is_not_followed_and_gets_no_credential(location):
    fake = FakeBitbucket()
    headers = {} if location is None else {"Location": location}
    fake.override = lambda r: (httpx.Response(302, headers=headers)
                               if len(fake.requests) == 1 else None)
    client = _through_destination(fake)
    with pytest.raises(Refused) as refused:
        await client._get_following(PR + "/diff")
    assert refused.value.code == "bitbucket_error"
    assert len(fake.requests) == 1
    assert "evil" not in refused.value.message


async def test_a_second_redirect_is_not_followed():
    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(
        302, headers={"Location": f"https://{HOST}/2.0/again"})
    client, _ = _client(fake)
    with pytest.raises(Refused) as refused:
        await client._get_following(PR + "/diff")
    assert refused.value.code == "bitbucket_error" and len(fake.requests) == 2


async def test_a_relative_location_stays_on_the_host_and_keeps_its_escapes():
    fake = FakeBitbucket()
    fake.override = lambda r: (httpx.Response(302, headers={"Location": "/2.0/a%0Db?x=1"})
                               if len(fake.requests) == 1 else httpx.Response(200, text="ok"))
    client, _ = _client(fake)
    await client._get_following(PR + "/diff")
    assert fake.requests[1].url.raw_path == b"/2.0/a%0Db?x=1"
    assert fake.requests[1].url.host == HOST


async def test_pages_follow_next_on_the_same_host_and_stop_at_the_cap():
    fake = FakeBitbucket()
    fake.page_size = 2
    fake.repos = ["r1", "r2", "r3", "r4", "r5"]
    client, _ = _client(fake)
    values, more = await client._pages(f"/2.0/repositories/{WS}", {"pagelen": "2"}, max_pages=5)
    assert [v["slug"] for v in values] == fake.repos and more is False
    values, more = await client._pages(f"/2.0/repositories/{WS}", {"pagelen": "2"}, max_pages=2)
    assert len(values) == 4 and more is True


async def test_a_next_link_to_another_host_is_refused():
    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(
        200, json={"values": [{"slug": "r1"}], "next": "https://evil.example.test/2.0/next"})
    client, _ = _client(fake)
    with pytest.raises(Refused) as refused:
        await client._pages(f"/2.0/repositories/{WS}", None, max_pages=3)
    assert refused.value.code == "bitbucket_error" and len(fake.requests) == 1


async def test_429_is_retried_with_backoff_then_given_up():
    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(429, text=MARK) if len(fake.requests) <= 2 else None
    client, slept = _client(fake)
    assert client._json(await client._send("GET", "/2.0/user"))["account_id"] == "acc-1"
    assert slept == list(BACKOFF_SECONDS[:2])

    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(429, text=MARK)
    client, slept = _client(fake)
    with pytest.raises(Refused) as refused:
        await client._send("GET", "/2.0/user")
    assert refused.value.code == "bitbucket_throttled"
    assert slept == list(BACKOFF_SECONDS) and len(fake.requests) == len(BACKOFF_SECONDS) + 1


@pytest.mark.parametrize("status, code", [
    (401, "bitbucket_unauthorized"), (403, "bitbucket_forbidden"), (404, "not_found"),
    (400, "bitbucket_error"), (500, "bitbucket_error"), (555, "bitbucket_error")])
async def test_a_status_becomes_a_fixed_code_and_text(status, code, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(
        status, json={"error": {"message": MARK, "detail": MARK}},
        headers={"X-Planted": MARK})
    client, _ = _client(fake)
    with pytest.raises(Refused) as refused:
        client._json(await client._send("GET", "/2.0/user"))
    assert refused.value.code == code
    said = str(refused.value.as_error()) + "\n".join(r.getMessage() for r in caplog.records)
    assert MARK not in said and TOKEN not in said


@pytest.mark.parametrize("raised, code", [
    (httpx.ConnectError("boom " + MARK), "bitbucket_unreachable"),
    (httpx.ReadTimeout("slow " + MARK), "bitbucket_unreachable"),
    (DestinationError("destination " + MARK), "destination_error"),
    (RuntimeError("odd " + MARK), "bitbucket_error"),
])
async def test_a_failure_is_said_by_class_never_by_its_text(raised, code, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeBitbucket()

    def fail(request):
        raise raised

    fake.override = fail
    client, _ = _client(fake)
    with pytest.raises(Refused) as refused:
        await client._send("GET", "/2.0/user")
    assert refused.value.code == code and refused.value.__cause__ is None
    said = str(refused.value.as_error()) + "\n".join(
        caplog.handler.format(r) for r in caplog.records)
    assert MARK not in said and TOKEN not in said
    assert type(raised).__name__ in said          # the class is logged


async def test_a_body_that_is_no_json_object_is_refused_without_its_text():
    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(200, text="<html>" + MARK)
    client, _ = _client(fake)
    with pytest.raises(Refused) as refused:
        client._json(await client._send("GET", "/2.0/user"))
    assert refused.value.code == "bitbucket_error" and MARK not in refused.value.message


async def test_the_token_never_reaches_a_log_line_at_debug(caplog):
    caplog.set_level(logging.DEBUG)          # httpx logs each request URL at INFO
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 7)
    client = _through_destination(fake)
    await client._get_following(PR + "/diff")
    said = "\n".join(caplog.handler.format(r) for r in caplog.records)
    assert TOKEN not in said and "PLANTED-TOKEN" not in said


def test_without_a_binding_the_build_names_the_server_only():
    with pytest.raises(DestinationError, match="builtin:bitbucket: no destination service binding"):
        build_http_client(BASE_CFG, "builtin:bitbucket")


# -- beyond the brief's cases --------------------------------------------------

ODD = (f"/2.0/repositories/{WS}/a..b/src/abc123/d%20e/"
       "f%2Fg%0D%25h%3F%23%C2%85%E2%80%AE.txt?format=meta")


@pytest.mark.parametrize("dest_url, prefix", [
    (f"https://{HOST}", ""), (f"https://{HOST}/2.0/", ""),
    ("https://apim.example.test/bitbucket/", "/bitbucket"),
    ("https://apim.example.test/bb/2.0", "/bb"),
])
async def test_an_encoded_path_reaches_the_destination_byte_for_byte(dest_url, prefix):
    # DestinationAuth alone decodes and re-encodes a placeholder path: %2F
    # becomes a segment separator, %25 a bare %, %0D or %3F an invalid URL.
    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(200, json={})
    client = _through_destination(fake, dest_url)
    await client._send("GET", ODD)
    sent = fake.requests[0]
    assert sent.url.raw_path == (prefix + ODD).encode()
    assert sent.url.scheme == "https"
    assert sent.headers["host"] == sent.url.host == httpx.URL(dest_url).host
    assert sent.headers["authorization"] == TOKEN


@pytest.mark.parametrize("dest_url", ["http://api.bitbucket.org", "https:///2.0", "ftp://x.test/"])
async def test_a_destination_that_is_not_https_with_a_host_gets_no_request(dest_url, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeBitbucket()
    client = _through_destination(fake, dest_url)
    with pytest.raises(Refused) as refused:
        await client._send("GET", "/2.0/user")
    assert refused.value.code in ("destination_error", "bitbucket_error")
    assert fake.requests == []
    said = str(refused.value.as_error()) + "\n".join(
        caplog.handler.format(r) for r in caplog.records)
    assert "x.test" not in said and "api.bitbucket.org" not in said and TOKEN not in said


async def test_a_refused_credential_is_asked_again_once_then_said():
    fake = FakeBitbucket()
    fake.user_status = 401
    resolver = FakeResolver()
    http = build_http_client(BASE_CFG, "builtin:bitbucket", transport=fake.transport(),
                             resolver=resolver)
    client = BitbucketClient(http, PINS)
    with pytest.raises(Refused) as refused:
        client._json(await client._send("GET", "/2.0/user"))
    assert refused.value.code == "bitbucket_unauthorized"
    assert len(fake.requests) == 2 and resolver.invalidated == 1
    assert [r.url.raw_path for r in fake.requests] == [b"/2.0/user"] * 2


@pytest.mark.parametrize("location", [
    "https://api.bitbucket.org/2.0/‮x", "https://api.bitbucket.org\\@evil.example.test/x",
    "/2.0/\x7fx", "/\\evil.example.test/x", "https://api.bitbucket.org.evil.example.test/2.0/x",
    "HTTPS://evil.example.test/x", "x/y", "?x=1", 7, ["/2.0/x"],
])
async def test_a_target_that_is_not_plain_ascii_on_the_host_is_no_target(location):
    from agents.bitbucket_tools import _same_host_target

    assert _same_host_target(location, httpx.URL(f"https://{HOST}/2.0/a")) is None


def test_a_target_on_the_host_is_taken_as_given():
    from agents.bitbucket_tools import _same_host_target

    sent = httpx.URL(f"https://{HOST}/2.0/a?old=1")
    given = f"https://{HOST}/2.0/r?q=state%3D%22OPEN%22&page=2"
    assert str(_same_host_target(given, sent)) == given
    assert _same_host_target(f"https://{HOST.upper()}:443/2.0/r", sent).raw_path == b"/2.0/r"
    assert _same_host_target("/2.0/r%0Dx?p=%7B1%7D", sent).raw_path == b"/2.0/r%0Dx?p=%7B1%7D"


@pytest.mark.parametrize("values", [None, 7, "abc", {"slug": "x"}, [1, "a", None, {"slug": "r1"}]])
async def test_values_that_are_no_list_of_objects_are_not_values(values):
    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(200, json={"values": values})
    client, _ = _client(fake)
    got, more = await client._pages(f"/2.0/repositories/{WS}", None, max_pages=3)
    assert more is False
    assert got == ([{"slug": "r1"}] if isinstance(values, list) else [])


async def _other_client_line(caplog) -> bool:
    """Whether a request of a client that is not this toolset's is logged."""
    caplog.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200))) as other:
        await other.get("https://other.example.test/seen")
    return any(r.name == "httpx" and "other.example.test/seen" in r.getMessage()
               for r in caplog.records)


async def test_httpx_does_not_log_the_urls_of_this_client(caplog):
    # httpx logs "HTTP Request: GET <url> ..." at INFO; a redirect target and a
    # paging link are Bitbucket's text, and a later task puts file paths there.
    # The level is set on the `httpx` logger itself: `app.py` raises it to
    # WARNING for the whole process when a test before this one imported the
    # app, and then httpx makes no request line at all, for any client: this
    # test would prove nothing about the filter in its first half and fail in
    # its second.
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="httpx")
    # The control: the lines are made and reach the handler ...
    assert await _other_client_line(caplog)
    caplog.clear()
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 7)
    client = _through_destination(fake)
    await client._get_following(PR + "/diff")
    assert len(fake.requests) == 2
    # ... and none of this client's two requests made one.
    assert not [r for r in caplog.records if r.name == "httpx"]
    said = "\n".join(caplog.handler.format(r) for r in caplog.records)
    assert "svc-a" not in said and "aaaaaaaaaaaa" not in said and HOST not in said
    assert bitbucket_tools._sending.get() is False

    # Another client's requests are logged as before, also after a refusal here
    # (the context variable is reset on that way out too).
    fake.override = lambda r: httpx.Response(429)
    with pytest.raises(Refused):
        await BitbucketClient(fake.client(), PINS, sleep=_no_sleep)._send("GET", "/2.0/user")
    assert bitbucket_tools._sending.get() is False
    assert await _other_client_line(caplog)


async def test_the_filter_drops_only_what_the_sending_task_logs(caplog):
    """The rule of the filter by itself, whatever else sits on the logger
    (``agents.sharepoint_tools`` puts its own filter there in the real app)."""
    caplog.set_level(logging.INFO, logger="httpx")
    log = logging.getLogger("httpx")
    mine = [f for f in log.filters if type(f).__name__ == "_NoRequestUrl"
            and type(f).__module__ == bitbucket_tools.__name__]
    assert len(mine) == 1
    bitbucket_tools._install_once()               # a second call adds none
    assert [f for f in log.filters if type(f) is type(mine[0])] == mine
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                               "HTTP Request: GET %s", ("https://x.test/a",), None)
    assert mine[0].filter(record) is True         # not sending: no business with it
    token = bitbucket_tools._sending.set(True)
    try:
        assert mine[0].filter(record) is False
        log.info("HTTP Request: GET %s", "https://x.test/dropped")
        # A thread that does not carry the sending context is not silenced.
        seen = await asyncio.get_running_loop().run_in_executor(
            None, lambda: mine[0].filter(record))
        assert seen is True
    finally:
        bitbucket_tools._sending.reset(token)
    log.info("HTTP Request: GET %s", "https://x.test/kept")
    texts = [r.getMessage() for r in caplog.records]
    assert any("x.test/kept" in t for t in texts)
    assert not any("x.test/dropped" in t for t in texts)


@pytest.mark.parametrize("failure", ["transport", "cancel", "auth"])
async def test_the_sending_mark_is_gone_however_a_request_ends(failure):
    fake = FakeBitbucket()

    def broken(request):
        if failure == "cancel":
            raise asyncio.CancelledError
        raise (httpx.ReadTimeout("x", request=request) if failure == "transport"
               else RuntimeError("x"))

    fake.override = broken
    client = BitbucketClient(fake.client(), PINS, sleep=_no_sleep)
    with pytest.raises((Refused, asyncio.CancelledError, RuntimeError)):
        await client._send("GET", "/2.0/user")
    assert bitbucket_tools._sending.get() is False


async def _no_sleep(seconds):
    return None


# -- the credential goes to the destination's https origin only -----------------

@pytest.mark.parametrize("dest_url, url", [
    (f"https://{HOST}", f"http://{HOST}/2.0/user"),
    (f"https://{HOST}", f"https://{HOST}:8443/2.0/user"),
    (f"https://{HOST}", f"http://{HOST}:443/2.0/user"),
    (f"https://{HOST}:8443", f"https://{HOST}/2.0/user"),
    (f"https://{HOST}:8443", f"https://{HOST}:443/2.0/user"),
])
async def test_an_absolute_url_off_the_destinations_https_origin_sends_nothing(
        dest_url, url, caplog):
    # The base class checks the host of an absolute URL only: the clear-text
    # scheme or another port of that host would get the Authorization header.
    caplog.set_level(logging.DEBUG)
    fake = FakeBitbucket()
    client = _through_destination(fake, dest_url)
    with pytest.raises(Refused) as refused:
        await client._send("GET", url)
    assert refused.value.code == "destination_error"
    assert fake.requests == []
    said = str(refused.value.as_error()) + "\n".join(
        caplog.handler.format(r) for r in caplog.records)
    assert HOST not in said and "8443" not in said and TOKEN not in said


@pytest.mark.parametrize("dest_url, url", [
    (f"https://{HOST}", f"https://{HOST}/2.0/user"),
    (f"https://{HOST}", f"https://{HOST.upper()}:443/2.0/user"),
    (f"https://{HOST}:8443/2.0", f"https://{HOST}:8443/2.0/user"),
])
async def test_an_absolute_url_on_the_destinations_https_origin_is_sent(dest_url, url):
    fake = FakeBitbucket()
    client = _through_destination(fake, dest_url)
    await client._send("GET", url)
    assert len(fake.requests) == 1 and fake.requests[0].headers["authorization"] == TOKEN
    assert fake.requests[0].url.raw_path == b"/2.0/user"


# --- an answer over its cap ---------------------------------------------------

async def test_an_answer_over_its_cap_is_a_refusal_not_an_empty_success():
    # A caller that reads `.content` of what `_send` returns must never see an
    # empty 200 where the answer was too long to keep.
    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(200, content=b"x" * 2000)
    client, _ = _client(fake)
    with pytest.raises(Refused) as refused:
        await client._send("GET", "/2.0/user", cap=1000)
    assert refused.value.code == "result_too_large"
    assert refused.value.message == "Bitbucket's answer is too large to read"


async def test_only_a_caller_that_asks_for_it_gets_the_over_cap_answer_and_it_is_empty():
    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(200, content=b"x" * 2000)
    client, _ = _client(fake)
    response = await client._send("GET", "/2.0/user", cap=1000, allow_over_cap=True)
    assert response.extensions.get("bitbucket_over_cap") is True and response.content == b""
    with pytest.raises(TypeError):
        await client._send("GET", "/2.0/user", None, None, 1000, True)   # keyword only


async def test_an_answer_at_its_cap_is_read():
    fake = FakeBitbucket()
    fake.override = lambda r: httpx.Response(200, content=b"x" * 1000)
    client, _ = _client(fake)
    assert (await client._send("GET", "/2.0/user", cap=1000)).content == b"x" * 1000
