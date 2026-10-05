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
``{"detail": [{"loc", "msg", "type"}]}``, nothing else per item. It applies
to every route of the app, not to a list of prefixes: a route added later
must not have to remember to opt in.

``msg`` is pydantic's text, or the text of a ``ValueError`` one of this app's
validators raised. Pydantic's own messages name the rule, not the value; the
app's validators are written to name the field (``tests/
test_admin_validation_errors.py`` pins the ones in ``agents/admin.py``).
Routes that validate inside the handler and answer an ``HTTPException`` with
a string ``detail`` (the OData catalogue, workflows) are not touched here.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def encodable(text: str) -> str:
    """``text`` with anything UTF-8 cannot carry (a lone surrogate) replaced."""
    return text.encode("utf-8", "replace").decode("utf-8")


def validation_item(err: dict[str, Any]) -> dict[str, Any]:
    """One ``detail`` item: ``loc`` / ``msg`` / ``type`` only.

    ``input`` and ``ctx`` are what the client sent; ``url`` is pydantic's
    documentation link. A ``loc`` part is a field name or an index -- a dict
    key the client chose can appear there, so it is made encodable too.
    """
    return {
        "loc": [p if isinstance(p, int) else encodable(str(p)) for p in err.get("loc", ())],
        "msg": encodable(str(err.get("msg", ""))),
        "type": str(err.get("type", "")),
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
