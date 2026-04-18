"""Dynamic ASGI wrapper around the pydantic-ai chat web app.

On each reload of the agent registry, the underlying `Agent.to_web()` app
is rebuilt and swapped in. Incoming requests are dispatched to the current
app, so administrators can add/edit/remove agents and press 'Reload' to
see the changes take effect without restarting the process.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agents.registry import registry

logger = logging.getLogger(__name__)

CHAT_HTML = Path(__file__).resolve().parent.parent / "templates" / "chat.html"


class DynamicChatApp:
    """ASGI app that forwards to the current orchestrator's web app."""

    def __init__(self) -> None:
        self._app = None

    def refresh(self) -> None:
        """Rebuild the inner web app from the current orchestrator."""
        html_source = CHAT_HTML if CHAT_HTML.is_file() else None
        self._app = registry.orchestrator.to_web(html_source=html_source)

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self.refresh()
        assert self._app is not None
        await self._app(scope, receive, send)


dynamic_chat_app = DynamicChatApp()
