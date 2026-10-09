"""The tools of ``builtin:bitbucket`` against a fake Bitbucket API."""

from __future__ import annotations

import json
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

from agents.bitbucket_tools import MAX_DIFF_CHARS, MAX_FILE_BYTES, bitbucket_toolset  # noqa: E402
from tests.bitbucket_helpers import BASE_CFG, WS, FakeBitbucket  # noqa: E402

HEAD = "aaaaaaaaaaaa"
# What a pull request author wrote. Allowed in the data fields of a result,
# never in an error, a log line or (task 6) run activity.
SOURCE_MARK = "PLANTED_SOURCE_LINE_zq7"


BEYOND_NOTE = ("beyond_reach: true: there are more repositories or open pull requests than "
               "this tool reads, and a later run does not reach them either; report it, a "
               "person has to look")


async def _noop_sleep(_):
    return None


def _toolset(fake, **cfg):
    return bitbucket_toolset({**BASE_CFG, **cfg}, http=fake.client(), sleep=_noop_sleep)


async def _call(toolset, tool, **kw):
    return await toolset.tools[tool].function(**kw)


def _fake() -> FakeBitbucket:
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 7, head=HEAD, title="Fix the parser", description="Why: " + SOURCE_MARK)
    fake.diffs[("svc-a", 7)] = f"diff --git a/src/x.py b/src/x.py\n+{SOURCE_MARK}\n"
    fake.files[("svc-a", HEAD, "src/x.py")] = f"print('{SOURCE_MARK}')\n".encode()
    return fake


def test_the_read_tools_are_registered_and_no_write_tool_without_the_switch():
    assert set(_toolset(_fake()).tools) >= {"get_pull_request", "get_diff", "get_file"}
    assert not {"add_inline_comment", "submit_review"} & set(_toolset(_fake()).tools)


def test_a_bad_entry_or_mode_is_refused_at_build():
    with pytest.raises(ValueError, match="^oauth.workspace:"):
        bitbucket_toolset({"destination": "BITBUCKET"}, http=_fake().client())
    with pytest.raises(ValueError, match="requires auth_mode=destination"):
        bitbucket_toolset(BASE_CFG, http=_fake().client(), auth_mode="oauth2")


async def test_get_pull_request_returns_the_review_facts():
    fake = _fake()
    fake.add_comment("svc-a", 7, "Looks odd", inline={"path": "src/x.py", "to": 3})
    fake.add_comment("svc-a", 7, "gone", deleted=True)
    fake.statuses[("svc-a", 7)] = [{"state": "SUCCESSFUL"}, {"state": "SUCCESSFUL"}]
    out = await _call(_toolset(fake), "get_pull_request", repository="svc-a", id=7)
    assert out["head_commit"] == HEAD and out["draft"] is False
    assert out["title"] == "Fix the parser" and out["author"] == "Ann Author"
    assert out["builds"] == {"state": "green", "total": 2}
    assert out["comments"] == [{"id": 100, "author": "Carl Commenter", "text": "Looks odd",
                                "inline": {"path": "src/x.py", "line": 3, "side": "new"},
                                "own": False}]


@pytest.mark.parametrize("states, expected", [
    ([], "none"), (["SUCCESSFUL"], "green"), (["SUCCESSFUL", "FAILED"], "not_green"),
    (["INPROGRESS"], "not_green"), (["STOPPED"], "not_green"), (["SOMETHING_NEW"], "not_green"),
    ([None], "not_green")])
async def test_only_successful_builds_are_green(states, expected):
    fake = _fake()
    fake.statuses[("svc-a", 7)] = [{"state": s} for s in states]
    out = await _call(_toolset(fake), "get_pull_request", repository="svc-a", id=7)
    assert out["builds"]["state"] == expected


async def test_long_text_is_truncated_and_says_so():
    fake = _fake()
    fake.prs[("svc-a", 7)]["description"] = "d" * 5000
    out = await _call(_toolset(fake), "get_pull_request", repository="svc-a", id=7)
    assert len(out["description"]) < 4100 and out["description"].endswith("…[truncated]")


@pytest.mark.parametrize("tool, extra", [("get_pull_request", {}), ("get_diff", {}),
                                         ("get_file", {"path": "src/x.py"})])
@pytest.mark.parametrize("change, kw, code", [
    ({}, {"repository": "svc-b", "id": 7}, "not_found"),
    ({}, {"repository": "svc-a", "id": 8}, "not_found"),
    ({}, {"repository": "svc-a", "id": 0}, "not_found"),
    ({}, {"repository": "svc-a", "id": True}, "not_found"),
    ({}, {"repository": "../other-ws/secret", "id": 7}, "repository_not_allowed"),
    ({}, {"repository": "SVC-A", "id": 7}, "repository_not_allowed"),
    ({"state": "MERGED"}, {"repository": "svc-a", "id": 7}, "not_open"),
    ({"destination": {"branch": {"name": "develop"}}}, {"repository": "svc-a", "id": 7},
     "wrong_branch"),
    ({"source": {"commit": {"hash": "../../x"}}}, {"repository": "svc-a", "id": 7},
     "bitbucket_error"),
])
async def test_every_tool_runs_the_pull_request_guard(tool, extra, change, kw, code):
    fake = _fake()
    fake.prs[("svc-a", 7)].update(change)
    out = await _call(_toolset(fake), tool, **kw, **extra)
    assert out["error"]["code"] == code
    assert set(out) == {"error"} and SOURCE_MARK not in json.dumps(out)


async def test_a_pinned_list_confines_the_repository_and_never_echoes_the_argument():
    fake = _fake()
    fake.add_pr("svc-z", 1)
    out = await _call(_toolset(fake, repositories=["svc-a"]), "get_diff",
                      repository="svc-z", id=1)
    assert out == {"error": {"code": "repository_not_allowed",
                             "message": "this agent may not use that repository",
                             "hint": "repositories: svc-a"}}
    assert not any("svc-z" in p for p in fake.paths())     # nothing was requested


async def test_get_diff_returns_the_text_and_passes_a_confined_path():
    fake = _fake()
    toolset = _toolset(fake)
    out = await _call(toolset, "get_diff", repository="svc-a", id=7)
    assert SOURCE_MARK in out["diff"] and out["chars"] == len(out["diff"]) and out["path"] == ""
    await _call(toolset, "get_diff", repository="svc-a", id=7, path="src/x.py")
    assert fake.requests[-2].url.params["path"] == "src/x.py"
    out = await _call(toolset, "get_diff", repository="svc-a", id=7, path="../x")
    assert out["error"]["code"] == "invalid_path"


@pytest.mark.parametrize("how", ["555", "long"])
async def test_a_diff_over_the_cap_is_refused_with_the_diffstat_never_cut(how):
    fake = _fake()
    fake.diffs[("svc-a", 7)] = "x" * (MAX_DIFF_CHARS + 1)
    fake.diffstats[("svc-a", 7)] = [
        {"status": "modified", "lines_added": 3, "lines_removed": 1,
         "new": {"path": "src/x.py"}, "old": {"path": "src/x.py"}},
        {"status": "removed", "lines_added": 0, "lines_removed": 9, "new": None,
         "old": {"path": "src/old.py"}},
        {"status": "<b>odd</b>", "lines_added": "many", "lines_removed": -1,
         "new": {"path": "a\nb"}},
    ]
    if how == "555":
        fake.override = lambda r: (httpx.Response(555, text="too big")
                                   if "/diff/" in r.url.path else None)
    out = await _call(_toolset(fake), "get_diff", repository="svc-a", id=7)
    assert out["error"]["code"] == "result_too_large" and "diff" not in out
    assert out["diffstat"] == [
        {"path": "src/x.py", "status": "modified", "lines_added": 3, "lines_removed": 1},
        {"path": "src/old.py", "status": "removed", "lines_added": 0, "lines_removed": 9},
        {"path": None, "status": None, "lines_added": None, "lines_removed": None},
    ]
    assert out["diffstat_truncated"] is False


async def test_get_file_reads_meta_first_then_the_bytes_at_the_head_commit():
    fake = _fake()
    out = await _call(_toolset(fake), "get_file", repository="svc-a", id=7, path="src/x.py")
    assert out["commit"] == HEAD and SOURCE_MARK in out["content"]
    src = [p for p in fake.paths() if "/src/" in p]
    assert src == [f"GET /2.0/repositories/{WS}/svc-a/src/{HEAD}/src/x.py?format=meta",
                   f"GET /2.0/repositories/{WS}/svc-a/src/{HEAD}/src/x.py"]


async def test_a_path_segment_is_escaped_not_interpreted():
    fake = _fake()
    fake.files[("svc-a", HEAD, "docs/a b&c=d.md")] = b"ok"
    out = await _call(_toolset(fake), "get_file", repository="svc-a", id=7, path="docs/a b&c=d.md")
    assert out["content"] == "ok"
    assert fake.requests[-1].url.raw_path.endswith(b"/docs/a%20b%26c%3Dd.md")


@pytest.mark.parametrize("meta, data, code", [
    ({"attributes": ["binary"]}, b"x", "binary_file"),
    ({"attributes": ["lfs"]}, b"x", "binary_file"),
    ({"type": "commit_directory"}, b"x", "invalid_path"),
    ({"size": MAX_FILE_BYTES + 1}, b"x", "result_too_large"),
    ({"size": "big"}, b"x", "result_too_large"),
    ({}, b"\xff\xfe\x00bin", "binary_file"),
    ({}, b"a\x00b", "binary_file"),
    ({"size": 1}, b"y" * (MAX_FILE_BYTES + 1), "result_too_large"),   # meta lied
])
async def test_a_file_that_is_binary_or_too_large_is_refused(meta, data, code):
    fake = _fake()
    fake.files[("svc-a", HEAD, "blob")] = data
    fake.meta[("svc-a", HEAD, "blob")] = meta
    out = await _call(_toolset(fake), "get_file", repository="svc-a", id=7, path="blob")
    assert out["error"]["code"] == code and "content" not in out


async def test_an_lfs_redirect_is_never_followed():
    fake = _fake()
    fake.override = lambda r: (httpx.Response(
        301, headers={"Location": "https://media.example.test/lfs/" + SOURCE_MARK})
        if "/src/" in r.url.path else None)
    out = await _call(_toolset(fake), "get_file", repository="svc-a", id=7, path="src/x.py")
    assert out["error"]["code"] == "binary_file"
    assert all(r.url.host == "api.bitbucket.org" for r in fake.requests)


@pytest.mark.parametrize("path", ["", "/abs", "a/../b", "a%2fb", "a?b", "a#b", "a\\b"])
async def test_get_file_refuses_a_path_that_is_not_confined(path):
    fake = _fake()
    out = await _call(_toolset(fake), "get_file", repository="svc-a", id=7, path=path)
    assert out["error"]["code"] == "invalid_path"
    assert not any("/src/" in p for p in fake.paths())


async def test_an_unexpected_exception_becomes_a_fixed_error_without_its_text(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    fake = _fake()
    toolset = _toolset(fake)

    async def boom(*a, **k):
        raise RuntimeError("bug " + SOURCE_MARK)

    monkeypatch.setattr(toolset.client, "_send", boom)
    out = await _call(toolset, "get_diff", repository="svc-a", id=7)
    assert out == {"error": {"code": "bitbucket_error",
                             "message": "the call could not be completed"}}
    said = "\n".join(caplog.handler.format(r) for r in caplog.records)
    assert SOURCE_MARK not in said and "RuntimeError" in said


async def test_no_source_text_in_any_log_line_of_a_successful_read(caplog):
    caplog.set_level(logging.DEBUG)
    toolset = _toolset(_fake())
    await _call(toolset, "get_pull_request", repository="svc-a", id=7)
    await _call(toolset, "get_diff", repository="svc-a", id=7)
    await _call(toolset, "get_file", repository="svc-a", id=7, path="src/x.py")
    assert SOURCE_MARK not in "\n".join(caplog.handler.format(r) for r in caplog.records)


# -- beyond the brief's cases --------------------------------------------------

class _Endless(httpx.AsyncByteStream):
    """A body that never ends by itself; counts what was taken from it."""

    def __init__(self, chunk: bytes, chunks: int = 10_000) -> None:
        self.chunk, self.left, self.taken, self.closed = chunk, chunks, 0, False

    async def __aiter__(self):
        while self.left:
            self.left -= 1
            self.taken += len(self.chunk)
            yield self.chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("tool, extra, marker, slack", [
    ("get_diff", {}, "/diff/", 4 * MAX_DIFF_CHARS),
    ("get_file", {"path": "src/x.py"}, "/src/", MAX_FILE_BYTES),
])
async def test_a_body_is_read_up_to_its_cap_and_no_further(tool, extra, marker, slack):
    # The cap holds while reading: the rest of the body is never taken.
    fake = _fake()
    fake.meta[("svc-a", HEAD, "src/x.py")] = {"size": 10}       # meta lied
    body = _Endless(b"x" * 65_536)
    fake.override = lambda r: (httpx.Response(200, stream=body)
                               if marker in r.url.path and "format" not in r.url.params
                               else None)
    out = await _call(_toolset(fake), tool, repository="svc-a", id=7, **extra)
    assert out["error"]["code"] == "result_too_large"
    assert body.taken <= slack + 2 * 65_536 and body.closed


async def test_an_oversized_json_answer_is_refused_while_reading():
    from agents.bitbucket_tools import MAX_JSON_BYTES

    fake = _fake()
    body = _Endless(b'{"a": "' + b"x" * 65_536)
    fake.override = lambda r: httpx.Response(200, stream=body)
    out = await _call(_toolset(fake), "get_pull_request", repository="svc-a", id=7)
    assert out == {"error": {"code": "result_too_large",
                             "message": "Bitbucket's answer is too large to read"}}
    assert body.taken <= MAX_JSON_BYTES + 2 * 65_600 and body.closed


async def test_a_diff_that_is_not_utf8_is_still_text_and_a_status_is_a_refusal():
    fake = _fake()
    fake.override = lambda r: (httpx.Response(200, content=b"+caf\xe9\n")
                               if "/diff/" in r.url.path else None)
    out = await _call(_toolset(fake), "get_diff", repository="svc-a", id=7)
    assert out["diff"] == "+caf\ufffd\n" and out["chars"] == 6
    fake.override = lambda r: (httpx.Response(500, text=SOURCE_MARK)
                               if "/diff/" in r.url.path else None)
    out = await _call(_toolset(fake), "get_diff", repository="svc-a", id=7)
    assert out == {"error": {"code": "bitbucket_error", "message": "Bitbucket answered HTTP 500"}}


async def test_the_diffstat_stops_at_its_cap_and_says_so():
    fake = _fake()
    fake.page_size = 100
    fake.diffs[("svc-a", 7)] = "x" * (MAX_DIFF_CHARS + 1)
    fake.diffstats[("svc-a", 7)] = [
        {"status": "added", "lines_added": 1, "lines_removed": 0, "new": {"path": f"f{i}"}}
        for i in range(301)]
    out = await _call(_toolset(fake), "get_diff", repository="svc-a", id=7)
    assert len(out["diffstat"]) == 300 and out["diffstat_truncated"] is True
    assert out["error"]["hint"] == "ask for one file with path"


@pytest.mark.parametrize("answer", [
    httpx.Response(500, text=SOURCE_MARK),
    httpx.Response(200, text="<html>" + SOURCE_MARK),
    httpx.Response(302, headers={"Location": "https://evil.example.test/" + SOURCE_MARK}),
])
async def test_without_a_readable_diffstat_the_refusal_stands_alone(answer):
    fake = _fake()
    fake.diffs[("svc-a", 7)] = "x" * (MAX_DIFF_CHARS + 1)
    fake.override = lambda r: answer if "/diffstat" in r.url.path else None
    out = await _call(_toolset(fake), "get_diff", repository="svc-a", id=7)
    assert out == {"error": {"code": "result_too_large",
                             "message": "the diff does not fit a tool answer",
                             "hint": "ask for one file with path"}}
    assert all(r.url.host == "api.bitbucket.org" for r in fake.requests)


async def test_a_diffstat_path_with_a_bidi_or_line_separator_character_is_not_passed_on():
    fake = _fake()
    fake.diffs[("svc-a", 7)] = "x" * (MAX_DIFF_CHARS + 1)
    fake.diffstats[("svc-a", 7)] = [
        {"status": "modified", "new": {"path": "a\u202eb"}},
        {"status": "modified", "new": {"path": "a\u2028b"}},
        {"status": "modified", "new": {"path": "p" * 401}},
        {"status": "modified", "lines_added": True, "new": {"path": 7}, "old": ["x"]},
        "not an entry",
    ]
    out = await _call(_toolset(fake), "get_diff", repository="svc-a", id=7)
    assert [e["path"] for e in out["diffstat"]] == [None, None, None, None]
    assert out["diffstat"][3]["lines_added"] is None


async def test_comments_stop_at_two_pages_and_say_so():
    fake = _fake()
    for i in range(101):
        fake.add_comment("svc-a", 7, f"c{i}")
    out = await _call(_toolset(fake), "get_pull_request", repository="svc-a", id=7)
    assert len(out["comments"]) == 100 and out["comments_truncated"] is True
    assert out["builds"] == {"state": "none", "total": 0}


async def test_odd_comment_and_pull_request_fields_do_not_break_the_answer():
    fake = _fake()
    fake.prs[("svc-a", 7)].update(title=["x"], author="nobody", draft="false")
    fake.comments[("svc-a", 7)] = [
        {"id": 1, "content": {"raw": "t" * 1500}, "user": None, "inline": {"path": 3, "from": 9}},
        {"id": "x", "content": {"raw": "no id"}},
        {"id": 2, "content": None, "deleted": True},
    ]
    out = await _call(_toolset(fake), "get_pull_request", repository="svc-a", id=7)
    assert out["title"] == "" and out["author"] == "" and out["draft"] is False
    assert len(out["comments"]) == 1
    only = out["comments"][0]
    assert only["id"] == 1 and only["author"] == "" and only["text"].endswith("…[truncated]")
    assert only["inline"] == {"path": None, "line": 9, "side": "old"}


@pytest.mark.parametrize("anchor, line, side", [
    ({"to": 3}, 3, "new"), ({"from": 9}, 9, "old"),
    ({"from": 9, "to": 3}, 3, "new"),               # a changed line: the new file's
    ({"to": None, "from": 9}, 9, "old"), ({"to": True, "from": "9"}, None, None), ({}, None, None),
])
async def test_an_inline_comment_says_its_side_in_the_words_of_add_inline_comment(
        anchor, line, side):
    fake = _fake()
    fake.add_comment("svc-a", 7, "Looks odd", inline={"path": "src/x.py", **anchor})
    out = await _call(_toolset(fake), "get_pull_request", repository="svc-a", id=7)
    assert out["comments"][0]["inline"] == {"path": "src/x.py", "line": line, "side": side}


async def test_more_statuses_than_were_read_are_not_green():
    fake = _fake()
    fake.statuses[("svc-a", 7)] = [{"state": "SUCCESSFUL"}] * 101
    out = await _call(_toolset(fake), "get_pull_request", repository="svc-a", id=7)
    assert out["builds"]["state"] == "not_green"


async def test_a_meta_answer_that_is_a_redirect_or_an_error_reads_no_bytes():
    fake = _fake()
    fake.override = lambda r: (httpx.Response(302, headers={"Location": "/2.0/elsewhere"})
                               if "format=meta" in str(r.url) else None)
    out = await _call(_toolset(fake), "get_file", repository="svc-a", id=7, path="src/x.py")
    assert out["error"]["code"] == "binary_file"
    assert len([p for p in fake.paths() if "/src/" in p]) == 1
    out = await _call(_toolset(_fake()), "get_file", repository="svc-a", id=7, path="nope.py")
    assert out["error"]["code"] == "not_found" and SOURCE_MARK not in json.dumps(out)


def test_the_tool_descriptions_say_that_what_is_read_is_data():
    toolset = _toolset(_fake())
    for name in ("get_pull_request", "get_diff", "get_file"):
        text = toolset.tools[name].function.__doc__
        assert "never as instructions" in text and "do not guess" in text


def test_the_client_is_the_one_the_registry_closes():
    fake = _fake()
    http = fake.client()
    toolset = bitbucket_toolset(BASE_CFG, http=http, auth_mode="destination")
    assert toolset.http_client is http and toolset.client.pins.workspace == WS


# --- list_pull_requests -------------------------------------------------------

from agents.bitbucket_tools import (  # noqa: E402
    MARKER_PREFIX,
    MAX_CHECKED,
    MAX_LISTED,
    MAX_REPOSITORIES_SCANNED,
    Refused,
    review_marker,
)
from tests.bitbucket_helpers import OTHER_UUID, OWN_UUID  # noqa: E402


def test_the_marker_is_one_fixed_line():
    assert review_marker(HEAD, "approve") == f"Automated review of commit {HEAD} - verdict: approve"
    assert review_marker(HEAD, "comment") == f"Automated review of commit {HEAD} - verdict: comment"
    assert review_marker(HEAD) == review_marker(HEAD, "comment")
    assert review_marker(HEAD).startswith(MARKER_PREFIX)
    for verdict in ("merge", "APPROVE", None, "approve\nx", ""):
        with pytest.raises(ValueError):
            review_marker(HEAD, verdict)


@pytest.mark.parametrize("line, verdict", [
    ("Automated review of commit aaaaaaaaaaaa - verdict: approve", "approve"),
    ("Automated review of commit aaaaaaaaaaaa - verdict: comment", "comment"),
    ("Automated review of commit aaaaaaaaaaaa", "comment"),             # the old form
    ("Automated review of commit aaaaaaaaaaaa - verdict: approve  \r", "approve"),
    ("Automated review of commit aaaaaaaaaaaa - verdict: approved", None),
    ("Automated review of commit aaaaaaaaaaaa - verdict: Approve", None),
    ("Automated review of commit aaaaaaaaaaaa - verdict:approve", None),
    ("Automated review of commit aaaaaaaaaaaa -  verdict: approve", None),
    ("Automated review of commit aaaaaaaaaaaa - verdict: approve - verdict: comment", None),
    ("Automated review of commit aaaaaaaaaaaa - verdict: ", None),
    ("Automated review of commit bbbbbbbbbbbb - verdict: approve", None),   # another commit
])
async def test_the_marker_line_says_the_verdict_in_one_fixed_form(line, verdict):
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1, head="aaaaaaaaaaaa")
    fake.add_comment("svc-a", 1, line + "\n\nVerdict: approve", own=True)
    client = _toolset(fake).client
    base = f"/2.0/repositories/{WS}/svc-a/pullrequests/1"
    assert await client.reviewed(base, "aaaaaaaaaaaa", await client.whoami()) == verdict


async def test_two_markers_for_one_commit_are_an_approve_only_when_both_say_so():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1)
    fake.add_comment("svc-a", 1, review_marker(HEAD, "approve"), own=True)
    client = _toolset(fake).client
    base = f"/2.0/repositories/{WS}/svc-a/pullrequests/1"
    assert await client.reviewed(base, HEAD, OWN_UUID) == "approve"
    fake.add_comment("svc-a", 1, review_marker(HEAD, "comment"), own=True)
    assert await client.reviewed(base, HEAD, OWN_UUID) == "comment"


async def test_the_list_holds_open_pull_requests_to_the_pinned_branch_only():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1, title="One")
    fake.add_pr("svc-a", 2, branch="develop")
    fake.add_pr("svc-a", 3, state="MERGED")
    fake.add_pr("svc-b", 4, draft=True, author="Bob Builder")
    out = await _call(_toolset(fake), "list_pull_requests")
    assert [(p["repository"], p["id"]) for p in out["pull_requests"]] == [
        ("svc-a", 1), ("svc-b", 4)]
    assert out["pull_requests"][1] == {
        "repository": "svc-b", "id": 4, "title": "A change", "author": "Bob Builder",
        "head_commit": HEAD, "draft": True, "updated_on": "2026-10-09T08:00:00.000000+00:00"}
    assert out["reviewed_filter"] == "active" and out["already_reviewed"] == 0
    assert out["repositories"] == 2 and out["repositories_failed"] == 0
    assert out["more"] is False and "note" not in out and out["pull_requests_unchecked"] == 0
    assert out["beyond_reach"] is False
    assert 'destination.branch.name="main" AND state="OPEN"' in [
        r.url.params.get("q") for r in fake.requests]


async def test_a_pinned_list_is_the_only_thing_that_is_read():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1)
    fake.add_pr("svc-z", 9)
    out = await _call(_toolset(fake, repositories=["svc-a"]), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [1]
    assert not any("svc-z" in p or p.endswith(f"/repositories/{WS}") for p in fake.paths())


async def test_a_branch_pin_is_used_in_the_query_and_checked_again_on_the_answer():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1, branch="release/2.x")
    fake.add_pr("svc-a", 2)
    # A server that ignores the query must not widen the list.
    fake.override = lambda r: (httpx.Response(200, json={"values": [
        fake.prs[("svc-a", 1)], fake.prs[("svc-a", 2)]]})
        if r.url.path.endswith("/pullrequests") else None)
    out = await _call(_toolset(fake, branch="release/2.x"), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [1]
    assert 'destination.branch.name="release/2.x" AND state="OPEN"' in [
        r.url.params.get("q") for r in fake.requests]


async def test_an_item_that_is_no_open_pull_request_with_a_head_is_dropped_silently():
    fake = FakeBitbucket()
    good = fake.add_pr("svc-a", 1)
    fake.add_pr("svc-a", 5)
    odd = [{**good, "id": True}, {**good, "id": 0}, {**good, "id": "2"},
           {**good, "state": "MERGED"}, {**good, "source": {"commit": {"hash": "../x"}}},
           {**good, "source": None}, {**good, "destination": "main"},
           {**good, "id": 5, "title": ["x"], "author": None, "draft": "false",
            "updated_on": "yesterday <b>"}]
    fake.override = lambda r: (httpx.Response(200, json={"values": [*odd, "text", good]})
                               if r.url.path.endswith("/pullrequests") else None)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [5, 1]
    assert out["pull_requests"][0] == {"repository": "svc-a", "id": 5, "title": "", "author": "",
                                       "head_commit": HEAD, "draft": False, "updated_on": None}


async def test_a_pull_request_reviewed_at_its_head_commit_is_dropped_until_a_new_commit():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1, head="aaaaaaaaaaaa")
    fake.add_pr("svc-a", 2, head="cccccccccccc")
    fake.add_comment("svc-a", 1, review_marker("aaaaaaaaaaaa") + "\n\nVerdict: comment\n\nok",
                     own=True)
    fake.add_comment("svc-a", 2, review_marker("aaaaaaaaaaaa") + "\n\nold commit", own=True)
    toolset = _toolset(fake)
    out = await _call(toolset, "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [2] and out["already_reviewed"] == 1
    fake.prs[("svc-a", 1)]["source"]["commit"]["hash"] = "dddddddddddd"     # a new commit
    out = await _call(toolset, "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [1, 2] and out["already_reviewed"] == 0


@pytest.mark.parametrize("raw, own, deleted", [
    ("Automated review of commit aaaaaaaaaaaa", False, False),   # somebody else typed it
    ("Automated review of commit aaaaaaaaaaaa", True, True),     # deleted
    ("I saw: Automated review of commit aaaaaaaaaaaa", True, False),   # not the first line
    ("Verdict: approve\nAutomated review of commit aaaaaaaaaaaa", True, False),
    ("\nAutomated review of commit aaaaaaaaaaaa", True, False),
    (" Automated review of commit aaaaaaaaaaaa", True, False),   # not as the code writes it
    ("> Automated review of commit aaaaaaaaaaaa", True, False),  # a quote
    ("Automated review of commit aaaaaaaaaaaa and more", True, False),
    ("Automated review of commit AAAAAAAAAAAA", True, False),
    ("automated review of commit aaaaaaaaaaaa", True, False),
    ("Automated review of commit aaaaaa", True, False),          # too short for a hash
    ("Automated review of commit aaaaaaaaaaab", True, False),    # another commit
    ("Automated review of commit zzzz", True, False),            # no hash
    ("Automated review of commit ", True, False),
])
async def test_only_this_accounts_own_marker_line_counts(raw, own, deleted):
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1, head="aaaaaaaaaaaa")
    fake.add_comment("svc-a", 1, raw, own=own, deleted=deleted)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [1] and out["already_reviewed"] == 0


@pytest.mark.parametrize("change", [
    {"inline": {"path": "src/x.py", "to": 3}},         # an inline comment is no summary
    {"parent": {"id": 100}},                           # neither is a reply
    {"user": {"uuid": OWN_UUID.upper().replace("1", "2")}},
    {"user": {"uuid": [OWN_UUID]}},
    {"user": OWN_UUID},
    {"user": {"display_name": "Review Bot", "account_id": OWN_UUID}},
    {"deleted": "false", "user": {"uuid": OTHER_UUID}},
    {"content": {"raw": ["Automated review of commit aaaaaaaaaaaa"]}},
    {"content": {"html": "Automated review of commit aaaaaaaaaaaa", "raw": "hello"}},
])
async def test_a_marker_counts_only_in_a_top_level_comment_of_this_account(change):
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1, head="aaaaaaaaaaaa")
    fake.add_comment("svc-a", 1, review_marker("aaaaaaaaaaaa"), own=True)
    fake.comments[("svc-a", 1)][0].update(change)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [1] and out["already_reviewed"] == 0


async def test_the_author_pasting_the_marker_does_not_hide_the_pull_request():
    fake = FakeBitbucket()
    marker = review_marker("aaaaaaaaaaaa")
    fake.add_pr("svc-a", 1, head="aaaaaaaaaaaa", title=marker, description=marker)
    fake.add_comment("svc-a", 1, marker)                         # the author, top level
    fake.add_comment("svc-a", 1, marker + "\n\nVerdict: approve")
    fake.add_comment("svc-a", 1, f"As the bot said:\n{marker}", own=True)   # quoted by the bot
    out = await _call(_toolset(fake), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [1] and out["already_reviewed"] == 0
    assert out["reviewed_filter"] == "active"


async def test_a_long_hash_in_the_marker_matches_the_short_head():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1, head="aaaaaaaaaaaa")
    fake.add_comment("svc-a", 1, "Automated review of commit " + "a" * 40, own=True)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert out["pull_requests"] == [] and out["already_reviewed"] == 1


async def test_the_marker_is_found_as_bitbucket_may_hand_it_back():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1, head="aaaaaaaaaaaa")
    fake.add_comment("svc-a", 1, review_marker("aaaaaaaaaaaa") + "  \r\n\r\nVerdict", own=True)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert out["pull_requests"] == [] and out["already_reviewed"] == 1


async def test_an_unknown_account_gives_an_unfiltered_list_that_says_so(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeBitbucket()
    fake.user_status = 502
    fake.add_pr("svc-a", 1)
    fake.add_comment("svc-a", 1, review_marker(HEAD), own=True)
    toolset = _toolset(fake)
    out = await _call(toolset, "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [1]
    assert out["reviewed_filter"] == "unavailable" and "could not be identified" in out["note"]
    assert "top-level comment" in out["note"] and "first line" in out["note"]
    assert out["already_reviewed"] == 0
    assert not any(p.endswith("/comments") for p in fake.paths())
    logged = "\n".join(caplog.handler.format(r) for r in caplog.records)
    assert "PLANTED-ERROR-BODY" not in json.dumps(out) + logged
    assert "builtin:bitbucket: the reviewing account could not be identified" in logged
    # The failure is not cached: the next call filters again.
    fake.user_status = 200
    out = await _call(toolset, "list_pull_requests")
    assert out["pull_requests"] == [] and out["reviewed_filter"] == "active"


async def test_the_account_is_asked_once_when_it_is_known():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1)
    toolset = _toolset(fake)
    await _call(toolset, "list_pull_requests")
    await _call(toolset, "list_pull_requests")
    await _call(toolset, "get_pull_request", repository="svc-a", id=1)
    assert fake.paths().count("GET /2.0/user") == 1


@pytest.mark.parametrize("body", [
    {"uuid": "<script>"}, {"uuid": OWN_UUID.strip("{}")}, {"uuid": OWN_UUID + "\n"},
    {"uuid": None}, {"uuid": [OWN_UUID]}, {"account_id": "acc-1"}, [OWN_UUID]])
async def test_a_uuid_that_has_not_the_form_of_one_is_no_account(body):
    fake = FakeBitbucket()
    fake.override = lambda r: (httpx.Response(200, json=body)
                               if r.url.path == "/2.0/user" else None)
    fake.add_pr("svc-a", 1)
    toolset = _toolset(fake)
    out = await _call(toolset, "list_pull_requests")
    assert out["reviewed_filter"] == "unavailable"
    assert await toolset.client.whoami() == ""


async def test_the_two_helpers_the_write_tools_need():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1, head="aaaaaaaaaaaa")
    fake.add_comment("svc-a", 1, review_marker("aaaaaaaaaaaa"), own=True)
    client = _toolset(fake).client
    account = await client.whoami()
    assert account == OWN_UUID
    base = f"/2.0/repositories/{WS}/svc-a/pullrequests/1"
    assert await client.reviewed(base, "aaaaaaaaaaaa", account) == "comment"
    assert await client.reviewed(base, "bbbbbbbbbbbb", account) is None
    # Without an account, or without a head commit of 12 hex characters, the
    # helper refuses instead of answering "not reviewed"; nothing is asked.
    asked = len(fake.requests)
    with pytest.raises(Refused) as refused:
        await client.reviewed(base, "aaaaaaaaaaaa", "")
    assert refused.value.code == "account_unknown"
    for head in ("", "aaaaaaa", "a" * 11, "A" * 12, "../x", None, 7, "a" * 41):
        with pytest.raises(Refused) as refused:
            await client.reviewed(base, head, account)
        assert refused.value.code == "review_state_unknown"
    assert len(fake.requests) == asked


@pytest.mark.parametrize("marked, head, same", [
    ("a" * 12, "a" * 12, True), ("a" * 40, "a" * 12, True), ("a" * 12, "a" * 40, True),
    ("a" * 12 + "b" * 28, "a" * 12 + "c" * 28, True),      # the first 12 decide
    ("a" * 11 + "b", "a" * 12, False), ("a" * 7, "a" * 12, False), ("a" * 11, "a" * 40, False),
])
async def test_a_marker_and_a_head_are_compared_on_their_first_twelve_characters(
        marked, head, same):
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1, head=head)
    fake.add_comment("svc-a", 1, "Automated review of commit " + marked, own=True)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert out["already_reviewed"] == (1 if same else 0)
    assert len(out["pull_requests"]) == (0 if same else 1)


async def test_a_head_commit_shorter_than_twelve_characters_is_never_called_reviewed():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1, head="aaaaaaa")
    fake.add_comment("svc-a", 1, "Automated review of commit aaaaaaa", own=True)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert out["pull_requests"] == [] and out["already_reviewed"] == 0
    assert out["pull_requests_unchecked"] == 1


async def test_the_list_is_capped_and_says_that_more_exist():
    fake = FakeBitbucket()
    for i in range(1, MAX_LISTED + 6):
        fake.add_pr("svc-a", i)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert len(out["pull_requests"]) == MAX_LISTED and out["more"] is True


async def test_a_full_list_stops_reading_and_says_more_while_repositories_are_unread():
    fake = FakeBitbucket()
    for i in range(1, MAX_LISTED + 1):
        fake.add_pr("svc-a", i)
    fake.add_pr("svc-b", 99)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert len(out["pull_requests"]) == MAX_LISTED and out["more"] is True
    assert out["repositories"] == 1 and not any("/svc-b/" in p for p in fake.paths())


async def test_a_second_page_of_open_pull_requests_is_not_read_but_said():
    fake = FakeBitbucket()
    fake.page_size = 2
    for i in range(1, 4):
        fake.add_pr("svc-a", i)
    out = await _call(_toolset(fake, repositories=["svc-a"]), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [1, 2] and out["more"] is True
    # The rotation never gets behind page 1: no promise of a later run.
    assert out["beyond_reach"] is True and out["note"] == BEYOND_NOTE
    assert sum(p.split("?")[0].endswith("/pullrequests") for p in fake.paths()) == 1


async def test_at_most_max_checked_pull_requests_have_their_comments_read():
    fake = FakeBitbucket()
    for i in range(1, MAX_CHECKED + 6):
        fake.add_pr("svc-a", i)
        fake.add_comment("svc-a", i, review_marker(HEAD), own=True)
    toolset = _toolset(fake)
    out = await _call(toolset, "list_pull_requests")
    assert out["pull_requests"] == [] and out["already_reviewed"] == MAX_CHECKED
    assert out["more"] is True and "later run" in out["note"]
    assert out["beyond_reach"] is False and "a person has to look" not in out["note"]
    assert sum(p.split("?")[0].endswith("/comments") for p in fake.paths()) == MAX_CHECKED


async def test_reviewed_pull_requests_do_not_starve_the_ones_behind_the_check_budget():
    fake = FakeBitbucket()
    last = range(MAX_CHECKED + 1, MAX_CHECKED + 6)
    for i in range(1, MAX_CHECKED + 6):
        fake.add_pr("svc-a", i)
        if i <= MAX_CHECKED:
            fake.add_comment("svc-a", i, review_marker(HEAD), own=True)
    toolset = _toolset(fake)
    out = await _call(toolset, "list_pull_requests")
    assert out["pull_requests"] == [] and out["more"] is True
    # The next call starts after the last pull request the first one checked.
    before = len(fake.requests)
    out = await _call(toolset, "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == list(last)
    assert out["already_reviewed"] == MAX_CHECKED - 5 and out["more"] is True
    checked = [p for p in fake.paths()[before:] if p.split("?")[0].endswith("/comments")]
    assert len(checked) == MAX_CHECKED and "/pullrequests/41/comments" in checked[0]
    # A third call goes on where the second stopped: nobody is skipped.
    out = await _call(toolset, "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == list(last)
    assert out["already_reviewed"] == MAX_CHECKED - 5 and out["more"] is True


async def test_the_turn_goes_round_over_several_repositories_and_a_full_list():
    fake = FakeBitbucket()
    for i in range(1, 16):
        fake.add_pr("svc-a", i)
    for i in range(16, 31):
        fake.add_pr("svc-b", i)
    toolset = _toolset(fake)
    seen = []
    for _ in range(3):
        out = await _call(toolset, "list_pull_requests")
        seen.append([p["id"] for p in out["pull_requests"]])
    assert seen[0] == list(range(1, 21)) and seen[1] == [*range(21, 31), *range(1, 11)]
    assert seen[2] == list(range(11, 31))


async def test_a_cursor_on_a_pull_request_that_is_gone_starts_its_repository_again():
    fake = FakeBitbucket()
    for i in range(1, MAX_LISTED + 6):
        fake.add_pr("svc-a", i)
    toolset = _toolset(fake)
    await _call(toolset, "list_pull_requests")               # stopped after number 20
    fake.prs[("svc-a", MAX_LISTED)]["state"] = "MERGED"
    out = await _call(toolset, "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]][:3] == [1, 2, 3]


async def test_the_comments_of_a_candidate_are_read_three_pages_deep():
    fake = FakeBitbucket()
    fake.page_size = 2
    fake.add_pr("svc-a", 1)
    for i in range(5):
        fake.add_comment("svc-a", 1, f"c{i}")
    fake.add_comment("svc-a", 1, review_marker(HEAD), own=True)       # on the third page
    fake.add_pr("svc-a", 2)
    for i in range(6):
        fake.add_comment("svc-a", 2, f"c{i}")
    fake.add_comment("svc-a", 2, review_marker(HEAD), own=True)       # on the fourth
    fake.add_pr("svc-b", 3)
    for i in range(6):
        fake.add_comment("svc-b", 3, f"c{i}")                         # three full pages, no more
    out = await _call(_toolset(fake, repositories=["svc-a", "svc-b"]), "list_pull_requests")
    # Number 2 has more comments than are read: its marker may be among them,
    # so it is neither "reviewed" nor "new", and it is not listed.
    assert [p["id"] for p in out["pull_requests"]] == [3] and out["already_reviewed"] == 1
    assert out["pull_requests_unchecked"] == 1 and "could not be checked" in out["note"]
    assert sum("/pullrequests/2/comments" in p for p in fake.paths()) == 3


async def test_an_author_cannot_push_the_marker_out_of_sight_to_get_reviewed_again():
    fake = FakeBitbucket()
    fake.page_size = 2
    fake.add_pr("svc-a", 1)
    fake.add_comment("svc-a", 1, review_marker(HEAD), own=True)
    toolset = _toolset(fake)
    assert (await _call(toolset, "list_pull_requests"))["already_reviewed"] == 1
    fake.comments[("svc-a", 1)][:0] = [
        {"id": i, "content": {"raw": "noise"}, "user": {"uuid": OTHER_UUID}} for i in range(1, 8)]
    out = await _call(toolset, "list_pull_requests")
    assert out["pull_requests"] == [] and out["already_reviewed"] == 0
    assert out["pull_requests_unchecked"] == 1


async def test_the_workspace_listing_keeps_slugs_only_and_stops_at_its_cap():
    fake = FakeBitbucket()
    fake.page_size = 100
    fake.repos = ["svc-a", "Not A Slug", "../x"] + [
        f"r{i}" for i in range(MAX_REPOSITORIES_SCANNED)]
    fake.add_pr("svc-a", 1)

    def answer(request):
        if request.url.path == f"/2.0/repositories/{WS}":
            return httpx.Response(200, json={"values": [
                *({"slug": r} for r in fake.repos), {"slug": 7}, "text", {"name": "x"}]})
        return None

    fake.override = answer
    out = await _call(_toolset(fake), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [1]
    assert out["repositories"] == MAX_REPOSITORIES_SCANNED and out["more"] is True
    assert out["beyond_reach"] is True and out["note"] == BEYOND_NOTE
    assert not any("Not" in p or ".." in p for p in fake.paths())
    listing = [r for r in fake.requests if r.url.path == f"/2.0/repositories/{WS}"]
    assert len(listing) == 1 and listing[0].url.params.get("pagelen") == "100"


async def test_a_workspace_listing_with_a_next_page_says_more():
    fake = FakeBitbucket()
    fake.page_size = 1
    fake.add_pr("svc-a", 1)
    fake.repos.append("svc-b")
    out = await _call(_toolset(fake), "list_pull_requests")
    assert out["repositories"] == 1 and out["more"] is True
    assert sum(p.split("?")[0].endswith(f"/repositories/{WS}") for p in fake.paths()) == 1
    assert out["beyond_reach"] is True and out["note"] == BEYOND_NOTE


async def test_a_workspace_that_cannot_be_listed_is_the_calls_error():
    fake = FakeBitbucket()
    fake.override = lambda r: (httpx.Response(403, text="PLANTED-ERROR-BODY")
                               if r.url.path == f"/2.0/repositories/{WS}" else None)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert out["error"]["code"] == "bitbucket_forbidden" and "PLANTED" not in json.dumps(out)


async def test_a_repository_that_is_refused_costs_only_itself():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1)
    fake.add_pr("svc-b", 2)
    fake.override = lambda r: (httpx.Response(403, text="PLANTED-ERROR-BODY")
                               if "/svc-a/pullrequests" in r.url.path else None)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [2] and out["repositories_failed"] == 1
    assert out["repositories"] == 1 and "PLANTED" not in json.dumps(out)


async def test_a_pull_request_whose_comments_cannot_be_read_is_not_listed_as_unreviewed():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1)
    fake.add_pr("svc-a", 2)
    fake.add_pr("svc-b", 3)
    fake.override = lambda r: (httpx.Response(500, text="PLANTED-ERROR-BODY")
                               if r.url.path.endswith("/svc-a/pullrequests/2/comments") else None)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [1, 3]
    assert out["repositories_failed"] == 0 and out["repositories"] == 2
    assert out["pull_requests_unchecked"] == 1


@pytest.mark.parametrize("answer", [
    httpx.Response(500, text="PLANTED-ERROR-BODY"), httpx.Response(404, text="PLANTED-ERROR-BODY"),
    httpx.Response(200, content=b'{"values": ["' + b"x" * 2_000_001 + b'"]}'),
    httpx.Response(200, text="<html>PLANTED-ERROR-BODY</html>"),
])
async def test_one_unreadable_pull_request_does_not_drop_the_rest_of_its_repository(answer):
    fake = FakeBitbucket()
    for i in (1, 2, 3):
        fake.add_pr("svc-a", i)
    fake.add_comment("svc-a", 3, review_marker(HEAD), own=True)
    fake.override = lambda r: (answer if r.url.path.endswith("/svc-a/pullrequests/1/comments")
                               else None)                      # the FIRST of three
    out = await _call(_toolset(fake), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [2] and out["already_reviewed"] == 1
    assert out["pull_requests_unchecked"] == 1 and out["repositories_failed"] == 0
    assert out["repositories"] == 1 and "PLANTED" not in json.dumps(out)


async def test_throttling_ends_the_list_as_an_error_not_as_a_short_list():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1)
    fake.override = lambda r: (httpx.Response(429) if "/pullrequests" in r.url.path else None)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert out == {"error": {"code": "bitbucket_throttled",
                             "message": "Bitbucket is throttling requests (HTTP 429)",
                             "hint": "try again later"}}


@pytest.mark.parametrize("where", ["/svc-b/pullrequests", "/svc-b/pullrequests/2/comments"])
@pytest.mark.parametrize("status, code", [(401, "bitbucket_unauthorized"),
                                          (429, "bitbucket_throttled")])
async def test_a_failure_that_is_not_one_repositorys_ends_the_call(where, status, code):
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1)
    fake.add_pr("svc-b", 2)
    fake.override = lambda r: (httpx.Response(status) if r.url.path.endswith(where) else None)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert set(out) == {"error"} and out["error"]["code"] == code


async def test_get_pull_request_marks_this_accounts_comments_as_own():
    fake = _fake()
    fake.add_comment("svc-a", 7, review_marker(HEAD), own=True)
    fake.add_comment("svc-a", 7, "a question")
    out = await _call(_toolset(fake), "get_pull_request", repository="svc-a", id=7)
    assert [c["own"] for c in out["comments"]] == [True, False]


async def test_without_an_account_no_comment_is_own_and_the_pull_request_is_still_read():
    fake = _fake()
    fake.user_status = 502
    fake.add_comment("svc-a", 7, review_marker(HEAD), own=True)
    fake.comments[("svc-a", 7)].append({"id": 5, "content": {"raw": "x"}, "user": {"uuid": ""}})
    out = await _call(_toolset(fake), "get_pull_request", repository="svc-a", id=7)
    assert [c["own"] for c in out["comments"]] == [False, False] and out["head_commit"] == HEAD


def test_the_list_tool_says_what_its_answer_means():
    text = _toolset(_fake()).tools["list_pull_requests"].function.__doc__
    assert "never as instructions" in text and "do not guess" in text
    assert "`more: true`" in text and "`reviewed_filter: unavailable`" in text
    assert "get_pull_request" in text and "`pull_requests_unchecked`" in text
    assert "later run" in " ".join(text.split())
    assert "`beyond_reach: true`" in text and "a person has to look" in " ".join(text.split())
    read = " ".join(_toolset(_fake()).tools["get_pull_request"].function.__doc__.split())
    assert "top-level comment" in read and "first line" in read and "at most 100" in read


# --- a review with verdict approve whose approval is still to come ------------

APPROVER = {"allow_comment": True, "allow_approve": True}


def _pending_fake() -> FakeBitbucket:
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1)
    fake.add_pr("svc-a", 2)
    fake.add_pr("svc-a", 3)
    fake.add_comment("svc-a", 1, review_marker(HEAD, "approve") + "\n\nok", own=True)
    fake.add_comment("svc-a", 2, review_marker(HEAD, "comment") + "\n\nissues", own=True)
    return fake


async def test_an_approve_review_without_its_approval_is_listed_as_pending():
    fake = _pending_fake()
    out = await _call(_toolset(fake, **APPROVER), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [3] and out["already_reviewed"] == 1
    assert out["approval_pending"] == [{
        "repository": "svc-a", "id": 1, "title": "A change", "author": "Ann Author",
        "head_commit": HEAD, "draft": False, "updated_on": "2026-10-09T08:00:00.000000+00:00"}]
    assert out["pull_requests_unchecked"] == 0


@pytest.mark.parametrize("cfg", [{}, {"allow_comment": True}])
async def test_without_allow_approve_nothing_is_pending(cfg):
    fake = _pending_fake()
    out = await _call(_toolset(fake, **cfg), "list_pull_requests")
    assert "approval_pending" not in out and out["already_reviewed"] == 2
    assert [p["id"] for p in out["pull_requests"]] == [3]
    assert fake.paths().count(f"GET /2.0/repositories/{WS}/svc-a/pullrequests/1") == 0


@pytest.mark.parametrize("participants, where", [
    ([{"user": {"uuid": OWN_UUID}, "approved": True}], "already_reviewed"),
    ([{"user": {"uuid": OTHER_UUID}, "approved": True}], "approval_pending"),
    ([{"user": {"uuid": OWN_UUID}, "approved": False}], "approval_pending"),
    ([], "approval_pending"),
    (None, "pull_requests_unchecked"), ("none", "pull_requests_unchecked"),
    (["text"], "pull_requests_unchecked"),
    ([{"user": {"uuid": OWN_UUID}, "approved": "false"}], "pull_requests_unchecked"),
    ([{"user": {"uuid": OWN_UUID}}], "pull_requests_unchecked"),
    ([{"user": {"uuid": OWN_UUID}, "approved": False},
      {"user": {"uuid": OWN_UUID}, "approved": True}], "pull_requests_unchecked"),
])
async def test_pending_is_decided_by_this_accounts_own_participant_entry(participants, where):
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1)["participants"] = participants
    fake.add_comment("svc-a", 1, review_marker(HEAD, "approve"), own=True)
    out = await _call(_toolset(fake, **APPROVER), "list_pull_requests")
    assert out["pull_requests"] == []
    assert len(out["approval_pending"]) == (1 if where == "approval_pending" else 0)
    assert out["already_reviewed"] == (1 if where == "already_reviewed" else 0)
    assert out["pull_requests_unchecked"] == (1 if where == "pull_requests_unchecked" else 0)


async def test_a_pull_request_without_participants_in_its_answer_is_unchecked():
    fake = FakeBitbucket()
    del fake.add_pr("svc-a", 1)["participants"]
    fake.add_comment("svc-a", 1, review_marker(HEAD, "approve"), own=True)
    out = await _call(_toolset(fake, **APPROVER), "list_pull_requests")
    assert out["approval_pending"] == [] and out["pull_requests_unchecked"] == 1


async def test_somebody_elses_approve_marker_makes_nothing_pending():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 1)
    fake.add_comment("svc-a", 1, review_marker(HEAD, "approve"))               # the author
    fake.add_comment("svc-a", 1, "x\n" + review_marker(HEAD, "approve"), own=True)
    out = await _call(_toolset(fake, **APPROVER), "list_pull_requests")
    assert out["approval_pending"] == [] and [p["id"] for p in out["pull_requests"]] == [1]


async def test_a_pending_pull_request_that_cannot_be_read_again_is_unchecked_not_dropped():
    fake = _pending_fake()
    fake.override = lambda r: (httpx.Response(500, text="PLANTED-ERROR-BODY")
                               if r.url.path.endswith("/svc-a/pullrequests/1") else None)
    out = await _call(_toolset(fake, **APPROVER), "list_pull_requests")
    assert out["approval_pending"] == [] and out["pull_requests_unchecked"] == 1
    assert [p["id"] for p in out["pull_requests"]] == [3]


@pytest.mark.parametrize("change", [
    {"state": "MERGED"}, {"state": "DECLINED"}, {"destination": {"branch": {"name": "develop"}}},
    {"source": {"commit": {"hash": "cccccccccccc"}}}])
async def test_a_pull_request_that_closed_or_moved_since_the_listing_is_dropped(change):
    fake = _pending_fake()
    answer = httpx.Response(200, json={**fake.prs[("svc-a", 1)], **change})
    fake.override = lambda r: answer if r.url.path.endswith("/svc-a/pullrequests/1") else None
    out = await _call(_toolset(fake, **APPROVER), "list_pull_requests")
    assert out["approval_pending"] == [] and out["pull_requests_unchecked"] == 0
    assert [p["id"] for p in out["pull_requests"]] == [3] and out["already_reviewed"] == 1
    assert "note" not in out


async def test_both_limits_are_said_each_with_its_own_note():
    fake = FakeBitbucket()
    fake.page_size = MAX_LISTED + 1
    for i in range(1, MAX_LISTED + 3):
        fake.add_pr("svc-a", i)
    out = await _call(_toolset(fake, repositories=["svc-a"]), "list_pull_requests")
    assert out["more"] is True and out["beyond_reach"] is True
    assert out["note"] == "more: true: a limit was reached; the rest comes in a later run; " \
        + BEYOND_NOTE


async def test_approve_is_not_answered_from_a_partial_read_but_comment_is():
    fake = FakeBitbucket()
    fake.page_size = 2
    fake.add_pr("svc-a", 1)
    fake.add_comment("svc-a", 1, review_marker(HEAD, "approve"), own=True)
    for i in range(6):
        fake.add_comment("svc-a", 1, f"c{i}")
    client = _toolset(fake).client
    base = f"/2.0/repositories/{WS}/svc-a/pullrequests/1"
    with pytest.raises(Refused) as refused:
        await client.reviewed(base, HEAD, OWN_UUID)
    assert refused.value.code == "review_state_unknown"
    fake.comments[("svc-a", 1)][1] = {**fake.comments[("svc-a", 1)][0], "id": 999,
                                      "content": {"raw": review_marker(HEAD, "comment")}}
    assert await client.reviewed(base, HEAD, OWN_UUID) == "comment"
    fake.comments[("svc-a", 1)] = fake.comments[("svc-a", 1)][:1]         # all of them read
    assert await client.reviewed(base, HEAD, OWN_UUID) == "approve"


def test_the_list_tool_says_what_pending_means():
    text = " ".join(_toolset(_fake(), **APPROVER).tools["list_pull_requests"]
                    .function.__doc__.split())
    assert "`approval_pending`" in text and "complete_approval" in text


# -- pull request text is filtered by character before the model sees it --------

# Control, format (bidi, zero width) and line/paragraph separator characters.
_ODD = "\x00\x07\x1b\x7f\x85​‎‮⁦⁩﻿  "


async def test_pull_request_text_loses_control_and_format_characters():
    fake = FakeBitbucket()
    odd = "".join(f"w{c}" for c in _ODD)
    plain = "w" * len(_ODD)
    fake.add_pr("svc-a", 7, head=HEAD, title=f"A\r\nti\ttle{odd}", author=f"An\nn\t{odd}",
                description=f"line 1\r\n\tline 2{odd}")
    fake.add_comment("svc-a", 7, f"see\n\tbelow{odd}")
    fake.comments[("svc-a", 7)][0]["user"]["display_name"] = f"Ca\nrl{odd}"
    toolset = _toolset(fake)
    out = await _call(toolset, "get_pull_request", repository="svc-a", id=7)
    # One line: no line break and no tab at all.
    assert out["title"] == "Atitle" + plain and out["author"] == "Ann" + plain
    # Several lines: the line feed and the tab stay, nothing else.
    assert out["description"] == "line 1\n\tline 2" + plain
    assert out["comments"][0]["text"] == "see\n\tbelow" + plain
    assert out["comments"][0]["author"] == "Carl" + plain
    listed = (await _call(toolset, "list_pull_requests"))["pull_requests"]
    assert [(p["title"], p["author"]) for p in listed] == [("Atitle" + plain, "Ann" + plain)]


async def test_the_length_cut_counts_what_is_left_after_the_filter():
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 7, head=HEAD, title="‮" * 400 + "t" * 300,
                description="\x00" * 5000 + "d" * 4001)
    out = await _call(_toolset(fake), "get_pull_request", repository="svc-a", id=7)
    assert out["title"] == "t" * 300
    assert out["description"] == "d" * 4000 + "…[truncated]"


async def test_diff_and_file_content_are_not_filtered():
    fake = _fake()
    fake.diffs[("svc-a", 7)] = "+a‮b\r\n+\x0c\n"
    fake.files[("svc-a", HEAD, "src/x.py")] = "a‮b\r\n\x0c ".encode()
    toolset = _toolset(fake)
    diff = await _call(toolset, "get_diff", repository="svc-a", id=7)
    file = await _call(toolset, "get_file", repository="svc-a", id=7, path="src/x.py")
    assert diff["diff"] == "+a‮b\r\n+\x0c\n" and file["content"] == "a‮b\r\n\x0c "


async def test_the_review_marker_is_still_read_from_the_unfiltered_comment():
    # The filter is for what the model reads; what counts as this account's
    # review is decided on Bitbucket's own text, as before.
    from agents.bitbucket_tools import review_marker
    fake = FakeBitbucket()
    fake.add_pr("svc-a", 7, head=HEAD)
    fake.add_comment("svc-a", 7, review_marker(HEAD).replace("review", "re​view"), own=True)
    out = await _call(_toolset(fake), "list_pull_requests")
    assert [p["id"] for p in out["pull_requests"]] == [7] and out["already_reviewed"] == 0


async def test_a_too_large_diff_of_one_file_does_not_ask_for_one_file_again():
    """Final review n9: with `path` set the hint "ask for one file with path"
    sent the model round in a circle."""
    fake = _fake()
    fake.diffs[("svc-a", 7)] = "x" * (MAX_DIFF_CHARS + 1)
    fake.diffstats[("svc-a", 7)] = [
        {"status": "modified", "lines_added": 1, "lines_removed": 0, "new": {"path": "src/x.py"}}]
    out = await _call(_toolset(fake), "get_diff", repository="svc-a", id=7, path="src/x.py")
    assert out["error"] == {"code": "result_too_large",
                            "message": "the diff does not fit a tool answer",
                            "hint": "this file's diff is too large to return"}
