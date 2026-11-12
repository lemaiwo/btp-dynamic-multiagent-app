"""Go/no-go probe for the SAP Ariba Event Management API through a destination.

Answers, before a toolset is designed, what the destination and the API give
us for one sourcing event (an RFP):

  1. Does the destination resolve: https host, a minted token, the ``apiKey``
     header and the ``realm`` / ``user`` / ``passwordAdapter`` query
     properties present?
  2. Does the event answer (state, type, number of items)?
  3. Which suppliers are invited, and how many have a bid?
  4. What do the supplier bids look like: how many, how many attachments?
  5. Does one attachment download (``--attachments``: every attachment found,
     at most ``--max-attachments``), and what is it by its first bytes?

Without ``--event`` step 2 is replaced by the ids of the events created in the
last ``--days`` days (``/events/identifiers``; ``GET /events`` needs a filter).

Standalone on purpose: it imports nothing from this app, touches no database
and changes nothing in Ariba. GET requests only (the one POST is the token
request to the destination service).

It prints one ``PASS`` / ``FAIL`` / ``SKIP`` line per step: statuses, counts,
sizes, key names, event ids, state names and host names. It never prints a token, a
header value, a realm, a user, a supplier or contact name, a title, a file
name, a response body or an exception text. The raw answers are written only
with ``--dump DIR`` (a directory under the system temp directory): they hold
supplier and personal data, so copy them into the local, gitignored
``docs/evidence/`` and delete them where they were made.

    python scripts/probe_ariba.py --destination <NAME> [--event <Doc...>] \\
        [--attachments] [--max-attachments 5] [--dump /tmp/ariba]

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
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

try:
    import httpx
except ImportError:  # pragma: no cover - the script may run outside the venv
    sys.exit("httpx is required:  pip install httpx   (or use .venv's python)")

EVENT = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
_PATH = re.compile(r"[A-Za-z0-9._~/-]*")
_WORD = re.compile(r"[A-Za-z0-9_ -]{1,60}")
QUERY_KEYS = ("realm", "user", "passwordAdapter")
MAX_BYTES = 30 * 1024 * 1024
DUMP_REFUSED = "--dump must name a directory under the system temp directory"


class ProbeError(Exception):
    """A step failed; the text is fixed and safe to print."""


@dataclass
class Target:
    """Where the destination points and what it sends; ``repr`` shows neither."""

    base: str
    headers: dict[str, str] = field(repr=False)
    queries: dict[str, str] = field(repr=False)

    def __repr__(self) -> str:
        return "Target(…)"


class Report:
    def __init__(self) -> None:
        self.failed = False

    def line(self, verdict: str, step: str, detail: str) -> None:
        self.failed = self.failed or verdict == "FAIL"
        print(f"{verdict}  {step}: {detail}")


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def _word(value: object) -> str:
    """A state or type name, shown only when it has the form of one."""
    return value if isinstance(value, str) and _WORD.fullmatch(value) else "?"


def _dump_dir(directory: str) -> Path:
    target = Path(directory).resolve()
    roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}
    if not any(root == target or root in target.parents for root in roots):
        raise ProbeError(DUMP_REFUSED)
    return target


def _dump(directory: str | None, name: str, raw: bytes) -> None:
    if not directory:
        return
    target = _dump_dir(directory)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    (target / name).write_bytes(raw)


def resolve(environ: dict[str, str], destination: str,
            client: httpx.Client) -> tuple[Target, str]:
    """Step 1: the destination's target plus the printable summary of it."""
    try:
        creds = json.loads(environ.get("VCAP_SERVICES") or "{}")["destination"][0]["credentials"]
        token_url = creds["url"].rstrip("/") + "/oauth/token"
        uri, cid, secret = creds["uri"].rstrip("/"), creds["clientid"], creds["clientsecret"]
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ProbeError("no destination service binding in VCAP_SERVICES") from None
    tok = client.post(token_url, data={"grant_type": "client_credentials"}, auth=(cid, secret))
    if tok.status_code != 200:
        raise ProbeError(f"destination service token, HTTP {tok.status_code}")
    found = client.get(
        f"{uri}/destination-configuration/v1/destinations/{quote(destination, safe='')}",
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
    headers = {k[len("URL.headers."):]: str(v) for k, v in config.items()
               if k.startswith("URL.headers.")}
    queries = {k[len("URL.queries."):]: str(v) for k, v in config.items()
               if k.startswith("URL.queries.")}
    tokens = body.get("authTokens")
    first = tokens[0] if isinstance(tokens, list) and tokens else {}
    minted = isinstance(first, dict) and isinstance(first.get("value"), str) and bool(first["value"])
    placeholders = sum(1 for v in list(headers.values()) + list(queries.values())
                       if v.startswith("REPLACE_"))
    summary = (f"host={url.host} https: {_yes(url.scheme == 'https')} "
               f"token minted: {_yes(minted)} apiKey header: {_yes('apiKey' in headers)} "
               f"queries: {','.join(k for k in QUERY_KEYS if k in queries) or 'none'} "
               f"placeholders left: {placeholders}")
    if url.scheme != "https" or not minted or placeholders:
        raise ProbeError(summary)
    headers["Authorization"] = f"{first.get('type') or 'Bearer'} {first['value']}"
    headers["Accept"] = "application/json"
    port = f":{url.port}" if url.port else ""
    return Target(f"https://{url.host}{port}{path}", headers, queries), summary


def get(client: httpx.Client, target: Target, path: str, *, params: dict | None = None,
        accept: str | None = None) -> httpx.Response:
    headers = dict(target.headers)
    if accept:
        headers["Accept"] = accept
    return client.get(target.base + path, params={**target.queries, **(params or {})},
                      headers=headers)


def _payload(response: httpx.Response, step: str) -> object:
    if response.status_code != 200:
        code = ""
        try:
            body = response.json()
            code = _word(body.get("code") or body.get("errorCode")) if isinstance(body, dict) else ""
        except ValueError:
            pass
        raise ProbeError(f"{step}, HTTP {response.status_code}" + (f" code={code}" if code else ""))
    try:
        body = response.json()
    except ValueError:
        raise ProbeError(f"{step}, HTTP 200 but not JSON") from None
    return body.get("payload") if isinstance(body, dict) and "payload" in body else body


def find_attachments(node: object, item_id: str = "", field_id: str = "",
                     invitation: str = "", alternative: str = "") -> list[dict]:
    """Every ``attachmentValue`` below ``node`` with the ids its download needs."""
    found: list[dict] = []
    if isinstance(node, list):
        for child in node:
            found += find_attachments(child, item_id, field_id, invitation, alternative)
    elif isinstance(node, dict):
        item_id = node.get("itemId") if isinstance(node.get("itemId"), str) else item_id
        field_id = node.get("fieldId") if isinstance(node.get("fieldId"), str) else field_id
        invitation = (node.get("invitationId") if isinstance(node.get("invitationId"), str)
                      else invitation)
        alternative = (node.get("alternativeId") if isinstance(node.get("alternativeId"), str)
                       else alternative)
        for key, child in node.items():
            if key == "attachmentValue" and isinstance(child, dict) and child.get("id"):
                found.append({"id": str(child["id"]), "itemId": item_id, "fieldId": field_id,
                              "invitationId": invitation, "alternativeId": alternative,
                              "contentType": child.get("contentType"),
                              "contentLength": child.get("contentLength")})
            else:
                found += find_attachments(child, item_id, field_id, invitation, alternative)
    return found


def sniff(head: bytes) -> str:
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip (docx/xlsx/zip)"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole (doc/xls/msg)"
    if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff"):
        return "image"
    return "other"


def probe(args: argparse.Namespace, client: httpx.Client) -> int:
    report = Report()
    try:
        if args.dump:
            _dump_dir(args.dump)  # refused before anything is sent
        target, summary = resolve(dict(os.environ), args.destination, client)
        report.line("PASS", "destination", summary)
    except ProbeError as exc:
        report.line("FAIL", "destination", str(exc))
        return 1
    except Exception as exc:  # the text may hold a URL or a credential
        report.line("FAIL", "destination", type(exc).__name__)
        return 1

    def step(name: str, call) -> object:
        try:
            return call()
        except ProbeError as exc:
            report.line("FAIL", name, str(exc))
        except Exception as exc:
            report.line("FAIL", name, type(exc).__name__)
        return None

    if not args.event:
        def events() -> object:
            # GET /events needs a filter; the identifiers listing filters by date.
            now = int(time.time() * 1000)
            since = now - args.days * 86_400_000
            r = get(client, target, "/events/identifiers", params={
                "$filter": f"(createDateFrom gt {since} and createDateTo lt {now})",
                "$top": "100"})
            _dump(args.dump, "identifiers.json", r.content)
            payload = _payload(r, "events")
            rows = [x for x in payload if isinstance(x, dict)] if isinstance(payload, list) else []
            ids = [x["internalId"] for x in rows
                   if isinstance(x.get("internalId"), str) and EVENT.fullmatch(x["internalId"])]
            report.line("PASS", "events", f"{len(rows)} events created in the last "
                        f"{args.days} days; ids: " + (", ".join(ids[:40]) or "none"))
            return rows
        step("events", events)
        report.line("SKIP", "event", "no --event given")
        return 1 if report.failed else 0

    event = quote(args.event, safe="")

    def one_event() -> object:
        r = get(client, target, f"/events/{event}")
        _dump(args.dump, "event.json", r.content)
        payload = _payload(r, "event")
        if not isinstance(payload, dict):
            raise ProbeError("event, the payload is not an object")
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        report.line("PASS", "event", f"state={_word(payload.get('eventStateName'))} "
                    f"type={_word(payload.get('eventTypeName'))} items={len(items)} "
                    f"keys={len(payload)}")
        return payload

    def invitations() -> object:
        r = get(client, target, f"/events/{event}/supplierInvitations")
        _dump(args.dump, "supplierInvitations.json", r.content)
        payload = _payload(r, "supplierInvitations")
        rows = [x for x in payload if isinstance(x, dict)] if isinstance(payload, list) else []
        report.line("PASS", "supplierInvitations", f"{len(rows)} invited, "
                    f"{sum(1 for x in rows if x.get('hasBid') is True)} with a bid")
        return rows

    def bids() -> list[dict]:
        r = get(client, target, f"/events/{event}/supplierBids")
        _dump(args.dump, "supplierBids.json", r.content)
        payload = _payload(r, "supplierBids")
        rows = payload if isinstance(payload, list) else []
        attachments = find_attachments(rows)
        suppliers = {a["invitationId"] for a in attachments if a["invitationId"]}
        keys = sorted({k for row in rows if isinstance(row, dict) for k in row})
        report.line("PASS", "supplierBids", f"{len(rows)} bid rows, {len(attachments)} "
                    f"attachments from {len(suppliers)} suppliers; row keys: "
                    + ",".join(k for k in keys if _WORD.fullmatch(k)))
        return attachments

    step("event", one_event)
    step("supplierInvitations", invitations)
    attachments = step("supplierBids", bids) or []

    if not args.attachments:
        report.line("SKIP", "attachment", "no --attachments given")
    elif not attachments:
        report.line("SKIP", "attachment", "no attachment found in the bids")
    if args.item:
        attachments = [a for a in attachments if a["itemId"] == args.item]
    for n, a in enumerate(attachments[: args.max_attachments] if args.attachments else []):
        if n:
            time.sleep(args.pause)  # Ariba rate-limits: a burst is answered 429
        def download(a: dict = a, n: int = n) -> None:
            if not (a["invitationId"] and a["itemId"] and a["fieldId"]):
                raise ProbeError("attachment, its invitation, item or field id was not found")
            params = {"itemId": a["itemId"], "fieldId": a["fieldId"]}
            if a["alternativeId"]:
                params["alternativeId"] = a["alternativeId"]
            path = (f"/events/{event}/supplierBids/{quote(a['invitationId'], safe='')}"
                    f"/attachments/{quote(a['id'], safe='')}/content")
            # Ariba answers 406 to "application/octet-stream" (seen 2026-11-12).
            headers = dict(target.headers, Accept="*/*")
            with client.stream("GET", target.base + path, headers=headers,
                               params={**target.queries, **params}) as r:
                if r.status_code != 200:
                    raise ProbeError(f"attachment, HTTP {r.status_code}")
                raw = b""
                for chunk in r.iter_bytes():
                    raw += chunk
                    if len(raw) > MAX_BYTES:
                        raise ProbeError("attachment, over 30 MB")
            _dump(args.dump, f"attachment_{n}.bin", raw)
            declared = a["contentType"] if isinstance(a["contentType"], str) else ""
            report.line("PASS", f"attachment {n}", f"{len(raw)} bytes, by content: "
                        f"{sniff(raw[:8])}, declared: {_word(declared.replace('/', ' '))}")
        step(f"attachment {n}", download)
    return 1 if report.failed else 0


def _matching(pattern: re.Pattern[str], rule: str):
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise argparse.ArgumentTypeError(rule)
        return value
    return check


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--destination", required=True, help="BTP destination to the Ariba API")
    ap.add_argument("--event", type=_matching(EVENT, "must be an event id such as Doc123456"),
                    help="event (RFP) id; without it the events are only counted")
    ap.add_argument("--days", type=int, default=90,
                    help="without --event: list the events created in this many days")
    ap.add_argument("--attachments", action="store_true", help="download the bid attachments")
    ap.add_argument("--max-attachments", type=int, default=5)
    ap.add_argument("--item", type=_matching(EVENT, "must be an item id"),
                    help="download only the attachments of this item (question) id")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="seconds to wait between two downloads")
    ap.add_argument("--dump", help="write the raw answers to this directory under the temp dir")
    args = ap.parse_args(argv)
    # httpx logs every request URL at INFO: the realm and user are in it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with httpx.Client(timeout=60, transport=transport, follow_redirects=False) as client:
        return probe(args, client)


if __name__ == "__main__":
    sys.exit(main())
