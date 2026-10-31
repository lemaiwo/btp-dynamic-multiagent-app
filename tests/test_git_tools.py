"""``builtin:git``: each tool against a fake transport per provider.

The request shapes of the three commit flows (GitHub blobs -> tree -> commit
-> ref, Bitbucket one multipart ``src`` POST, GitLab one ``commits`` POST with
``actions``), one commit per call, the prefix rule, the pipeline-file
refusal, the caps, ``already_exists`` / ``already_open``, the 429 retry,
``outcome_unknown``, binary files, the redirect rules, and that no URL or
token ends up in a result or a log line."""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import httpx  # noqa: E402
import pytest  # noqa: E402

from agents.destination import Destination  # noqa: E402
from agents.git_http import ERROR_CODES, build_http_client  # noqa: E402
from agents.git_tools import git_toolset  # noqa: E402

TOKEN = "Bearer t0k3n-Zx9"
SHA = {n: (str(n) * 40)[:40] for n in range(1, 10)}
HEAD, TREE, BLOB, NEWTREE, COMMIT = SHA[1], SHA[2], SHA[3], SHA[4], SHA[5]
GH = {"destination": "GITHUB", "provider": "github", "repositories": ["acme/web"],
      "allow_push": True, "allow_pull_request": True}
BB = {"destination": "BITBUCKET", "provider": "bitbucket", "repositories": ["acme-ws/svc-a"],
      "allow_push": True, "allow_pull_request": True}
GL = {"destination": "GITLAB", "provider": "gitlab", "repositories": ["group/sub/project"],
      "allow_push": True, "allow_pull_request": True}
GH_URL, BB_URL, GL_URL = ("https://api.github.com", "https://api.bitbucket.org/2.0",
                          "https://gitlab.example.com/api/v4")
GH_REPO = "/repos/acme/web"
BB_REPO = "/2.0/repositories/acme-ws/svc-a"
GL_REPO = "/api/v4/projects/group%2Fsub%2Fproject"


class FakeResolver:
    name = "GIT"

    def __init__(self, url: str, proxy_type: str = "Internet") -> None:
        self.url, self.proxy_type = url, proxy_type

    async def resolve(self, *, force: bool = False, user_token=None, principal=None):
        return Destination(url=self.url, headers={"Authorization": TOKEN},
                           expires_at=time.monotonic() + 60, proxy_type=self.proxy_type)

    def invalidate(self, principal: str | None = None) -> None:
        pass


def J(status: int, body: Any = None, headers: dict[str, str] | None = None):
    """A route answer: a fresh response per call."""
    return lambda request: httpx.Response(status, json=body, headers=headers)


class Fake:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Any] = {}

    def on(self, method: str, path: str, answer: Any) -> None:
        self.routes[(method, path)] = answer

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.split(b"?")[0].decode("ascii")
        answer = self.routes.get((request.method, path))
        if answer is None:
            return httpx.Response(404)
        return answer(request)

    def seen(self) -> list[str]:
        return [f"{r.method} {r.url.raw_path.split(b'?')[0].decode()}" for r in self.requests]

    def body(self, index: int) -> Any:
        return json.loads(self.requests[index].content)


def make(cfg: dict[str, Any], fake: Fake, url: str, *, proxy_type: str = "Internet",
         agent: str = "ui5-dev"):
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    http = build_http_client(cfg, "builtin:git", transport=httpx.MockTransport(fake.handler),
                             resolver=FakeResolver(url, proxy_type))
    return git_toolset(cfg, http=http, agent_name=agent, sleep=sleep), slept


async def call(toolset, tool_name: str, /, **kw: Any) -> dict[str, Any]:
    return await toolset.tools[tool_name].function(**kw)


def code(result: dict[str, Any]) -> str | None:
    error = result.get("error")
    return error.get("code") if isinstance(error, dict) else None


# --- registration of the tools -------------------------------------------------

READS = {"list_repositories", "list_files", "read_repository_file", "list_branches",
         "get_pull_request", "get_pipeline_status"}


def test_the_write_tools_exist_only_with_their_switch():
    fake = Fake()
    cfg = {k: v for k, v in GH.items() if not k.startswith("allow_")}
    assert set(make(cfg, fake, GH_URL)[0].tools) == READS
    assert set(make({**cfg, "allow_push": True}, fake, GH_URL)[0].tools) == READS | {
        "create_branch", "commit_files"}
    assert set(make({**cfg, "allow_pull_request": True}, fake, GH_URL)[0].tools) == READS | {
        "open_pull_request"}


def test_no_tool_is_named_like_a_scratchpad_tool():
    from agents.deep import DeepConfig, deep_toolset

    toolset, _ = make(GH, Fake(), GH_URL)
    from pydantic_ai.models.test import TestModel

    deep = deep_toolset(DeepConfig(enabled=True), parent_toolsets=[], model=TestModel(),
                        agent_name="a")
    assert not set(toolset.tools) & set(deep.tools)


# --- the three commit flows ----------------------------------------------------

def _github_commit_routes(fake: Fake) -> None:
    fake.on("GET", f"{GH_REPO}/git/ref/heads/agent/fix", J(200, {"object": {"sha": HEAD}}))
    fake.on("GET", f"{GH_REPO}/git/commits/{HEAD}", J(200, {"tree": {"sha": TREE}}))
    fake.on("POST", f"{GH_REPO}/git/blobs", J(201, {"sha": BLOB}))
    fake.on("POST", f"{GH_REPO}/git/trees", J(201, {"sha": NEWTREE}))
    fake.on("POST", f"{GH_REPO}/git/commits",
            J(201, {"sha": COMMIT, "html_url": f"https://github.com/acme/web/commit/{COMMIT}"}))
    fake.on("PATCH", f"{GH_REPO}/git/refs/heads/agent/fix", J(200, {"object": {"sha": COMMIT}}))


async def test_github_commits_through_blobs_tree_commit_and_one_ref_update():
    from agents.auth import current_principal
    from agents.webchat.docgen import ChatRunContext, current_chat_run

    fake = Fake()
    _github_commit_routes(fake)
    toolset, _ = make(GH, fake, GH_URL)

    async def save(*a):
        return {}

    p = current_principal.set("dev@example.com")
    c = current_chat_run.set(ChatRunContext(conversation_id="conv-42", message_id="m1",
                                            principal="dev@example.com", save=save))
    try:
        result = await call(toolset, "commit_files", repository="acme/web", branch="agent/fix",
                            message="Fix the label", files=[
                                {"path": "src/a.js", "content": "a"},
                                {"path": "src/b.js", "content": "b"}],
                            deletes=["old.txt"])
    finally:
        current_chat_run.reset(c)
        current_principal.reset(p)
    assert result == {"repository": "acme/web", "commit": COMMIT, "branch": "agent/fix",
                      "files": ["src/a.js", "src/b.js"], "deletes": ["old.txt"],
                      "url": f"https://github.com/acme/web/commit/{COMMIT}"}
    assert fake.seen() == [
        f"GET {GH_REPO}/git/ref/heads/agent/fix", f"GET {GH_REPO}/git/commits/{HEAD}",
        f"POST {GH_REPO}/git/blobs", f"POST {GH_REPO}/git/blobs", f"POST {GH_REPO}/git/trees",
        f"POST {GH_REPO}/git/commits", f"PATCH {GH_REPO}/git/refs/heads/agent/fix"]
    assert fake.body(2) == {"content": "a", "encoding": "utf-8"}
    assert fake.body(4) == {"base_tree": TREE, "tree": [
        {"path": "src/a.js", "mode": "100644", "type": "blob", "sha": BLOB},
        {"path": "src/b.js", "mode": "100644", "type": "blob", "sha": BLOB},
        {"path": "old.txt", "mode": "100644", "type": "blob", "sha": None}]}
    commit = fake.body(5)
    assert commit["tree"] == NEWTREE and commit["parents"] == [HEAD]
    assert commit["message"] == ("Fix the label\n\nWritten by ui5-dev for dev@example.com in "
                                 "web chat conversation conv-42")
    assert fake.body(6) == {"sha": COMMIT, "force": False}
    assert all(r.headers["authorization"] == TOKEN for r in fake.requests)
    assert all(r.url.host == "api.github.com" for r in fake.requests)


async def test_the_trailer_names_the_run_outside_the_web_chat():
    from agents.run_activity import current_run_id

    fake = Fake()
    _github_commit_routes(fake)
    toolset, _ = make(GH, fake, GH_URL, agent="cap-dev‮\nx")
    token = current_run_id.set("run-7")
    try:
        await call(toolset, "commit_files", repository="acme/web", branch="agent/fix",
                   message="m", files=[{"path": "a", "content": "x"}])
    finally:
        current_run_id.reset(token)
    assert fake.body(4)["message"] == "m\n\nWritten by cap-devx for an unknown user in run run-7"


async def test_bitbucket_commits_with_one_multipart_src_post():
    fake = Fake()
    fake.on("GET", f"{BB_REPO}/refs/branches/agent/fix", J(200, {"target": {"hash": HEAD}}))
    fake.on("POST", f"{BB_REPO}/src", J(201, None, {
        "Location": f"https://api.bitbucket.org/2.0/repositories/acme-ws/svc-a/commit/{COMMIT}"}))
    toolset, _ = make(BB, fake, BB_URL)
    result = await call(toolset, "commit_files", repository="acme-ws/svc-a", branch="agent/fix",
                        message="Change", files=[{"path": "src/app.js", "content": "x = 1\n"},
                                                 {"path": "message", "content": "m"}],
                        deletes=["gone.txt"])
    assert result["commit"] == COMMIT and result["url"] is None
    assert fake.seen() == [f"GET {BB_REPO}/refs/branches/agent/fix", f"POST {BB_REPO}/src"]
    sent = fake.requests[1]
    assert sent.headers["content-type"].startswith("multipart/form-data")
    body = sent.content.decode()
    for field in ('name="message"', 'name="branch"', 'name="parents"', 'name="files"',
                  'name="src/app.js"', 'name="/message"'):
        assert field in body
    assert "agent/fix" in body and HEAD in body and "gone.txt" in body and "x = 1" in body
    assert "Change\n\nWritten by ui5-dev for an unknown user" in body


async def test_gitlab_commits_with_one_commits_post_and_actions():
    fake = Fake()
    fake.on("GET", f"{GL_REPO}/repository/branches/agent%2Ffix", J(200, {"commit": {"id": HEAD}}))
    fake.on("HEAD", f"{GL_REPO}/repository/files/src%2Fold.js",
            lambda r: httpx.Response(200))
    fake.on("POST", f"{GL_REPO}/repository/commits", J(201, {
        "id": COMMIT, "web_url": "https://gitlab.example.com/group/sub/project/-/commit/x"}))
    toolset, _ = make(GL, fake, GL_URL)
    result = await call(toolset, "commit_files", repository="group/sub/project",
                        branch="agent/fix", message="Change",
                        files=[{"path": "src/old.js", "content": "1"},
                               {"path": "src/new.js", "content": "2"}],
                        deletes=["README.old"])
    assert result["commit"] == COMMIT
    assert result["url"] == "https://gitlab.example.com/group/sub/project/-/commit/x"
    assert fake.seen() == [
        f"GET {GL_REPO}/repository/branches/agent%2Ffix",
        f"HEAD {GL_REPO}/repository/files/src%2Fold.js",
        f"HEAD {GL_REPO}/repository/files/src%2Fnew.js",
        f"POST {GL_REPO}/repository/commits"]
    body = fake.body(3)
    assert body["branch"] == "agent/fix" and body["commit_message"].startswith("Change\n\n")
    assert body["actions"] == [
        {"action": "update", "file_path": "src/old.js", "content": "1", "encoding": "text"},
        {"action": "create", "file_path": "src/new.js", "content": "2", "encoding": "text"},
        {"action": "delete", "file_path": "README.old"}]
    assert sum(1 for s in fake.seen() if s.startswith("POST")) == 1


# --- the prefix rule, the pipeline files and the caps ---------------------------

@pytest.mark.parametrize("branch", ["main", "develop", "feature/x", "agent/", "agentx/y",
                                    "agent/../main", None, 7])
async def test_a_push_goes_only_to_an_agent_branch(branch):
    fake = Fake()
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "commit_files", repository="acme/web", branch=branch,
                        message="m", files=[{"path": "a", "content": "x"}])
    assert code(result) == "branch_not_allowed" and fake.requests == []
    result = await call(toolset, "open_pull_request", repository="acme/web", branch=branch,
                        title="t", body="")
    assert code(result) == "branch_not_allowed" and fake.requests == []


@pytest.mark.parametrize("path", [".github/workflows/deploy.yml", "bitbucket-pipelines.yml",
                                  ".gitlab-ci.yml", ".gitlab/ci.yml"])
async def test_pipeline_files_are_refused_before_anything_is_sent(path):
    fake = Fake()
    toolset, _ = make(GH, fake, GH_URL)
    for kw in ({"files": [{"path": path, "content": "x"}]},
               {"files": [], "deletes": [path]}):
        result = await call(toolset, "commit_files", repository="acme/web", branch="agent/x",
                            message="m", **kw)
        assert code(result) == "pipeline_file"
    assert fake.requests == []


@pytest.mark.parametrize("kw, expected", [
    ({"files": [{"path": f"f{i}", "content": "x"} for i in range(51)]}, "invalid_argument"),
    ({"files": [{"path": "f", "content": "x"}], "deletes": [f"d{i}" for i in range(50)]},
     "invalid_argument"),
    ({"files": []}, "invalid_argument"),
    ({"files": [{"path": "big", "content": "x" * 1_000_001}]}, "file_too_large"),
    ({"files": [{"path": f"f{i}", "content": "é" * 400_000} for i in range(7)]},
     "file_too_large"),
    ({"files": [{"path": "bin", "content": "a\x00b"}]}, "binary_file"),
    ({"files": [{"path": "bad", "content": "\ud800"}]}, "binary_file"),
    ({"files": [{"path": "n", "content": 3}]}, "invalid_argument"),
    ({"files": [{"path": "/abs", "content": "x"}]}, "invalid_path"),
    ({"files": [{"path": "a/../b", "content": "x"}]}, "invalid_path"),
    ({"files": [{"path": "a\x07", "content": "x"}]}, "invalid_path"),
    ({"files": [{"path": "a", "content": "x", "mode": "755"}]}, "invalid_argument"),
    ({"files": [{"path": "a", "content": "x"}], "deletes": ["a"]}, "invalid_argument"),
    ({"files": [{"path": "a", "content": "x"}], "message": ""}, "invalid_argument"),
    ({"files": [{"path": "a", "content": "x"}], "message": "m" * 2001}, "invalid_argument"),
    ({"files": [{"path": "a", "content": "x"}], "message": "a​b"}, "invalid_argument"),
])
async def test_the_caps_refuse_before_anything_is_sent(kw, expected):
    fake = Fake()
    toolset, _ = make(GH, fake, GH_URL)
    args = {"repository": "acme/web", "branch": "agent/x", "message": "m", **kw}
    result = await call(toolset, "commit_files", **args)
    assert code(result) == expected and fake.requests == []


async def test_a_repository_outside_the_pin_is_refused_naming_the_pin_only():
    fake = Fake()
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "read_repository_file", repository="evil/Zx9repo", path="a")
    assert code(result) == "repository_not_allowed"
    assert result["error"]["hint"] == "repositories: acme/web"
    assert "Zx9repo" not in json.dumps(result) and fake.requests == []


# --- branches and pull requests ---------------------------------------------------

async def test_create_branch_prefixes_the_name_and_starts_at_the_base_branch():
    fake = Fake()
    fake.on("GET", f"{GH_REPO}/git/ref/heads/main", J(200, {"object": {"sha": HEAD}}))
    fake.on("POST", f"{GH_REPO}/git/refs", J(201, {"ref": "refs/heads/agent/fix-1"}))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "create_branch", repository="acme/web", name="fix-1")
    assert result == {"repository": "acme/web", "branch": "agent/fix-1", "from": "main",
                      "commit": HEAD}
    assert fake.seen() == [f"GET {GH_REPO}/git/ref/heads/agent/fix-1",
                           f"GET {GH_REPO}/git/ref/heads/main", f"POST {GH_REPO}/git/refs"]
    assert fake.body(2) == {"ref": "refs/heads/agent/fix-1", "sha": HEAD}


async def test_create_branch_answers_already_exists_and_sends_no_write():
    fake = Fake()
    fake.on("GET", f"{GH_REPO}/git/ref/heads/agent/fix-1", J(200, {"object": {"sha": HEAD}}))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "create_branch", repository="acme/web", name="fix-1")
    assert code(result) == "already_exists"
    assert [s for s in fake.seen() if not s.startswith("GET")] == []


@pytest.mark.parametrize("name", ["", "-x", "a..b", "x" * 82, "a b", None])
async def test_create_branch_refuses_a_bad_name(name):
    fake = Fake()
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "create_branch", repository="acme/web", name=name)
    assert code(result) == "invalid_argument" and fake.requests == []


async def test_create_branch_from_a_missing_branch_is_not_found():
    fake = Fake()
    toolset, _ = make(BB, fake, BB_URL)
    result = await call(toolset, "create_branch", repository="acme-ws/svc-a", name="x",
                        from_ref="release/1")
    assert code(result) == "not_found"
    assert fake.seen() == [f"GET {BB_REPO}/refs/branches/agent/x",
                           f"GET {BB_REPO}/refs/branches/release/1"]


async def test_an_open_pull_request_is_answered_instead_of_a_second_one():
    fake = Fake()
    fake.on("GET", f"{GH_REPO}/git/ref/heads/agent/fix", J(200, {"object": {"sha": HEAD}}))
    fake.on("GET", f"{GH_REPO}/pulls", J(200, [
        {"number": 12, "state": "open", "html_url": "https://github.com/acme/web/pull/12"}]))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "open_pull_request", repository="acme/web",
                        branch="agent/fix", title="Fix", body="Body")
    assert result == {"repository": "acme/web", "number": 12,
                      "url": "https://github.com/acme/web/pull/12", "state": "open",
                      "already_open": True}
    assert not [s for s in fake.seen() if s.startswith("POST")]
    assert fake.requests[1].url.params["head"] == "acme:agent/fix"
    assert fake.requests[1].url.params["base"] == "main"


@pytest.mark.parametrize("cfg, url, routes, post_path, expect_body", [
    (GH, GH_URL, {("GET", f"{GH_REPO}/git/ref/heads/agent/fix"): {"object": {"sha": HEAD}},
                  ("GET", f"{GH_REPO}/pulls"): []},
     f"{GH_REPO}/pulls", {"title": "Fix", "body": "", "head": "agent/fix", "base": "main"}),
    (BB, BB_URL, {("GET", f"{BB_REPO}/refs/branches/agent/fix"): {"target": {"hash": HEAD}},
                  ("GET", f"{BB_REPO}/pullrequests"): {"values": []}},
     f"{BB_REPO}/pullrequests", {"title": "Fix", "description": "",
                                 "source": {"branch": {"name": "agent/fix"}},
                                 "destination": {"branch": {"name": "main"}}}),
    (GL, GL_URL, {("GET", f"{GL_REPO}/repository/branches/agent%2Ffix"): {"commit": {"id": HEAD}},
                  ("GET", f"{GL_REPO}/merge_requests"): []},
     f"{GL_REPO}/merge_requests", {"source_branch": "agent/fix", "target_branch": "main",
                                   "title": "Fix", "description": ""}),
])
async def test_open_pull_request_per_provider(cfg, url, routes, post_path, expect_body):
    fake = Fake()
    for (method, path), body in routes.items():
        fake.on(method, path, J(200, body))
    answer = {"number": 3, "id": 3, "iid": 3, "state": "open" if cfg is GH else (
        "OPEN" if cfg is BB else "opened"), "html_url": "https://x.example/pr/3",
        "web_url": "https://x.example/pr/3", "links": {"html": {"href": "https://x.example/pr/3"}}}
    fake.on("POST", post_path, J(201, answer))
    toolset, _ = make(cfg, fake, url)
    result = await call(toolset, "open_pull_request", repository=cfg["repositories"][0],
                        branch="agent/fix", title="Fix", body="")
    assert result["number"] == 3 and result["state"] == "open"
    assert result["already_open"] is False and result["url"] == "https://x.example/pr/3"
    assert fake.body(len(fake.requests) - 1) == expect_body


async def test_pull_request_title_and_body_caps():
    fake = Fake()
    toolset, _ = make(GH, fake, GH_URL)
    for kw in ({"title": "t" * 301, "body": ""}, {"title": "", "body": ""},
               {"title": "t", "body": "b" * 20_001}, {"title": "a\x1bb", "body": ""}):
        result = await call(toolset, "open_pull_request", repository="acme/web",
                            branch="agent/x", **kw)
        assert code(result) == "invalid_argument"
    assert fake.requests == []


async def test_get_pull_request_reads_state_mergeable_and_the_checks_of_its_head():
    fake = Fake()
    fake.on("GET", f"{GH_REPO}/pulls/5", J(200, {
        "number": 5, "state": "open", "title": "Fix‮ label\nnow", "mergeable": True,
        "html_url": "https://github.com/acme/web/pull/5", "head": {"sha": HEAD}}))
    fake.on("GET", f"{GH_REPO}/commits/{HEAD}/status", J(200, {"statuses": [
        {"context": "ci/build", "state": "success"}, {"context": "lint", "state": "error"}]}))
    fake.on("GET", f"{GH_REPO}/commits/{HEAD}/check-runs", J(200, {"total_count": 2,
            "check_runs": [{"name": "test", "status": "in_progress"},
                           {"name": "e2e", "status": "completed", "conclusion": "odd"}]}))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "get_pull_request", repository="acme/web", number=5)
    assert result["state"] == "open" and result["mergeable"] is True
    assert result["title"] == "Fix labelnow" and result["head_commit"] == HEAD
    assert result["checks"] == [{"name": "ci/build", "status": "success"},
                                {"name": "lint", "status": "failure"},
                                {"name": "test", "status": "pending"},
                                {"name": "e2e", "status": "unknown"}]
    assert result["checks_truncated"] is False


async def test_gitlab_merge_request_state_and_pipeline_checks():
    fake = Fake()
    fake.on("GET", f"{GL_REPO}/merge_requests/9", J(200, {
        "iid": 9, "state": "merged", "title": "T", "detailed_merge_status": "checking",
        "sha": HEAD, "web_url": "https://gitlab.example.com/g/p/-/merge_requests/9"}))
    fake.on("GET", f"{GL_REPO}/repository/commits/{HEAD}/statuses", J(200, [
        {"name": "build", "status": "success"}, {"name": "deploy", "status": "manual"},
        {"name": "test", "status": "failed"}]))
    toolset, _ = make(GL, fake, GL_URL)
    result = await call(toolset, "get_pull_request", repository="group/sub/project", number=9)
    assert result["state"] == "merged" and result["mergeable"] is None
    assert [c["status"] for c in result["checks"]] == ["success", "pending", "failure"]


async def test_bitbucket_pipeline_status_of_a_branch():
    fake = Fake()
    fake.on("GET", f"{BB_REPO}/refs/branches/agent/fix", J(200, {"target": {"hash": HEAD}}))
    fake.on("GET", f"{BB_REPO}/commit/{HEAD}/statuses", J(200, {"values": [
        {"name": "Pipeline #4", "state": "INPROGRESS"}, {"key": "k", "state": "SUCCESSFUL"}]}))
    toolset, _ = make(BB, fake, BB_URL)
    result = await call(toolset, "get_pipeline_status", repository="acme-ws/svc-a",
                        ref="agent/fix")
    assert result == {"repository": "acme-ws/svc-a", "ref": "agent/fix", "commit": HEAD,
                      "checks": [{"name": "Pipeline #4", "status": "pending"},
                                 {"name": "k", "status": "success"}],
                      "checks_truncated": False}


# --- reads -------------------------------------------------------------------------

def _gh_file(content: bytes) -> dict[str, Any]:
    return {"type": "file", "size": len(content), "encoding": "base64",
            "content": base64.b64encode(content).decode()}


async def test_read_repository_file_answers_text_and_cuts_long_text():
    fake = Fake()
    fake.on("GET", f"{GH_REPO}/contents/src/app.js", J(200, _gh_file(b"const a = 1;\n")))
    fake.on("GET", f"{GH_REPO}/contents/big.txt", J(200, _gh_file(b"x" * 200_001)))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "read_repository_file", repository="acme/web",
                        path="src/app.js")
    assert result == {"repository": "acme/web", "path": "src/app.js", "ref": "main",
                      "content": "const a = 1;\n", "size": 13, "truncated": False}
    assert fake.requests[0].url.params["ref"] == "main"
    result = await call(toolset, "read_repository_file", repository="acme/web",
                        path="big.txt", ref="agent/x")
    assert len(result["content"]) == 200_000 and result["truncated"] is True
    assert fake.requests[1].url.params["ref"] == "agent/x"


@pytest.mark.parametrize("content", [b"\x89PNG\r\n\x1a\n\x00\x00", b"\xff\xfe\xfd"])
async def test_a_binary_file_is_refused(content):
    fake = Fake()
    fake.on("GET", f"{GH_REPO}/contents/logo.png", J(200, _gh_file(content)))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "read_repository_file", repository="acme/web",
                        path="logo.png")
    assert code(result) == "binary_file"


async def test_a_file_over_one_megabyte_is_refused():
    fake = Fake()
    fake.on("GET", f"{GH_REPO}/contents/huge.json", J(200, {
        "type": "file", "size": 2_000_000, "encoding": "none", "content": ""}))
    fake.on("GET", f"{BB_REPO}/src/{HEAD}/huge.json",
            lambda r: (httpx.Response(200, json={"type": "commit_file", "size": 10})
                       if r.url.params.get("format") == "meta"
                       else httpx.Response(200, content=b"x" * 1_000_001)))
    gh, _ = make(GH, fake, GH_URL)
    bb, _ = make(BB, fake, BB_URL)
    assert code(await call(gh, "read_repository_file", repository="acme/web",
                           path="huge.json")) == "file_too_large"
    assert code(await call(bb, "read_repository_file", repository="acme-ws/svc-a",
                           path="huge.json", ref=HEAD)) == "file_too_large"


async def test_gitlab_reads_a_file_with_the_path_as_one_segment():
    fake = Fake()
    fake.on("GET", f"{GL_REPO}/repository/files/src%2Fapp.js", J(200, {
        "size": 3, "encoding": "base64", "content": base64.b64encode(b"abc").decode()}))
    toolset, _ = make(GL, fake, GL_URL)
    result = await call(toolset, "read_repository_file", repository="group/sub/project",
                        path="src/app.js")
    assert result["content"] == "abc"
    assert fake.requests[0].url.raw_path.startswith(
        b"/api/v4/projects/group%2Fsub%2Fproject/repository/files/src%2Fapp.js?")


async def test_list_files_caps_the_entries_and_drops_unreadable_names():
    fake = Fake()
    listing = [{"path": f"f{i}", "type": "file", "size": i} for i in range(600)]
    listing[0] = {"path": "evil‮name", "type": "file", "size": 1}
    listing[1] = {"path": "src", "type": "dir"}
    fake.on("GET", f"{GH_REPO}/contents", J(200, listing))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "list_files", repository="acme/web")
    assert len(result["entries"]) == 500 and result["truncated"] is True
    assert result["skipped"] == 1 and result["entries"][0] == {
        "path": "src", "type": "dir", "size": None}


async def test_list_branches_marks_the_agent_branches_and_follows_paging_on_the_host():
    fake = Fake()
    nxt = f"{GL_URL}/projects/group%2Fsub%2Fproject/repository/branches?page=2"
    fake.on("GET", f"{GL_REPO}/repository/branches",
            lambda r: httpx.Response(200, json=[{"name": "main"}, {"name": "agent/a"}],
                                     headers={"Link": f'<{nxt}>; rel="next"'})
            if r.url.params.get("page") != "2"
            else httpx.Response(200, json=[{"name": "agent/b"}]))
    toolset, _ = make(GL, fake, GL_URL)
    result = await call(toolset, "list_branches", repository="group/sub/project")
    assert result["branches"] == [{"name": "main", "agent": False},
                                  {"name": "agent/a", "agent": True},
                                  {"name": "agent/b", "agent": True}]
    assert len(fake.requests) == 2


async def test_list_repositories_reports_each_default_branch():
    fake = Fake()
    cfg = {**GH, "repositories": ["acme/web", "acme/api"]}
    fake.on("GET", "/repos/acme/web", J(200, {"default_branch": "main"}))
    toolset, _ = make(cfg, fake, GH_URL)
    result = await call(toolset, "list_repositories")
    assert result == {"repositories": [
        {"repository": "acme/web", "default_branch": "main"},
        {"repository": "acme/api", "default_branch": None, "error": "not_found"}],
        "base_branch": "main", "branch_prefix": "agent/"}


# --- the transport -------------------------------------------------------------------

async def test_a_429_is_retried_after_2_4_and_8_seconds_then_refused():
    fake = Fake()
    fake.on("GET", "/repos/acme/web", J(429))
    toolset, slept = make(GH, fake, GH_URL)
    result = await call(toolset, "list_repositories")
    assert result["repositories"][0]["error"] == "throttled"
    assert slept == [2.0, 4.0, 8.0] and len(fake.requests) == 4


async def test_a_write_answered_429_is_repeated_and_then_made_once():
    fake = Fake()
    _github_commit_routes(fake)
    answers = iter([httpx.Response(429), httpx.Response(200, json={})])
    fake.on("PATCH", f"{GH_REPO}/git/refs/heads/agent/fix", lambda r: next(answers))
    toolset, slept = make(GH, fake, GH_URL)
    result = await call(toolset, "commit_files", repository="acme/web", branch="agent/fix",
                        message="m", files=[{"path": "a", "content": "x"}])
    assert result["commit"] == COMMIT and slept == [2.0]


@pytest.mark.parametrize("failure", [
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("t", request=r)),
    lambda r: (_ for _ in ()).throw(httpx.RemoteProtocolError("x", request=r)),
    J(502), J(500), J(204), J(302, None, {"Location": "/x"}),
])
async def test_a_write_that_may_have_left_is_outcome_unknown_and_never_repeated(failure):
    fake = Fake()
    _github_commit_routes(fake)
    fake.on("PATCH", f"{GH_REPO}/git/refs/heads/agent/fix", failure)
    toolset, slept = make(GH, fake, GH_URL)
    result = await call(toolset, "commit_files", repository="acme/web", branch="agent/fix",
                        message="m", files=[{"path": "a", "content": "x"}])
    assert code(result) == "outcome_unknown" and slept == []
    assert sum(1 for s in fake.seen() if s.startswith("PATCH")) == 1


async def test_a_write_that_never_connected_is_unreachable():
    fake = Fake()
    _github_commit_routes(fake)
    fake.on("PATCH", f"{GH_REPO}/git/refs/heads/agent/fix",
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("no", request=r)))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "commit_files", repository="acme/web", branch="agent/fix",
                        message="m", files=[{"path": "a", "content": "x"}])
    assert code(result) == "unreachable"


async def test_a_moved_branch_is_branch_moved():
    fake = Fake()
    _github_commit_routes(fake)
    fake.on("PATCH", f"{GH_REPO}/git/refs/heads/agent/fix", J(422, {"message": "Zx9 nope"}))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "commit_files", repository="acme/web", branch="agent/fix",
                        message="m", files=[{"path": "a", "content": "x"}])
    assert code(result) == "branch_moved" and "Zx9" not in json.dumps(result)


async def test_a_redirect_on_the_same_host_is_followed_once():
    fake = Fake()
    fake.on("GET", "/repos/acme/web", J(301, None, {"Location": "/repositories/1"}))
    fake.on("GET", "/repositories/1", J(200, {"default_branch": "trunk"}))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "list_repositories")
    assert result["repositories"][0]["default_branch"] == "trunk"
    assert [r.headers["authorization"] for r in fake.requests] == [TOKEN, TOKEN]


@pytest.mark.parametrize("location", [
    "https://evil.example.test/x", "http://api.github.com/x", "https://api.github.com:8443/x",
    "https://u:p@api.github.com/x", "//evil.example.test/x", None])
async def test_a_redirect_elsewhere_is_refused_and_gets_no_credential(location):
    fake = Fake()
    fake.on("GET", "/repos/acme/web",
            J(302, None, {} if location is None else {"Location": location}))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "list_repositories")
    assert result["repositories"][0]["error"] == "redirect_refused"
    assert len(fake.requests) == 1


async def test_a_second_redirect_is_refused():
    fake = Fake()
    fake.on("GET", "/repos/acme/web", J(302, None, {"Location": "/repos/acme/web"}))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "list_repositories")
    assert result["repositories"][0]["error"] == "redirect_refused" and len(fake.requests) == 2


async def test_a_paging_link_to_another_host_is_not_followed():
    fake = Fake()
    fake.on("GET", f"{GH_REPO}/branches", J(200, [{"name": "main"}], {
        "Link": '<https://evil.example.test/x?page=2>; rel="next"'}))
    toolset, _ = make(GH, fake, GH_URL)
    result = await call(toolset, "list_branches", repository="acme/web")
    assert code(result) == "redirect_refused" and len(fake.requests) == 1


async def test_an_onpremise_destination_is_refused_before_anything_is_sent():
    fake = Fake()
    toolset, _ = make(GH, fake, "https://git.internal.example", proxy_type="OnPremise")
    result = await call(toolset, "list_branches", repository="acme/web")
    assert code(result) == "destination_error" and fake.requests == []


async def test_an_answer_that_is_no_json_is_not_json():
    fake = Fake()
    fake.on("GET", f"{GH_REPO}/branches",
            lambda r: httpx.Response(200, text="<html>sign in</html>",
                                     headers={"content-type": "text/html"}))
    toolset, _ = make(GH, fake, GH_URL)
    assert code(await call(toolset, "list_branches", repository="acme/web")) == "not_json"


async def test_no_url_token_or_provider_text_in_any_result_or_log_line(caplog):
    caplog.set_level(logging.DEBUG)
    fake = Fake()
    planted = "PLANTED-Zx9-body https://leak.example.test/?token=abc"
    for path in ("/repos/acme/web", f"{GH_REPO}/branches", f"{GH_REPO}/pulls/1",
                 f"{GH_REPO}/contents/a"):
        fake.on("GET", path, lambda r: httpx.Response(500, text=planted))
    fake.on("GET", f"{GH_REPO}/git/ref/heads/agent/x", J(200, {"object": {"sha": HEAD}}))
    fake.on("GET", f"{GH_REPO}/git/commits/{HEAD}",
            lambda r: (_ for _ in ()).throw(httpx.ReadError(planted, request=r)))
    toolset, _ = make(GH, fake, GH_URL)
    results = [
        await call(toolset, "list_repositories"),
        await call(toolset, "list_branches", repository="acme/web"),
        await call(toolset, "get_pull_request", repository="acme/web", number=1),
        await call(toolset, "read_repository_file", repository="acme/web", path="a"),
        await call(toolset, "commit_files", repository="acme/web", branch="agent/x",
                   message="m", files=[{"path": "a", "content": "x"}]),
    ]
    text = json.dumps(results) + caplog.text
    for leak in ("PLANTED", "leak.example", "api.github.com", "t0k3n", "token=", "https://"):
        assert leak not in text, leak
    assert "HTTP Request" not in caplog.text
    for result in results:
        codes = [result.get("error", {}).get("code")] + [
            r.get("error") for r in result.get("repositories", [])]
        assert all(c in ERROR_CODES for c in codes if c)


async def test_a_tool_that_fails_unexpectedly_answers_a_fixed_error(monkeypatch):
    fake = Fake()
    toolset, _ = make(GH, fake, GH_URL)

    async def boom(*a, **k):
        raise RuntimeError("Zx9 https://leak.example.test")

    monkeypatch.setattr(toolset.client.api, "branches", boom)
    result = await call(toolset, "list_branches", repository="acme/web")
    assert result == {"error": {"code": "provider_error",
                                "message": "the call could not be completed"}}
