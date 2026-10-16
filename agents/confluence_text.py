"""Confluence storage-format XHTML to plain text, standard library only.

What an agent reads of a page (``get_page`` in :mod:`agents.confluence_tools`).
The storage format is XHTML with Atlassian's ``ac:`` / ``ri:`` elements; this
turns it into text an agent can quote: headings as ``#`` lines, lists as
``- `` / ``1. `` lines, tables as ``| a | b |`` rows with a ``| --- |`` line
after the first row, a macro as ``[macro: name]`` followed by its title and
body text, task lists as ``- [ ]`` / ``- [x]``, a ``ri:`` reference as its
file name or page title in brackets. Scripts, styles, comments, images,
emoticons, control characters and Unicode format characters become nothing.

Nothing is executed and nothing is fetched: the parser is ``HTMLParser`` from
the standard library, fed a string that was checked against
:data:`MAX_HTML_BYTES` first. The text is data written by other people; the
tool's docstring says so to the model, and this module does not try to tell
an instruction from a sentence.
"""

from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser

MAX_HTML_BYTES = 4 * 1024 * 1024


class TooLarge(ValueError):
    """The document is over :data:`MAX_HTML_BYTES`; nothing of it was parsed."""


_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# Elements that end the current line before and after their content.
_BLOCKS = frozenset({
    "p", "div", "blockquote", "hr", "table", "thead", "tbody", "tfoot", "section",
    "article", "header", "footer", "ac:layout", "ac:layout-section", "ac:layout-cell",
    "ac:rich-text-body", "ac:task-list",
})
# Elements that keep their line breaks and indentation.
_PRE = frozenset({"pre", "ac:plain-text-body"})
# Elements whose whole subtree is dropped.
_DROPPED = frozenset({
    "script", "style", "ac:emoticon", "ac:image", "ac:placeholder",
    # A task's id and uuid are bookkeeping, not text.
    "ac:task-id", "ac:task-uuid",
    # New-editor blocks: the attribute values are layout, and the fallback
    # repeats the content of `ac:adf-content`.
    "ac:adf-attribute", "ac:adf-fallback",
})
_ALL_WS_RE = re.compile(r"\s+")
# Elements without an end tag, so they do not count towards nesting.
_VOID = frozenset({"br", "hr", "img", "input", "meta", "link", "col", "wbr", "source"})
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES_RE = re.compile(r"[ \t\r\f\v ]+")


def _clean_chars(text: str) -> str:
    text = _CONTROL_RE.sub("", text)
    # Format characters (zero width, bidi marks) and lone surrogates (a JSON
    # "\ud800" from Confluence would fail the tool-result serialisation).
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cs"))


class _TextBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buf: list[str] = []
        self._skip = 0  # nesting depth inside a dropped element
        self._pre = 0  # nesting depth inside a preformatted element
        self._lists: list[tuple[str, int]] = []  # ("ul" | "ol", items so far)
        self._tables: list[int] = []  # rows emitted per open table
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._status: list[str] | None = None  # text of an open ac:task-status
        self._task_done = False
        self._lead = ""  # a list / task marker waiting for its line's text
        self._inline_tables = 0  # tables open inside a cell: their text joins the cell

    # -- lines ---------------------------------------------------------------

    def _append(self, text: str) -> None:
        (self._cell if self._cell is not None else self._buf).append(text)

    def _flush(self) -> None:
        if self._cell is not None:
            # Inside a table cell a line break is a space: the row is one line.
            self._cell.append(" ")
            return
        text = "".join(self._buf)
        self._buf = []
        line = text.rstrip() if self._pre else _SPACES_RE.sub(" ", text).strip()
        if line:
            self.lines.append(self._lead + line)
            self._lead = ""

    def _break(self) -> None:
        """End the line and leave a blank one (paragraph end)."""
        if self._cell is not None:
            self._cell.append(" ")
            return
        self._flush()
        if self._lists:
            # A paragraph inside a list item ends its line, not the item.
            return
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    # -- parser callbacks ----------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip:
            if tag not in _VOID:
                self._skip += 1
            return
        a = {k: v or "" for k, v in attrs}
        if tag in _DROPPED:
            self._skip = 1
            return
        if tag == "ac:parameter":
            # Only a macro's title is text worth reading; the rest is layout.
            if a.get("ac:name", "").lower() == "title":
                self._append(" ")
            else:
                self._skip = 1
            return
        if tag.startswith("ri:"):
            # A file name or a page title, never `ri:value` (a URL, query
            # string included): the link's own body text says where it goes.
            label = a.get("ri:filename") or a.get("ri:content-title")
            if label:
                self._append(f"[{label}] ")
            return
        if tag in ("ac:structured-macro", "ac:macro"):
            self._flush()
            self._append(f"[macro: {a.get('ac:name') or '?'}]")
            return
        if tag in _HEADINGS:
            self._break()
            self._append("#" * _HEADINGS[tag] + " ")
            return
        if tag == "br":
            self._flush()
            return
        if tag in ("ul", "ol"):
            self._flush()
            self._lists.append((tag, 0))
            return
        if tag == "li":
            self._flush()
            if self._lists:
                kind, n = self._lists[-1]
                self._lists[-1] = (kind, n + 1)
                indent = "  " * (len(self._lists) - 1)
                self._lead = f"{indent}{n + 1}. " if kind == "ol" else f"{indent}- "
            else:
                self._lead = "- "
            return
        if tag == "table":
            if self._cell is not None:
                # A table inside a cell cannot be a second pipe table on the
                # same line: its cells join the enclosing cell as text.
                self._inline_tables += 1
                return
            self._break()
            self._tables.append(0)
            return
        if tag in ("tr", "td", "th") and self._inline_tables:
            self._flush()
            return
        if tag == "tr":
            self._flush()
            self._row = []
            return
        if tag in ("td", "th"):
            if self._row is None:
                self._row = []
            self._cell = []
            return
        if tag == "ac:task":
            self._flush()
            self._task_done = False
            return
        if tag == "ac:task-status":
            self._status = []
            return
        if tag == "ac:task-body":
            self._lead = "- [x] " if self._task_done else "- [ ] "
            return
        if tag in _PRE:
            self._flush()
            self._pre += 1
            return
        if tag in _BLOCKS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag not in _VOID:
                self._skip -= 1
            return
        if tag == "table" and self._inline_tables:
            self._inline_tables -= 1
            self._flush()
            return
        if tag in ("tr", "td", "th") and self._inline_tables:
            self._flush()
            return
        if tag in ("td", "th"):
            if self._cell is not None and self._row is not None:
                # One line per row: a line break or a code block in a cell
                # becomes a space, else the pipe table splits mid-row.
                self._row.append(_ALL_WS_RE.sub(" ", "".join(self._cell)).strip())
            self._cell = None
            return
        if tag == "tr":
            if self._row is not None:
                self.lines.append("| " + " | ".join(self._row) + " |")
                if self._tables:
                    if self._tables[-1] == 0:
                        self.lines.append("| " + " | ".join("---" for _ in self._row) + " |")
                    self._tables[-1] += 1
            self._row = None
            return
        if tag == "table":
            if self._tables:
                self._tables.pop()
            self._break()
            return
        if tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            if self._lists:
                self._flush()
            else:
                self._break()
            return
        if tag == "ac:task-status":
            self._task_done = "".join(self._status or []).strip().lower() == "complete"
            self._status = None
            return
        if tag in _PRE:
            self._flush()
            self._pre = max(0, self._pre - 1)
            return
        if tag in _HEADINGS or tag in ("p", "div", "blockquote", "ac:structured-macro",
                                        "ac:macro"):
            self._break()
            return
        if tag in ("li", "ac:task"):
            self._flush()
            self._lead = ""  # an empty item leaves no marker behind
            return
        if tag in _BLOCKS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._status is not None:
            self._status.append(data)
            return
        if self._pre and self._cell is None:
            pieces = data.split("\n")
            for piece in pieces[:-1]:
                self._append(piece)
                self._flush()
            self._append(pieces[-1])
            return
        self._append(data)


def to_text(document: str) -> str:
    """The plain text of a storage-format document; see the module docstring."""
    text = str(document or "")
    if len(text.encode("utf-8", "surrogatepass")) > MAX_HTML_BYTES:
        raise TooLarge(f"document over {MAX_HTML_BYTES} bytes")
    # A CDATA section (every code macro) is escaped text to this parser:
    # HTMLParser's own CDATA handling differs between Python versions.
    text = _CDATA_RE.sub(lambda m: html.escape(m.group(1), quote=False), text)
    builder = _TextBuilder()
    builder.feed(text)
    builder.close()
    builder._flush()
    out: list[str] = []
    for raw in builder.lines:
        line = _clean_chars(raw).rstrip()
        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    return "\n".join(out).strip()
