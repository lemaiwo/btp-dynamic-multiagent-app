"""How far back a listing may reach.

Shared by the mail and Jira toolsets. Neither owns it: a time window is not a
mail concept, and importing it from a mail module was only ever an accident of
which feature needed it first.
"""

from __future__ import annotations

import re
from typing import Any

# Suffixes accepted by `lookback`. Minutes is the internal unit: it divides
# every other unit exactly, so no window is unrepresentable.
_LOOKBACK_UNITS = {"m": 1, "h": 60, "d": 60 * 24, "w": 60 * 24 * 7}

# A plain decimal number, optionally followed by one unit letter. Deliberately
# narrower than float(): that also accepts "inf" (OverflowError on rounding),
# "nan" (a bare ValueError with no hint) and "1e3" (1000 hours, which nobody
# meant), and every one of those is reachable from a model-supplied argument.
_LOOKBACK_RE = re.compile(r"^(\d+(?:\.\d+)?|\.\d+)\s*([mhdw]?)$")

# Anything past this is a configuration mistake, not a window; it also keeps
# the minute count inside what a database integer column and a date filter
# can hold.
MAX_LOOKBACK_MINUTES = 100 * 366 * 24 * 60  # about a century


def parse_lookback(value: Any) -> int | None:
    """A lookback window in minutes, or None when unset.

    Accepts ``"90m"``, ``"5h"``, ``"2d"``, ``"1w"``, and a bare number, which
    means **hours** -- the unit people reach for when saying how far back to
    look. A unit suffix is the unambiguous form and the one the docs use.

    Raises on anything it cannot parse rather than defaulting. A typo'd window
    that silently became "no filter" would quietly hand the agent a whole
    backlog, which is the precise failure this setting exists to prevent.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None

    match = _LOOKBACK_RE.match(text)
    if match is None:
        raise ValueError(
            f"invalid lookback {value!r}; use a number of hours or a value with "
            f"a unit such as '90m', '5h', '2d', '1w'"
        )
    number, suffix = match.groups()
    unit = _LOOKBACK_UNITS[suffix or "h"]  # bare number means hours

    amount = float(number)
    if amount <= 0:
        raise ValueError(f"lookback must be positive, got {value!r}")
    # Before rounding: a long enough digit string is already float('inf').
    if amount * unit > MAX_LOOKBACK_MINUTES:
        raise ValueError(
            f"lookback {value!r} is too large; the longest accepted window is "
            "100 years"
        )
    return max(int(round(amount * unit)), 1)
