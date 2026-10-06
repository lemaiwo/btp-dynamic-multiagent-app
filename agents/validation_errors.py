"""The app's answer to a refused request: 422 without the client's input.

FastAPI's own answer to a ``RequestValidationError`` carries, per error, the
refused value as ``input`` (the whole body when the body is the wrong shape)
and a ``ctx`` that can hold it again. Two things went wrong with that:

* an admin who pastes a credential into a field that validation refuses reads
  it back in the answer, and so does everything that stores response bodies
  (browser history, a proxy log, a support ticket);
* a lone surrogate in the value (a field cut inside an emoji) cannot be
  encoded as UTF-8, so the refusal itself failed and became a 500.

The answer keeps the status and the shape the UIs read field errors from:
``{"detail": [{"loc", "msg", "type"}]}``, nothing else per item.

Scope: every route of the FastAPI app this is installed on, not a list of
prefixes -- a router added later must not have to remember to opt in. Not
covered, because this error never occurs there: the chat is a mounted
sub-application with its own handlers; routes that read the body themselves
(A2A, the session-cookie route, which answers 400) or validate inside the
handler and answer an ``HTTPException`` with a string ``detail`` (the OData
catalogue, the workflow gate).

What can still carry client text, and what is done about it:

* ``msg`` from pydantic. Its messages name the rule, not the value, with the
  exceptions listed in ``_FIXED_MESSAGES``: those error types interpolate the
  input into the text (``union_tag_invalid``: "Input tag '<value>' found
  using ..."), so their ``msg`` is replaced by fixed text. ``type`` stays.
* ``msg`` from a ``ValueError`` raised by one of this app's validators
  ("Value error, ..."). Those are written to name the field
  (``tests/test_admin_validation_errors.py`` pins the ones reachable from
  the admin API).
* ``loc``. Field names and list indexes, except that the key of an unknown
  field (``extra_forbidden``) or of a free-form dict is the client's own
  text. A string part is repeated only when it has the form of a field name;
  anything else is ``<unknown field>`` (the OData catalogue's gate,
  ``agents.odata.models._loc``, does the same).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agents.loc_fields import UNKNOWN_FIELD, loc_field

__all__ = ["UNKNOWN_FIELD", "encodable", "install_validation_handler", "validation_item"]


def encodable(text: str) -> str:
    """``text`` with anything UTF-8 cannot carry (a lone surrogate) replaced."""
    return text.encode("utf-8", "replace").decode("utf-8")


# pydantic error types whose message embeds the input; see the docstring.
_FIXED_MESSAGES = {
    "union_tag_invalid": "Input tag is not one of the expected values",
}

def _loc_part(part: Any) -> int | str:
    if isinstance(part, int) and not isinstance(part, bool):
        return part
    # A key is repeated only when it has the form of a field name.
    return loc_field(part)


def validation_item(err: dict[str, Any]) -> dict[str, Any]:
    """One ``detail`` item: ``loc`` / ``msg`` / ``type`` only.

    ``input`` and ``ctx`` are what the client sent; ``url`` is pydantic's
    documentation link.
    """
    kind = str(err.get("type", ""))
    return {
        "loc": [_loc_part(p) for p in err.get("loc", ())],
        "msg": _FIXED_MESSAGES.get(kind) or encodable(str(err.get("msg", ""))),
        "type": kind,
    }


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 for a refused request, without the client's input."""
    return JSONResponse(
        status_code=422,
        content={"detail": [validation_item(e) for e in exc.errors()]},
    )


def install_validation_handler(app: FastAPI) -> None:
    """Register :func:`validation_error` on the app (a router cannot carry an
    exception handler); ``app.py`` calls it."""
    app.add_exception_handler(RequestValidationError, validation_error)
