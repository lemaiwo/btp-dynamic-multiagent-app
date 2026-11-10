"""One SQL tokenizer for ``builtin:hana``: the SELECT guard and the masking.

The guard must never read a statement differently from HANA, so the strict
mode is closed: anything it does not positively recognise (a character
outside ASCII SQL, an unterminated literal, quoted identifier or block
comment, a control or format character anywhere, a number glued to a word)
is ``SelectRefused("unreadable")``. Literals are tokens, never keywords, so a
``';'`` or ``'DROP'`` inside a string means nothing to the guard.

The lenient mode is for masking statement texts HANA hands back (possibly cut
by HANA itself): it never raises; an unterminated literal, identifier or
comment runs to the end of the text, an unknown character is a one-character
``symbol``.

Standard library only.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

KINDS = ("word", "quoted", "string", "number", "binary", "symbol", "comment", "param")

RULE_TEXTS: dict[str, str] = {
    "too_long": "the statement is longer than 4,000 characters",
    "unreadable": (
        "the statement could not be read (an unterminated literal, identifier or "
        "comment, unbalanced parentheses, or a character that is not SQL)"
    ),
    "no_comments": "comments are not allowed",
    "one_statement": "exactly one statement is allowed, without ';'",
    "select_only": "only one SELECT statement (optionally opened by WITH) is allowed",
    "no_into": "SELECT ... INTO is not allowed",
    "no_for_update": "FOR UPDATE and other locking clauses are not allowed",
    "table_not_allowed": (
        "only SYS.<name>, _SYS_STATISTICS.<name> or unqualified M_* views may be "
        "read, by name or in a parenthesised SELECT"
    ),
}


class SelectRefused(ValueError):
    """A statement the guard will not send. The text names the rule, never the SQL."""

    def __init__(self, rule: str) -> None:
        if rule not in RULE_TEXTS:
            rule = "unreadable"
        self.rule = rule
        super().__init__(f"sql: {rule}: {RULE_TEXTS[rule]}")


@dataclass(frozen=True)
class Token:
    """``value`` is the upper-cased word, the unescaped quoted identifier, or the text."""

    kind: str
    text: str
    value: str
    start: int
    end: int
    closed: bool = True

    def is_word(self, *words: str) -> bool:
        return self.kind == "word" and self.value in words

    def is_symbol(self, *symbols: str) -> bool:
        return self.kind == "symbol" and self.text in symbols

    @property
    def is_ident(self) -> bool:
        return self.kind in ("word", "quoted")


_STRICT_WS = " \t\r\n"
_ALLOWED_CONTROLS = "\t\r\n"
_TWO_CHAR = ("<=", ">=", "<>", "!=", "||", "=>")
_ONE_CHAR = "(),.;=<>!+-*/|%"
_WORD_STRICT = re.compile(r"[A-Za-z_][A-Za-z0-9_#$]*")
_WORD_LENIENT = re.compile(r"[^\W\d][\w#$]*")
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?")
_HEX = re.compile(r"[0-9A-Fa-f]*")
_PARAM_NAME = re.compile(r"[A-Za-z0-9_]+")


def _check_characters(sql: str) -> None:
    """Strict mode: no control, format, private or unassigned character anywhere."""
    for char in sql:
        if char in _ALLOWED_CONTROLS:
            continue
        category = unicodedata.category(char)
        if category[0] == "C" or category in ("Zl", "Zp"):
            raise SelectRefused("unreadable")


def _quoted_end(sql: str, start: int, quote: str) -> int:
    """Index after the closing quote (doubled quotes escape), or -1."""
    pos = start + 1
    while True:
        found = sql.find(quote, pos)
        if found < 0:
            return -1
        if sql.startswith(quote * 2, found):
            pos = found + 2
            continue
        return found + 1


def tokenize(sql: str, *, lenient: bool = False) -> list[Token]:
    """Split ``sql`` into tokens; whitespace is not a token."""
    if not lenient:
        _check_characters(sql)
    tokens: list[Token] = []
    pos = 0
    size = len(sql)

    def refuse() -> SelectRefused:
        return SelectRefused("unreadable")

    while pos < size:
        char = sql[pos]
        if char in _STRICT_WS or (lenient and char.isspace()):
            pos += 1
            continue
        start = pos
        nxt = sql[pos + 1] if pos + 1 < size else ""

        if (char == "-" and nxt == "-") or (char == "/" and nxt == "/"):
            end = sql.find("\n", pos)
            end = size if end < 0 else end
            tokens.append(Token("comment", sql[start:end], "", start, end))
            pos = end
            continue
        if char == "/" and nxt == "*":
            found = sql.find("*/", pos + 2)
            if found < 0:
                if not lenient:
                    raise refuse()
                tokens.append(Token("comment", sql[start:], "", start, size, closed=False))
                pos = size
                continue
            end = found + 2
            tokens.append(Token("comment", sql[start:end], "", start, end))
            pos = end
            continue

        if char == "'" or (char in "nN" and nxt == "'"):
            quote_at = pos if char == "'" else pos + 1
            end = _quoted_end(sql, quote_at, "'")
            if end < 0:
                if not lenient:
                    raise refuse()
                tokens.append(Token("string", sql[start:], "", start, size, closed=False))
                pos = size
                continue
            tokens.append(Token("string", sql[start:end], "", start, end))
            pos = end
            continue

        if char in "xX" and nxt == "'":
            found = sql.find("'", pos + 2)
            if found < 0:
                if not lenient:
                    raise refuse()
                tokens.append(Token("binary", sql[start:], "", start, size, closed=False))
                pos = size
                continue
            digits = sql[pos + 2 : found]
            if not lenient and (_HEX.fullmatch(digits) is None or len(digits) % 2):
                raise refuse()
            end = found + 1
            tokens.append(Token("binary", sql[start:end], "", start, end))
            pos = end
            continue

        if char == '"':
            end = _quoted_end(sql, pos, '"')
            if end < 0:
                if not lenient:
                    raise refuse()
                tokens.append(Token("quoted", sql[start:], "", start, size, closed=False))
                pos = size
                continue
            value = sql[start + 1 : end - 1].replace('""', '"')
            if not lenient and not value:
                raise refuse()
            tokens.append(Token("quoted", sql[start:end], value, start, end))
            pos = end
            continue

        if char.isdigit() and (not lenient or char.isascii()):
            match = _NUMBER.match(sql, pos)
            if match is None:  # pragma: no cover - a digit always matches
                raise refuse()
            end = match.end()
            if not lenient and end < size and (sql[end].isalnum() or sql[end] in "_#$\"'"):
                raise refuse()
            tokens.append(Token("number", sql[start:end], sql[start:end], start, end))
            pos = end
            continue

        if char == "?":
            tokens.append(Token("param", "?", "?", start, pos + 1))
            pos += 1
            continue
        if char == ":" and nxt:
            match = _PARAM_NAME.match(sql, pos + 1)
            if match is not None:
                end = match.end()
                tokens.append(Token("param", sql[start:end], sql[start:end], start, end))
                pos = end
                continue

        match = (_WORD_LENIENT if lenient else _WORD_STRICT).match(sql, pos)
        if match is not None:
            end = match.end()
            text = sql[start:end]
            tokens.append(Token("word", text, text.upper(), start, end))
            pos = end
            continue

        two = sql[pos : pos + 2]
        if two in _TWO_CHAR:
            tokens.append(Token("symbol", two, two, start, pos + 2))
            pos += 2
            continue
        if char in _ONE_CHAR or lenient:
            tokens.append(Token("symbol", char, char, start, pos + 1))
            pos += 1
            continue
        raise refuse()
    return tokens
