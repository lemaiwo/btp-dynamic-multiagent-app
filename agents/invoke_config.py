"""The one reading of an agent's invoke settings. Standard library only.

Storage (``agents/db.py``) and the invoke API (``agents/invoke.py``) both call
this module, so neither accepts what the other refuses. Two settings live on
``AgentConfig``:

* ``invoke_limits_json``: what the invoke API accepts as attachments,
  ``{"media_types": [...], "max_file_bytes": n, "max_files": n,
  "max_pdf_pages": n}``. Every key is optional and falls back to its default;
  unknown keys are dropped. A media type is one of the three this module can
  recognise by its magic bytes (PNG, JPEG, PDF): a type it cannot sniff could
  only be taken on the client's word.
* ``result_schema_json``: the JSON schema ``submit_result`` checks the
  agent's structured result against.

Why a schema subset of our own and not ``jsonschema``: that package is in the
virtualenv only as a dependency of ``mcp``, not pinned by this app, and a
keyword the validator does not enforce would be a promise nothing keeps. So
the schema is refused at save when it uses any keyword outside the subset
below, and the subset is enforced in full:

    type (one name or a list of names: object, array, string, integer,
    number, boolean, null), properties, required, additionalProperties
    (a boolean or a schema), items (one schema), enum (scalars), minLength,
    maxLength, minimum, maximum, minItems, maxItems.

Annotations are allowed and ignored: title, description, $schema, $id,
$comment, examples, default, format (an annotation in JSON Schema 2020-12 as
well). The root must be ``type: object``. A refusal names the rule, never a
value of the schema or of the result.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

PNG = "image/png"
JPEG = "image/jpeg"
PDF = "application/pdf"
SNIFFABLE_MEDIA_TYPES = (PNG, JPEG, PDF)

DEFAULT_MEDIA_TYPES: tuple[str, ...] = (PNG, JPEG, PDF)
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_FILES = 5
DEFAULT_MAX_PDF_PAGES = 20

# Ceilings an admin cannot raise: the attachments are held in memory for the
# whole run, and a model provider refuses far larger documents anyway.
CEILING_MAX_FILE_BYTES = 25 * 1024 * 1024
CEILING_MAX_FILES = 10
CEILING_MAX_PDF_PAGES = 100

LIMIT_KEYS = ("media_types", "max_file_bytes", "max_files", "max_pdf_pages")

MAX_SCHEMA_CHARS = 32_000
MAX_SCHEMA_DEPTH = 10
MAX_PROBLEMS = 20

_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems",
})
_ANNOTATIONS = frozenset({
    "title", "description", "$schema", "$id", "$comment", "examples", "default", "format",
})
SUPPORTED_SUBSET = (
    "type, properties, required, additionalProperties, items, enum, minLength, "
    "maxLength, minimum, maximum, minItems, maxItems"
)


class InvokeConfigError(ValueError):
    """A refused setting. The text names the field and the rule only."""


@dataclass(frozen=True)
class InvokeLimits:
    media_types: tuple[str, ...] = DEFAULT_MEDIA_TYPES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_files: int = DEFAULT_MAX_FILES
    max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES


def _count(value: Any, ceiling: int) -> int | None:
    """A positive integer no larger than ``ceiling`` (a bool is no integer)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= ceiling else None


def clean_limits(value: Any) -> dict[str, Any] | None:
    """The limits object as stored: unknown keys dropped, every known key
    checked; ``None`` for no object. ``InvokeConfigError`` for a value that
    is not allowed (the text names the key and the rule)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvokeConfigError("invoke_limits: must be an object")
    out: dict[str, Any] = {}
    if "media_types" in value:
        types = value["media_types"]
        if (not isinstance(types, list) or not types
                or not all(isinstance(t, str) and t in SNIFFABLE_MEDIA_TYPES for t in types)):
            raise InvokeConfigError(
                "invoke_limits.media_types: must be a non-empty list of "
                f"{', '.join(SNIFFABLE_MEDIA_TYPES)}"
            )
        out["media_types"] = list(dict.fromkeys(types))
    for key, ceiling in (("max_file_bytes", CEILING_MAX_FILE_BYTES),
                         ("max_files", CEILING_MAX_FILES),
                         ("max_pdf_pages", CEILING_MAX_PDF_PAGES)):
        if key in value:
            number = _count(value[key], ceiling)
            if number is None:
                raise InvokeConfigError(
                    f"invoke_limits.{key}: must be an integer from 1 to {ceiling}"
                )
            out[key] = number
    return out


def parse_limits(raw: str | None) -> InvokeLimits:
    """The limits of a stored row, each key falling back to its default when
    absent or not valid (a row written by hand never widens a ceiling)."""
    if not raw:
        return InvokeLimits()
    try:
        data = json.loads(raw)
    except ValueError:
        return InvokeLimits()
    if not isinstance(data, dict):
        return InvokeLimits()
    types = data.get("media_types")
    media = DEFAULT_MEDIA_TYPES
    if (isinstance(types, list) and types
            and all(isinstance(t, str) and t in SNIFFABLE_MEDIA_TYPES for t in types)):
        media = tuple(dict.fromkeys(types))
    return InvokeLimits(
        media_types=media,
        max_file_bytes=_count(data.get("max_file_bytes"), CEILING_MAX_FILE_BYTES)
        or DEFAULT_MAX_FILE_BYTES,
        max_files=_count(data.get("max_files"), CEILING_MAX_FILES) or DEFAULT_MAX_FILES,
        max_pdf_pages=_count(data.get("max_pdf_pages"), CEILING_MAX_PDF_PAGES)
        or DEFAULT_MAX_PDF_PAGES,
    )


def loads_or_none(raw: str | None) -> Any:
    """A stored JSON column for an answer; ``None`` when empty or malformed."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# result_schema
# ---------------------------------------------------------------------------
def _schema_error(rule: str) -> InvokeConfigError:
    return InvokeConfigError(f"result_schema: {rule}")


def _check_node(node: Any, depth: int) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise _schema_error(f"nested deeper than {MAX_SCHEMA_DEPTH} levels")
    if isinstance(node, bool):
        return  # true / false are schemas too
    if not isinstance(node, dict):
        raise _schema_error("every schema must be an object or a boolean")
    for key in node:
        if key not in _KEYWORDS and key not in _ANNOTATIONS:
            raise _schema_error(
                f"uses a keyword outside the supported subset ({SUPPORTED_SUBSET})"
            )
    if "type" in node:
        kinds = node["type"] if isinstance(node["type"], list) else [node["type"]]
        if not kinds or not all(isinstance(k, str) and k in _TYPES for k in kinds):
            raise _schema_error("type must name JSON types")
    if "properties" in node:
        props = node["properties"]
        if not isinstance(props, dict):
            raise _schema_error("properties must be an object")
        for sub in props.values():
            _check_node(sub, depth + 1)
    if "required" in node:
        req = node["required"]
        if not isinstance(req, list) or not all(isinstance(r, str) for r in req):
            raise _schema_error("required must be a list of property names")
    if "additionalProperties" in node:
        _check_node(node["additionalProperties"], depth + 1)
    if "items" in node:
        _check_node(node["items"], depth + 1)
    if "enum" in node:
        enum = node["enum"]
        if (not isinstance(enum, list) or not enum or not all(
                v is None or isinstance(v, (str, int, float, bool)) for v in enum)):
            raise _schema_error("enum must be a non-empty list of scalar values")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in node and (isinstance(node[key], bool) or not isinstance(node[key], int)
                            or node[key] < 0):
            raise _schema_error(f"{key} must be a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in node and (isinstance(node[key], bool)
                            or not isinstance(node[key], (int, float))
                            or not math.isfinite(node[key])):
            raise _schema_error(f"{key} must be a number")


def check_result_schema(value: Any) -> dict[str, Any] | None:
    """The schema as it is stored (unchanged), or ``None`` for none.
    ``InvokeConfigError`` for a schema outside the subset."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _schema_error("must be an object")
    if len(json.dumps(value)) > MAX_SCHEMA_CHARS:
        raise _schema_error(f"longer than {MAX_SCHEMA_CHARS} characters")
    if value.get("type") != "object":
        raise _schema_error('the root must have "type": "object"')
    _check_node(value, 0)
    return value


def _type_of(value: Any) -> set[str]:
    if value is None:
        return {"null"}
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"integer", "number"} if value.is_integer() else {"number"}
    if isinstance(value, str):
        return {"string"}
    if isinstance(value, list):
        return {"array"}
    if isinstance(value, dict):
        return {"object"}
    return set()


def _same(a: Any, b: Any) -> bool:
    """Enum equality that does not take True for 1."""
    return type(a) is type(b) and a == b or (
        not isinstance(a, bool) and not isinstance(b, bool)
        and isinstance(a, (int, float)) and isinstance(b, (int, float)) and a == b
    )


def validate(schema: Any, value: Any, path: str = "$") -> list[str]:
    """The problems of ``value`` against ``schema`` (a checked schema), as
    ``"<path>: <rule>"`` lines; empty when valid. A path is built from the
    schema's own property names and array positions; a property the schema
    does not allow is reported without its name."""
    problems: list[str] = []
    _validate(schema, value, path, problems)
    return problems[:MAX_PROBLEMS]


def _validate(schema: Any, value: Any, path: str, problems: list[str]) -> None:
    if len(problems) >= MAX_PROBLEMS:
        return
    if schema is True:
        return
    if schema is False:
        problems.append(f"{path}: no value is allowed here")
        return
    if not isinstance(schema, dict):
        return
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not _type_of(value) & set(kinds):
            problems.append(f"{path}: must be of type {' or '.join(kinds)}")
            return
    if "enum" in schema and not any(_same(value, v) for v in schema["enum"]):
        problems.append(f"{path}: must be one of the allowed values")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path}: longer than {schema['maxLength']} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: above the maximum {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(schema["items"], item, f"{path}[{index}]", problems)
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                problems.append(f"{path}.{name}: is required")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                _validate(props[key], item, f"{path}.{key}", problems)
            elif extra is False:
                problems.append(f"{path}: has a property the schema does not allow")
            elif isinstance(extra, dict):
                _validate(extra, item, f"{path}.*", problems)
