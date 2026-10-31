"""Request models and rules of the web chat's personal agents and skills.

One reading for both ways in: the CRUD routes (``agents/webchat/my_routes.py``,
a refusal is the app's 422 without the input) and a bundle import (each item
validated by the same model; a refusal becomes ``{item, field, rule}`` with a
fixed rule text). Text is stored without control characters (tab and line
feed kept; a carriage return becomes a line feed); a description is one
line. Nothing here names a value in a refusal.

Names: ``NAME_RE`` is the addendum's rule plus ``(`` and ``)``, so the name an
import gives on a conflict (``Report (2)``) is itself a valid name that can be
saved again. ``name_key`` is the case-folded name (ASCII only, so it has the
name's length).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator

from agents.webchat.store import one_line

MAX_AGENTS = 50
MAX_SKILLS = 100
NAME_MAX = 64
DESCRIPTION_MAX = 300
INSTRUCTIONS_MAX = 20_000
CONTENT_MAX = 50_000
MAX_AGENT_SKILLS = 20
MAX_CONSULTS = 10
MODEL_MAX = 128
BUNDLE_MAX_BYTES = 1024 * 1024
BUNDLE_FORMAT = "webchat-bundle/1"
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _.()-]{0,63}$"
NAME_RE = re.compile(NAME_PATTERN)

_KEEP = frozenset("\t\n")


def clean_text(value: Any) -> Any:
    """``value`` without control characters (tab and line feed kept, CR and
    the Unicode line/paragraph separators become a line feed); format
    characters other than the zero width joiner (emoji) are dropped too, so a
    bidi override in a shared bundle cannot hide text from its reader.
    A non-string is passed on for the strict type check to refuse."""
    if not isinstance(value, str):
        return value
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for ch in value:
        if ch in _KEEP:
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat in ("Zl", "Zp"):
            out.append("\n")
        elif cat in ("Cc", "Cs") or (cat == "Cf" and ch != "‍"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def _clean_name(value: Any) -> Any:
    return clean_text(value).strip() if isinstance(value, str) else value


def _clean_line(value: Any) -> Any:
    # No cut here: a description over the limit is refused, not shortened.
    return one_line(value, limit=len(value) + 1) if isinstance(value, str) else value


def name_key(name: str) -> str:
    return name.casefold()


Name = Annotated[str, BeforeValidator(_clean_name),
                 Field(min_length=1, max_length=NAME_MAX, pattern=NAME_PATTERN)]
Description = Annotated[str, BeforeValidator(_clean_line), Field(max_length=DESCRIPTION_MAX)]


class SkillRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scope: Literal["admin", "mine"]
    name: Name


class AgentIn(BaseModel):
    """A personal agent as created, replaced (``PUT``) or imported."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: Name
    description: Description = ""
    instructions: Annotated[str, BeforeValidator(clean_text),
                            Field(min_length=1, max_length=INSTRUCTIONS_MAX)]
    skills: list[SkillRef] = Field(default_factory=list, max_length=MAX_AGENT_SKILLS)
    consults: list[Name] = Field(default_factory=list, max_length=MAX_CONSULTS)
    model: Annotated[str, Field(min_length=1, max_length=MODEL_MAX)] | None = None
    # The deep-agent tools (plan, scratchpad, sub-agents); only a developer
    # may switch them on (checked against the caller, not here).
    deep: bool = False

    @field_validator("instructions")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("instructions: must not be empty")
        return value

    @field_validator("skills")
    @classmethod
    def _distinct_skills(cls, value: list[SkillRef]) -> list[SkillRef]:
        seen: set[tuple[str, str]] = set()
        out = []
        for ref in value:
            key = (ref.scope, name_key(ref.name))
            if key not in seen:
                seen.add(key)
                out.append(ref)
        return out

    @field_validator("consults")
    @classmethod
    def _distinct_consults(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class SkillIn(BaseModel):
    """A personal skill as created, replaced or imported."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: Name
    description: Description = ""
    content: Annotated[str, BeforeValidator(clean_text),
                       Field(min_length=1, max_length=CONTENT_MAX)]

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content: must not be empty")
        return value


# ---------------------------------------------------------------------------
# Item errors of an import: fixed rule texts
# ---------------------------------------------------------------------------
RULE_REQUIRED = "required"
RULE_TYPE = "wrong type"
RULE_TOO_LONG = "too long"
RULE_TOO_SHORT = "too short or empty"
RULE_TOO_MANY = "too many entries"
RULE_NAME = "not a valid name"
RULE_NOT_ALLOWED = "not an allowed value"
RULE_UNKNOWN_FIELD = "unknown field"
RULE_INVALID = "invalid"
RULE_NOT_OBJECT = "not an object"
RULE_DUPLICATE = "duplicate name in the bundle"
RULE_LIMIT = "limit of personal agents or skills reached"
RULE_NO_FREE_NAME = "no free name for a renamed copy"

_RULES = {
    "missing": RULE_REQUIRED,
    "string_too_long": RULE_TOO_LONG,
    "string_too_short": RULE_TOO_SHORT,
    "too_long": RULE_TOO_LONG,
    "too_short": RULE_TOO_SHORT,
    "string_pattern_mismatch": RULE_NAME,
    "literal_error": RULE_NOT_ALLOWED,
    "extra_forbidden": RULE_UNKNOWN_FIELD,
    # Only the blank checks of this module raise a ValueError.
    "value_error": RULE_TOO_SHORT,
}
_FIELDS = {"name", "description", "instructions", "skills", "consults", "model", "content",
           "deep"}
_LIST_FIELDS = {"skills", "consults"}
UNKNOWN_FIELD = "<unknown field>"


def item_errors(exc: ValidationError) -> list[tuple[str, str]]:
    """``(field, rule)`` per error: the top-level field when it is one of
    ours (an unknown key is the file's own text), a fixed rule text."""
    out: list[tuple[str, str]] = []
    for err in exc.errors():
        loc = err.get("loc") or ()
        head = loc[0] if loc else ""
        field = head if isinstance(head, str) and head in _FIELDS else UNKNOWN_FIELD
        kind = str(err.get("type", ""))
        if kind.endswith("_type") or kind in ("model_type", "dict_type", "list_type"):
            rule = RULE_TYPE
        elif kind == "too_long" and field in _LIST_FIELDS and len(loc) == 1:
            rule = RULE_TOO_MANY
        else:
            rule = _RULES.get(kind, RULE_INVALID)
        if (field, rule) not in out:
            out.append((field, rule))
    return out


def item_label(kind: str, raw: Any, index: int) -> str:
    """``agent:<name>`` with the item's own name cut to 64 characters and
    made plain one-line text, else ``agent:#<n>`` (1-based)."""
    name = raw.get("name") if isinstance(raw, dict) else None
    if isinstance(name, str):
        shown = one_line(name, NAME_MAX)
        if shown:
            return f"{kind}:{shown}"
    return f"{kind}:#{index + 1}"


def renamed(name: str, taken: set[str]) -> str | None:
    """``name (2)``, ``name (3)`` … the first whose key is not in ``taken``,
    the base cut so the result keeps within :data:`NAME_MAX`."""
    for n in range(2, 1000):
        suffix = f" ({n})"
        base = name[: NAME_MAX - len(suffix)].rstrip()
        candidate = f"{base}{suffix}"
        if NAME_RE.match(candidate) and name_key(candidate) not in taken:
            return candidate
    return None
