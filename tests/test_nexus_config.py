"""Settings, the on/off switch and the 404 of the destination service.

The settings of the agent-register logging are read from the environment
with defaults; a bad value falls back to its default with a WARNING that
names the variable, never the value.
"""
import httpx
import pytest

from agents.destination import (
    DestinationError,
    DestinationNotFound,
    DestinationResolver,
    DestinationServiceConfig,
)
from agents.nexus import config, state


def test_defaults():
    s = config.settings({})
    assert (s.destination, s.user_destination) == ("NEXUS_AI_PLATFORM", "NEXUS_AI_PLATFORM_USER")
    assert s.fallback_use_case is None and s.repush_minutes == 15 and s.send_payload is False
    assert s.queue_max == 2000 and s.timeout_seconds == 5.0


@pytest.mark.parametrize(
    "raw,expected", [("true", True), ("TRUE", False), ("1", False), ("", False)]
)
def test_send_payload_only_exactly_true(raw, expected):
    assert config.settings({"NEXUS_SEND_PAYLOAD": raw}).send_payload is expected


def test_bad_number_is_default_with_a_warning_naming_the_variable_not_the_value(caplog):
    s = config.settings({"NEXUS_QUEUE_MAX": "lots", "NEXUS_REPUSH_MINUTES": "-3"})
    assert s.queue_max == 2000 and s.repush_minutes == 15
    assert "NEXUS_QUEUE_MAX" in caplog.text and "lots" not in caplog.text


def test_repush_zero_means_no_timer():
    assert config.settings({"NEXUS_REPUSH_MINUTES": "0"}).repush_minutes == 0


def test_blank_user_destination_means_none():
    assert config.settings({"NEXUS_USER_DESTINATION": "  "}).user_destination is None


def test_fallback_use_case_over_60_is_ignored(caplog):
    assert config.settings({"NEXUS_FALLBACK_USE_CASE": "X" * 61}).fallback_use_case is None


@pytest.mark.parametrize("raw", ["0", "-1", "31", "soon", "nan", "inf"])
def test_timeout_outside_range_is_default(raw, caplog):
    assert config.settings({"NEXUS_TIMEOUT_SECONDS": raw}).timeout_seconds == 5.0
    assert "NEXUS_TIMEOUT_SECONDS" in caplog.text


def test_resource_group_default_and_stripped():
    assert config.resource_group({}) == "default"
    assert config.resource_group({"AICORE_RESOURCE_GROUP": "  "}) == "default"
    assert config.resource_group({"AICORE_RESOURCE_GROUP": " rg-1 "}) == "rg-1"


def test_switch_starts_off():
    state.reset_for_tests()
    assert state.enabled() is False and state.reason() == "off (not started)"
    state.set_on("NEXUS_AI_PLATFORM")
    assert state.enabled() is True
    assert state.reason() == "on (destination NEXUS_AI_PLATFORM)"
    state.set_off("off (no destination service)")
    assert state.enabled() is False
    assert state.reason() == "off (no destination service)"
    state.reset_for_tests()


async def test_404_is_destination_not_found():
    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        return httpx.Response(
            404, json={"ErrorMessage": "Configuration with the specified name was not found"}
        )
    cfg = DestinationServiceConfig(client_id="c", client_secret="s",
                                   token_url="https://uaa.example.test/oauth/token",
                                   api_url="https://dest.example.test")
    r = DestinationResolver("NEXUS_AI_PLATFORM", cfg, transport=httpx.MockTransport(handler))
    with pytest.raises(DestinationNotFound) as info:
        await r.resolve_properties(force=True)
    assert isinstance(info.value, DestinationError)
