"""OAuth2 authorization-code callback for per-user MCP auth (auth_mode="oauth2").

The target authorization server redirects the user's browser here after they
sign in. We exchange the code for tokens (PKCE) and persist them per user, then
show a small page telling the user to return to the chat.

This route is served from the app root (mounted in ``app.py`` before the chat
catch-all). On Cloud Foundry it is reached through the approuter, so the
``JWTBindingMiddleware`` has already bound the calling user's identity.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from agents.auth import current_base_url, current_principal
from agents.oauth2 import begin_authorization_for_agent, complete_authorization

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])


def _page(title: str, body: str, *, ok: bool) -> HTMLResponse:
    color = "#2e7d32" if ok else "#c62828"
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background:#f4f5f7; color:#1d2129; display:flex; min-height:100vh; margin:0;
         align-items:center; justify-content:center; }}
  .card {{ background:#fff; border:1px solid #d9dce1; border-radius:8px; padding:32px 36px;
          max-width:440px; text-align:center; box-shadow:0 1px 4px rgba(0,0,0,.06); }}
  h1 {{ font-size:18px; margin:0 0 10px; color:{color}; }}
  p {{ color:#4b5563; line-height:1.5; margin:0 0 18px; }}
  a {{ display:inline-block; background:#0070f3; color:#fff; text-decoration:none;
      padding:9px 18px; border-radius:5px; font-weight:500; font-size:14px; }}
</style></head>
<body><div class="card"><h1>{title}</h1><p>{body}</p>
<a href="/">Return to chat</a></div></body></html>"""
    return HTMLResponse(html, status_code=200 if ok else 400)


@router.get("/oauth/login")
async def oauth_login(request: Request, agent: str):
    """Start the OAuth2 authorization flow for an agent and redirect the user
    to the target's sign-in page. Linked from the chat when a specialist needs
    authorization, so the long authorize URL never has to survive the chat."""
    user_id = current_principal.get()
    base_url = current_base_url.get()
    if not user_id or not base_url:
        return _page(
            "Cannot sign in",
            "Open the app through its approuter URL and try again.",
            ok=False,
        )
    try:
        url = await begin_authorization_for_agent(agent, user_id=user_id, base_url=base_url)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to start authorization for %s", agent)
        url = None
    if not url:
        return _page(
            "Cannot sign in",
            f"No OAuth2 sign-in is configured for '{agent}'.",
            ok=False,
        )
    return RedirectResponse(url, status_code=302)


@router.get("/oauth/callback")
async def oauth_callback(request: Request) -> HTMLResponse:
    params = request.query_params
    error = params.get("error")
    if error:
        desc = params.get("error_description") or error
        return _page("Authorization failed", desc, ok=False)

    code = params.get("code")
    state = params.get("state")
    if not code or not state:
        return _page(
            "Authorization failed",
            "The authorization response was missing required parameters.",
            ok=False,
        )

    try:
        await complete_authorization(
            code=code, state=state, principal=current_principal.get()
        )
    except ValueError as e:
        return _page("Authorization failed", str(e), ok=False)
    except Exception:  # noqa: BLE001
        logger.exception("OAuth callback failed")
        return _page(
            "Authorization failed",
            "An unexpected error occurred while completing sign-in. Please retry.",
            ok=False,
        )

    return _page(
        "You're signed in",
        "Authorization complete. Return to the chat and send your request again.",
        ok=True,
    )
