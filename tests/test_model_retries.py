"""How often the OpenAI-family model client repeats a refused request.

A rate limit of the deployment (429, counted per minute) outlasts the two
quick retries of the openai SDK, so ``_build_openai_model`` sets
``max_retries`` from ``AICORE_MAX_RETRIES``. The client built here is the
real gen_ai_hub one, on a proxy client that is never asked for anything:
the value must arrive on the client that sends the requests.
"""

from __future__ import annotations

import logging

import pytest

import agents.shared as shared


class _ProxyClient:
    """Stands in for the AI Core proxy client; nothing is sent."""

    request_header: dict[str, str] = {}


@pytest.fixture
def build(monkeypatch):
    import gen_ai_hub.proxy

    monkeypatch.setattr(gen_ai_hub.proxy, "get_proxy_client", lambda *a, **k: _ProxyClient())
    monkeypatch.delenv("AICORE_MAX_RETRIES", raising=False)

    def _build() -> int:
        return shared._build_openai_model("gpt-4o").client.max_retries

    return _build


def test_default_is_eight(build):
    assert shared.AICORE_MAX_RETRIES_DEFAULT == 8
    assert build() == 8


def test_environment_value_is_used(build, monkeypatch):
    monkeypatch.setenv("AICORE_MAX_RETRIES", "3")
    assert build() == 3


def test_zero_means_no_retries(build, monkeypatch):
    monkeypatch.setenv("AICORE_MAX_RETRIES", "0")
    assert build() == 0


def test_blank_is_the_default(build, monkeypatch):
    monkeypatch.setenv("AICORE_MAX_RETRIES", "  ")
    assert build() == 8


@pytest.mark.parametrize("raw", ["many", "2.5", "-1"])
def test_unusable_value_falls_back_with_one_warning(build, monkeypatch, caplog, raw):
    monkeypatch.setenv("AICORE_MAX_RETRIES", raw)
    with caplog.at_level(logging.WARNING, logger="agents.shared"):
        assert build() == 8
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "AICORE_MAX_RETRIES" in warnings[0].getMessage()
    # The variable is named, its value is not.
    assert raw not in warnings[0].getMessage()
