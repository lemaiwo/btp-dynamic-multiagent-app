"""How large a tool result is as the model receives it.

A neutral module (standard library, plus pydantic-ai imported inside the
measure) so that every in-process toolset that refuses an oversized answer
measures it the same way, without importing another toolset and what that
one pulls in (``agents/sharepoint_tools.py`` brings openpyxl, httpx and an
httpx log filter).
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["result_chars"]


def result_chars(result: dict[str, Any]) -> int:
    """The size of a tool result as the model receives it, in characters.

    Measured the way pydantic-ai turns a tool return into the text of the
    tool message (``ToolReturnPart.model_response_str``: its
    ``tool_return_ta`` dumped as JSON, which is compact and leaves non-ASCII
    characters as they are). Should that ever come out shorter than plain
    ``json.dumps`` without ASCII escapes, the larger of the two counts: the
    cap must not be passed because of how it was measured.
    """
    plain = len(json.dumps(result, ensure_ascii=False, default=str,
                           separators=(",", ":")))
    try:
        # Imported here, not at the top: the admin routes and the built-in
        # registry import the toolsets that use this, and a renamed name in
        # pydantic-ai must cost this one measure, not the start of the app.
        from pydantic_ai.messages import tool_return_ta
    except ImportError:
        return plain
    return max(len(tool_return_ta.dump_json(result).decode()), plain)
