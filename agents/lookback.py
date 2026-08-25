"""How far back a listing may reach.

Shared by the mail and Jira toolsets. Neither owns it: a time window is not a
mail concept, and importing it from a mail module was only ever an accident of
which feature needed it first.
"""

from __future__ import annotations

from typing import Any

# Suffixes accepted by `lookback`. Minutes is the internal unit: it divides
# every other unit exactly, so no window is unrepresentable.
_LOOKBACK_UNITS = {"m": 1, "h": 60, "d": 60 * 24, "w": 60 * 24 * 7}


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

    unit = _LOOKBACK_UNITS.get(text[-1])
    number = text[:-1].strip() if unit else text
    if unit is None:
        unit = _LOOKBACK_UNITS["h"]  # bare number means hours

    try:
        amount = float(number)
    except ValueError:
        raise ValueError(
            f"invalid lookback {value!r}; use a number of hours or a value with "
            f"a unit such as '90m', '5h', '2d', '1w'"
        ) from None
    if amount <= 0:
        raise ValueError(f"lookback must be positive, got {value!r}")

    minutes = int(round(amount * unit))
    return max(minutes, 1)
