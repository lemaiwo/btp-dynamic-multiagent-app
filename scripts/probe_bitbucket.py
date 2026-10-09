"""Go/no-go probe for ``builtin:bitbucket``: is this destination usable?

Answers, before an agent is configured, the questions that decide whether the
BTP destination to Bitbucket Cloud is ready:

  1. Does the destination resolve, to which host, over https, and with or
     without the API root (``/2.0``) in its URL? (The toolset handles both.)
  2. Does Bitbucket identify the account behind the credential?
  3. Is the workspace visible?
  4. Is each named repository visible?
  5. Can the open pull requests of one repository be listed for a branch?
  6. Does the diff of a pull request answer, and does its redirect stay on
     the destination's host? (Only with ``--pull-request`` and a
     ``--repository``.)

Standalone on purpose: it imports nothing from this app, touches no database
and changes nothing in Bitbucket. GET requests only (the one POST is the
token request to the destination service).

It prints one ``PASS`` / ``FAIL`` / ``SKIP`` line per step: the names typed on
the command line, statuses, counts, sizes and host names. It never prints a
token, the ``Authorization`` value, a client secret, a ``Location``, a
response body, an exception text, a line of a diff, a pull request title or a
user name. A step that could not be sent is ``FAIL  <step>: <ExceptionClass>``.

Run it where the destination service binding is: inside the app container.

    python scripts/probe_bitbucket.py --destination <NAME> --workspace <slug> \\
        [--repository <slug> ...] [--branch main] [--pull-request <id>]

The binding is read from ``VCAP_SERVICES`` (label ``destination``). Exit code
0 when no step failed, 1 otherwise, 2 for a refused argument.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

try:
    import httpx
except ImportError:  # pragma: no cover - the script may run outside the venv
    sys.exit("httpx is required:  pip install httpx   (or use .venv's python)")

SLUG = re.compile(r"[a-z0-9_][a-z0-9._-]{0,61}")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
_PATH = re.compile(r"[A-Za-z0-9._~/-]*")
_TYPE = re.compile(r"[A-Za-z0-9]{1,40}")
_REDIRECTS = (301, 302, 303, 307, 308)


class ProbeError(Exception):
    """A step failed; the text is fixed and safe to print."""


@dataclass
class Binding:
    client_id: str
    client_secret: str
    token_url: str
    uri: str

    def __repr__(self) -> str:  # never the secret
        return f"Binding(client_id=…, uri={urlsplit(self.uri).hostname})"


@dataclass
class Target:
    """Where the destination points and what it sends; ``repr`` shows neither."""

    base: str
    authorization: str

    def __repr__(self) -> str:
        return "Target(…)"

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": self.authorization, "Accept": "application/json"}


class Report:
    """One line per step; remembers whether any step failed."""

    def __init__(self) -> None:
        self.failed = False

    def line(self, verdict: str, step: str, detail: str) -> None:
        self.failed = self.failed or verdict == "FAIL"
        print(f"{verdict}  {step}: {detail}")


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def read_binding(environ: dict[str, str]) -> Binding:
    """The destination service binding from ``VCAP_SERVICES``."""
    try:
        services = json.loads(environ.get("VCAP_SERVICES") or "{}")
        creds = services["destination"][0]["credentials"]
        return Binding(creds["clientid"], creds["clientsecret"],
                       creds["url"].rstrip("/") + "/oauth/token", creds["uri"].rstrip("/"))
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ProbeError(
            "no destination service binding in VCAP_SERVICES; run this inside the "
            "app container"
        ) from None


def _authorization(body: dict, config: dict) -> str:
    """The header the destination sends: minted, or stored as ``URL.headers``."""
    tokens = body.get("authTokens")
    first = tokens[0] if isinstance(tokens, list) and tokens else None
    if isinstance(first, dict):
        header = first.get("http_header")
        if (isinstance(header, dict) and str(header.get("key") or "").lower() == "authorization"
                and isinstance(header.get("value"), str) and header["value"]):
            return header["value"]
        kind, value = first.get("type"), first.get("value")
        if isinstance(kind, str) and kind and isinstance(value, str) and value:
            return f"{kind} {value}"
    stored = config.get("URL.headers.Authorization")
    if isinstance(stored, str) and stored:
        return stored
    # The token service's own text can quote a client id: fixed text only.
    raise ProbeError("the destination returned no credential (authTokens empty or errored)")


def resolve(binding: Binding, destination: str, client: httpx.Client) -> tuple[Target, str]:
    """Step 1: the destination's target plus the printable summary of it."""
    tok = client.post(binding.token_url, data={"grant_type": "client_credentials"},
                      auth=(binding.client_id, binding.client_secret))
    if tok.status_code != 200:
        raise ProbeError(f"destination service token, HTTP {tok.status_code}")
    found = client.get(
        f"{binding.uri}/destination-configuration/v1/destinations/{quote(destination, safe='')}",
        headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
    )
    if found.status_code != 200:
        raise ProbeError(f"destination lookup, HTTP {found.status_code}")
    body = found.json()
    config = body.get("destinationConfiguration")
    if not isinstance(config, dict):
        raise ProbeError("the destination lookup answered no configuration")
    try:
        url = httpx.URL(str(config.get("URL") or ""))
    except Exception:
        raise ProbeError("the destination has no usable URL") from None
    path = url.path.rstrip("/")
    if not _HOST.fullmatch(url.host) or not _PATH.fullmatch(path) or url.userinfo:
        raise ProbeError("the destination has no usable URL")
    api_root = path.endswith("/2.0")
    kind = config.get("Authentication")
    shown = kind if isinstance(kind, str) and _TYPE.fullmatch(kind) else "unknown"
    summary = (f"Authentication={shown} "
               f"host={url.host} https: {_yes(url.scheme == 'https')} "
               f"api root in URL: {_yes(api_root)}")
    if url.scheme != "https":
        # The credential would travel in clear text: nothing more is sent.
        raise ProbeError(summary)
    port = f":{url.port}" if url.port else ""
    base = f"https://{url.host}{port}{path[:-4] if api_root else path}/2.0"
    return Target(base, _authorization(body, config)), summary


def _json(response: httpx.Response) -> dict:
    """The body as an object, or an empty one: a body is never shown."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def step_account(client: httpx.Client, target: Target) -> tuple[bool, str]:
    r = client.get(f"{target.base}/user", headers=target.headers)
    uuid = _json(r).get("uuid") if r.status_code == 200 else None
    known = isinstance(uuid, str) and bool(uuid)
    return known, f"HTTP {r.status_code}, account identified: {_yes(known)}"


def step_workspace(client: httpx.Client, target: Target, workspace: str,
                   found: list[str]) -> tuple[bool, str]:
    """Also notes the first repository of the page, for step 5 without one."""
    r = client.get(f"{target.base}/repositories/{workspace}", headers=target.headers,
                   params={"pagelen": "50"})
    body = _json(r)
    values = body.get("values")
    if r.status_code != 200 or not isinstance(values, list):
        return False, f"HTTP {r.status_code}"
    for entry in values:
        slug = entry.get("slug") if isinstance(entry, dict) else None
        # A slug from the answer goes into a URL: only in the form of a slug.
        if isinstance(slug, str) and SLUG.fullmatch(slug):
            found.append(slug)
            break
    return True, (f"HTTP 200, repositories on the first page: {len(values)}, "
                  f"more: {_yes(bool(body.get('next')))}")


def step_repository(client: httpx.Client, target: Target, workspace: str,
                    repository: str) -> tuple[bool, str]:
    r = client.get(f"{target.base}/repositories/{workspace}/{repository}",
                   headers=target.headers)
    return r.status_code == 200, f"HTTP {r.status_code}"


def step_pull_requests(client: httpx.Client, target: Target, workspace: str,
                       repository: str, branch: str) -> tuple[bool, str]:
    r = client.get(
        f"{target.base}/repositories/{workspace}/{repository}/pullrequests",
        headers=target.headers,
        params={"q": f'destination.branch.name="{branch}" AND state="OPEN"', "pagelen": "1"},
    )
    values = _json(r).get("values")
    if r.status_code != 200 or not isinstance(values, list):
        return False, f"HTTP {r.status_code}"
    return True, f"HTTP 200, open pull requests on the first page: {len(values)}"


def step_diff(client: httpx.Client, target: Target, workspace: str, repository: str,
              pull_request: int) -> tuple[bool, str]:
    """The diff endpoint answers with a redirect; it is followed by hand.

    Only when the ``Location`` is ``https`` on the host and port that were
    asked (the credential goes with it), and then exactly as given: the URL
    is parsed to compare, never rebuilt.
    """
    r = client.get(
        f"{target.base}/repositories/{workspace}/{repository}/pullrequests/{pull_request}/diff",
        headers={"Authorization": target.authorization},
    )
    if r.status_code == 200:
        return True, f"HTTP 200, no redirect, {len(r.text)} characters"
    location = r.headers.get("location")
    if r.status_code not in _REDIRECTS or not location:
        return False, f"HTTP {r.status_code}"
    try:
        there = httpx.URL(location)
    except Exception:
        there = None
    asked = r.request.url
    stays = (there is not None and there.scheme == "https" and there.host == asked.host
             and there.port == asked.port and not there.userinfo)
    head = f"HTTP {r.status_code}, redirect stays on the host: {_yes(stays)}"
    if not stays:
        return False, head
    diff = client.get(there, headers={"Authorization": target.authorization})
    return diff.status_code == 200, (f"{head}, then HTTP {diff.status_code}, "
                                     f"{len(diff.text)} characters")


def _run(report: Report, step: str, call, *args) -> None:
    """One step, one line; a failure to send is the exception's class only."""
    try:
        ok, detail = call(*args)
    except Exception as e:  # class name only: the text may quote a URL or a token
        report.line("FAIL", step, type(e).__name__)
        return
    report.line("PASS" if ok else "FAIL", step, detail)


def probe(args: argparse.Namespace, client: httpx.Client) -> int:
    """All six steps. Every step is reported, whatever the one before it said."""
    report = Report()
    repositories: list[str] = args.repository or []
    target = None
    try:
        target, summary = resolve(read_binding(dict(os.environ)), args.destination, client)
        report.line("PASS", "destination", summary)
    except ProbeError as e:
        report.line("FAIL", "destination", str(e))
    except Exception as e:
        report.line("FAIL", "destination", type(e).__name__)
    if target is None:
        for step in ("account", "workspace", "repository", "pull requests", "diff"):
            report.line("SKIP", step, "the destination did not resolve")
        return 1

    _run(report, "account", step_account, client, target)
    found: list[str] = []
    _run(report, f"workspace {args.workspace}", step_workspace, client, target,
         args.workspace, found)
    for slug in repositories:
        _run(report, f"repository {slug}", step_repository, client, target,
             args.workspace, slug)
    if not repositories:
        report.line("SKIP", "repository", "no --repository given")

    listed = repositories[0] if repositories else (found[0] if found else None)
    if listed is None:
        report.line("SKIP", "pull requests", "no repository to list them of")
    else:
        where = (f"pull requests of {listed}" if repositories
                 else "pull requests of the first repository of the workspace")
        _run(report, where, step_pull_requests, client, target, args.workspace, listed,
             args.branch)

    if args.pull_request is None or not repositories:
        report.line("SKIP", "diff", "needs --pull-request and a --repository")
    else:
        _run(report, "diff", step_diff, client, target, args.workspace, repositories[0],
             args.pull_request)
    return 1 if report.failed else 0


def _matching(pattern: re.Pattern[str], rule: str):
    """An argparse type that refuses by rule, never echoing the value."""
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise argparse.ArgumentTypeError(rule)
        return value
    return check


def _positive(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,17}", value):
        raise argparse.ArgumentTypeError("must be a positive whole number")
    return int(value)


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    slug = _matching(SLUG, "must be a slug: lower-case letters, digits, '.', '_' and '-'")
    ap.add_argument("--destination", required=True, help="BTP destination to Bitbucket Cloud")
    ap.add_argument("--workspace", required=True, type=slug, help="workspace slug")
    ap.add_argument("--repository", action="append", type=slug,
                    help="repository slug; may be given more than once")
    ap.add_argument("--branch", default="main",
                    type=_matching(BRANCH, "must be a branch name without spaces or quotes"),
                    help="destination branch of the pull request list (default: main)")
    ap.add_argument("--pull-request", type=_positive,
                    help="id of a pull request of the first --repository, for the diff step")
    args = ap.parse_args(argv)
    # httpx logs every request URL at INFO, the Location of the diff included.
    log = logging.getLogger("httpx")
    level = log.level
    log.setLevel(logging.WARNING)
    try:
        with httpx.Client(transport=transport, follow_redirects=False, trust_env=False,
                          timeout=30.0) as client:
            return probe(args, client)
    finally:
        log.setLevel(level)


if __name__ == "__main__":
    sys.exit(main())
