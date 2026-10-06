"""Final review B3: ONE rule for a decimal given as a JSON number.

Whoever parsed the JSON has already rounded a number with more than 15
significant digits, so it is not sent -- in a V2 or V4 body, and in a V2 or
V4 URL literal (a key, a function parameter). The rule lives in
``agents.odata.common.plain_float`` and all four places call it.
"""

from __future__ import annotations

import pytest

from agents.odata import common
from agents.odata.client import ODataError
from agents.odata.models import EntitySetDef
from agents.odata.v2 import V2Dialect
from agents.odata.v4 import V4Dialect

# 17 significant digits: what a JSON parser makes of a longer number.
ROUNDED = 1234567890123456.7
SET = EntitySetDef.model_validate(
    {
        "name": "A_Amount",
        "keys": [{"name": "Id"}],
        "operations": ["list", "get", "create", "update"],
        "fields": [
            {"name": "Id", "selectable": True},
            {"name": "Amount", "type": "Edm.Decimal", "selectable": True, "writable": True},
        ],
    }
)
DIALECTS = [V2Dialect(), V4Dialect()]
PLACES = ["body", "url"]


def send(dialect, place: str, value):
    if place == "body":
        return dialect.encode_body(SET, {"Amount": value})["Amount"]
    return dialect.literal("Edm.Decimal", value)


@pytest.mark.parametrize("dialect", DIALECTS, ids=["v2", "v4"])
@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("value", [ROUNDED, 0.12345678901234568, 123456789012345.67])
def test_a_decimal_number_that_may_be_rounded_is_not_sent(dialect, place, value):
    with pytest.raises(ODataError) as err:
        send(dialect, place, value)
    assert err.value.code == "invalid_argument"
    assert str(value) not in err.value.message  # no refusal repeats a value
    if place == "body":
        assert "pass it as text" in err.value.hint


@pytest.mark.parametrize("dialect", DIALECTS, ids=["v2", "v4"])
@pytest.mark.parametrize("place", PLACES)
def test_a_short_decimal_number_is_sent(dialect, place):
    assert send(dialect, place, 12.5) in ("12.5", 12.5, "12.5M")
    assert send(dialect, place, 123456789012345.0) is not None  # 15 digits; `.0` is none


def test_the_hint_of_a_url_parameter_says_pass_it_as_text():
    from agents.odata import v2, v4

    assert v2._PARAM_HINTS["Edm.Decimal"] == common.DECIMAL_TEXT_HINT
    assert v4._LITERAL_HINTS["Edm.Decimal"] == common.DECIMAL_TEXT_HINT
    assert "pass it as text" in common.DECIMAL_TEXT_HINT


@pytest.mark.parametrize("dialect", DIALECTS, ids=["v2", "v4"])
@pytest.mark.parametrize("place", PLACES)
def test_all_four_places_ask_the_one_rule(monkeypatch, dialect, place):
    """Cannot drift again: with the shared rule saying no, none sends."""
    monkeypatch.setattr(common, "plain_float", lambda value: None)
    with pytest.raises(ODataError):
        send(dialect, place, 12.5)


def test_the_same_digits_as_text_go_out_in_v2():
    text = "1234567890123456.7"
    assert send(V2Dialect(), "body", text) == text
    assert send(V2Dialect(), "url", text) == text + "M"
    assert send(V4Dialect(), "url", text) == text
