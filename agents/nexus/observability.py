"""The AI Core observability headers of one model call.

With these two headers SAP AI Core keeps a metadata record of an inference
request (no prompt, no answer) labelled with our values, so the record can be
joined to the agent register's ``logRequest`` / ``logResponse`` by the same
``correlationId``.

A label value is used only when it is already in the safe form (1 to 64 ASCII
letters, digits, ``.``, ``/`` or ``-``); anything else is left out, never
rewritten: a rewritten value would name something else. Standard library only.
"""
from __future__ import annotations

import re
from typing import Any

PERSISTENCE_MODE_HEADER = "ai-inference-observability-persistence-mode"
LABELS_HEADER = "ai-inference-observability-labels"
PERSISTENCE_MODE = "metadata"
LABEL_PREFIX = "ext.ai.sap.com/"

_LABEL_VALUE = re.compile(r"[A-Za-z0-9./-]{1,64}")


def label_value(text: Any) -> str | None:
    """``text`` when it is a valid label value as it stands, else ``None``."""
    if isinstance(text, str) and _LABEL_VALUE.fullmatch(text):
        return text
    return None


def headers_for(correlation_id: str, use_case: str | None,
                landscape: str | None) -> dict[str, str]:
    """The two headers; ``{}`` when the correlation id is no valid label value."""
    correlation = label_value(correlation_id)
    if correlation is None:
        return {}
    labels = [f"{LABEL_PREFIX}correlation={correlation}"]
    for name, value in (("landscape", landscape), ("use-case", use_case)):
        checked = label_value(value)
        if checked is not None:
            labels.append(f"{LABEL_PREFIX}{name}={checked}")
    return {PERSISTENCE_MODE_HEADER: PERSISTENCE_MODE, LABELS_HEADER: ",".join(labels)}


__all__ = ["label_value", "headers_for", "PERSISTENCE_MODE_HEADER", "LABELS_HEADER"]
