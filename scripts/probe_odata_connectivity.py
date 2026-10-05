"""Connectivity probe for on-premise OData through the BTP connectivity proxy.

Settles, before the OData client is built on it, the questions only a landscape
can answer:

  1. Does the connectivity proxy accept a per-request ``Proxy-Authorization``
     for an ``http://`` virtual host (HTTP forward mode, no CONNECT tunnel)?
  2. Does the technical-user path work (the destination's own credential)?
  3. Which principal-propagation mechanism does the proxy accept for the
     signed-in user: ``exchange`` (a jwt-bearer exchange at the connectivity
     instance's XSUAA, the token goes into ``Proxy-Authorization``) or
     ``header`` (the app token in ``Proxy-Authorization`` plus the user's JWT
     in ``SAP-Connectivity-Authentication``)?

Standalone on purpose: stdlib and ``httpx`` only, nothing imported from this
app, so the file can be copied into (or piped to) a running app container of
any release. It reads the bindings from ``VCAP_SERVICES`` and sends one GET
per attempt. It writes nothing anywhere.

What it prints: property names, yes/no facts, HTTP status codes, the value of
the ``sap-authenticated-user`` response header (that is the point of the
probe: which SAP user the call ran as) and the length of the answer. It never
prints a credential, a token, a cookie or a response body, and an error is
reported by its class or its OAuth error code, never by its text.

Usage (inside the app container, with the app's Python):

    python probe_odata_connectivity.py --destination <name> \
        --path /sap/opu/odata/sap/<SRV>/ [--user] [--mode exchange|header|both] \
        [--origin sap.custom] [--timeout 30]

Without ``--user`` the technical path is tried. With ``--user`` the probe asks
for a one-time passcode (from the terminal, never an argument or a file),
exchanges it at the app's XSUAA for a user JWT that is held in memory only, and
tries the user mode(s). The passcode prompt needs a terminal: when the script
itself arrives on stdin there is no way to ask, so run it from a file in an
interactive session (``cf ssh <app> -t``).

The spike record needs two runs: one without ``--user`` against the
technical-user destination (settles ``TECH_USER``) and one with ``--user``
against the principal-propagation destination (settles ``PP_MODE``). Each run
prints ``not-tested`` for what it did not try; that is expected, not a failure.
In the final block the first word after each key is one fixed value; anything
that needs explaining follows in ``NOTE:`` lines.

A user run never sends a credential stored in the destination: SAP would get
the technical user next to the user token, could answer 2xx as the technical
user, and the run would look like working principal propagation. On a
destination whose ``Authentication`` is not ``PrincipalPropagation`` the user
attempts are still made (without the destination's headers), a ``WARNING`` is
printed and ``PP_MODE`` stays ``not-tested``.

``LOCATION_ID_NEEDED`` says that the destination carries a
``CloudConnectorLocationId`` (which is then sent), not that a call without it
was tried and failed.

Exit code: 0 when at least one attempted path answered 2xx, 1 when none did,
2 when the probe could not get as far as an attempt.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import urllib.parse
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

try:
    import httpx
except ImportError:  # pragma: no cover - the script may run outside the venv
    sys.exit("httpx is required: run this with the app's Python")

USER_MODES = ("exchange", "header")
PRINCIPAL_PROPAGATION = "PrincipalPropagation"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_QUERY_PREFIX = "URL.queries."
_HEADER_PREFIX = "URL.headers."
# Headers a destination must never be able to set through URL.headers.*: they
# decide which identity the proxy and the Cloud Connector see.
_RESERVED = ("proxy-authorization", "sap-connectivity-authentication", "host")


class ProbeError(Exception):
    """A failure the operator can act on; its text never holds a secret."""


@dataclass(frozen=True)
class OAuthClient:
    """Client credentials of a bound service instance (the secret is masked)."""

    client_id: str
    client_secret: str = field(repr=False)
    token_url: str


@dataclass(frozen=True)
class DestinationBinding:
    oauth: OAuthClient
    uri: str


@dataclass(frozen=True)
class ConnectivityBinding:
    oauth: OAuthClient
    proxy_host: str
    proxy_port: int


@dataclass(frozen=True)
class Bindings:
    destination: DestinationBinding
    connectivity: ConnectivityBinding
    xsuaa: OAuthClient


@dataclass(frozen=True)
class ResolvedDestination:
    """The parts of a destination the probe needs; never printed as a whole."""

    url: str
    authentication: str
    proxy_type: str
    location_id: str
    queries: dict[str, str] = field(repr=False)
    static_headers: dict[str, str] = field(repr=False)  # URL.headers.* properties
    token_headers: dict[str, str] = field(repr=False)  # from authTokens
    auth_tokens: bool = False
    auth_token_error: bool = False


@dataclass(frozen=True)
class ProbeResponse:
    status: int
    sap_user: str
    length: int


# ---- bindings ---------------------------------------------------------------


def _token_url(base: str) -> str:
    base = base.rstrip("/")
    return base if base.endswith("/oauth/token") else base + "/oauth/token"


def _credentials(services: Mapping[str, object], label: str) -> dict:
    instances = services.get(label)
    if not isinstance(instances, list) or not instances:
        raise ProbeError(f"VCAP_SERVICES has no '{label}' binding")
    creds = instances[0].get("credentials") if isinstance(instances[0], dict) else None
    if not isinstance(creds, dict):
        raise ProbeError(f"the '{label}' binding has no credentials")
    return creds


def _oauth(creds: dict, label: str, *url_keys: str) -> OAuthClient:
    base = next((creds[k] for k in url_keys if creds.get(k)), "")
    if not (creds.get("clientid") and creds.get("clientsecret") and base):
        # An X.509 binding has a certificate and key instead of a secret.
        raise ProbeError(
            f"the '{label}' binding has no clientid/clientsecret/url "
            "(a binding with credential-type x509 is not supported by this probe)"
        )
    return OAuthClient(str(creds["clientid"]), str(creds["clientsecret"]), _token_url(str(base)))


def read_bindings(vcap_services: str) -> Bindings:
    """Parse ``VCAP_SERVICES`` into the three bindings the probe uses."""
    if not (vcap_services or "").strip():
        raise ProbeError("VCAP_SERVICES is empty: run the probe inside the app container")
    try:
        services = json.loads(vcap_services)
    except ValueError:
        raise ProbeError("VCAP_SERVICES is not JSON") from None
    if not isinstance(services, dict):
        raise ProbeError("VCAP_SERVICES is not a JSON object")

    dest = _credentials(services, "destination")
    if not dest.get("uri"):
        raise ProbeError("the 'destination' binding has no uri")
    conn = _credentials(services, "connectivity")
    host = conn.get("onpremise_proxy_host")
    port = conn.get("onpremise_proxy_http_port") or conn.get("onpremise_proxy_port")
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        port_number = 0
    if not host or not port_number:
        raise ProbeError("the 'connectivity' binding has no onpremise proxy host/port")
    return Bindings(
        destination=DestinationBinding(
            _oauth(dest, "destination", "url"), str(dest["uri"]).rstrip("/")
        ),
        connectivity=ConnectivityBinding(
            _oauth(conn, "connectivity", "token_service_url", "url"), str(host), port_number
        ),
        xsuaa=_oauth(_credentials(services, "xsuaa"), "xsuaa", "url"),
    )


def describe_bindings(cfg: Bindings) -> None:
    """Say which bindings were found. Facts only: no id, secret or host."""
    print("BINDING destination: found")
    print(f"BINDING connectivity: found (onpremise proxy port {cfg.connectivity.proxy_port})")
    print("BINDING xsuaa: found")


# ---- pure helpers -----------------------------------------------------------


def proxy_headers(
    mode: str,
    *,
    app_token: str | None,
    user_jwt: str | None,
    exchanged: str | None,
    location_id: str,
) -> dict[str, str]:
    """The connectivity headers of one attempt.

    A user mode with a missing user token raises instead of sending the app
    token: a fall-back would run the call as the wrong identity and report it
    as principal propagation.
    """
    if mode == "technical":
        if not app_token:
            raise ProbeError("technical mode needs the connectivity app token")
        headers = {"Proxy-Authorization": f"Bearer {app_token}"}
    elif mode == "exchange":
        if not exchanged:
            raise ProbeError("exchange mode needs the exchanged user token")
        headers = {"Proxy-Authorization": f"Bearer {exchanged}"}
    elif mode == "header":
        if not (app_token and user_jwt):
            raise ProbeError("header mode needs the app token and the user JWT")
        headers = {
            "Proxy-Authorization": f"Bearer {app_token}",
            "SAP-Connectivity-Authentication": f"Bearer {user_jwt}",
        }
    else:
        raise ProbeError(f"unknown mode '{mode}'")
    if location_id:
        headers["SAP-Connectivity-SCC-Location_ID"] = location_id
    return headers


def destination_headers(dest: ResolvedDestination, mode: str) -> dict[str, str]:
    """The destination's own headers an attempt may carry.

    The technical path sends all of them: that is the credential under test.
    A user path sends only what ``authTokens`` returned for a
    ``PrincipalPropagation`` destination, and nothing at all for any other
    ``Authentication``: a stored credential next to the user token would let
    SAP answer as the technical user and pass for principal propagation.
    ``URL.headers.*`` are left out of a user path for the same reason (a
    static ``Authorization`` is a stored credential).
    """
    if mode == "technical":
        return {**dest.static_headers, **dest.token_headers}
    if dest.authentication == PRINCIPAL_PROPAGATION:
        return dict(dest.token_headers)
    return {}


def build_request_url(base_url: str, path: str, queries: Mapping[str, str]) -> str:
    """``<destination URL><path>?$format=json`` plus the destination's queries.

    Built by hand so that ``$format`` goes out as written (an encoded ``%24``
    is legal but not what every gateway release accepts).
    """
    params = {"$format": "json", **queries}
    query = urllib.parse.urlencode(params, safe="$", quote_via=urllib.parse.quote)
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}?{query}"


def _is_2xx(status: int | None) -> bool:
    return status is not None and 200 <= status < 300


def report_block(
    statuses: Mapping[str, int | None],
    *,
    location_id: str,
    auth_tokens: bool,
    sap_client: bool = False,
    scheme: str = "http",
    pp_destination: bool = True,
) -> str:
    """The spike record block. A path that was not attempted says so.

    ``statuses`` maps each attempted mode to its HTTP status (``None`` when no
    answer came back). ``pp_destination`` is false when the user modes ran
    against a destination that is not ``PrincipalPropagation``: no result is
    claimed for them then. The six lines each hold one fixed value; the
    explanations follow as ``NOTE:`` lines so the block stays parseable.
    """
    notes: list[str] = []
    ok = any(_is_2xx(s) for s in statuses.values())

    user = {m: statuses[m] for m in USER_MODES if m in statuses}
    if not user:
        pp_mode = "not-tested"
    elif not pp_destination:
        pp_mode = "not-tested"
        notes.append(
            "PP_MODE: the destination is not PrincipalPropagation, so the user attempts "
            "say nothing about principal propagation"
        )
    elif _is_2xx(user.get("exchange")):
        pp_mode = "exchange"  # the mechanism SAP recommends wins when both work
    elif _is_2xx(user.get("header")):
        pp_mode = "header"
    elif len(user) == len(USER_MODES):
        pp_mode = "none-works"
    else:
        pp_mode = "not-tested"
        notes.append(f"PP_MODE: only {next(iter(user))} was tried and it failed")

    answered = [s for s in statuses.values() if s is not None and s != 407 and s < 500]
    if scheme != "http" or not statuses:
        forward = "not-tested"
    elif ok:
        forward = "ok"
    elif answered:
        forward = "ok"
        notes.append(
            "ONPREM_HTTP_FORWARD: no 2xx; the proxy took the request and the target "
            f"answered {answered[0]}"
        )
    else:
        forward = "failed"

    if "technical" not in statuses:
        tech = "not-tested"
    else:
        tech = "ok" if _is_2xx(statuses["technical"]) else "failed"

    if not sap_client:
        client = "needs URL.queries.sap-client"
        notes.append("SAP_CLIENT: the destination sets none, so SAP used its default client")
    elif ok:
        client = "URL.queries.sap-client honoured"
        notes.append(
            "SAP_CLIENT: the property was sent and a 2xx came back; the probe cannot see "
            "which client SAP used"
        )
    else:
        client = "needs retest"
        notes.append("SAP_CLIENT: URL.queries.sap-client was sent, but no attempt answered 2xx")

    return "\n".join(
        [
            f"PP_MODE: {pp_mode}",
            f"ONPREM_HTTP_FORWARD: {forward}",
            f"TECH_USER: {tech}",
            f"LOCATION_ID_NEEDED: {f'yes ({location_id})' if location_id else 'no'}",
            f"FIND_DESTINATION_PP: authTokens {'present' if auth_tokens else 'absent'}",
            f"SAP_CLIENT: {client}",
            *(f"NOTE: {note}" for note in notes),
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe_odata_connectivity.py",
        description="Probe an OnPremise destination through the BTP connectivity proxy. "
        "Prints statuses and yes/no facts, never a credential, token or response body.",
    )
    parser.add_argument("--destination", required=True, help="name of the BTP destination")
    parser.add_argument(
        "--path", required=True, help="service path, e.g. /sap/opu/odata/sap/<SRV>/"
    )
    parser.add_argument(
        "--user",
        action="store_true",
        help="probe as a signed-in user: asks for a one-time code on the terminal",
    )
    parser.add_argument(
        "--mode",
        choices=("exchange", "header", "both"),
        default="both",
        help="principal-propagation mechanism to try with --user (default: both)",
    )
    parser.add_argument(
        "--origin", default="sap.custom", help="identity provider origin key (default: sap.custom)"
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per request")
    return parser


# ---- HTTP -------------------------------------------------------------------


async def send_through_proxy(
    proxy_url: str, url: str, *, headers: Mapping[str, str], timeout: float
) -> ProbeResponse:
    """One GET through an HTTP forward proxy; the body is counted, not kept.

    For an ``http://`` target ``httpx`` sends the request to the proxy in
    absolute form with the request's own headers, so a per-request
    ``Proxy-Authorization`` reaches the proxy. ``trust_env=False`` keeps
    ``HTTP_PROXY``/``NO_PROXY`` and ``.netrc`` from changing the route.
    """
    async with httpx.AsyncClient(
        proxy=proxy_url, timeout=timeout, trust_env=False, follow_redirects=False
    ) as client:
        response = await client.get(url, headers=dict(headers))
    user = response.headers.get("sap-authenticated-user", "")
    user = "".join(ch for ch in user if ch.isprintable() and not ch.isspace())[:64]
    return ProbeResponse(response.status_code, user, len(response.content))


async def _token(
    client: httpx.AsyncClient, oauth: OAuthClient, form: dict[str, str], what: str
) -> str:
    try:
        response = await client.post(
            oauth.token_url, data=form, auth=(oauth.client_id, oauth.client_secret)
        )
    except httpx.HTTPError as exc:
        raise ProbeError(f"{what}: {type(exc).__name__}") from None
    token = None
    code = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            token = payload.get("access_token")
            code = str(payload.get("error") or "")[:40]
    except ValueError:
        pass
    if response.status_code != 200 or not isinstance(token, str) or not token:
        detail = f"HTTP {response.status_code}" + (f" {code}" if code.isidentifier() else "")
        raise ProbeError(f"{what}: {detail}")
    return token


async def _find_destination(
    client: httpx.AsyncClient, binding: DestinationBinding, name: str, user_jwt: str | None
) -> ResolvedDestination:
    token = await _token(
        client, binding.oauth, {"grant_type": "client_credentials"}, "destination service token"
    )
    headers = {"Authorization": f"Bearer {token}"}
    if user_jwt:
        headers["X-user-token"] = user_jwt
    url = f"{binding.uri}/destination-configuration/v1/destinations/{urllib.parse.quote(name, '')}"
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ProbeError(f"find destination: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise ProbeError(f"find destination: HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise ProbeError("find destination: the answer is not JSON") from None
    cfg = payload.get("destinationConfiguration") if isinstance(payload, dict) else None
    if not isinstance(cfg, dict):
        raise ProbeError("find destination: no destinationConfiguration in the answer")

    static_headers: dict[str, str] = {}
    token_headers: dict[str, str] = {}
    queries: dict[str, str] = {}
    for key, value in cfg.items():
        if key.startswith(_QUERY_PREFIX) and key[len(_QUERY_PREFIX) :]:
            queries[key[len(_QUERY_PREFIX) :]] = str(value)
        elif key.startswith(_HEADER_PREFIX) and key[len(_HEADER_PREFIX) :]:
            if key[len(_HEADER_PREFIX) :].lower() not in _RESERVED:
                static_headers[key[len(_HEADER_PREFIX) :]] = str(value)

    tokens = payload.get("authTokens") or []
    token_error = False
    if tokens:
        first = tokens[0] if isinstance(tokens, list) else None
        header = first.get("http_header") or {} if isinstance(first, dict) else None
        if not isinstance(header, dict):
            raise ProbeError("find destination: authTokens has an unexpected shape")
        token_error = bool(first.get("error"))
        if not token_error:
            if header.get("key") and header.get("value"):
                if str(header["key"]).lower() not in _RESERVED:
                    token_headers[str(header["key"])] = str(header["value"])
            elif first.get("type") and first.get("value"):
                token_headers["Authorization"] = f"{first['type']} {first['value']}"
    return ResolvedDestination(
        url=str(cfg.get("URL") or ""),
        authentication=str(cfg.get("Authentication") or ""),
        proxy_type=str(cfg.get("ProxyType") or ""),
        location_id=str(cfg.get("CloudConnectorLocationId") or ""),
        queries=queries,
        static_headers=static_headers,
        token_headers=token_headers,
        auth_tokens=bool(tokens),
        auth_token_error=token_error,
    )


def _yes(value: object) -> str:
    return "yes" if value else "no"


def _describe_destination(dest: ResolvedDestination, scheme: str) -> None:
    print(f"DESTINATION Authentication: {dest.authentication or '-'}")
    print(f"DESTINATION ProxyType: {dest.proxy_type or '-'}")
    print(f"DESTINATION CloudConnectorLocationId present: {_yes(dest.location_id)}")
    print(f"DESTINATION authTokens present: {_yes(dest.auth_tokens)}")
    if dest.auth_tokens:
        print(f"DESTINATION authTokens error: {_yes(dest.auth_token_error)}")
    print(f"DESTINATION URL scheme: {scheme or '-'}")
    print(f"DESTINATION URL.queries: {', '.join(sorted(dest.queries)) or '-'}")


def _has_terminal() -> bool:
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        return sys.stdin.isatty()


def _read_passcode() -> str:
    """Ask on the terminal. Refuses when the code could not be read unseen.

    Without a terminal ``getpass`` falls back to stdin, which is the script
    itself when the probe was piped into ``python -``. When it cannot turn
    echo off it warns (``GetPassWarning``) and reads an echoed line; the
    warning is made an error here, which is raised before anything is read.
    """
    if not _has_terminal():
        raise ProbeError(
            "--user needs a terminal for the passcode: run the probe from a file "
            "in an interactive session (cf ssh <app> -t)"
        )
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("One-time passcode: ")
        except getpass.GetPassWarning:
            raise ProbeError(
                "the terminal cannot hide the passcode (echo cannot be turned off): "
                "nothing was read; use an interactive session (cf ssh <app> -t)"
            ) from None


async def run(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    read_passcode: Callable[[], str] = _read_passcode,
) -> int:
    """Run the probe. ``transport`` replaces the direct (non-proxy) calls in tests."""
    try:
        return await _run(args, environ, transport, read_passcode)
    except ProbeError as exc:
        print(f"ERROR: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - the text may hold a URL or a token
        # No traceback and no message: an httpx.InvalidURL or a parser error
        # quotes its input, which here is landscape data.
        print(f"ERROR: unexpected {type(exc).__name__}")
        return 2


async def _run(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    transport: httpx.AsyncBaseTransport | None,
    read_passcode: Callable[[], str],
) -> int:
    cfg = read_bindings(environ.get("VCAP_SERVICES", ""))
    describe_bindings(cfg)
    modes = (
        (USER_MODES if args.mode == "both" else (args.mode,)) if args.user else ("technical",)
    )

    async with httpx.AsyncClient(
        transport=transport, timeout=args.timeout, trust_env=False
    ) as client:
        user_jwt: str | None = None
        if args.user:
            # First, while the code is fresh: a passcode lives for a few minutes.
            print("A one-time passcode comes from <the app's XSUAA url>/passcode.")
            passcode = read_passcode().strip()
            if not passcode:
                raise ProbeError("no passcode given")
            user_jwt = await _token(
                client,
                cfg.xsuaa,
                {
                    "grant_type": "password",
                    "passcode": passcode,
                    "login_hint": json.dumps({"origin": args.origin}),
                    "response_type": "token",
                },
                "user token (passcode grant)",
            )
            del passcode
            print("USER token: obtained")

        dest = await _find_destination(client, cfg.destination, args.destination, user_jwt)
        scheme = urllib.parse.urlsplit(dest.url).scheme.lower()
        _describe_destination(dest, scheme)
        if dest.proxy_type != "OnPremise":
            raise ProbeError(
                "this probe is for OnPremise destinations (see 'DESTINATION ProxyType' above)"
            )
        if scheme != "http":
            # An https target would be a CONNECT tunnel: the request's own
            # Proxy-Authorization then travels inside the tunnel, not to the proxy.
            raise ProbeError(
                "the destination URL is not http:// - the connectivity proxy is an HTTP "
                "forward proxy; use http://<virtual host>:<port> in the destination"
            )

        pp_destination = dest.authentication == PRINCIPAL_PROPAGATION
        from_destination = destination_headers(dest, modes[0])
        taken = any(name.lower() == "authorization" for name in from_destination)
        print(f"DESTINATION Authorization header taken from destination: {_yes(taken)}")
        if args.user and not pp_destination:
            print(
                "WARNING: --user against a destination whose Authentication is not "
                "PrincipalPropagation. Its headers and credential are not sent, and the "
                "attempts below are not reported as a principal-propagation result."
            )

        url = build_request_url(dest.url, args.path, dest.queries)
        proxy_url = f"http://{cfg.connectivity.proxy_host}:{cfg.connectivity.proxy_port}"
        statuses: dict[str, int | None] = {}
        app_token: str | None = None
        for mode in modes:
            error = ""
            result: ProbeResponse | None = None
            try:
                exchanged: str | None = None
                if mode == "exchange":
                    exchanged = await _token(
                        client,
                        cfg.connectivity.oauth,
                        {
                            "grant_type": JWT_BEARER,
                            "assertion": user_jwt or "",
                            "token_format": "jwt",
                            "response_type": "token",
                        },
                        "token_exchange",
                    )
                else:
                    if app_token is None:
                        app_token = await _token(
                            client,
                            cfg.connectivity.oauth,
                            {"grant_type": "client_credentials"},
                            "connectivity_token",
                        )
                headers = {
                    "Accept": "application/json",
                    **destination_headers(dest, mode),
                    **proxy_headers(
                        mode,
                        app_token=app_token,
                        user_jwt=user_jwt,
                        exchanged=exchanged,
                        location_id=dest.location_id,
                    ),
                }
                result = await send_through_proxy(
                    proxy_url, url, headers=headers, timeout=args.timeout
                )
            except ProbeError as exc:
                error = str(exc).replace(": ", "=").replace(" ", "_")
            except Exception as exc:  # noqa: BLE001 - class only, see run()
                error = type(exc).__name__
            statuses[mode] = result.status if result else None
            line = (
                f"RESULT mode={mode} status={result.status if result else '-'} "
                f"sap_user_header={(result.sap_user if result else '') or '-'} "
                f"length={result.length if result else 0}"
            )
            print(line + (f" error={error}" if error else ""))

    print()
    print(
        report_block(
            statuses,
            location_id=dest.location_id,
            auth_tokens=dest.auth_tokens,
            sap_client="sap-client" in dest.queries,
            scheme=scheme,
            pp_destination=pp_destination,
        )
    )
    return 0 if any(_is_2xx(s) for s in statuses.values()) else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Results must appear as they happen, also when the output is piped.
    sys.stdout.reconfigure(line_buffering=True)
    return asyncio.run(run(args, os.environ))


if __name__ == "__main__":
    sys.exit(main())
