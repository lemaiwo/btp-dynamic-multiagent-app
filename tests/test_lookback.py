"""The shared lookback window parser accepts plain windows and nothing else."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from agents.lookback import parse_lookback  # noqa: E402


@pytest.mark.parametrize("value, minutes", [
    ("90m", 90), ("5h", 300), ("2d", 2880), ("1w", 10080),
    ("1.5h", 90), (" 2 D ", 2880), ("3", 180), (3, 180), ("0.5", 30),
    (".5h", 30), ("0.001m", 1),
])
def test_accepts_plain_decimal_windows(value, minutes):
    assert parse_lookback(value) == minutes


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_is_none(value):
    assert parse_lookback(value) is None


@pytest.mark.parametrize("value", [
    "inf", "infinity", "nan", "1e3", "1E3", "-5", "+5", "banana", "5x",
    "0x10", "1_000", "5 hours", "1,5h",
])
def test_rejects_everything_that_is_not_a_plain_number(value):
    with pytest.raises(ValueError, match="invalid lookback"):
        parse_lookback(value)


@pytest.mark.parametrize("value", ["0", "0h", "0.0d"])
def test_rejects_zero(value):
    with pytest.raises(ValueError, match="must be positive"):
        parse_lookback(value)


def test_rejects_an_absurd_window():
    with pytest.raises(ValueError, match="too large"):
        parse_lookback("9" * 400)
    with pytest.raises(ValueError, match="too large"):
        parse_lookback("999999w")
