"""``/admin/api/model-providers``: the direct model providers' settings
(``agents/model_providers.py``). Every route needs the admin scope; a body is
read raw and validated in the handler, so a refusal never echoes input (an
API key least of all)."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from agents import model_providers
from agents.auth import current_principal, require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/model-providers")
BODY_BYTES = 8_192


def _view() -> dict[str, Any]:
    from agents.shared import available_models, default_model_name

    answer = model_providers.public()
    answer["available_models"] = available_models()
    answer["default_model"] = default_model_name()
    return answer


@router.get("", dependencies=[Depends(require_admin)])
async def list_providers() -> dict[str, Any]:
    return _view()


@router.put("/{provider}", dependencies=[Depends(require_admin)])
async def save_provider(provider: str, request: Request) -> dict[str, Any]:
    if provider not in model_providers.PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    raw = await request.body()
    if len(raw) > BODY_BYTES:
        raise HTTPException(status_code=413, detail="The body is too large")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(status_code=422, detail="body: a JSON object") from None
    try:
        await model_providers.save(provider, data, current_principal.get())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    # The models on offer changed: the registry (its orchestrator and every
    # specialist hold a model) and the chats' model lists must follow.
    reloaded, reload_failed = False, False
    try:
        from agents.chat_app import dynamic_chat_app
        from agents.registry import registry
        from agents.webchat import run as webchat_run

        webchat_run.catalog._names = None  # noqa: SLF001 - drop its 60 s cache
        await registry.reload()
        dynamic_chat_app.refresh()
        reloaded = True
    except Exception as exc:  # noqa: BLE001 - the save itself is committed
        reload_failed = True
        logger.warning("reload after a model provider save failed (%s)", type(exc).__name__)
    answer = _view()
    answer.update({"reloaded": reloaded, "reload_failed": reload_failed})
    return answer


@router.post("/{provider}/test", dependencies=[Depends(require_admin)])
async def test_provider(provider: str) -> dict[str, Any]:
    if provider not in model_providers.PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    return await model_providers.test(provider)
