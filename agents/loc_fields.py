"""What a refusal of the gates may say, and the one-line rule of their texts.

A validation error names where it is (``loc``). List indexes and field names
are ours; the key of an unknown field or of a free-form dict is the client's
own text -- and a value pasted where a key belongs (a URL, a token) would
come back in the refusal. So a key is repeated only when it has the form of
a field name, and anything else is named ``<unknown field>``.

One rule for every gate that reports a ``loc``: the admin API's validation
handler (``agents.validation_errors``), the workflow step configs
(``agents.step_kinds``), the OData catalogue (``agents.odata.models``,
``agents.odata.admin_routes``), the data catalogue routes and the InA query
gate (``agents.ina.models``). ``refusal_text`` is the one sentence those
gates build from a ``ValidationError``: ``loc: msg`` per problem, never the
input. ``one_line_text`` is the one rule for a text that is printed as one
line (a title, a label). No imports beyond the standard library at run
time, so any of them can use it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the type only: no run-time import of pydantic here
    from pydantic import ValidationError

# At most this many characters of a key are a field name.
MAX_LOC_FIELD_CHARS = 64
# What a location part may look like to be repeated: every field of the
# request models does, and so does an honestly mistyped one.
LOC_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,%d}" % (MAX_LOC_FIELD_CHARS - 1))
UNKNOWN_FIELD = "<unknown field>"
# How many problems one refusal lists; the rest is counted.
MAX_REPORTED_ERRORS = 20

# Never in a one-line text: control characters (C0, DEL, C1: a tab, a line
# feed, an escape) and the line and paragraph separators.
_LINE_BREAKING = frozenset({"Cc", "Zl", "Zp"})
# And, where the caller asks for it, the invisible format characters (Cf):
# zero-width characters, the bidi embeddings and overrides U+202A-202E and
# the isolates U+2066-2069, a byte order mark, a soft hyphen.
_LINE_BREAKING_OR_FORMAT = _LINE_BREAKING | {"Cf"}


def loc_field(part: Any) -> str:
    """``part`` when it is a string with the form of a field name, else
    ``<unknown field>``. ``fullmatch``: a trailing newline does not pass.
    Not for list indexes: the caller decides how it shows an ``int``."""
    if isinstance(part, str) and LOC_FIELD_RE.fullmatch(part):
        return part
    return UNKNOWN_FIELD


def loc_path(parts: tuple[Any, ...], root: str) -> str:
    """Where a problem is, as ``a.0.b``; ``root`` when it is the whole input.

    A list index is repeated as it is, a key only when it has the form of a
    field name (``loc_field``).
    """
    shown = [
        str(part) if isinstance(part, int) and not isinstance(part, bool) else loc_field(part)
        for part in parts
    ]
    return ".".join(shown) or root


def refusal_text(exc: ValidationError, root: str) -> str:
    """``loc: msg`` per problem, joined by ``; `` -- never the input.

    At most ``MAX_REPORTED_ERRORS`` problems are named, the rest is counted
    (``and 3 more``): a refusal is echoed in a 422 and logged, and a client
    that sends a thousand bad rows must not get a thousand lines back. The
    error list is read without its input, context and URL, because a field
    is where a URL with a token in it gets pasted by mistake. ``root`` names
    a problem of the whole input (``service``, ``body``, ``ina_query``).

    A caller raises its own exception from this text ``from None``: the
    ``ValidationError`` carries the input in its repr.
    """
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    lines = [f"{loc_path(err['loc'], root)}: {err['msg']}" for err in errors[:MAX_REPORTED_ERRORS]]
    if len(errors) > MAX_REPORTED_ERRORS:
        lines.append(f"and {len(errors) - MAX_REPORTED_ERRORS} more")
    return "; ".join(lines)


def one_line_text(value: str, *, refuse_format: bool = False, field: str | None = None) -> str:
    """``value`` when it is one line of text, else a ``ValueError``.

    These texts are printed as one line each -- a service's ``purpose`` in an
    agent's instructions, titles, labels and value meanings in a tool's
    answer, a row of an admin table -- so a line break in one of them would
    start a line of its own there, which reads as something the catalogue
    did not say. With ``refuse_format`` the invisible format characters are
    refused as well, for text that is not the admin's own (names and labels
    SAP described): a bidi override reorders how the line reads, a
    zero-width character hides a difference between two names. The message
    names ``field`` and the rule, never the text.
    """
    refused = _LINE_BREAKING_OR_FORMAT if refuse_format else _LINE_BREAKING
    if any(unicodedata.category(ch) in refused for ch in value):
        what = "control or format characters" if refuse_format else "control characters"
        subject = f"{field} must" if field else "must"
        raise ValueError(f"{subject} be one line of text without {what}")
    return value


def one_line(value: str, info: Any) -> str:
    """``one_line_text`` as a pydantic validator (``info.field_name`` is named)."""
    return one_line_text(value, field=info.field_name)


def one_line_strict(value: str, info: Any) -> str:
    """``one_line_text`` with ``refuse_format``, as a pydantic validator."""
    return one_line_text(value, refuse_format=True, field=info.field_name)
