"""A fake Bitbucket Cloud API for the ``builtin:bitbucket`` tests.

``FakeBitbucket().client()`` is an ``httpx.AsyncClient`` on a MockTransport
that answers the endpoints the toolset uses, from plain dicts a test fills.
No network.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import httpx

from agents.destination import Destination

HOST = "api.bitbucket.org"
WS = "acme-ws"
OWN_UUID = "{11111111-1111-1111-1111-111111111111}"
OTHER_UUID = "{22222222-2222-2222-2222-222222222222}"
TOKEN = "Basic dGVjaDpQTEFOVEVELVRPS0VO"   # never in a log, error or result
BASE_CFG = {"destination": "BITBUCKET", "workspace": WS}

_REPO = rf"/2\.0/repositories/{WS}/(?P<repo>[^/]+)"
_PR = _REPO + r"/pullrequests/(?P<id>[0-9]+)"


class FakeResolver:
    def __init__(self, url: str = f"https://{HOST}", name: str = "BITBUCKET"):
        self.name, self.url, self.invalidated = name, url, 0

    async def resolve(self, *, force: bool = False, user_token=None, principal=None):
        return Destination(url=self.url, headers={"Authorization": TOKEN},
                           expires_at=time.monotonic() + 60)

    def invalidate(self, principal: str | None = None) -> None:
        self.invalidated += 1


class FakeBitbucket:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.user_status = 200
        self.repos: list[str] = ["svc-a"]
        self.prs: dict[tuple[str, int], dict[str, Any]] = {}
        self.comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.statuses: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.diffs: dict[tuple[str, int], str] = {}
        self.diffstats: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.files: dict[tuple[str, str, str], bytes] = {}     # (repo, commit, path)
        self.meta: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.posted: list[tuple[str, int, dict[str, Any]]] = []
        self.approved: list[tuple[str, int]] = []
        self.page_size = 50
        # A test's own answer for a request; None falls through to the fake.
        self.override: Callable[[httpx.Request], httpx.Response | None] | None = None

    # -- filling ---------------------------------------------------------------
    def add_pr(self, repo: str, pr_id: int, *, head: str = "aaaaaaaaaaaa", branch: str = "main",
               state: str = "OPEN", draft: bool = False, title: str = "A change",
               description: str = "", author: str = "Ann Author") -> dict[str, Any]:
        pr = {"id": pr_id, "title": title, "description": description, "state": state,
              "draft": draft, "author": {"display_name": author, "uuid": OTHER_UUID},
              "source": {"commit": {"hash": head}, "branch": {"name": "feature/x"}},
              "destination": {"branch": {"name": branch}},
              "updated_on": "2026-10-09T08:00:00.000000+00:00", "participants": []}
        self.prs[(repo, pr_id)] = pr
        if repo not in self.repos:
            self.repos.append(repo)
        return pr

    def add_comment(self, repo: str, pr_id: int, raw: str, *, own: bool = False,
                    inline: dict[str, Any] | None = None, deleted: bool = False) -> None:
        items = self.comments.setdefault((repo, pr_id), [])
        items.append({"id": 100 + len(items), "content": {"raw": raw}, "deleted": deleted,
                      "user": {"uuid": OWN_UUID if own else OTHER_UUID,
                               "display_name": "Review Bot" if own else "Carl Commenter"},
                      **({"inline": inline} if inline else {})})

    # -- answering -------------------------------------------------------------
    def _page(self, request: httpx.Request, items: list[Any]) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * self.page_size
        body: dict[str, Any] = {"values": items[start:start + self.page_size],
                                "pagelen": self.page_size}
        if start + self.page_size < len(items):
            body["next"] = str(request.url.copy_merge_params({"page": str(page + 1)}))
        return httpx.Response(200, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None:
            answer = self.override(request)
            if answer is not None:
                return answer
        path, method = request.url.path, request.method
        if path == "/2.0/user":
            if self.user_status != 200:
                return httpx.Response(self.user_status, text="PLANTED-ERROR-BODY")
            return httpx.Response(200, json={"uuid": OWN_UUID, "account_id": "acc-1"})
        if path == f"/2.0/repositories/{WS}":
            return self._page(request, [{"slug": r} for r in self.repos])
        m = re.fullmatch(_REPO + r"/pullrequests", path)
        if m:
            branch = re.search(r'destination\.branch\.name="([^"]+)"',
                               request.url.params.get("q", ""))
            return self._page(request, [
                pr for (repo, _), pr in sorted(self.prs.items())
                if repo == m["repo"] and pr["state"] == "OPEN"
                and (branch is None or pr["destination"]["branch"]["name"] == branch[1])])
        m = re.fullmatch(_PR + r"(?P<rest>/[a-z]+)?", path)
        if m:
            key = (m["repo"], int(m["id"]))
            pr = self.prs.get(key)
            if pr is None:
                return httpx.Response(404, text="PLANTED-ERROR-BODY")
            rest = m["rest"] or ""
            if rest == "":
                return httpx.Response(200, json=pr)
            if rest == "/comments" and method == "GET":
                return self._page(request, self.comments.get(key, []))
            if rest == "/comments" and method == "POST":
                body = json.loads(request.content)
                self.posted.append((*key, body))
                self.add_comment(*key, body["content"]["raw"], own=True,
                                 inline=body.get("inline"))
                return httpx.Response(201, json=self.comments[key][-1])
            if rest == "/statuses":
                return self._page(request, self.statuses.get(key, []))
            if rest in ("/diff", "/diffstat"):
                spec = f"{WS}/{m['repo']}:{pr['source']['commit']['hash']}%0Dbbbbbbbbbbbb"
                query = f"?from_pullrequest_id={m['id']}"
                if request.url.params.get("path"):
                    query += "&path=" + request.url.params["path"]
                return httpx.Response(302, headers={"Location":
                    f"https://{request.url.host}/2.0/repositories/{WS}/{m['repo']}{rest}/{spec}{query}"})
            if rest == "/approve" and method == "POST":
                self.approved.append(key)
                return httpx.Response(200, json={"approved": True, "user": {"uuid": OWN_UUID}})
        m = re.fullmatch(_REPO + r"/(?P<kind>diff|diffstat)/.+", path)
        if m:
            pr_id = int(request.url.params["from_pullrequest_id"])
            if m["kind"] == "diff":
                return httpx.Response(200, text=self.diffs.get((m["repo"], pr_id), ""),
                                      headers={"content-type": "text/plain"})
            return self._page(request, self.diffstats.get((m["repo"], pr_id), []))
        m = re.fullmatch(_REPO + r"/src/(?P<commit>[0-9a-f]+)/(?P<path>.+)", path)
        if m:
            key = (m["repo"], m["commit"], m["path"])
            if key not in self.files:
                return httpx.Response(404, text="PLANTED-ERROR-BODY")
            data = self.files[key]
            if request.url.params.get("format") == "meta":
                return httpx.Response(200, json={"type": "commit_file", "size": len(data),
                                                 "attributes": [], **self.meta.get(key, {})})
            return httpx.Response(200, content=data)
        return httpx.Response(404, text="PLANTED-ERROR-BODY")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self) -> httpx.AsyncClient:
        """A client without destination auth: paths are ``/2.0/...`` on HOST."""
        return httpx.AsyncClient(base_url=f"https://{HOST}", transport=self.transport())

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.raw_path.decode()}" for r in self.requests]
