"""Cloud Foundry API helper — restart the current app.

Used by the admin 'Restart' button. Authentication strategy:

1. If `VCAP_APPLICATION` is not present, this is a local dev run — no-op.
2. Otherwise, look for a bound user-provided service named `cf-api` with
   credentials `{username, password}`, or the env vars `CF_USERNAME` /
   `CF_PASSWORD`. Obtain a UAA token via password grant with the public
   `cf` client, then call `POST /v3/apps/{guid}/actions/restart`.

Note: an in-memory registry reload is usually sufficient to pick up new
agents. A full CF restart is useful when you also want to reset httpx
connections, refresh environment variables, or reload Python modules.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _cf_credentials() -> tuple[str, str] | None:
    vcap = os.environ.get("VCAP_SERVICES")
    if vcap:
        try:
            services = json.loads(vcap)
            for group in ("user-provided", "user_provided"):
                for svc in services.get(group, []):
                    if svc.get("name") == "cf-api":
                        c = svc.get("credentials", {})
                        if c.get("username") and c.get("password"):
                            return c["username"], c["password"]
        except Exception:
            logger.exception("Failed to parse VCAP_SERVICES for cf-api")

    user = os.environ.get("CF_USERNAME")
    pw = os.environ.get("CF_PASSWORD")
    if user and pw:
        return user, pw
    return None


def _app_metadata() -> tuple[str, str] | None:
    vcap_app = os.environ.get("VCAP_APPLICATION")
    if not vcap_app:
        return None
    try:
        data = json.loads(vcap_app)
    except Exception:
        return None
    cf_api = data.get("cf_api")
    app_id = data.get("application_id")
    if cf_api and app_id:
        return cf_api.rstrip("/"), app_id
    return None


async def restart_self() -> dict[str, Any]:
    """Restart the current CF app via the CF v3 API. Returns a status dict."""
    meta = _app_metadata()
    if meta is None:
        return {"ok": False, "reason": "not-running-on-cf"}
    cf_api, app_id = meta

    creds = _cf_credentials()
    if creds is None:
        return {
            "ok": False,
            "reason": "no-cf-credentials",
            "hint": "Bind a user-provided service 'cf-api' or set CF_USERNAME/CF_PASSWORD",
        }

    username, password = creds

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            info = (await client.get(f"{cf_api}/v2/info")).json()
            auth_endpoint = info["authorization_endpoint"].rstrip("/")

            token_resp = await client.post(
                f"{auth_endpoint}/oauth/token",
                data={
                    "grant_type": "password",
                    "username": username,
                    "password": password,
                },
                headers={"Authorization": "Basic Y2Y6"},  # client 'cf', no secret
            )
            token_resp.raise_for_status()
            token = token_resp.json()["access_token"]

            restart_resp = await client.post(
                f"{cf_api}/v3/apps/{app_id}/actions/restart",
                headers={"Authorization": f"Bearer {token}"},
            )
            if restart_resp.status_code >= 400:
                return {
                    "ok": False,
                    "reason": "cf-api-error",
                    "status": restart_resp.status_code,
                    "body": restart_resp.text[:500],
                }
            return {"ok": True, "app_id": app_id}
        except Exception as e:
            logger.exception("CF restart failed")
            return {"ok": False, "reason": "exception", "error": str(e)}
