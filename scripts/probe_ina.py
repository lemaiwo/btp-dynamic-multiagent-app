"""Go/no-go probe for InA (``/sap/bw/ina``) on an OnPremise destination.

Run inside the app container (it needs the destination and connectivity
bindings), as the technical user of the destination:

    cf ssh pydantic-agent -c '/tmp/lifecycle/launcher /home/vcap/app \\
        "python scripts/probe_ina.py --destination saperp-sia-100" ""'

Three steps, one line each: ``GetServerInfo`` (is InA there, which services
and how many capabilities it reports), a metadata catalogue request (how many
queries it lists and the first names), and a metadata request for one query
when ``--query`` names it (how many dimensions and measures). Prints statuses,
counts, keys and object NAMES, never a credential, a token, a URL or a value
of the data. Reuses the binding, destination and proxy helpers of
``probe_odata_connectivity.py``; nothing of the app is imported.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_odata_connectivity import (  # noqa: E402
    ProbeError,
    _find_destination,
    _token,
    build_request_url,
    describe_bindings,
    destination_headers,
    proxy_headers,
    read_bindings,
)

INA_INFO = "/sap/bw/ina/GetServerInfo"
INA_RESPONSE = "/sap/bw/ina/GetResponse"
MAX_NAMES = 25
MAX_BODY = 4 * 1024 * 1024


CLIENT_INFO = {"Version": "1.0", "Identifier": "btp-dynamic-multiagent-probe",
               "Component": "probe"}


def _envelope(metadata: dict[str, Any], capabilities: list[str]) -> dict[str, Any]:
    body = {"ClientInfo": CLIENT_INFO, "Metadata": {"Capabilities": capabilities,
                                                   "Context": "Analytics", **metadata},
            "Options": ["StatefulServer"]}
    return body


def _catalog_requests(capabilities: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Catalogue requests to try, most likely first: the ``$$DataSource$$``
    catalogue (what SAC sends), then a wildcard query list."""
    return [
        ("catalog", _envelope({
            "Expand": ["Cubes"],
            "DataSource": {"ObjectName": "$$DataSource$$", "PackageName": "", "Type": "Catalog"},
            "Language": "EN",
        }, capabilities)),
        ("catalog-notype", _envelope({
            "Expand": ["Cubes"],
            "DataSource": {"ObjectName": "$$DataSource$$", "Type": "Catalog"},
            "Language": "EN",
        }, capabilities)),
        ("query-wildcard", _envelope({
            "Expand": ["Cubes"],
            "DataSource": {"ObjectName": "*", "Type": "Query"},
            "Language": "EN",
        }, capabilities)),
    ]


def _query_request(name: str, capabilities: list[str]) -> dict[str, Any]:
    return _envelope({
        "DataSource": {"ObjectName": name, "Type": "Query"},
        "Expand": ["Cube", "Axis?Name=Rows", "Axis?Name=Columns", "Axis?Name=Filter",
                   "Variables", "DynamicFilter"],
        "Language": "EN",
    }, capabilities)


def _name(value: Any) -> str:
    text = str(value) if value is not None else ""
    return "".join(ch for ch in text if ch.isprintable())[:80]


def _cubes(payload: Any) -> list[str]:
    """The query names of a catalogue answer, whatever nesting the server used."""
    found: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > 8 or len(found) >= 500:
            return
        if isinstance(node, dict):
            if "Cubes" in node and isinstance(node["Cubes"], list):
                for cube in node["Cubes"]:
                    if isinstance(cube, dict):
                        name = cube.get("ObjectName") or cube.get("Name")
                        if name:
                            found.append(_name(name))
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(payload, 0)
    return found


def _count(payload: Any, key: str) -> int:
    total = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal total
        if depth > 8:
            return
        if isinstance(node, dict):
            if key in node and isinstance(node[key], list):
                total += len(node[key])
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(payload, 0)
    return total


async def _send(
    client: httpx.AsyncClient, method: str, url: str, headers: dict[str, str],
    body: Any | None, csrf: dict[str, str],
) -> tuple[int, str, Any]:
    """One call through the proxy client. ICF wants a CSRF token on a POST:
    the GET fetches it (``X-CSRF-Token: Fetch``, the session cookie stays in
    the client), the POST sends it back; ``csrf`` carries it between calls."""
    sent = dict(headers)
    if method == "GET":
        sent["X-CSRF-Token"] = "Fetch"
        response = await client.get(url, headers=sent)
        token = response.headers.get("x-csrf-token", "")
        if token and token.lower() != "required":
            csrf["token"] = token
    else:
        if csrf.get("token"):
            sent["X-CSRF-Token"] = csrf["token"]
        response = await client.post(url, headers=sent, json=body)
    ctype = response.headers.get("content-type", "").split(";")[0].strip()[:60]
    payload: Any = None
    if len(response.content) <= MAX_BODY and "json" in ctype:
        try:
            payload = response.json()
        except ValueError:
            payload = None
    return response.status_code, ctype, payload


def _top_keys(payload: Any) -> str:
    if isinstance(payload, dict):
        return ",".join(_name(k) for k in list(payload)[:12])
    return type(payload).__name__


async def run(args: argparse.Namespace, environ: dict[str, str]) -> int:
    try:
        cfg = read_bindings(environ.get("VCAP_SERVICES", ""))
        describe_bindings(cfg)
        async with httpx.AsyncClient(timeout=args.timeout, trust_env=False) as client:
            dest = await _find_destination(client, cfg.destination, args.destination, None)
            app_token = await _token(
                client, cfg.connectivity.oauth, {"grant_type": "client_credentials"},
                "connectivity token",
            )
        print(f"DESTINATION proxy_type={_name(dest.proxy_type)} "
              f"authentication={_name(dest.authentication)} "
              f"location_id={'yes' if dest.location_id else 'no'}")
        if dest.proxy_type.lower() != "onpremise":
            raise ProbeError("this probe routes through the connectivity proxy: "
                             "the destination must be OnPremise")
        proxy_url = f"http://{cfg.connectivity.proxy_host}:{cfg.connectivity.proxy_port}"
        csrf: dict[str, str] = {}
        proxied = httpx.AsyncClient(
            proxy=proxy_url, timeout=args.timeout, trust_env=False, follow_redirects=False
        )
        headers = {
            **proxy_headers("technical", app_token=app_token, user_jwt=None, exchanged=None,
                            location_id=dest.location_id),
            **destination_headers(dest, "technical"),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        # 1. GetServerInfo
        url = build_request_url(dest.url, INA_INFO, dest.queries)
        status, ctype, payload = await _send(proxied, "GET", url, headers, None, csrf)
        services = payload.get("Services") if isinstance(payload, dict) else None
        names = [_name(s.get("Service")) for s in services if isinstance(s, dict)] \
            if isinstance(services, list) else []
        caps = sum(len(s.get("Capabilities") or []) for s in services if isinstance(s, dict)) \
            if isinstance(services, list) else 0
        analytics = next((s for s in (services or []) if isinstance(s, dict)
                          and s.get("Service") == "Analytics"), {})
        capabilities = [c.get("Capability") if isinstance(c, dict) else str(c)
                        for c in (analytics.get("Capabilities") or [])]
        capabilities = [c for c in capabilities if isinstance(c, str)]
        verdict = "PASS" if status == 200 and isinstance(payload, dict) else "FAIL"
        print(f"{verdict} GetServerInfo: HTTP {status} {ctype} keys={_top_keys(payload)} "
              f"services={names} capabilities={caps} csrf={'yes' if csrf.get('token') else 'no'}")
        if verdict == "FAIL":
            return 1
        # 1b. GetCatalog: the catalogue endpoint next to GetResponse
        url = build_request_url(dest.url, "/sap/bw/ina/GetCatalog", dest.queries)
        status, ctype, payload = await _send(proxied, "GET", url, headers, None, csrf)
        cat_cubes = _cubes(payload)
        print(f"{'PASS' if status == 200 else 'FAIL'} GetCatalog: HTTP {status} {ctype} "
              f"keys={_top_keys(payload)} cubes={len(cat_cubes)} first={cat_cubes[:MAX_NAMES]}"
              f" size={len(json.dumps(payload)) if payload is not None else 0}")
        # 2. catalogue of queries: the first shape that lists something wins
        url = build_request_url(dest.url, INA_RESPONSE, dest.queries)
        for label, body in _catalog_requests(capabilities):
            status, ctype, payload = await _send(proxied, "POST", url, headers, body, csrf)
            cubes = _cubes(payload)
            ok = status == 200 and isinstance(payload, dict) and bool(cubes)
            verdict = "PASS" if ok else "FAIL"
            print(f"{verdict} catalogue[{label}]: HTTP {status} {ctype} "
                  f"keys={_top_keys(payload)} queries={len(cubes)} first={cubes[:MAX_NAMES]}")
            if isinstance(payload, dict) and payload.get("Messages"):
                for m in payload["Messages"][:3]:
                    if isinstance(m, dict):
                        print(f"  message number={_name(m.get('Number'))} "
                              f"type={_name(m.get('Type'))} "
                              f"text={_name(m.get('Text'))[:300]}")
            if ok:
                break
        # 3. one query's metadata
        if args.query:
            status, ctype, payload = await _send(
                proxied, "POST", url, headers, _query_request(args.query, capabilities), csrf)
            verdict = "PASS" if status == 200 and isinstance(payload, dict) else "FAIL"
            print(f"{verdict} query {_name(args.query)}: HTTP {status} {ctype} "
                  f"keys={_top_keys(payload)} dimensions={_count(payload, 'Dimensions')} "
                  f"measures={_count(payload, 'Members')} variables={_count(payload, 'Variables')}")
        await proxied.aclose()
        return 0
    except ProbeError as exc:
        print(f"ERROR: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - the text may hold a URL or a token
        print(f"ERROR: unexpected {type(exc).__name__}")
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(prog="probe_ina.py",
                                     description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--destination", required=True, help="name of the OnPremise destination")
    parser.add_argument("--query", default="", help="an analytical query name to describe")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    return asyncio.run(run(args, dict(os.environ)))


if __name__ == "__main__":
    sys.exit(main())
