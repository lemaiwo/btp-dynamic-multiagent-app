"""The cost maths of ``agents/model_prices.py``: amounts as integers of 10^-9
on the database, decimal strings on the API, ``Decimal`` arithmetic."""

from __future__ import annotations

import pytest

from agents import model_prices as mp


def test_rates_round_trip_as_decimal_strings():
    assert mp.parse_amount("0.000123456", "x") == 123_456
    assert mp.parse_amount(2, "x") == 2_000_000_000
    assert mp.parse_amount(0.5, "x") == 500_000_000
    assert mp.parse_amount("1000000", "x") == 1_000_000 * mp.SCALE
    assert mp.parse_amount("0", "x") == 0
    assert mp.amount_text(123_456) == "0.000123456"
    assert mp.amount_text(2_000_000_000) == "2"
    assert mp.amount_text(500_000_000) == "0.5"
    assert mp.amount_text(0) == "0"
    assert mp.amount_text(1_000_000 * mp.SCALE) == "1000000"


@pytest.mark.parametrize("bad", [
    True, -1, "1e3", "0.0000000001", "abc", None, 1_000_001, float("nan"),
    float("inf"), "-1", " 1", "1.", ".5", "1000000.000000001", [], {}, "１",
    0.1234567891,
])
def test_a_bad_rate_is_refused_naming_the_field_only(bad):
    with pytest.raises(ValueError) as e:
        mp.parse_amount(bad, "prices[0].input_rate")
    assert str(e.value) == (
        "prices[0].input_rate: a number from 0 to 1000000 with at most 9 decimals")


BOOK = mp.PriceBook(cu_price=mp.parse_amount("7.5", "c"), rates={
    "a": (mp.parse_amount("0.5", "i"), mp.parse_amount("2", "o"), mp.parse_amount("0.05", "c")),
    "b": (mp.parse_amount("1", "i"), mp.parse_amount("4", "o"), None),
})


def _use(i: int, o: int, c: int, r: int = 1) -> dict[str, int]:
    return {"input_tokens": i, "output_tokens": o, "cache_read_tokens": c, "requests": r}


def test_cost_prices_cached_input_apart_and_sums_models():
    got = mp.cost(BOOK, {"a": _use(10_000, 1_000, 4_000, 2), "b": _use(1_000, 0, 0)})
    # a: (6000*0.5 + 4000*0.05 + 1000*2)/1000 = 5.2 CU; b: 1000*1/1000 = 1 CU; 6.2 CU * 7.5 EUR
    assert got == {"amount": 46.5, "currency": "EUR", "complete": True}


def test_a_null_cache_rate_prices_cached_input_as_input():
    # b: (500 * 1 + 500 * 1) / 1000 = 1 CU -> 7.5 EUR
    assert mp.cost(BOOK, {"b": _use(1_000, 0, 500)})["amount"] == 7.5


def test_more_cached_than_input_never_goes_negative():
    # a: (0 * 0.5 + 2000 * 0.05) / 1000 = 0.1 CU -> 0.75 EUR
    assert mp.cost(BOOK, {"a": _use(1_000, 0, 2_000)})["amount"] == 0.75


def test_a_missing_rate_makes_it_incomplete_and_no_cu_price_makes_it_null():
    part = mp.cost(BOOK, {"a": _use(1000, 0, 0), "zz": _use(5, 5, 0)})
    assert part["complete"] is False and part["amount"] == 3.75
    assert mp.cost(mp.PriceBook(cu_price=None, rates=BOOK.rates), {"a": _use(1, 0, 0)}) is None
    assert mp.cost(BOOK, {}) is None
    assert mp.cost(BOOK, {"a": _use(0, 0, 0, 0)}) is None


def test_the_amount_is_exact_to_six_decimals():
    book = mp.PriceBook(cu_price=mp.parse_amount("0.000000001", "c"),
                        rates={"m": (mp.parse_amount("0.000000001", "i"), 0, None)})
    # 1000 tokens * 1e-9 / 1000 = 1e-9 CU * 1e-9 EUR -> rounds to 0
    assert mp.cost(book, {"m": _use(1_000, 0, 0)})["amount"] == 0.0
    book = mp.PriceBook(cu_price=mp.parse_amount("0.1", "c"),
                        rates={"m": (mp.parse_amount("0.3", "i"), 0, None)})
    # 1000 * 0.3 / 1000 * 0.1 = 0.03 exactly (a float product would be 0.030000000000000002)
    assert mp.cost(book, {"m": _use(1_000, 0, 0)})["amount"] == 0.03
