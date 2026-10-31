"""A small markdown subset parsed into blocks, for the document tools.

The model writes markdown; ``agents/webchat/docgen.py`` turns it into Word
and PDF. No markdown library is a dependency and none is needed for what a
document needs: headings, paragraphs with bold / italic / inline code,
bullet and numbered lists, GFM pipe tables, fenced code and a horizontal
rule. Everything else is plain text.

Standard library only. Nothing is evaluated, fetched or interpreted: a link
becomes its text followed by the URL in brackets, raw HTML stays text, an
image is its alt text. The parser is line based and linear in the input
(the callers cap the input at ``MAX_MARKDOWN_CHARS``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_MARKDOWN_CHARS = 200_000
MAX_HEADING_LEVEL = 4
MAX_LIST_LEVEL = 3


@dataclass(frozen=True)
class Span:
    """A run of inline text with its formatting."""

    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


@dataclass
class Heading:
    level: int
    spans: list[Span]


@dataclass
class Paragraph:
    spans: list[Span]


@dataclass
class ListItem:
    spans: list[Span]
    level: int = 0


@dataclass
class ListBlock:
    ordered: bool
    items: list[ListItem] = field(default_factory=list)
    start: int = 1


@dataclass
class Table:
    header: list[list[Span]]
    rows: list[list[list[Span]]]


@dataclass
class CodeBlock:
    text: str
    language: str = ""


@dataclass
class Rule:
    pass


Block = Heading | Paragraph | ListBlock | Table | CodeBlock | Rule

_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([\w+-]*)")
_RULE = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
# Every inner part is bounded (``_SPAN``): an unclosed marker then costs a
# scan of at most that many characters, never one to the end of the text.
_SPAN = 400
_INLINE = re.compile(
    rf"`(?P<code>[^`]{{1,{_SPAN}}})`"
    rf"|\*\*(?P<b1>.{{1,{_SPAN}}}?)\*\*"
    rf"|__(?P<b2>.{{1,{_SPAN}}}?)__"
    rf"|\*(?P<i1>[^\s*](?:.{{0,{_SPAN}}}?[^\s*])?)\*"
    rf"|(?<![\w])_(?P<i2>[^\s_](?:.{{0,{_SPAN}}}?[^\s_])?)_(?![\w])"
    rf"|!\[(?P<alt>[^\]]{{0,{_SPAN}}})\]\([^)\s]{{0,{_SPAN}}}\)"
    rf"|\[(?P<link>[^\]]{{1,{_SPAN}}})\]\((?P<url>[^)\s]{{1,{_SPAN}}})\)"
)
# A horizontal rule is short; a long line is never one (the pattern
# backtracks over its repetitions).
_RULE_MAX = 120


def parse_inline(text: str, *, bold: bool = False, italic: bool = False) -> list[Span]:
    """Inline markdown to spans. Bold and italic nest; code does not."""
    spans: list[Span] = []
    pos = 0
    for match in _INLINE.finditer(text):
        if match.start() > pos:
            spans.append(Span(text[pos:match.start()], bold, italic))
        if match.group("code") is not None:
            spans.append(Span(match.group("code"), bold, italic, code=True))
        elif match.group("b1") is not None or match.group("b2") is not None:
            inner = match.group("b1") if match.group("b1") is not None else match.group("b2")
            spans.extend(parse_inline(inner, bold=True, italic=italic))
        elif match.group("i1") is not None or match.group("i2") is not None:
            inner = match.group("i1") if match.group("i1") is not None else match.group("i2")
            spans.extend(parse_inline(inner, bold=bold, italic=True))
        elif match.group("alt") is not None:
            if match.group("alt"):
                spans.append(Span(match.group("alt"), bold, italic))
        else:
            spans.extend(parse_inline(match.group("link"), bold=bold, italic=italic))
            spans.append(Span(f" ({match.group('url')})", bold, italic))
        pos = match.end()
    if pos < len(text):
        spans.append(Span(text[pos:], bold, italic))
    return _merge(spans)


def _merge(spans: list[Span]) -> list[Span]:
    merged: list[Span] = []
    for span in spans:
        if not span.text:
            continue
        last = merged[-1] if merged else None
        if last and (last.bold, last.italic, last.code) == (span.bold, span.italic, span.code):
            merged[-1] = Span(last.text + span.text, span.bold, span.italic, span.code)
        else:
            merged.append(span)
    return merged


def plain(spans: list[Span]) -> str:
    """The text of spans without formatting."""
    return "".join(span.text for span in spans)


def _cells(line: str) -> list[str]:
    """The cells of one table row; ``\\|`` is a literal pipe."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells, current, i = [], [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] == "|":
            current.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    cells.append("".join(current).strip())
    return cells


def _is_table_start(lines: list[str], i: int) -> bool:
    return (
        "|" in lines[i]
        and i + 1 < len(lines)
        and _TABLE_SEP.match(lines[i + 1]) is not None
        and "|" in lines[i + 1]
    )


def _starts_block(lines: list[str], i: int) -> bool:
    line = lines[i]
    return bool(
        _HEADING.match(line) or _FENCE.match(line)
        or (len(line) <= _RULE_MAX and _RULE.match(line))
        or _BULLET.match(line) or _ORDERED.match(line) or _is_table_start(lines, i)
    )


def _level(indent: str) -> int:
    width = len(indent.replace("\t", "    "))
    return min(width // 2, MAX_LIST_LEVEL)


def parse(markdown: str) -> list[Block]:
    """The blocks of ``markdown``, in order."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            body: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1  # the closing fence (or the end)
            blocks.append(CodeBlock("\n".join(body), fence.group(2)))
            continue
        heading = _HEADING.match(line)
        if heading:
            level = min(len(heading.group(1)), MAX_HEADING_LEVEL)
            blocks.append(Heading(level, parse_inline(heading.group(2))))
            i += 1
            continue
        if len(line) <= _RULE_MAX and _RULE.match(line):
            blocks.append(Rule())
            i += 1
            continue
        if _is_table_start(lines, i):
            header = [parse_inline(c) for c in _cells(line)]
            rows: list[list[list[Span]]] = []
            i += 2
            while i < n and lines[i].strip() and "|" in lines[i]:
                cells = [parse_inline(c) for c in _cells(lines[i])]
                # A row has the header's width: missing cells are empty,
                # extra ones are dropped (GFM does the same).
                cells = (cells + [[] for _ in header])[: len(header)]
                rows.append(cells)
                i += 1
            blocks.append(Table(header, rows))
            continue
        bullet, ordered = _BULLET.match(line), _ORDERED.match(line)
        if bullet or ordered:
            is_ordered = ordered is not None and bullet is None
            block = ListBlock(ordered=is_ordered,
                              start=int(ordered.group(2)) if is_ordered and ordered else 1)
            while i < n:
                b, o = _BULLET.match(lines[i]), _ORDERED.match(lines[i])
                if b and not is_ordered:
                    block.items.append(ListItem(parse_inline(b.group(2)), _level(b.group(1))))
                elif o and is_ordered:
                    block.items.append(ListItem(parse_inline(o.group(3)), _level(o.group(1))))
                elif (b or o) and block.items and (b or o).group(1):
                    # A nested list of the other kind: kept in this list,
                    # indented, its marker style taken from the outer list.
                    m = b or o
                    text = m.group(2) if b else m.group(3)
                    block.items.append(ListItem(parse_inline(text), _level(m.group(1))))
                elif (
                    lines[i].strip() and lines[i][:1] in (" ", "\t")
                    and block.items and not _starts_block(lines, i)
                ):
                    last = block.items[-1]
                    last.spans = _merge(last.spans + [Span(" ")]
                                        + parse_inline(lines[i].strip()))
                else:
                    break
                i += 1
            blocks.append(block)
            continue
        para = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not _starts_block(lines, i):
            para.append(lines[i].strip())
            i += 1
        blocks.append(Paragraph(parse_inline(" ".join(para))))
    return blocks
