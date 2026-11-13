"""Literal masking of statement texts for ``builtin:hana``.

Monitoring views (``M_EXPENSIVE_STATEMENTS``, ``M_ACTIVE_STATEMENTS``,
``M_SQL_PLAN_CACHE``, ...) hold the SQL other sessions ran, including the
values they filtered on: customer numbers, names, amounts. Only the shape of
such a statement may reach the model, so every literal (string, national
string, number, binary) becomes ``?``, comments are dropped (they can hold
anything), the operand of ``PASSWORD`` is masked whatever its form (HANA
writes a user's password as an identifier), and identifiers and quoted
identifiers are kept: they name objects, not data. Two exceptions: inside a
JSON document (``{ ... }`` / ``[ ... ]`` of a document-store statement) a
``"..."`` is a value, so it is masked; and the ``PASSWORD`` operand is masked
up to the next whitespace, whatever tokens it is made of.

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


def _whitespace_after(text: str, start: int) -> int:
    """Index of the first whitespace character at or after ``start``, or the end."""
    for index in range(start, len(text)):
        if text[index].isspace():
            return index
    return len(text)


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
    tokens = tokenize(text, lenient=True)
    pieces: list[str] = []
    position = 0
    mask_operand = False
    document_depth = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        pieces.append(text[position : token.start])
        position = token.end
        index += 1
        if token.kind == "comment":
            pieces.append(" ")
            continue
        if mask_operand and not token.is_symbol("="):
            # The operand of PASSWORD is everything up to the next whitespace
            # (a token that runs past it, such as a literal with a blank, is
            # taken whole): "1Welcome" is a number and a word to the lexer.
            end = _whitespace_after(text, token.start)
            while index < len(tokens) and tokens[index].start < end:
                if tokens[index].end > end:
                    end = _whitespace_after(text, tokens[index].end)
                index += 1
            if token.end > end:
                end = _whitespace_after(text, token.end)
            pieces.append("?")
            position = end
            mask_operand = False
            continue
        if token.is_symbol("{", "["):
            document_depth += 1
        elif token.is_symbol("}", "]"):
            document_depth = max(document_depth - 1, 0)
        if (
            token.kind in _LITERALS
            or (token.kind == "quoted" and (not token.closed or document_depth > 0))
        ):
            # Inside a JSON document (document store) a "..." is a value or a
            # key, not an identifier.
            pieces.append("?")
            continue
        if token.is_word("PASSWORD"):
            mask_operand = True
        pieces.append(token.text)
    pieces.append(text[position:])
    masked = _clean("".join(pieces))
    limit = max(int(limit), 1)
    if len(masked) > limit:
        masked = masked[: limit - 1] + _ELLIPSIS
    return masked
