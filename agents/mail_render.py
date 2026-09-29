"""Markdown -> Outlook-safe HTML for the mail this app originates.

The report agents already write markdown: the admin UI renders their run
reports through marked.js, so a workflow's final step produces headings,
bullets and pipe tables as a matter of course. The mail path used to escape all
of it into one unstyled column (``_text_to_html`` in ``outlook_tools``), which
is why the daily reports arrived looking like a log file.

Why a renderer of our own rather than a markdown library plus a CSS inliner:

* Outlook on Windows lays mail out with **Word's** rendering engine. No
  flexbox, no grid, no ``<style>`` block worth relying on, no reliable padding
  outside a table cell. Every generic markdown library emits semantic HTML that
  then needs its CSS inlined and its block layout rebuilt as tables anyway.
* The subset the agents actually write is small and known, so the parser can be
  small and known with it -- and a line matching no rule falls through to a
  paragraph, which is exactly the old behaviour. The failure mode is "looks
  like before", never broken markup.
* Escaping stays on this side. The model's text is never treated as markup, so
  a ``<`` in a program name cannot become a tag.

One deliberate omission: ``_underscore_`` is **not** emphasis. SAP text is full
of ``DBIF_RSQL_SQL_ERROR`` and ``SAP_COM_0345``; treating those underscores as
markup would eat half of every error report.
"""

from __future__ import annotations

import html
import re

# --- palette ---------------------------------------------------------------
# One place for the colours so the band, the callout and the tables cannot
# drift apart. Chosen for legibility against Outlook's white canvas; the
# backgrounds are explicit so a client forcing dark mode still has something
# defined to invert rather than guessing.
INK = "#22313f"          # body text
INK_SOFT = "#5b6b7c"     # secondary text
RULE = "#dfe5ec"         # hairlines and table borders
BAND = "#1f3348"         # header band
BAND_TEXT = "#ffffff"
BAND_SUB = "#b9c6d4"
SHELL = "#eef1f5"        # page background outside the sheet
SHEET = "#ffffff"        # the sheet itself
ZEBRA = "#f7f9fb"        # alternating table rows
HEAD_CELL = "#eef2f6"    # table header row
FOOT_TEXT = "#7b8794"

FONT = "'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "Consolas,'Courier New',monospace"

_BODY_TEXT = f"font-family:{FONT};font-size:14px;line-height:1.5;color:{INK};"

# Heading sizes by level. Levels past 3 reuse the smallest -- report agents do
# not nest deeper, and a run of ever-tinier headings reads worse than a plateau.
_HEADING = {
    1: f"font-family:{FONT};font-size:19px;font-weight:600;color:{INK};"
       f"padding:2px 0 8px 0;margin:0;",
    2: f"font-family:{FONT};font-size:16px;font-weight:600;color:{INK};"
       f"padding:18px 0 6px 0;margin:0;border-bottom:1px solid {RULE};",
    3: f"font-family:{FONT};font-size:14px;font-weight:600;color:{INK};"
       f"padding:14px 0 4px 0;margin:0;",
}

_CELL = f"border:1px solid {RULE};padding:6px 10px;font-family:{FONT};font-size:13px;color:{INK};"
_HEAD_CELL = (
    f"border:1px solid {RULE};padding:7px 10px;background-color:{HEAD_CELL};"
    f"font-family:{FONT};font-size:12px;font-weight:600;color:{INK};"
)
_CODE = (
    f"font-family:{MONO};font-size:12.5px;background-color:#f4f6f9;"
    f"border:1px solid {RULE};padding:1px 4px;color:{INK};"
)
_ANCHOR = f"color:#1d6fa5;text-decoration:underline;"
_LI = f"font-family:{FONT};font-size:14px;line-height:1.5;color:{INK};padding:0 0 4px 0;"
_TABLE = "border-collapse:collapse;width:100%;margin:8px 0 14px 0;"
_PLAIN_TABLE = 'role="presentation" cellpadding="0" cellspacing="0" border="0"'

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*```")
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
# A delimiter row must carry a pipe: a bare '---' is a horizontal rule, and the
# difference decides whether the line above it is a table header or a sentence.
_DELIM_RE = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$|^\s*\|\s*:?-{2,}:?\s*\|\s*$")

_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*")
# Emphasis needs tighter boundaries than bold: SAP reports carry SQL
# (`SELECT * FROM`) and arithmetic, so a star with whitespace beside it, or one
# sitting inside a word, is punctuation rather than a mark.
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_SLOT_RE = re.compile("\x00(\\d+)\x00")
# Only schemes that mean "open a page" or "write a mail". Anything else -- and
# `javascript:` in particular -- keeps its label and loses its link.
_SAFE_URL_RE = re.compile(r"^(?:https?://|mailto:)", re.I)


def split_subject(subject: str) -> tuple[str, str]:
    """A subject line as ``(title, subline)``.

    The workflows build their subject as ``<report name> -- <REPORT DATE>``,
    where the date is copied verbatim from the data (the agents have no clock,
    and the instructions are emphatic that they must never invent one). Peeling
    the tail off here gives the header band a date line without adding an
    argument the model would have to fill -- and therefore without giving it a
    second chance to state a date that does not match the subject.

    Splits once, on the first separator: a title may well contain a dash.
    """
    text = (subject or "").strip()
    if not text:
        return "", ""
    for separator in (" -- ", " — ", " – "):
        head, found, tail = text.partition(separator)
        if found:
            return head.strip(), tail.strip()
    return text, ""


def split_verdict(body: str) -> tuple[str, str]:
    """``(verdict, rest)`` -- the opening paragraph, and everything after it.

    Both daily workflows instruct their final step to open with a two-line
    verdict, which is exactly the line a reader wants pulled out and tinted.
    Only a plain paragraph qualifies: a report opening on a heading or a table
    has no verdict to lift, and guessing one out of a section title would
    dignify a heading as a judgement it never made.
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text.strip():
        return "", ""
    lines = text.split("\n")
    nxt = lines[1] if len(lines) > 1 else ""
    if _starts_block(lines[0], nxt):
        return "", text

    verdict: list[str] = []
    i = 0
    while i < len(lines) and lines[i].strip():
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if verdict and _starts_block(lines[i], nxt):
            break
        verdict.append(lines[i])
        i += 1
    return "\n".join(verdict).strip(), "\n".join(lines[i:]).strip("\n")


def _anchor(label: str, url: str) -> str:
    target = (url or "").strip()
    if not _SAFE_URL_RE.match(target):
        # Not a link we will follow -- keep the words, drop the link.
        return html.escape(label, quote=False)
    return (
        f'<a href="{html.escape(target, quote=True)}" style="{_ANCHOR}">'
        f"{html.escape(label, quote=False)}</a>"
    )


def _inline(text: str) -> str:
    """One line of markdown as HTML.

    Code spans and links are lifted out into slots *before* escaping, so their
    own escaping is done once and the remaining marks cannot reach inside them:
    a backticked ``**x**`` stays literal, which is the whole point of quoting
    it. Everything left over is escaped, and only then does bold apply -- so
    the model's text can never contribute a tag.
    """
    slots: list[str] = []

    def stash(snippet: str) -> str:
        slots.append(snippet)
        return f"\x00{len(slots) - 1}\x00"

    # The slot marker itself is the one byte we must not accept from outside.
    body = (text or "").replace("\x00", "")
    body = _CODE_SPAN_RE.sub(
        lambda m: stash(f'<code style="{_CODE}">{html.escape(m.group(1), quote=False)}</code>'),
        body,
    )
    body = _LINK_RE.sub(lambda m: stash(_anchor(m.group(1), m.group(2))), body)
    body = html.escape(body, quote=False)
    body = _BOLD_RE.sub(r"<strong>\1</strong>", body)
    body = _ITALIC_RE.sub(r"<em>\1</em>", body)

    # A link label may hold a code span, so a slot can sit inside a slot. Two
    # passes cover that; the bound keeps a pathological input from looping.
    for _ in range(3):
        if "\x00" not in body:
            break
        body = _SLOT_RE.sub(lambda m: slots[int(m.group(1))], body)
    return body


def _starts_block(line: str, nxt: str) -> bool:
    """True when ``line`` opens a block that is not a paragraph."""
    stripped = line.strip()
    if not stripped:
        return True
    return bool(
        _HEADING_RE.match(stripped)
        or _FENCE_RE.match(line)
        or _RULE_RE.match(line)
        or _QUOTE_RE.match(line)
        or _ITEM_RE.match(line)
        or ("|" in stripped and _DELIM_RE.match(nxt))
    )


def _cells(row: str) -> list[str]:
    body = row.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _alignments(delimiter: str) -> list[str]:
    out = []
    for cell in _cells(delimiter):
        left, right = cell.startswith(":"), cell.endswith(":")
        out.append("center" if left and right else "right" if right else "left")
    return out


def _table(header: str, delimiter: str, rows: list[str]) -> str:
    heads = _cells(header)
    aligns = _alignments(delimiter)

    def align(idx: int) -> str:
        return aligns[idx] if idx < len(aligns) else "left"

    parts = [f'<table {_PLAIN_TABLE} style="{_TABLE}">']
    parts.append("<tr>")
    for idx, cell in enumerate(heads):
        parts.append(f'<th style="{_HEAD_CELL}text-align:{align(idx)};">{_inline(cell)}</th>')
    parts.append("</tr>")
    for n, row in enumerate(rows):
        cells = _cells(row)
        # Ragged rows are common when a model lays a table out by hand: pad a
        # short one rather than dropping the row, and keep the extras on a long
        # one rather than silently losing a value.
        while len(cells) < len(heads):
            cells.append("")
        shade = f"background-color:{ZEBRA};" if n % 2 else ""
        parts.append("<tr>")
        for idx, cell in enumerate(cells):
            parts.append(
                f'<td style="{_CELL}{shade}text-align:{align(idx)};">{_inline(cell)}</td>'
            )
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _list(items: list[tuple[int, bool, str]], start: int, indent: int) -> tuple[str, int]:
    """One list level, recursing for deeper indents. Returns (html, next index)."""
    ordered = items[start][1]
    tag = "ol" if ordered else "ul"
    style = f"margin:4px 0 12px 0;padding:0 0 0 24px;"
    parts = [f'<{tag} style="{style}">']
    i = start
    while i < len(items):
        depth, _, text = items[i]
        if depth < indent:
            break
        if depth > indent:
            nested, i = _list(items, i, depth)
            # A nested list belongs inside the item above it; the item is still
            # open, so it closes after the nested list does.
            parts.append(nested + "</li>")
            continue
        if parts[-1].startswith("<li"):
            parts[-1] += "</li>"
        parts.append(f'<li style="{_LI}">{_inline(text)}')
        i += 1
    if parts[-1].startswith("<li"):
        parts[-1] += "</li>"
    parts.append(f"</{tag}>")
    return "".join(parts), i


def markdown_to_html(text: str) -> str:
    """The markdown subset the report agents write, as inline-styled HTML."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        if _FENCE_RE.match(line):
            i += 1
            code: list[str] = []
            while i < len(lines) and not _FENCE_RE.match(lines[i]):
                code.append(lines[i])
                i += 1
            i += 1  # the closing fence, or one past the end if it never came
            body = html.escape("\n".join(code), quote=False)
            out.append(
                f'<table {_PLAIN_TABLE} style="{_TABLE}"><tr><td style="background-color:#f4f6f9;'
                f'border:1px solid {RULE};padding:10px 12px;">'
                f'<pre style="margin:0;white-space:pre-wrap;font-family:{MONO};'
                f'font-size:12.5px;color:{INK};">{body}</pre></td></tr></table>'
            )
            continue

        heading = _HEADING_RE.match(line.strip())
        if heading:
            level = min(len(heading.group(1)), 3)
            out.append(
                f'<div style="{_HEADING[level]}">{_inline(heading.group(2).strip())}</div>'
            )
            i += 1
            continue

        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if "|" in line and _DELIM_RE.match(nxt):
            header, delimiter = line, nxt
            i += 2
            rows: list[str] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            out.append(_table(header, delimiter, rows))
            continue

        if _RULE_RE.match(line):
            out.append(
                f'<table {_PLAIN_TABLE} style="border-collapse:collapse;width:100%;'
                f'margin:16px 0;"><tr><td style="border-top:1px solid {RULE};'
                f'font-size:0;line-height:0;">&nbsp;</td></tr></table>'
            )
            i += 1
            continue

        if _QUOTE_RE.match(line):
            quoted: list[str] = []
            while i < len(lines) and _QUOTE_RE.match(lines[i]):
                quoted.append(_inline(_QUOTE_RE.match(lines[i]).group(1).strip()))
                i += 1
            out.append(
                f'<table {_PLAIN_TABLE} style="border-collapse:collapse;width:100%;'
                f'margin:4px 0 12px 0;"><tr><td style="border-left:3px solid #c3ccd6;'
                f'padding:4px 0 4px 12px;{_BODY_TEXT}color:{INK_SOFT};">'
                + "<br>".join(quoted)
                + "</td></tr></table>"
            )
            continue

        if _ITEM_RE.match(line):
            items: list[tuple[int, bool, str]] = []
            while i < len(lines) and _ITEM_RE.match(lines[i]):
                indent, marker, text = _ITEM_RE.match(lines[i]).groups()
                items.append((len(indent.expandtabs(4)), marker[-1] in ".)", text.strip()))
                i += 1
            rendered, _ = _list(items, 0, items[0][0])
            out.append(rendered)
            continue

        # Paragraph: consecutive non-blank lines that start no other block.
        chunk: list[str] = []
        while i < len(lines):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if chunk and _starts_block(lines[i], nxt):
                break
            if not lines[i].strip():
                break
            chunk.append(_inline(lines[i].strip()))
            i += 1
        out.append(
            f'<p style="{_BODY_TEXT}margin:0 0 10px 0;">' + "<br>".join(chunk) + "</p>"
        )

    return "".join(out)


# --- the report shell ------------------------------------------------------
# Status is the one piece of metadata the agent supplies rather than the data:
# the colour only ever repeats the verdict sentence printed beside it, so a
# mislabelled report is wrong in two visible places at once rather than one
# invisible one. An unrecognised word is treated as no claim at all.
_STATUS = {
    "ok": ("#1e7b4d", "#eef7f1"),
    "attention": ("#b9770e", "#fdf6e9"),
}
_NEUTRAL = ("#97a3b0", "#f3f5f8")
FOOT_RULE = "#e4e9ef"


def _callout(verdict_html: str, status: str) -> str:
    bar, tint = _STATUS.get((status or "").strip().lower(), _NEUTRAL)
    return (
        f'<tr><td style="padding:20px 24px 0 24px;">'
        f'<table {_PLAIN_TABLE} style="border-collapse:collapse;width:100%;">'
        f'<tr><td width="4" style="width:4px;background-color:{bar};font-size:0;'
        f'line-height:0;">&nbsp;</td>'
        f'<td style="background-color:{tint};padding:12px 16px;{_BODY_TEXT}">'
        f"{verdict_html}</td></tr></table></td></tr>"
    )


def render_report_html(
    body: str,
    *,
    title: str,
    subline: str = "",
    status: str = "",
    footer: str = "",
) -> str:
    """A report as one Outlook-safe HTML document.

    Nested tables and inline styles throughout, because Outlook on Windows lays
    mail out with Word's engine: a div with padding is a suggestion there, a
    table cell with padding is not.
    """
    verdict_md, rest_md = split_verdict(body)
    verdict_html = markdown_to_html(verdict_md)
    body_html = markdown_to_html(rest_md)

    rows = [
        f'<tr><td style="background-color:{BAND};padding:18px 24px;">'
        f'<div style="font-family:{FONT};font-size:19px;font-weight:600;'
        f'color:{BAND_TEXT};">{html.escape(title or "", quote=False)}</div>'
    ]
    if (subline or "").strip():
        rows.append(
            f'<div style="font-family:{FONT};font-size:12px;color:{BAND_SUB};'
            f'padding-top:5px;">{html.escape(subline.strip(), quote=False)}</div>'
        )
    rows.append("</td></tr>")

    if verdict_html:
        rows.append(_callout(verdict_html, status))
    if body_html:
        rows.append(
            f'<tr><td style="padding:18px 24px 24px 24px;{_BODY_TEXT}">{body_html}</td></tr>'
        )
    if (footer or "").strip():
        rows.append(
            f'<tr><td style="border-top:1px solid {FOOT_RULE};background-color:{ZEBRA};'
            f'padding:12px 24px;font-family:{FONT};font-size:11px;color:{FOOT_TEXT};">'
            f'{html.escape(footer.strip(), quote=False)}</td></tr>'
        )

    sheet = "".join(rows)
    return (
        f'<div style="margin:0;padding:0;background-color:{SHELL};">'
        f'<table {_PLAIN_TABLE} width="100%" style="background-color:{SHELL};'
        f'border-collapse:collapse;"><tr><td align="center" style="padding:24px 12px;">'
        f'<table {_PLAIN_TABLE} width="640" style="width:640px;max-width:640px;'
        f'background-color:{SHEET};border:1px solid #d8dee6;border-collapse:collapse;">'
        f"{sheet}</table></td></tr></table></div>"
    )
