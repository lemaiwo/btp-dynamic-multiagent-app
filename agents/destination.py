"""Reaching a service through a BTP destination.

The destination service holds the target's URL and its credential, and -- for
an ``OAuth2ClientCredentials`` destination -- performs the token exchange
itself, handing back a ready ``Authorization`` header. That is the whole
appeal: this application stores no credential for the target at all, and
rotating it is something the destination's owner does without touching us.

Contrast :mod:`agents.client_credentials`, which authenticates *as* the
application and therefore must hold a client secret in our own database. Here
the only thing configured is a name.

This module knows nothing about what sits behind the destination.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Mapping

logger = logging.getLogger(__name__)

# Refetch this many seconds before the token actually expires, so a request
# that takes a moment to reach the server does not arrive holding a token that
# expired in flight.
EXPIRY_SKEW_SECONDS = 60

# Used when the destination response omits expires_in. Short on purpose:
# re-resolving is one cheap call, and guessing long risks 401 loops.
DEFAULT_LIFETIME_SECONDS = 300

DESTINATION_PATH = "/destination-configuration/v1/destinations/"

MISSING_BINDING_MESSAGE = (
    "no destination service binding found. On Cloud Foundry, bind a "
    "'destination' service instance to the app. Locally, set "
    "DESTINATION_CLIENT_ID, DESTINATION_CLIENT_SECRET, DESTINATION_URI and "
    "either DESTINATION_TOKEN_URL or DESTINATION_UAA_URL -- all four come from "
    "'cf service-key <instance> <key>'"
)


class DestinationError(RuntimeError):
    """The destination could not be resolved.

    Carries the service's own message where there is one: a 404 naming the
    destination is far more useful than "request failed".
    """


@dataclass(frozen=True)
class DestinationServiceConfig:
    """Credentials for the destination service itself, not for the target."""

    client_id: str
    client_secret: str
    token_url: str
    api_url: str


def _token_url_from_uaa(uaa: str) -> str:
    """The token endpoint for an XSUAA base URL.

    Accepts a URL that already names the endpoint, because a service key's
    ``url`` does not but a hand-written .env entry often does.
    """
    base = str(uaa).strip().rstrip("/")
    if base.endswith("/oauth/token"):
        return base
    return f"{base}/oauth/token"


def _config_from_vcap(raw: str | None) -> DestinationServiceConfig | None:
    if not raw:
        return None
    try:
        services = json.loads(raw)
    except (TypeError, ValueError):
        # A malformed VCAP_SERVICES must not stop the env fallback: locally it
        # is sometimes set to something hand-edited and half-finished.
        logger.warning("Could not parse VCAP_SERVICES for destination", exc_info=True)
        return None
    for entry in services.get("destination") or []:
        creds = (entry or {}).get("credentials") or {}
        client_id = str(creds.get("clientid") or "").strip()
        secret = str(creds.get("clientsecret") or "").strip()
        uaa = str(creds.get("url") or "").strip()
        api = str(creds.get("uri") or "").strip().rstrip("/")
        if client_id and secret and uaa and api:
            return DestinationServiceConfig(
                client_id=client_id,
                client_secret=secret,
                token_url=_token_url_from_uaa(uaa),
                api_url=api,
            )
    return None


def config_from_environment(
    environ: Mapping[str, str],
) -> DestinationServiceConfig | None:
    """The destination service binding, or None when there is none.

    VCAP_SERVICES first, so a deployed app always uses its real binding even if
    stale DESTINATION_* variables are also present in the environment.
    """
    from_vcap = _config_from_vcap(environ.get("VCAP_SERVICES"))
    if from_vcap is not None:
        return from_vcap

    client_id = str(environ.get("DESTINATION_CLIENT_ID") or "").strip()
    secret = str(environ.get("DESTINATION_CLIENT_SECRET") or "").strip()
    api = str(environ.get("DESTINATION_URI") or "").strip().rstrip("/")
    token_url = str(environ.get("DESTINATION_TOKEN_URL") or "").strip()
    uaa = str(environ.get("DESTINATION_UAA_URL") or "").strip()
    if not token_url and uaa:
        token_url = _token_url_from_uaa(uaa)

    if client_id and secret and api and token_url:
        return DestinationServiceConfig(
            client_id=client_id,
            client_secret=secret,
            token_url=token_url,
            api_url=api,
        )
    return None
