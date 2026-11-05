"""Value helpers for comparing a migrated CSV cell with its source value.

Pure functions so the compare engine and its tests share one definition of
"the same": blank vs. space, leading zeros (SAP's ALPHA conversion) and
numbers written differently (``1.000`` vs. ``1``). Digits are ASCII only:
``str.isdigit`` also accepts other scripts' digits, which SAP would not pad.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_DIGITS = re.compile(r"[0-9]+")  # used with fullmatch: `$` would accept "12\n"
_MAX_NUMBER_CHARS = 64  # longer is not a number SAP stores; skip the Decimal work


def clean(v: object, *, trim: bool = True) -> str:
    """``None`` becomes ``""``; with ``trim`` surrounding whitespace goes, so ``' '`` is blank."""
    if v is None:
        return ""
    text = v if isinstance(v, str) else str(v)
    return text.strip() if trim else text


def alpha(v: str) -> str:
    """Drop leading zeros of an all-digit value (``"0"`` stays ``"0"``); others unchanged."""
    if _DIGITS.fullmatch(v):
        return v.lstrip("0") or "0"
    return v


_alpha = alpha  # `same` takes a keyword named `alpha`


def as_decimal(v: object) -> Decimal | None:
    """The value as a finite decimal number, or ``None`` when it is not one."""
    text = clean(v)
    if not text or len(text) > _MAX_NUMBER_CHARS:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def same(
    expected: object,
    actual: object,
    *,
    trim: bool = True,
    alpha: bool = False,
    numeric: bool = False,
) -> bool:
    """Whether two values count as equal under a rule's options.

    With ``numeric``, two values that are both numbers compare by value; if
    either is not a number they compare as text, so ``"abc"`` never equals ``"1"``.
    """
    left, right = clean(expected, trim=trim), clean(actual, trim=trim)
    if alpha:
        left, right = _alpha(left), _alpha(right)
    if numeric:
        a, b = as_decimal(left), as_decimal(right)
        if a is not None and b is not None:
            return a == b
    return left == right



def pad_key(raw: str, length: int) -> str:
    """Zero-pad an all-digit key shorter than ``length`` (as SAP stores it); others unchanged."""
    if _DIGITS.fullmatch(raw) and len(raw) < length:
        return raw.zfill(length)
    return raw
