"""The admin's settings of the direct model providers (Anthropic, OpenAI).

Next to SAP AI Core, a model can be served by a provider's own API
(``agents/shared.py``: names ``anthropic:<model>`` / ``openai:<model>``).
Its key can be an environment variable; this module lets an admin set it
(and the base URL and the model list) from the admin UI instead, on any
landscape. The setting is a row of ``model_providers`` per provider and wins
over the environment.

The API key is a secret: it is stored as the other stored credentials are
(the database), it is never returned (``public`` answers ``has_key`` and
where the key in effect comes from), never exported and never logged; a
refusal names the field and the rule, never a value.

The model functions of ``agents/shared.py`` are synchronous, so the rows are
kept there in memory: ``refresh()`` loads them at startup and after a save.
Another app instance sees a save at its next start or reload.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents import shared
from agents.db import ModelProvider, SessionLocal

logger = logging.getLogger(__name__)

PROVIDERS = shared.DIRECT_PROVIDERS
MAX_MODELS = 30
MAX_KEY_CHARS = 500
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
# Body keys of a save; ``api_key`` and ``base_url`` are optional (absent keeps
# what is stored, ``null`` clears it).
ALLOWED_KEYS = frozenset({"enabled", "api_key", "base_url", "models"})
_KEEP = object()


def _clean_key(value: Any) -> Any:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or len(value) > MAX_KEY_CHARS
        or value != value.strip()
        or any(ord(c) < 0x21 or ord(c) > 0x7E for c in value)
    ):
        raise ValueError(
            "api_key: printable characters without blanks, at most "
            f"{MAX_KEY_CHARS}"
        )
    return value


def _clean_base_url(value: Any) -> Any:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("base_url: a URL of at most 500 characters")
    parts = urlparse(value)
    host = parts.hostname or ""
    # http only where nothing leaves the machine or its container network:
    # loopback, or a single-label host name (a compose service).
    plain_ok = parts.scheme == "http" and (host in _LOOPBACK or "." not in host)
    if (
        not host
        or parts.username
        or parts.password
        or parts.fragment
        or parts.query
        or not (parts.scheme == "https" or plain_ok)
    ):
        raise ValueError(
            "base_url: https://host[/path] without credentials, query or fragment"
        )
    return value.rstrip("/")


def _clean_models(provider: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_MODELS:
        raise ValueError(f"models: a list of at most {MAX_MODELS} model names")
    names: list[str] = []
    for position, item in enumerate(value):
        name = item.strip() if isinstance(item, str) else ""
        # A name typed with this provider's prefix is the same model.
        if name.lower().startswith(provider + ":"):
            name = name[len(provider) + 1:]
        if not _MODEL.fullmatch(name):
            raise ValueError(f"models[{position}]: not a model name")
        if name not in names:
            names.append(name)
    return names


def validate(provider: str, data: Any) -> dict[str, Any]:
    """The cleaned save of ``provider``; ``ValueError`` of ``<field>: <rule>``.
    ``api_key`` / ``base_url`` are ``_KEEP`` when the body does not carry them."""
    if provider not in PROVIDERS:
        raise ValueError("provider: unknown")
    if not isinstance(data, dict):
        raise ValueError("body: a JSON object")
    if set(data) - ALLOWED_KEYS:
        raise ValueError("body: an unknown key")
    if not isinstance(data.get("enabled"), bool):
        raise ValueError("enabled: must be a boolean")
    return {
        "enabled": data["enabled"],
        "api_key": _clean_key(data["api_key"]) if "api_key" in data else _KEEP,
        "base_url": _clean_base_url(data["base_url"]) if "base_url" in data else _KEEP,
        "models": _clean_models(provider, data.get("models")),
    }


def _models_of(row: ModelProvider) -> list[str]:
    try:
        value = json.loads(row.models_json or "[]")
    except ValueError:
        return []
    return [m for m in value if isinstance(m, str)] if isinstance(value, list) else []


async def load(session: AsyncSession) -> dict[str, dict[str, Any]]:
    rows = (await session.execute(select(ModelProvider))).scalars().all()
    return {
        row.provider: {
            "enabled": row.enabled != 0,
            "api_key": row.api_key or "",
            "base_url": row.base_url or "",
            "models": _models_of(row),
        }
        for row in rows
        if row.provider in PROVIDERS
    }


async def refresh() -> None:
    """Load the stored settings into ``agents.shared`` (startup, after a save)."""
    async with SessionLocal() as session:
        settings = await load(session)
    shared.set_direct_provider_settings(settings)


async def save(provider: str, data: Any, principal: str | None) -> None:
    """Validate and store ``provider``'s setting, then refresh the cache."""
    cleaned = validate(provider, data)
    async with SessionLocal() as session:
        row = await session.get(ModelProvider, provider)
        if row is None:
            row = ModelProvider(provider=provider)
            session.add(row)
        row.enabled = 1 if cleaned["enabled"] else 0
        if cleaned["api_key"] is not _KEEP:
            row.api_key = cleaned["api_key"]
        if cleaned["base_url"] is not _KEEP:
            row.base_url = cleaned["base_url"]
        row.models_json = json.dumps(cleaned["models"])
        row.updated_by = (principal or "")[:255] or None
        from datetime import datetime, timezone

        row.updated_at = datetime.now(timezone.utc)
        await session.commit()
    # It decides where prompts go and who is billed: one line, no value.
    logger.warning(
        "model provider %s saved by %s (enabled=%s, key %s)",
        provider, principal or "?", cleaned["enabled"],
        "kept" if cleaned["api_key"] is _KEEP else ("cleared" if cleaned["api_key"] is None else "set"),
    )
    await refresh()


def public() -> dict[str, Any]:
    """What the admin UI may know: never a key."""
    providers = []
    for provider in PROVIDERS:
        setting = shared._provider_setting(provider)  # noqa: SLF001 - this module owns it
        source = shared.direct_key_source(provider)
        prefix = provider + ":"
        providers.append({
            "provider": provider,
            "stored": bool(setting),
            "enabled": bool(setting.get("enabled", True)) if setting else source != "none",
            "has_key": source != "none",
            "key_source": source,
            "has_stored_key": bool(setting.get("api_key")),
            "base_url": setting.get("base_url") or "",
            "models": list(setting.get("models") or []),
            "effective_models": [m for m in shared.direct_models() if m.startswith(prefix)],
        })
    return {"providers": providers, "aicore_configured": shared.aicore_configured()}


async def test(provider: str) -> dict[str, Any]:
    """One free call (list models) with the key in effect: is it accepted?
    Answers ``{ok, status?, error?}``; never the provider's text or the key."""
    if provider not in PROVIDERS:
        raise ValueError("provider: unknown")
    key = shared.direct_api_key(provider)
    if not key:
        return {"ok": False, "error": "no_key"}
    base_url = shared.direct_base_url(provider)
    try:
        if provider == "anthropic":
            from anthropic import AsyncAnthropic

            client: Any = AsyncAnthropic(api_key=key, base_url=base_url, max_retries=0, timeout=20)
            await client.models.list(limit=1)
        else:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=20)
            await client.models.list()
    except Exception as exc:  # noqa: BLE001 - reported by class and status only
        status = getattr(exc, "status_code", None)
        logger.info("model provider %s test failed (%s)", provider, type(exc).__name__)
        return {
            "ok": False,
            "status": status if isinstance(status, int) else None,
            "error": type(exc).__name__,
        }
    return {"ok": True}
