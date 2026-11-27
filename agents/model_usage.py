"""Token usage of a run tree, split per model (design: cost in euros, part 1).

A run site binds a ``UsageCollector`` with ``collecting()``; the model
wrapper (``agents.nexus.logged_model``) calls ``add`` for every ANSWERED
request (``request`` returned, a stream read to its end: what pydantic-ai's
``RunUsage`` counts), so the per-model sum equals the run's own totals.
Delegated peers and deep sub-agents run in the same context, so the collector
sees the whole tree. Nothing bound: nothing kept.

When ``agents.shared.get_model`` falls back to the unwrapped model (the
wrapper could not be imported or built, one WARNING), nothing is collected
for that model.

Standard library only (imports nothing from ``agents.*``); never raises into
a model call.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

logger = logging.getLogger(__name__)

MODEL_NAME_MAX = 128
_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens")


class UsageCollector:
    """Sums of the answered requests of one run tree, per model name."""

    def __init__(self) -> None:
        self._by_model: dict[str, list[int]] = {}

    def add(self, model: str, usage: Any) -> None:
        """Add one answered request; raises on a usage that cannot be read."""
        counts = [int(getattr(usage, k)) for k in _KEYS]
        if any(c < 0 for c in counts):
            raise ValueError("negative count")
        slot = self._by_model.setdefault(model[:MODEL_NAME_MAX], [0, 0, 0, 0])
        for i, c in enumerate(counts):
            slot[i] += c
        slot[3] += 1

    def rows(self) -> list[dict[str, int | str]]:
        """One row per model, sorted by model name."""
        return [{"model": m, "input_tokens": s[0], "output_tokens": s[1],
                 "cache_read_tokens": s[2], "requests": s[3]}
                for m, s in sorted(self._by_model.items())]


_current: ContextVar[UsageCollector | None] = ContextVar("model_usage_collector", default=None)


def current() -> UsageCollector | None:
    """The collector bound in this context, if any."""
    return _current.get()


@contextmanager
def collecting(collector: UsageCollector | None = None) -> Iterator[UsageCollector]:
    """Bind ``collector`` (a new one by default) for the block; reset in the same task."""
    collector = collector if collector is not None else UsageCollector()
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


def add(model: str | None, usage: Any) -> None:
    """Record one answered request of ``model``; a no-op without a collector, never raises."""
    collector = _current.get()
    if collector is None or not isinstance(model, str) or not model.strip():
        return
    try:
        collector.add(model.strip(), usage)
    except Exception as exc:  # noqa: BLE001 - never break a model call
        logger.warning("model usage not collected (%s)", type(exc).__name__)
