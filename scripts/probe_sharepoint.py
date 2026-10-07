"""Go/no-go probe for ``builtin:sharepoint``: can this credential read the file?

Answers, before an agent is configured, the questions that decide whether the
Entra / Graph side is ready:

  1. Does the BTP destination hand out a Graph token (client credentials)?
  2. Does the token resolve the site (``Sites.Selected`` granted on it)?
  3. Is the document library there under the configured name?
  4. Is the file there, how large is it, and is its download location on the
     site's own host?
  5. Does the download answer with a workbook?

Standalone on purpose: it imports nothing from this app, touches no database
and changes nothing in SharePoint. Reads only.

It prints names, sizes, statuses and host names. It never prints a token, a
client secret, the download URL (which is a credential) or a byte of the file.

Run it where the destination service binding is: inside the app container.

    python scripts/probe_sharepoint.py --destination <NAME> \\
        --site <tenant>.sharepoint.com:/sites/<site> \\
        --library Documents --path "<folder>/<workbook>.xlsx"

The binding is read from ``VCAP_SERVICES`` (label ``destination``). Exit code
0 when every step passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

try:
    import httpx
except ImportError:  # pragma: no cover - the script may run outside the venv
    sys.exit("httpx is required:  pip install httpx   (or use .venv's python)")

GRAPH = "https://graph.microsoft.com/v1.0"
MAX_BYTES = 20 * 1024 * 1024


class ProbeError(Exception):
    """A step failed; the text is safe to print."""


@dataclass
class Binding:
    client_id: str
    client_secret: str
    token_url: str
    uri: str

    def __repr__(self) -> str:  # never the secret
        return f"Binding(client_id=…, uri={urlsplit(self.uri).hostname})"


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


def graph_token(binding: Binding, destination: str, client: httpx.Client) -> str:
    """The Graph token the destination service mints for ``destination``."""
    tok = client.post(binding.token_url, data={"grant_type": "client_credentials"},
                      auth=(binding.client_id, binding.client_secret))
    if tok.status_code != 200:
        raise ProbeError(f"destination service token: HTTP {tok.status_code}")
    found = client.get(
        f"{binding.uri}/destination-configuration/v1/destinations/{quote(destination, safe='')}",
        headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
    )
    if found.status_code != 200:
        raise ProbeError(f"destination lookup: HTTP {found.status_code}")
    body = found.json()
    config = body.get("destinationConfiguration") or {}
    print(f"      destination Authentication={config.get('Authentication')} "
          f"host={urlsplit(str(config.get('URL') or '')).hostname}")
    tokens = body.get("authTokens") or []
    if not tokens or not tokens[0].get("value"):
        # The token service's own text can quote the client id: fixed text only.
        raise ProbeError("the destination returned no token (authTokens empty or errored)")
    return str(tokens[0]["value"])


def probe(site: str, library: str, path: str, token: str, client: httpx.Client) -> int:
    """Steps 2 to 5. Prints one PASS/FAIL line per step; returns the exit code."""
    headers = {"Authorization": f"Bearer {token}"}
    host, _, site_path = site.partition(":")

    r = client.get(f"{GRAPH}/sites/{host}:{quote(site_path)}", headers=headers)
    if r.status_code != 200:
        print(f"FAIL  site: HTTP {r.status_code}"
              + ("  (403: Sites.Selected is not granted on this site)"
                 if r.status_code == 403 else ""))
        return 1
    site_id = r.json().get("id", "")
    print(f"PASS  site resolved: {r.json().get('displayName')!r}")

    r = client.get(f"{GRAPH}/sites/{quote(site_id, safe=',')}/drives",
                   headers=headers, params={"$select": "id,name", "$top": "200"})
    if r.status_code != 200:
        print(f"FAIL  libraries: HTTP {r.status_code}")
        return 1
    drives = r.json().get("value") or []
    names = sorted(str(d.get("name")) for d in drives)
    match = [d for d in drives if str(d.get("name") or "").casefold() == library.casefold()]
    if len(match) != 1:
        print(f"FAIL  library {library!r} not found; the site has: {', '.join(names)}")
        return 1
    print(f"PASS  library found (of {len(names)}: {', '.join(names)})")

    item_path = "/".join(quote(p, safe="") for p in path.split("/"))
    r = client.get(f"{GRAPH}/drives/{quote(match[0]['id'], safe='!')}/root:/{item_path}",
                   headers=headers)
    if r.status_code != 200:
        print(f"FAIL  file: HTTP {r.status_code}")
        return 1
    item = r.json()
    print(f"PASS  file found: {item.get('name')!r} size={item.get('size')} "
          f"modified={item.get('lastModifiedDateTime')} etag={'yes' if item.get('eTag') else 'no'}")
    if not isinstance(item.get("file"), dict):
        print("FAIL  the path is not a file")
        return 1
    if int(item.get("size") or 0) > MAX_BYTES:
        print(f"FAIL  the file is larger than the {MAX_BYTES} byte cap of the toolset")
        return 1

    url = str(item.get("@microsoft.graph.downloadUrl") or "")
    if not url:
        print("FAIL  no download location in the item")
        return 1
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() != host.lower():
        print(f"FAIL  download host is {parts.hostname!r}, not the site host {host!r}: "
              "the toolset would refuse it")
        return 1
    print(f"PASS  download location is on {parts.hostname}")

    size = 0
    head = b""
    with client.stream("GET", url, follow_redirects=False) as dl:
        if dl.status_code != 200:
            print(f"FAIL  download: HTTP {dl.status_code}")
            return 1
        for chunk in dl.iter_bytes():
            if len(head) < 4:
                head = (head + chunk)[:4]
            size += len(chunk)
            if size > MAX_BYTES:
                break
    ok = head.startswith(b"PK")
    print(f"{'PASS' if ok else 'FAIL'}  downloaded {size} bytes, "
          f"{'a zip archive (xlsx)' if ok else 'not a workbook'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--destination", required=True, help="BTP destination to Microsoft Graph")
    ap.add_argument("--site", required=True, help="<tenant>.sharepoint.com:/sites/<site>")
    ap.add_argument("--library", required=True, help="document library name")
    ap.add_argument("--path", required=True, help="file path below the library root")
    args = ap.parse_args(argv)
    try:
        with httpx.Client(timeout=60, trust_env=False) as client:
            binding = read_binding(dict(os.environ))
            token = graph_token(binding, args.destination, client)
            print("PASS  the destination hands out a Graph token")
            return probe(args.site, args.library, args.path, token, client)
    except ProbeError as e:
        print(f"FAIL  {e}")
        return 1
    except httpx.HTTPError as e:
        print(f"FAIL  network: {type(e).__name__}")
        return 1
    except Exception as e:  # class name only: the text may quote a URL or a token
        print(f"FAIL  unexpected {type(e).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
