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
from dataclasses import dataclass, fields
from functools import lru_cache
from typing import Any

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

LINK = "#1d6fa5"

_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")
_FONT_RE = re.compile(r"[A-Za-z0-9 ,'\"-]{1,200}")
_COLOUR_FIELDS = (
    "band", "band_text", "band_sub", "accent", "link", "heading",
    "head_cell", "zebra", "shell",
)
_ORG_NAME_MAX = 80
_FOOTER_MAX = 300
_LOGO_URL_MAX = 2048


@dataclass(frozen=True)
class MailTheme:
    """Per-server look of originated mail: colours, font, logo, footer.

    Every default is the module palette above, so ``MailTheme()`` renders
    byte-for-byte what the renderer produced before themes existed. Only the
    brand surfaces are themable; the status tints (ok/attention/neutral) are
    semantic and stay fixed, so green always means the same thing.

    ``accent`` is empty by default: no accent rule under the band and the h2
    underline stays a hairline. ``footer`` overrides the caller's footer text.
    """

    band: str = BAND
    band_text: str = BAND_TEXT
    band_sub: str = BAND_SUB
    accent: str = ""
    link: str = LINK
    heading: str = INK
    head_cell: str = HEAD_CELL
    zebra: str = ZEBRA
    shell: str = SHELL
    font: str = FONT
    logo_url: str = ""
    org_name: str = ""
    footer: str = ""

    @classmethod
    def from_config(cls, cfg: Any) -> "MailTheme":
        """A theme from a server config's ``theme`` object, validated.

        Raises ValueError naming the offending key. Unknown keys are refused
        so a typo surfaces at save time instead of silently doing nothing.
        A missing, null or empty-string value keeps the default.
        """
        if cfg is None:
            return cls()
        if not isinstance(cfg, dict):
            raise ValueError("theme must be a JSON object")
        known = {f.name for f in fields(cls)}
        unknown = sorted(str(k) for k in cfg if k not in known)
        if unknown:
            raise ValueError(
                f"theme has unknown key(s): {', '.join(unknown)} "
                f"(allowed: {', '.join(sorted(known))})"
            )
        values: dict[str, str] = {}
        for key, raw in cfg.items():
            if raw is None or raw == "":
                continue
            if not isinstance(raw, str):
                raise ValueError(f"theme.{key} must be a string")
            value = raw.strip()
            if key in _COLOUR_FIELDS:
                if not _HEX_RE.fullmatch(raw):
                    raise ValueError(f"theme.{key} must be a hex colour like #1f3348")
            elif key == "logo_url":
                if (
                    not value.lower().startswith("https://")
                    or len(value) > _LOGO_URL_MAX
                    or any(c in value for c in "\"'<> \t\r\n")
                    or "javascript:" in value.lower()
                ):
                    raise ValueError(
                        "theme.logo_url must be an https:// URL without quotes, "
                        "spaces, '<' or '>'"
                    )
            elif key == "font":
                if not _FONT_RE.fullmatch(value):
                    raise ValueError(
                        "theme.font may contain only letters, digits, spaces, "
                        "commas, quotes and hyphens"
                    )
                # Double quotes would end the style="" attribute it lands in.
                value = value.replace('"', "'")
            elif key == "org_name":
                if len(value) > _ORG_NAME_MAX:
                    raise ValueError(f"theme.org_name is at most {_ORG_NAME_MAX} characters")
            elif key == "footer":
                if len(value) > _FOOTER_MAX:
                    raise ValueError(f"theme.footer is at most {_FOOTER_MAX} characters")
            values[key] = value
        return cls(**values)

    def to_config(self) -> dict[str, str]:
        """The keys that differ from the default -- what storage keeps."""
        default = MailTheme()
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) != getattr(default, f.name)
        }


class _Styles:
    """The inline style strings for one theme."""

    def __init__(self, theme: MailTheme) -> None:
        font = theme.font
        self.theme = theme
        self.font = font
        self.body_text = f"font-family:{font};font-size:14px;line-height:1.5;color:{INK};"
        h2_rule = theme.accent or RULE
        # Heading sizes by level. Levels past 3 reuse the smallest -- report
        # agents do not nest deeper, and a run of ever-tinier headings reads
        # worse than a plateau.
        self.heading = {
            1: f"font-family:{font};font-size:19px;font-weight:600;color:{theme.heading};"
               f"padding:2px 0 8px 0;margin:0;",
            2: f"font-family:{font};font-size:16px;font-weight:600;color:{theme.heading};"
               f"padding:18px 0 6px 0;margin:0;border-bottom:1px solid {h2_rule};",
            3: f"font-family:{font};font-size:14px;font-weight:600;color:{theme.heading};"
               f"padding:14px 0 4px 0;margin:0;",
        }
        self.cell = (
            f"border:1px solid {RULE};padding:6px 10px;font-family:{font};"
            f"font-size:13px;color:{INK};"
        )
        self.head_cell = (
            f"border:1px solid {RULE};padding:7px 10px;background-color:{theme.head_cell};"
            f"font-family:{font};font-size:12px;font-weight:600;color:{INK};"
        )
        self.code = (
            f"font-family:{MONO};font-size:12.5px;background-color:#f4f6f9;"
            f"border:1px solid {RULE};padding:1px 4px;color:{INK};"
        )
        self.anchor = f"color:{theme.link};text-decoration:underline;"
        self.li = (
            f"font-family:{font};font-size:14px;line-height:1.5;color:{INK};"
            f"padding:0 0 4px 0;"
        )


@lru_cache(maxsize=32)
def _styles(theme: MailTheme) -> _Styles:
    return _Styles(theme)


_DEFAULT = _styles(MailTheme())
# The default style strings under their historical names.
_BODY_TEXT = _DEFAULT.body_text
_HEADING = _DEFAULT.heading
_CELL = _DEFAULT.cell
_HEAD_CELL = _DEFAULT.head_cell
_CODE = _DEFAULT.code
_ANCHOR = _DEFAULT.anchor
_LI = _DEFAULT.li
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


def _anchor(label: str, url: str, st: _Styles = _DEFAULT) -> str:
    target = (url or "").strip()
    if not _SAFE_URL_RE.match(target):
        # Not a link we will follow -- keep the words, drop the link.
        return html.escape(label, quote=False)
    return (
        f'<a href="{html.escape(target, quote=True)}" style="{st.anchor}">'
        f"{html.escape(label, quote=False)}</a>"
    )


def _inline(text: str, st: _Styles = _DEFAULT) -> str:
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
        lambda m: stash(f'<code style="{st.code}">{html.escape(m.group(1), quote=False)}</code>'),
        body,
    )
    body = _LINK_RE.sub(lambda m: stash(_anchor(m.group(1), m.group(2), st)), body)
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


def _table(header: str, delimiter: str, rows: list[str], st: _Styles = _DEFAULT) -> str:
    heads = _cells(header)
    aligns = _alignments(delimiter)

    def align(idx: int) -> str:
        return aligns[idx] if idx < len(aligns) else "left"

    parts = [f'<table {_PLAIN_TABLE} style="{_TABLE}">']
    parts.append("<tr>")
    for idx, cell in enumerate(heads):
        parts.append(f'<th style="{st.head_cell}text-align:{align(idx)};">{_inline(cell, st)}</th>')
    parts.append("</tr>")
    for n, row in enumerate(rows):
        cells = _cells(row)
        # Ragged rows are common when a model lays a table out by hand: pad a
        # short one rather than dropping the row, and keep the extras on a long
        # one rather than silently losing a value.
        while len(cells) < len(heads):
            cells.append("")
        shade = f"background-color:{st.theme.zebra};" if n % 2 else ""
        parts.append("<tr>")
        for idx, cell in enumerate(cells):
            parts.append(
                f'<td style="{st.cell}{shade}text-align:{align(idx)};">{_inline(cell, st)}</td>'
            )
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _list(
    items: list[tuple[int, bool, str]], start: int, indent: int, st: _Styles = _DEFAULT
) -> tuple[str, int]:
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
            nested, i = _list(items, i, depth, st)
            # A nested list belongs inside the item above it; the item is still
            # open, so it closes after the nested list does.
            parts.append(nested + "</li>")
            continue
        if parts[-1].startswith("<li"):
            parts[-1] += "</li>"
        parts.append(f'<li style="{st.li}">{_inline(text, st)}')
        i += 1
    if parts[-1].startswith("<li"):
        parts[-1] += "</li>"
    parts.append(f"</{tag}>")
    return "".join(parts), i


def markdown_to_html(text: str, theme: MailTheme | None = None) -> str:
    """The markdown subset the report agents write, as inline-styled HTML."""
    st = _styles(theme) if theme is not None else _DEFAULT
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
                f'<div style="{st.heading[level]}">{_inline(heading.group(2).strip(), st)}</div>'
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
            out.append(_table(header, delimiter, rows, st))
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
                quoted.append(_inline(_QUOTE_RE.match(lines[i]).group(1).strip(), st))
                i += 1
            out.append(
                f'<table {_PLAIN_TABLE} style="border-collapse:collapse;width:100%;'
                f'margin:4px 0 12px 0;"><tr><td style="border-left:3px solid #c3ccd6;'
                f'padding:4px 0 4px 12px;{st.body_text}color:{INK_SOFT};">'
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
            rendered, _ = _list(items, 0, items[0][0], st)
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
            chunk.append(_inline(lines[i].strip(), st))
            i += 1
        out.append(
            f'<p style="{st.body_text}margin:0 0 10px 0;">' + "<br>".join(chunk) + "</p>"
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


def _callout(verdict_html: str, status: str, st: _Styles = _DEFAULT) -> str:
    bar, tint = _STATUS.get((status or "").strip().lower(), _NEUTRAL)
    return (
        f'<tr><td style="padding:20px 24px 0 24px;">'
        f'<table {_PLAIN_TABLE} style="border-collapse:collapse;width:100%;">'
        f'<tr><td width="4" style="width:4px;background-color:{bar};font-size:0;'
        f'line-height:0;">&nbsp;</td>'
        f'<td style="background-color:{tint};padding:12px 16px;{st.body_text}">'
        f"{verdict_html}</td></tr></table></td></tr>"
    )


def _band(title: str, subline: str, st: _Styles) -> str:
    """The header band: optional logo or org name, the title, the subline."""
    theme = st.theme
    title_html = (
        f'<div style="font-family:{st.font};font-size:19px;font-weight:600;'
        f'color:{theme.band_text};">{html.escape(title or "", quote=False)}</div>'
    )
    if (subline or "").strip():
        title_html += (
            f'<div style="font-family:{st.font};font-size:12px;color:{theme.band_sub};'
            f'padding-top:5px;">{html.escape(subline.strip(), quote=False)}</div>'
        )
    if theme.logo_url:
        # A two-cell table rather than a float: Word's engine ignores floats.
        # The explicit height attribute is what Outlook honours; the style
        # keeps other clients from stretching the image.
        logo = (
            f'<img src="{html.escape(theme.logo_url, quote=True)}" '
            f'alt="{html.escape(theme.org_name, quote=True)}" height="28" border="0" '
            f'style="display:block;height:28px;width:auto;border:0;outline:none;'
            f'text-decoration:none;">'
        )
        inner = (
            f'<table {_PLAIN_TABLE} style="border-collapse:collapse;"><tr>'
            f'<td valign="middle" style="padding:0 14px 0 0;">{logo}</td>'
            f'<td valign="middle">{title_html}</td></tr></table>'
        )
    elif theme.org_name:
        inner = (
            f'<div style="font-family:{st.font};font-size:11px;font-weight:600;'
            f'letter-spacing:0.5px;text-transform:uppercase;color:{theme.band_sub};'
            f'padding-bottom:6px;">{html.escape(theme.org_name, quote=False)}</div>'
            + title_html
        )
    else:
        inner = title_html
    row = f'<tr><td style="background-color:{theme.band};padding:18px 24px;">{inner}</td></tr>'
    if theme.accent:
        row += (
            f'<tr><td height="3" style="height:3px;background-color:{theme.accent};'
            f'font-size:0;line-height:0;">&nbsp;</td></tr>'
        )
    return row


def render_report_html(
    body: str,
    *,
    title: str,
    subline: str = "",
    status: str = "",
    footer: str = "",
    theme: MailTheme | None = None,
) -> str:
    """A report as one Outlook-safe HTML document.

    Nested tables and inline styles throughout, because Outlook on Windows lays
    mail out with Word's engine: a div with padding is a suggestion there, a
    table cell with padding is not.

    ``theme`` overrides the brand surfaces (band, accent, links, headings,
    table shading, shell, font, logo, footer); ``None`` is the default look.
    """
    st = _styles(theme) if theme is not None else _DEFAULT
    theme = st.theme
    verdict_md, rest_md = split_verdict(body)
    verdict_html = markdown_to_html(verdict_md, theme)
    body_html = markdown_to_html(rest_md, theme)

    rows = [_band(title, subline, st)]
    if verdict_html:
        rows.append(_callout(verdict_html, status, st))
    if body_html:
        rows.append(
            f'<tr><td style="padding:18px 24px 24px 24px;{st.body_text}">{body_html}</td></tr>'
        )
    footer_text = (theme.footer or footer or "").strip()
    if footer_text:
        rows.append(
            f'<tr><td style="border-top:1px solid {FOOT_RULE};background-color:{theme.zebra};'
            f'padding:12px 24px;font-family:{st.font};font-size:11px;color:{FOOT_TEXT};">'
            f'{html.escape(footer_text, quote=False)}</td></tr>'
        )

    sheet = "".join(rows)
    return (
        f'<div style="margin:0;padding:0;background-color:{theme.shell};">'
        f'<table {_PLAIN_TABLE} width="100%" style="background-color:{theme.shell};'
        f'border-collapse:collapse;"><tr><td align="center" style="padding:24px 12px;">'
        f'<table {_PLAIN_TABLE} width="640" style="width:640px;max-width:640px;'
        f'background-color:{SHEET};border:1px solid #d8dee6;border-collapse:collapse;">'
        f"{sheet}</table></td></tr></table></div>"
    )
