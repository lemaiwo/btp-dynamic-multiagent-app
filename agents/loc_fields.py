"""Which key of a refused request may be repeated in the answer.

A validation error names where it is (``loc``). List indexes and field names
are ours; the key of an unknown field or of a free-form dict is the client's
own text -- and a value pasted where a key belongs (a URL, a token) would
come back in the refusal. So a key is repeated only when it has the form of
a field name, and anything else is named ``<unknown field>``.

One rule for every gate that reports a ``loc``: the admin API's validation
handler (``agents.validation_errors``), the workflow step configs
(``agents.step_kinds``) and the OData catalogue (``agents.odata.models``,
``agents.odata.admin_routes``). No imports beyond the standard library, so
any of them can use it.
"""

from __future__ import annotations

import re
from typing import Any

# At most this many characters of a key are a field name.
MAX_LOC_FIELD_CHARS = 64
# What a location part may look like to be repeated: every field of the
# request models does, and so does an honestly mistyped one.
LOC_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,%d}" % (MAX_LOC_FIELD_CHARS - 1))
UNKNOWN_FIELD = "<unknown field>"


def loc_field(part: Any) -> str:
    """``part`` when it is a string with the form of a field name, else
    ``<unknown field>``. ``fullmatch``: a trailing newline does not pass.
    Not for list indexes: the caller decides how it shows an ``int``."""
    if isinstance(part, str) and LOC_FIELD_RE.fullmatch(part):
        return part
    return UNKNOWN_FIELD
