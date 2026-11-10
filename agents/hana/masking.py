"""Literal masking of statement texts for ``builtin:hana``.

Monitoring views (``M_EXPENSIVE_STATEMENTS``, ``M_ACTIVE_STATEMENTS``,
``M_SQL_PLAN_CACHE``, ...) hold the SQL other sessions ran, including the
values they filtered on: customer numbers, names, amounts. Only the shape of
such a statement may reach the model, so every literal (string, national
string, number, binary) becomes ``?``, comments are dropped (they can hold
anything), the operand of ``PASSWORD`` is masked whatever its form (HANA
writes a user's password as an identifier), and identifiers and quoted
identifiers are kept: they name objects, not data.

It reads the text with the same tokenizer as the SELECT guard, in its lenient
mode, so it never raises: a text HANA cut in the middle of a literal is
masked to its end.

Standard library only.
"""

from __future__ import annotations

import unicodedata

from agents.hana.sqltokens import tokenize

DEFAULT_LIMIT = 300
_LITERALS = frozenset({"string", "number", "binary"})
_ELLIPSIS = "…"


def _clean(text: str) -> str:
    """Whitespace to one space; control, format and separator characters dropped."""
    kept: list[str] = []
    for char in text:
        if char.isspace():
            kept.append(" ")
            continue
        category = unicodedata.category(char)
        if category[0] == "C" or category in ("Zl", "Zp"):
            continue
        kept.append(char)
    return " ".join("".join(kept).split())


def mask_statement(text: str | None, limit: int = DEFAULT_LIMIT) -> str:
    """The statement with every literal as ``?``, at most ``limit`` characters."""
    if not isinstance(text, str) or not text:
        return ""
    pieces: list[str] = []
    position = 0
    mask_operand = False
    for token in tokenize(text, lenient=True):
        pieces.append(text[position : token.start])
        position = token.end
        if token.kind == "comment":
            pieces.append(" ")
            continue
        if token.kind in _LITERALS or (token.kind == "quoted" and not token.closed):
            pieces.append("?")
            mask_operand = False
            continue
        if mask_operand:
            if token.kind in ("word", "quoted"):
                pieces.append("?")
                mask_operand = False
                continue
            if not token.is_symbol("="):
                mask_operand = False
        if token.is_word("PASSWORD"):
            mask_operand = True
        pieces.append(token.text)
    pieces.append(text[position:])
    masked = _clean("".join(pieces))
    limit = max(int(limit), 1)
    if len(masked) > limit:
        masked = masked[: limit - 1] + _ELLIPSIS
    return masked
