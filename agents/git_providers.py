"""The three provider adapters of ``builtin:git``: GitHub, Bitbucket Cloud
and GitLab, behind one set of methods.

The toolset (:mod:`agents.git_tools`) checks every argument and never
branches on the provider; an adapter only turns one checked call into the
provider's requests and its answer into plain data. Every value put into a
URL has passed a pattern of :mod:`agents.git_config` and is percent-encoded
here: segment by segment for a file path (GitHub, Bitbucket), as ONE segment
for a GitLab project, file or branch (``group%2Fproject``).

What a provider says goes out as data only: names and titles through the
character filter of :mod:`agents.bitbucket_tools` (``_shown_text``), a web
URL only when it is a plain https URL, a state only as one of a few fixed
words. Unit-tested against fakes only; never run against a real provider.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from agents.bitbucket_tools import _shown_text
from agents.git_config import is_hash
from agents.git_http import MAX_FILE_BYTES, Answer, GitHttp, GitRefused, status_refusal

MAX_CHECKS = 50
MAX_PAGES = 10
_WEB_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?")
_COMMIT_IN_LOCATION_RE = re.compile(r"/commit/([0-9a-f]{7,64})(?:[/?]|$)")

Entry = dict[str, Any]


def _seg(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.split("/"))


def _one(value: str) -> str:
    return quote(value, safe="")


def _dig(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _web_url(value: Any) -> str | None:
    """A provider's web link, when it is a plain https URL (no userinfo, no
    query, no fragment); anything else is left out."""
    if isinstance(value, str) and len(value) <= 500 and _WEB_URL_RE.fullmatch(value):
        return value
    return None


def _number(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _sha(value: Any) -> str | None:
    return value if is_hash(value) else None


def _size(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _items(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _check(name: Any, status: str) -> Entry:
    return {"name": _shown_text(name, 200) or "?", "status": status}


def _too_large() -> GitRefused:
    return GitRefused("file_too_large", "the file is over 1 MB and was not read")


def _not_a_directory() -> GitRefused:
    return GitRefused("invalid_path", "the path is a file, not a directory",
                      "read it with read_repository_file")


def _not_a_file() -> GitRefused:
    return GitRefused("invalid_path", "the path is a directory, not a file",
                      "list it with list_files")


def _base64(value: Any) -> bytes:
    try:
        return base64.b64decode(str(value or ""), validate=False)
    except ValueError:
        raise GitRefused("provider_error", "the provider answered a file that cannot be "
                                           "decoded") from None


class ProviderApi:
    """The methods every adapter has. ``HEADERS`` go with every request."""

    HEADERS: dict[str, str] = {"Accept": "application/json"}

    def __init__(self, http: GitHttp) -> None:
        self.http = http

    async def _collect(self, url: str, params: dict[str, Any] | None,
                       page_of: Callable[[Any, Answer], tuple[list[Any], Any]],
                       limit: int) -> tuple[list[Any], bool]:
        """The items of a paged list, at most ``limit`` and whether more were
        left behind (``limit`` or ``MAX_PAGES`` reached). A paging link is
        followed only on the host the request went to."""
        items: list[Any] = []
        target: Any = url
        for _ in range(MAX_PAGES):
            answer = await self.http.get(target, params=params)
            page, following = page_of(answer.json(), answer)
            items.extend(page)
            if len(items) >= limit:
                return items[:limit], len(items) > limit or bool(following)
            if not following:
                return items, False
            target, params = self.http.follow(following, answer), None
        return items, True

    async def _obj_or_none(self, url: str, params: Any = None) -> dict[str, Any] | None:
        answer = await self.http.get(url, params=params)
        if answer.status == 404:
            return None
        return answer.obj()

    async def _post_object(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """A POST that creates an object no branch points at yet (a blob, a
        tree, a commit): sent once, and a failure is a plain refusal because
        nothing anybody sees has changed."""
        answer = await self.http.send("POST", url, json=body)
        if answer.status != 201:
            raise status_refusal(answer.status)
        return answer.obj()

    # The interface. Each adapter overrides every method below.
    async def repo_info(self, repo: str) -> Entry: raise NotImplementedError
    async def list_dir(self, repo: str, path: str, ref: str) -> tuple[list[Entry], bool]:
        raise NotImplementedError
    async def read_file(self, repo: str, path: str, ref: str) -> bytes: raise NotImplementedError
    async def branches(self, repo: str, limit: int) -> tuple[list[str], bool]:
        raise NotImplementedError
    async def branch_head(self, repo: str, name: str) -> str | None: raise NotImplementedError
    async def create_branch(self, repo: str, name: str, sha: str) -> None:
        raise NotImplementedError
    async def commit(self, repo: str, branch: str, head: str, message: str,
                     files: list[tuple[str, str]], deletes: list[str]) -> Entry:
        raise NotImplementedError
    async def find_open_pr(self, repo: str, branch: str, base: str) -> Entry | None:
        raise NotImplementedError
    async def open_pr(self, repo: str, branch: str, base: str, title: str,
                      body: str) -> Entry:
        raise NotImplementedError
    async def pull_request(self, repo: str, number: int) -> Entry: raise NotImplementedError
    async def checks(self, repo: str, sha: str) -> tuple[list[Entry], bool]:
        raise NotImplementedError


# --------------------------------------------------------------------- GitHub
_GITHUB_STATUS = {"success": "success", "failure": "failure", "error": "failure",
                  "pending": "pending"}
_GITHUB_CONCLUSION = {"success": "success", "neutral": "success", "skipped": "success",
                      "failure": "failure", "cancelled": "failure", "timed_out": "failure",
                      "action_required": "failure", "startup_failure": "failure"}


class GitHubApi(ProviderApi):
    """GitHub REST v3; a commit through the Git Data API (blobs, tree,
    commit, then the one ref update that moves the branch)."""

    HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    @staticmethod
    def _pr(body: dict[str, Any]) -> Entry:
        state = "merged" if body.get("merged") is True or body.get("merged_at") else (
            body.get("state") if body.get("state") in ("open", "closed") else "unknown")
        mergeable = body.get("mergeable")
        return {"number": _number(body.get("number")), "url": _web_url(body.get("html_url")),
                "state": state, "title": _shown_text(body.get("title"), 300),
                "mergeable": mergeable if isinstance(mergeable, bool) else None,
                "head": _sha(_dig(body, "head", "sha"))}

    async def repo_info(self, repo: str) -> Entry:
        body = (await self.http.get(f"/repos/{repo}")).obj()
        return {"default_branch": body.get("default_branch")}

    async def list_dir(self, repo: str, path: str, ref: str) -> tuple[list[Entry], bool]:
        url = f"/repos/{repo}/contents/{_seg(path)}" if path else f"/repos/{repo}/contents"
        data = (await self.http.get(url, params={"ref": ref})).json()
        if not isinstance(data, list):
            raise _not_a_directory()
        entries = [{"path": item.get("path"), "type": "dir" if item.get("type") == "dir"
                    else "file", "size": None if item.get("type") == "dir"
                    else _size(item.get("size"))} for item in data if isinstance(item, dict)]
        return entries, False

    async def read_file(self, repo: str, path: str, ref: str) -> bytes:
        data = (await self.http.get(f"/repos/{repo}/contents/{_seg(path)}",
                                    params={"ref": ref})).json()
        if not isinstance(data, dict) or data.get("type") != "file":
            raise _not_a_file()
        size = _size(data.get("size"))
        if size is None or size > MAX_FILE_BYTES or data.get("encoding") != "base64":
            raise _too_large()   # over 1 MB GitHub sends no content ("encoding": "none")
        return _base64(data.get("content"))

    async def branches(self, repo: str, limit: int) -> tuple[list[str], bool]:
        return await self._collect(
            f"/repos/{repo}/branches", {"per_page": 100},
            lambda data, answer: ([b.get("name") for b in data if isinstance(b, dict)]
                                  if isinstance(data, list) else [], answer.next_link()),
            limit)

    async def branch_head(self, repo: str, name: str) -> str | None:
        body = await self._obj_or_none(f"/repos/{repo}/git/ref/heads/{_seg(name)}")
        return None if body is None else _sha(_dig(body, "object", "sha"))

    async def create_branch(self, repo: str, name: str, sha: str) -> None:
        await self.http.write("POST", f"/repos/{repo}/git/refs", ok=(201,),
                              json={"ref": f"refs/heads/{name}", "sha": sha})

    async def commit(self, repo: str, branch: str, head: str, message: str,
                     files: list[tuple[str, str]], deletes: list[str]) -> Entry:
        parent = (await self.http.get(f"/repos/{repo}/git/commits/{head}")).obj()
        base_tree = _sha(_dig(parent, "tree", "sha"))
        if base_tree is None:
            raise GitRefused("provider_error", "the provider answered without a tree")
        tree: list[dict[str, Any]] = []
        for path, content in files:
            blob = await self._post_object(f"/repos/{repo}/git/blobs",
                                           {"content": content, "encoding": "utf-8"})
            blob_sha = _sha(blob.get("sha"))
            if blob_sha is None:
                raise GitRefused("provider_error", "the provider answered without a blob")
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})
        tree += [{"path": path, "mode": "100644", "type": "blob", "sha": None}
                 for path in deletes]
        made = await self._post_object(f"/repos/{repo}/git/trees",
                                       {"base_tree": base_tree, "tree": tree})
        commit = await self._post_object(
            f"/repos/{repo}/git/commits",
            {"message": message, "tree": made.get("sha"), "parents": [head]})
        sha = _sha(commit.get("sha"))
        if sha is None:
            raise GitRefused("provider_error", "the provider answered without a commit")
        # The one request that changes what anybody sees. Not forced: a branch
        # that moved meanwhile is a 422 and keeps what it has.
        await self.http.write(
            "PATCH", f"/repos/{repo}/git/refs/heads/{_seg(branch)}", ok=(200,),
            json={"sha": sha, "force": False},
            refusals={422: GitRefused("branch_moved", "the branch moved while the commit was "
                                      "made; nothing was changed", "read it and commit again")})
        return {"commit": sha, "url": _web_url(commit.get("html_url"))}

    async def find_open_pr(self, repo: str, branch: str, base: str) -> Entry | None:
        owner = repo.split("/", 1)[0]
        data = (await self.http.get(f"/repos/{repo}/pulls", params={
            "state": "open", "head": f"{owner}:{branch}", "base": base, "per_page": 10})).json()
        found = [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []
        return self._pr(found[0]) if found else None

    async def open_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> Entry:
        answer = await self.http.write("POST", f"/repos/{repo}/pulls", ok=(201,), json={
            "title": title, "body": body, "head": branch, "base": base})
        return self._pr(answer.obj())

    async def pull_request(self, repo: str, number: int) -> Entry:
        return self._pr((await self.http.get(f"/repos/{repo}/pulls/{number}")).obj())

    async def checks(self, repo: str, sha: str) -> tuple[list[Entry], bool]:
        status = (await self.http.get(f"/repos/{repo}/commits/{sha}/status")).obj()
        runs = (await self.http.get(f"/repos/{repo}/commits/{sha}/check-runs",
                                    params={"per_page": MAX_CHECKS})).obj()
        statuses = _items(status.get("statuses"))
        check_runs = _items(runs.get("check_runs"))
        found = [_check(item.get("context"), _GITHUB_STATUS.get(item.get("state"), "unknown"))
                 for item in statuses]
        for item in check_runs:
            state = ("pending" if item.get("status") != "completed"
                     else _GITHUB_CONCLUSION.get(item.get("conclusion"), "unknown"))
            found.append(_check(item.get("name"), state))
        total = runs.get("total_count")
        more = type(total) is int and total > len(check_runs)
        return found[:MAX_CHECKS], more or len(found) > MAX_CHECKS


# ------------------------------------------------------------------ Bitbucket
_BITBUCKET_STATE = {"SUCCESSFUL": "success", "FAILED": "failure", "STOPPED": "failure",
                    "INPROGRESS": "pending"}
_BITBUCKET_PR_STATE = {"OPEN": "open", "MERGED": "merged", "DECLINED": "closed",
                       "SUPERSEDED": "closed"}
# Form fields of the src endpoint: a file of the same name at the root is
# sent with a leading '/' so that it is not read as the field.
_BITBUCKET_FIELDS = frozenset({"message", "branch", "author", "parents", "files"})


class BitbucketApi(ProviderApi):
    """Bitbucket Cloud REST 2.0 (the destination's URL ends in ``/2.0``); a
    commit is ONE multipart ``POST .../src``."""

    @staticmethod
    def _pr(body: dict[str, Any]) -> Entry:
        return {"number": _number(body.get("id")),
                "url": _web_url(_dig(body, "links", "html", "href")),
                "state": _BITBUCKET_PR_STATE.get(body.get("state"), "unknown"),
                "title": _shown_text(body.get("title"), 300), "mergeable": None,
                "head": _sha(_dig(body, "source", "commit", "hash"))}

    @staticmethod
    def _values(data: Any, _answer: Answer) -> tuple[list[Any], Any]:
        values = data.get("values") if isinstance(data, dict) else None
        return ([v for v in values if isinstance(v, dict)] if isinstance(values, list) else [],
                data.get("next") if isinstance(data, dict) else None)

    async def _commit_of(self, repo: str, ref: str) -> str:
        if is_hash(ref):
            return ref
        head = await self.branch_head(repo, ref)
        if head is None:
            raise status_refusal(404)
        return head

    async def _meta(self, repo: str, commit: str, path: str) -> dict[str, Any]:
        return (await self.http.get(f"/repositories/{repo}/src/{commit}/{_seg(path)}",
                                    params={"format": "meta"})).obj()

    async def repo_info(self, repo: str) -> Entry:
        body = (await self.http.get(f"/repositories/{repo}")).obj()
        return {"default_branch": _dig(body, "mainbranch", "name")}

    async def list_dir(self, repo: str, path: str, ref: str) -> tuple[list[Entry], bool]:
        commit = await self._commit_of(repo, ref)
        if path and (await self._meta(repo, commit, path)).get("type") != "commit_directory":
            raise _not_a_directory()
        url = f"/repositories/{repo}/src/{commit}/{_seg(path) + '/' if path else ''}"
        items, more = await self._collect(url, {"pagelen": 100}, self._values, 500)
        return [{"path": i.get("path"),
                 "type": "dir" if i.get("type") == "commit_directory" else "file",
                 "size": None if i.get("type") == "commit_directory" else _size(i.get("size"))}
                for i in items], more

    async def read_file(self, repo: str, path: str, ref: str) -> bytes:
        commit = await self._commit_of(repo, ref)
        meta = await self._meta(repo, commit, path)
        if meta.get("type") != "commit_file":
            raise _not_a_file()
        size = _size(meta.get("size"))
        if size is not None and size > MAX_FILE_BYTES:
            raise _too_large()
        answer = await self.http.get(f"/repositories/{repo}/src/{commit}/{_seg(path)}",
                                     cap=MAX_FILE_BYTES)
        if answer.status != 200:
            raise status_refusal(answer.status)
        if answer.over:
            raise _too_large()
        return answer.body

    async def branches(self, repo: str, limit: int) -> tuple[list[str], bool]:
        items, more = await self._collect(f"/repositories/{repo}/refs/branches",
                                          {"pagelen": 100}, self._values, limit)
        return [i.get("name") for i in items], more

    async def branch_head(self, repo: str, name: str) -> str | None:
        body = await self._obj_or_none(f"/repositories/{repo}/refs/branches/{_seg(name)}")
        return None if body is None else _sha(_dig(body, "target", "hash"))

    async def create_branch(self, repo: str, name: str, sha: str) -> None:
        await self.http.write("POST", f"/repositories/{repo}/refs/branches", ok=(201,),
                              json={"name": name, "target": {"hash": sha}})

    async def commit(self, repo: str, branch: str, head: str, message: str,
                     files: list[tuple[str, str]], deletes: list[str]) -> Entry:
        data: dict[str, Any] = {"message": message, "branch": branch, "parents": head}
        if deletes:
            data["files"] = list(deletes)
        upload = [("/" + path if path in _BITBUCKET_FIELDS else path,
                   (path.rsplit("/", 1)[-1], content.encode("utf-8"), "text/plain"))
                  for path, content in files]
        answer = await self.http.write("POST", f"/repositories/{repo}/src", ok=(201,),
                                       data=data, files=upload or None)
        match = _COMMIT_IN_LOCATION_RE.search(answer.headers.get("location", ""))
        return {"commit": match.group(1) if match else None, "url": None}

    async def find_open_pr(self, repo: str, branch: str, base: str) -> Entry | None:
        # Both names passed the branch pattern: no quote can close the string.
        query = f'source.branch.name="{branch}" AND destination.branch.name="{base}"'
        data = (await self.http.get(f"/repositories/{repo}/pullrequests",
                                    params={"state": "OPEN", "q": query})).obj()
        found, _ = self._values(data, None)  # type: ignore[arg-type]
        return self._pr(found[0]) if found else None

    async def open_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> Entry:
        answer = await self.http.write("POST", f"/repositories/{repo}/pullrequests", ok=(201,),
                                       json={"title": title, "description": body,
                                             "source": {"branch": {"name": branch}},
                                             "destination": {"branch": {"name": base}}})
        return self._pr(answer.obj())

    async def pull_request(self, repo: str, number: int) -> Entry:
        return self._pr((await self.http.get(
            f"/repositories/{repo}/pullrequests/{number}")).obj())

    async def checks(self, repo: str, sha: str) -> tuple[list[Entry], bool]:
        data = (await self.http.get(f"/repositories/{repo}/commit/{sha}/statuses",
                                    params={"pagelen": MAX_CHECKS})).obj()
        values, following = self._values(data, None)  # type: ignore[arg-type]
        found = [_check(v.get("name") or v.get("key"),
                        _BITBUCKET_STATE.get(v.get("state"), "unknown")) for v in values]
        return found[:MAX_CHECKS], bool(following) or len(found) > MAX_CHECKS


# --------------------------------------------------------------------- GitLab
_GITLAB_STATUS = {"success": "success", "failed": "failure", "canceled": "failure",
                  "pending": "pending", "running": "pending", "created": "pending",
                  "manual": "pending", "scheduled": "pending", "preparing": "pending",
                  "waiting_for_resource": "pending"}
_GITLAB_MR_STATE = {"opened": "open", "merged": "merged", "closed": "closed",
                    "locked": "closed"}
_GITLAB_UNDECIDED = frozenset({"checking", "unchecked", "preparing", "approvals_syncing"})


class GitLabApi(ProviderApi):
    """GitLab REST v4 (gitlab.com or a self-hosted ``/api/v4``); a commit is
    ONE ``POST .../repository/commits`` with ``actions``."""

    @staticmethod
    def _base(repo: str) -> str:
        return f"/projects/{_one(repo)}"

    @staticmethod
    def _list(data: Any, answer: Answer) -> tuple[list[Any], Any]:
        return ([v for v in data if isinstance(v, dict)] if isinstance(data, list) else [],
                answer.next_link())

    @staticmethod
    def _pr(body: dict[str, Any]) -> Entry:
        detailed = body.get("detailed_merge_status")
        if isinstance(detailed, str):
            mergeable = None if detailed in _GITLAB_UNDECIDED else detailed == "mergeable"
        else:
            mergeable = {"can_be_merged": True, "cannot_be_merged": False}.get(
                body.get("merge_status"))
        return {"number": _number(body.get("iid")), "url": _web_url(body.get("web_url")),
                "state": _GITLAB_MR_STATE.get(body.get("state"), "unknown"),
                "title": _shown_text(body.get("title"), 300), "mergeable": mergeable,
                "head": _sha(body.get("sha"))}

    async def repo_info(self, repo: str) -> Entry:
        body = (await self.http.get(self._base(repo))).obj()
        return {"default_branch": body.get("default_branch")}

    async def list_dir(self, repo: str, path: str, ref: str) -> tuple[list[Entry], bool]:
        params: dict[str, Any] = {"ref": ref, "per_page": 100}
        if path:
            params["path"] = path
        items, more = await self._collect(f"{self._base(repo)}/repository/tree", params,
                                          self._list, 500)
        return [{"path": i.get("path"), "type": "dir" if i.get("type") == "tree" else "file",
                 "size": None} for i in items], more

    async def read_file(self, repo: str, path: str, ref: str) -> bytes:
        body = (await self.http.get(f"{self._base(repo)}/repository/files/{_one(path)}",
                                    params={"ref": ref})).obj()
        size = _size(body.get("size"))
        if size is None or size > MAX_FILE_BYTES:
            raise _too_large()
        if body.get("encoding") != "base64":
            raise GitRefused("provider_error", "the provider answered a file that cannot be "
                                               "decoded")
        return _base64(body.get("content"))

    async def branches(self, repo: str, limit: int) -> tuple[list[str], bool]:
        items, more = await self._collect(f"{self._base(repo)}/repository/branches",
                                          {"per_page": 100}, self._list, limit)
        return [i.get("name") for i in items], more

    async def branch_head(self, repo: str, name: str) -> str | None:
        body = await self._obj_or_none(f"{self._base(repo)}/repository/branches/{_one(name)}")
        return None if body is None else _sha(_dig(body, "commit", "id"))

    async def create_branch(self, repo: str, name: str, sha: str) -> None:
        await self.http.write("POST", f"{self._base(repo)}/repository/branches", ok=(201,),
                              params={"branch": name, "ref": sha})

    async def _exists(self, repo: str, path: str, branch: str) -> bool:
        answer = await self.http.send("HEAD", f"{self._base(repo)}/repository/files/"
                                      f"{_one(path)}", params={"ref": branch})
        if answer.status in (200, 404):
            return answer.status == 200
        raise status_refusal(answer.status)

    async def commit(self, repo: str, branch: str, head: str, message: str,
                     files: list[tuple[str, str]], deletes: list[str]) -> Entry:
        actions: list[dict[str, Any]] = []
        for path, content in files:
            action = "update" if await self._exists(repo, path, branch) else "create"
            actions.append({"action": action, "file_path": path, "content": content,
                            "encoding": "text"})
        actions += [{"action": "delete", "file_path": path} for path in deletes]
        answer = await self.http.write("POST", f"{self._base(repo)}/repository/commits",
                                       ok=(201,), json={"branch": branch,
                                                        "commit_message": message,
                                                        "actions": actions})
        body = answer.obj()
        return {"commit": _sha(body.get("id")), "url": _web_url(body.get("web_url"))}

    async def find_open_pr(self, repo: str, branch: str, base: str) -> Entry | None:
        data = (await self.http.get(f"{self._base(repo)}/merge_requests", params={
            "state": "opened", "source_branch": branch, "target_branch": base})).json()
        found, _ = self._list(data, Answer(200))
        return self._pr(found[0]) if found else None

    async def open_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> Entry:
        answer = await self.http.write("POST", f"{self._base(repo)}/merge_requests", ok=(201,),
                                       json={"source_branch": branch, "target_branch": base,
                                             "title": title, "description": body})
        return self._pr(answer.obj())

    async def pull_request(self, repo: str, number: int) -> Entry:
        return self._pr((await self.http.get(
            f"{self._base(repo)}/merge_requests/{number}")).obj())

    async def checks(self, repo: str, sha: str) -> tuple[list[Entry], bool]:
        answer = await self.http.get(f"{self._base(repo)}/repository/commits/{sha}/statuses",
                                     params={"per_page": MAX_CHECKS})
        found, following = self._list(answer.json(), answer)
        checks = [_check(v.get("name"), _GITLAB_STATUS.get(v.get("status"), "unknown"))
                  for v in found]
        return checks[:MAX_CHECKS], bool(following) or len(checks) > MAX_CHECKS


ADAPTERS: dict[str, type[ProviderApi]] = {
    "github": GitHubApi, "bitbucket": BitbucketApi, "gitlab": GitLabApi,
}
