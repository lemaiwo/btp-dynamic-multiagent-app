"""Destinations from a local file, for a deployment without BTP.

On BTP every built-in reaches its target through the destination service
(``agents.destination``). A self-hosted deployment (Docker next to the SAP
systems, ``deploy/local``) has no destination service, so
``LOCAL_DESTINATIONS_FILE`` names a JSON file that stands in for it. Nothing
else changes: ``find`` answers the destination service's own "find
destination" shape (``destinationConfiguration`` plus ``authTokens``), so the
resolver, its caches, ``DestinationAuth`` and every built-in work as they do
on BTP. A destination service binding in ``VCAP_SERVICES`` wins over the file.

The file::

    {
      "saml": {"issuer": "agent-local-saml-idp",
               "key_file": "/run/secrets/saml-idp.key",
               "cert_file": "/run/secrets/saml-idp.crt",
               "ttl_seconds": 300},
      "destinations": [
        {"Name": "S4_USER", "URL": "https://s4.example.corp:44300",
         "Authentication": "SAMLAssertion", "audience": "S4H_100",
         "URL.queries.sap-client": "100"},
        {"Name": "S4_TECH", "URL": "https://s4.example.corp:44300",
         "Authentication": "BasicAuthentication",
         "User": "AGENT_TECH", "Password": "${S4_TECH_PASSWORD}"}
      ]
    }

Property names are the destination service's (``URL``, ``Authentication``,
``User``, ``Password``, ``URL.headers.*``, ``URL.queries.*``, ``Type``,
``Description``; a ``MAIL`` or HANA destination keeps its own properties).
``${NAME}`` in a value is replaced by that environment variable, so a
password need not be in the file; a variable that is not set is an error
naming the variable. Relative ``key_file`` / ``cert_file`` are relative to
the file.

``Authentication``:

``NoAuthentication``     nothing is added (``URL.headers.Authorization`` may
                         hold a static credential, as on BTP).
``BasicAuthentication``  ``User`` / ``Password`` as a technical user.
``SAMLAssertion``        **the signed-in user**: a SAML 2.0 bearer assertion
                         signed with this deployment's key
                         (``agents.saml_bearer``), sent as ``Authorization:
                         SAML2.0 ...``. Properties: ``audience`` (required:
                         the ABAP system's SAML2 local provider name),
                         ``userIdSource`` (the token claim that names the
                         user, default ``email``), ``nameIdFormat`` (``email``
                         | ``unspecified``, default ``email``), ``recipient``
                         (default: the URL), ``authnContextClassRef``.
                         ``x-sap-security-session: create`` is sent unless a
                         ``URL.headers.x-sap-security-session`` property says
                         otherwise (``none`` sends nothing).
``ForwardUserToken``     **the signed-in user**: their own OIDC access token
                         as ``Authorization: Bearer``, for a target that
                         validates the same issuer (an ARC-1 MCP server).

The two user types need a validated user token: the token is validated again
here (``agents.auth.get_validator``), never merely decoded, and without a
validator nothing is minted. A user is never replaced by a technical one.
``ProxyType: OnPremise`` is refused: there is no connectivity proxy here,
the target is reached directly, over ``https``. ``"AllowPlainHttp": "true"``
admits an ``http://`` URL for a service on the same private network (an ARC-1
container next to the app); never for ``SAMLAssertion``. ``TLSServerName``
names the host the server's certificate is checked against when that is not
the URL's host (a system whose own certificate says ``localhost``): the
certificate must still verify (put it, or its CA, into ``/extra-ca``).

A log line names the destination and, for a minted assertion, the principal;
never a URL, a credential, a NameID or an assertion.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agents.destination import DestinationError, DestinationServiceConfig

logger = logging.getLogger(__name__)

FILE_ENV = "LOCAL_DESTINATIONS_FILE"
SAML_ASSERTION = "SAMLAssertion"
FORWARD_USER_TOKEN = "ForwardUserToken"
LOCAL_AUTH_TYPES = frozenset({
    "NoAuthentication", "BasicAuthentication", SAML_ASSERTION, FORWARD_USER_TOKEN,
})
USER_AUTH_TYPES = frozenset({SAML_ASSERTION, FORWARD_USER_TOKEN})
SESSION_HEADER = "x-sap-security-session"
# ``"AllowPlainHttp": "true"``: the one way an ``http://`` URL is accepted.
PLAIN_HTTP = "AllowPlainHttp"
# ``"TLSServerName": "<name>"``: the name the server's certificate is checked
# against, for a system whose certificate names another host than its URL.
TLS_SERVER_NAME = "TLSServerName"
_HOST_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
MAX_FILE_BYTES = 1_000_000
MAX_DESTINATIONS = 200
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_CLAIM = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")


@dataclass(frozen=True)
class LocalDestinationConfig(DestinationServiceConfig):
    """Marks "the destination service is a local file" for every caller
    that holds a ``DestinationServiceConfig``; ``path`` is that file."""

    path: str = ""


def config_from_environment(environ: Mapping[str, str]) -> LocalDestinationConfig | None:
    path = str(environ.get(FILE_ENV) or "").strip()
    if not path:
        return None
    return LocalDestinationConfig(
        client_id="local", client_secret="", token_url="", api_url="", path=path
    )


def is_local(config: object) -> bool:
    return isinstance(config, LocalDestinationConfig)


# --- the file ----------------------------------------------------------------
_lock = threading.Lock()
_loaded: dict[str, tuple[float, dict[str, Any]]] = {}
_issuers: dict[tuple[str, str, float, float], Any] = {}


def _load(path: str) -> dict[str, Any]:
    """The parsed file, re-read when it changed on disk."""
    try:
        stat = os.stat(path)
    except OSError:
        raise DestinationError(
            f"the local destinations file ({FILE_ENV}) cannot be read"
        ) from None
    with _lock:
        hit = _loaded.get(path)
        if hit is not None and hit[0] == stat.st_mtime:
            return hit[1]
    if stat.st_size > MAX_FILE_BYTES:
        raise DestinationError("the local destinations file is too large")
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DestinationError("the local destinations file is not valid JSON") from None
    entries = document.get("destinations") if isinstance(document, dict) else None
    if not isinstance(entries, list) or len(entries) > MAX_DESTINATIONS:
        raise DestinationError(
            "the local destinations file needs a 'destinations' list "
            f"(at most {MAX_DESTINATIONS})"
        )
    by_name: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(entries):
        name = entry.get("Name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise DestinationError(
                f"local destinations file: destinations[{position}] has no usable Name"
            )
        if name in by_name:
            raise DestinationError(
                f"local destinations file: destinations[{position}] repeats a Name"
            )
        by_name[name] = entry
    saml = document.get("saml")
    parsed = {"destinations": by_name, "saml": saml if isinstance(saml, dict) else {}}
    with _lock:
        _loaded[path] = (stat.st_mtime, parsed)
    return parsed


def _expand(name: str, key: str, value: Any) -> str:
    """``value`` as text, with ``${VAR}`` replaced from the environment."""
    text = "" if value is None else str(value)

    def replace(match: re.Match[str]) -> str:
        found = os.environ.get(match.group(1))
        if found is None:
            raise DestinationError(
                f"local destination {name!r}: property {key!r} names the "
                f"environment variable {match.group(1)}, which is not set"
            )
        return found

    return _VARIABLE.sub(replace, text)


def _properties(name: str, entry: Mapping[str, Any]) -> dict[str, str]:
    props = {str(key): _expand(name, str(key), value) for key, value in entry.items()}
    props.setdefault("Type", "HTTP")
    props.setdefault("Authentication", "NoAuthentication")
    props.setdefault("ProxyType", "Internet")
    if props["ProxyType"] != "Internet":
        raise DestinationError(
            f"local destination {name!r}: ProxyType must be Internet; a local "
            f"deployment reaches the target directly, not through a Cloud Connector"
        )
    scheme = props.get("URL", "").split("://", 1)[0].lower()
    if props["Type"] == "HTTP" and scheme != "https":
        if scheme != "http" or props.get(PLAIN_HTTP) != "true":
            raise DestinationError(
                f"local destination {name!r}: the URL must be https:// (http:// only "
                f"with {PLAIN_HTTP} \"true\", for a service on the same private network)"
            )
        if props["Authentication"] == SAML_ASSERTION:
            raise DestinationError(
                f"local destination {name!r}: a {SAML_ASSERTION} destination must be https://"
            )
        logger.warning(
            "local destination %r is plain http: its credential is not protected "
            "on the way (acceptable inside a container network only)", name,
        )
    server_name = props.get(TLS_SERVER_NAME, "")
    if server_name and (scheme != "https" or not _HOST_NAME.fullmatch(server_name)):
        raise DestinationError(
            f"local destination {name!r}: {TLS_SERVER_NAME} is a host name, on an https:// URL"
        )
    if props["Authentication"] not in LOCAL_AUTH_TYPES:
        raise DestinationError(
            f"local destination {name!r}: Authentication must be one of "
            f"{', '.join(sorted(LOCAL_AUTH_TYPES))}"
        )
    return props


def _issuer(path: str, saml: Mapping[str, Any]) -> Any:
    """The signing identity of the file's ``saml`` block, re-read when the
    key or the certificate changed on disk."""
    from agents.saml_bearer import SamlBearerError, SamlIssuer

    def located(key: str) -> str:
        value = _expand("saml", key, saml.get(key)).strip()
        if not value:
            raise DestinationError(
                f"local destinations file: saml.{key} is required for a "
                f"{SAML_ASSERTION} destination"
            )
        return value if os.path.isabs(value) else str(Path(path).parent / value)

    key_file, cert_file = located("key_file"), located("cert_file")
    entity = _expand("saml", "issuer", saml.get("issuer")).strip()
    try:
        stamp = (key_file, cert_file, os.stat(key_file).st_mtime, os.stat(cert_file).st_mtime)
        with _lock:
            hit = _issuers.get(stamp)
        if hit is not None and hit.issuer == entity:
            return hit
        issuer = SamlIssuer.from_pem(
            entity, Path(key_file).read_bytes(), Path(cert_file).read_bytes()
        )
    except OSError:
        raise DestinationError(
            "local destinations file: the SAML key or certificate file cannot be read"
        ) from None
    except SamlBearerError as exc:
        raise DestinationError(f"local destinations file: saml.{exc}") from None
    with _lock:
        _issuers.clear()
        _issuers[stamp] = issuer
    return issuer


# --- the signed-in user ------------------------------------------------------
async def _claims(name: str, user_token: str) -> dict[str, Any]:
    """The validated claims of ``user_token``; never an unverified decode."""
    from fastapi import HTTPException

    from agents.auth import get_validator

    validator = get_validator()
    if validator is None:
        raise DestinationError(
            f"local destination {name!r} acts as the signed-in user, which needs "
            f"token validation (OIDC_ISSUER): without it nobody's identity is proven"
        )
    try:
        claims = await asyncio.to_thread(validator.validate, user_token)
    except HTTPException:
        raise DestinationError(
            f"local destination {name!r}: the signed-in user's token is not valid"
        ) from None
    return claims


def _name_id(name: str, props: Mapping[str, str], claims: Mapping[str, Any]) -> str:
    claim = (props.get("userIdSource") or "email").strip()
    if not _CLAIM.fullmatch(claim):
        raise DestinationError(f"local destination {name!r}: userIdSource is not a claim name")
    value = claims.get(claim)
    if not isinstance(value, str) or not value.strip():
        raise DestinationError(
            f"local destination {name!r}: the user's token has no usable "
            f"'{claim}' claim to name the SAP user by"
        )
    if claim == "email" and claims.get("email_verified") is False:
        raise DestinationError(
            f"local destination {name!r}: the identity provider marks the "
            f"user's e-mail address as not verified"
        )
    return value.strip()


def _token(kind: str, value: str, expires_in: int | None = None) -> dict[str, Any]:
    token: dict[str, Any] = {
        "type": kind,
        "value": value,
        "http_header": {"key": "Authorization", "value": f"{kind} {value}"},
    }
    if expires_in is not None:
        token["expires_in"] = str(expires_in)
    return token


async def find(
    config: LocalDestinationConfig, name: str, *, user_token: str | None = None
) -> dict[str, Any]:
    """The "find destination" answer for ``name``."""
    document = await asyncio.to_thread(_load, config.path)
    entry = document["destinations"].get(name)
    if entry is None:
        raise DestinationError(
            f"destination {name!r} does not exist in the local destinations file"
        )
    props = _properties(name, entry)
    kind = props["Authentication"]
    tokens: list[dict[str, Any]] = []
    if kind == "BasicAuthentication":
        user, password = props.get("User", ""), props.get("Password", "")
        if not user or not password:
            raise DestinationError(
                f"local destination {name!r}: BasicAuthentication needs User and Password"
            )
        pair = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        tokens = [_token("Basic", pair)]
    elif kind in USER_AUTH_TYPES:
        if not user_token:
            raise DestinationError(
                f"local destination {name!r} acts as the signed-in user "
                f"({kind}); resolve it with user context"
            )
        claims = await _claims(name, user_token)
        if kind == FORWARD_USER_TOKEN:
            try:
                left = int(float(claims.get("exp"))) - int(time.time())
            except (TypeError, ValueError):
                left = 0
            tokens = [_token("Bearer", user_token, max(left, 0))]
        else:
            tokens = [await _saml_token(config.path, document["saml"], name, props, claims)]
            session = _session_key(props)
            if session is None:
                props[f"URL.headers.{SESSION_HEADER}"] = "create"
            elif props[session].strip().lower() == "none":
                del props[session]
    return {"destinationConfiguration": props, "authTokens": tokens}


def _session_key(props: Mapping[str, str]) -> str | None:
    wanted = f"url.headers.{SESSION_HEADER}"
    return next((key for key in props if key.lower() == wanted), None)


async def _saml_token(
    path: str,
    saml: Mapping[str, Any],
    name: str,
    props: Mapping[str, str],
    claims: Mapping[str, Any],
) -> dict[str, Any]:
    from agents.auth import _principal_claim
    from agents.saml_bearer import (
        AUTHN_CONTEXT_PREVIOUS_SESSION,
        SamlBearerError,
        build_assertion,
        clamp_ttl,
    )

    audience = (props.get("audience") or "").strip()
    if not audience:
        raise DestinationError(
            f"local destination {name!r}: {SAML_ASSERTION} needs 'audience', the "
            f"ABAP system's SAML2 local provider name"
        )
    name_id = _name_id(name, props, claims)
    issuer = await asyncio.to_thread(_issuer, path, saml)
    try:
        assertion = build_assertion(
            issuer,
            name_id=name_id,
            audience=audience,
            recipient=(props.get("recipient") or props.get("URL") or "").strip(),
            name_id_format=props.get("nameIdFormat") or "email",
            authn_context=(props.get("authnContextClassRef") or "").strip()
            or AUTHN_CONTEXT_PREVIOUS_SESSION,
            ttl_seconds=clamp_ttl(saml.get("ttl_seconds")),
        )
    except SamlBearerError as exc:
        raise DestinationError(f"local destination {name!r}: {exc}") from None
    logger.info(
        "local destination %r: SAML bearer assertion issued for principal %s",
        name, _principal_claim(dict(claims)) or "?",
    )
    # expires_in 0: the resolver keeps it for about a second, so every call
    # gets an assertion of its own (an ABAP system may refuse a replay).
    return _token("SAML2.0", assertion.token, 0)


def listing(config: LocalDestinationConfig) -> list[dict[str, Any]]:
    """Name, description, type, proxy type and authentication of every
    destination in the file: what the admin's destination picker reads."""
    document = _load(config.path)
    out = []
    for name, entry in document["destinations"].items():
        kind = str(entry.get("Authentication") or "NoAuthentication")
        out.append({
            "Name": name,
            "Description": str(entry.get("Description") or ""),
            "Type": str(entry.get("Type") or "HTTP"),
            "ProxyType": "Internet",
            # The picker knows BTP's types: a forwarded user token is, for
            # it, a user-propagating bearer type.
            "Authentication": "OAuth2JWTBearer" if kind == FORWARD_USER_TOKEN else kind,
        })
    return out
