"""Shared doubles of the ``tests/test_ina_*.py`` suites: SAP's InA service
behind ``httpx.MockTransport``. No network.

* :func:`fixture` reads ``tests/fixtures/ina/``;
* :func:`grid` builds a result grid in the Version2 columnar form SIA
  answered (``run_sia_grid.json``, recorded 2026-11-04);
* :class:`Sap` answers ``GetServerInfo`` (a new CSRF token and session
  cookie per call) and ``GetResponse`` from a queue per kind of request
  (:func:`kind_of`: describe, VariableSubmit, read, Close).

Placeholders only: no real host, destination, user or data belongs here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ina"
INFO_PATH = "/sap/bw/ina/GetServerInfo"
RESPONSE_PATH = "/sap/bw/ina/GetResponse"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _wrapped(values: Any) -> dict[str, Any]:
    """A list as a Version2 grid wraps it."""
    values = list(values)
    return {"Encoding": "None", "Size": len(values), "Values": values}


def grid(
    dimensions: list[str],
    rows: list[list[str]],
    measures: list[str],
    cells: list[dict[str, Any]],
    *,
    texts: list[list[str]] | None = None,
    structure: str = "Measures",
    total: int | None = None,
) -> dict[str, Any]:
    """A result grid in the Version2 columnar form SIA answered
    (``run_sia_grid.json``): per row dimension a member table (the key
    attribute ``<dim>.KEY``, and ``<dim>.LONG_TEXT`` when ``texts`` holds one
    text per row and dimension), the tuples as member indexes, the measure
    structure on Columns without attributes, the cells row-major. ``rows ==
    [[]]`` without dimensions is the one total row of a run without
    ``group_by`` (``Dimensions`` and ``Tuples`` empty, ``TupleCount`` 1).
    ``total`` is ``TupleCountTotal`` (default: the rows given)."""
    row_dimensions: list[dict[str, Any]] = []
    tuples: list[dict[str, Any]] = []
    for position, name in enumerate(dimensions):
        keys: list[str] = []
        names: list[str] = []
        indexes: list[int] = []
        for index, row in enumerate(rows):
            if row[position] not in keys:
                keys.append(row[position])
                if texts is not None:
                    names.append(texts[index][position])
            indexes.append(keys.index(row[position]))
        attributes: list[dict[str, Any]] = [{
            "Name": f"{name}.KEY", "Description": "Internal Key", "IsKey": True,
            "PresentationType": "Key", "Obtainability": "Always", "ValueType": "String",
            "Values": keys, "ValuesException": [0] * len(keys),
        }]
        if texts is not None:
            attributes.append({
                "Name": f"{name}.LONG_TEXT", "Description": "Long Text", "IsKey": False,
                "PresentationType": "LongText", "Obtainability": "Always", "ValueType": "String",
                "Values": names,
            })
        row_dimensions.append({
            "Name": name, "DimensionType": 3, "Attributes": attributes,
            "MemberTypes": _wrapped([0] * len(keys)),
        })
        tuples.append({"MemberIndexes": _wrapped(indexes)})
    count = len(rows)
    return {
        "Grids": [{
            "Axes": [
                {
                    "Name": "ROWS", "Type": "Rows", "Dimensions": row_dimensions,
                    "TupleCount": count, "TupleCountTotal": count if total is None else total,
                    "Tuples": tuples,
                },
                {
                    "Name": "COLUMNS", "Type": "Columns",
                    "Dimensions": [{
                        "Name": structure, "DimensionType": 2, "Attributes": [],
                        "MemberTypes": _wrapped([0] * len(measures)),
                    }],
                    "TupleCount": len(measures), "TupleCountTotal": len(measures),
                    "Tuples": [{"MemberIndexes": _wrapped(range(len(measures)))}],
                },
            ],
            "Cells": {
                "Values": _wrapped(c["Value"] for c in cells),
                "Units": _wrapped(c.get("Unit") or "" for c in cells),
            },
            "CellArraySizes": [count, len(measures)],
        }],
        "Messages": [],
    }


# What SIA answered to a Close (run 8): an information message, no error.
CLOSED = {
    "Messages": [{
        "Type": 0, "MessageClass": "BW_BICS", "Number": 13, "OlapMessageClass": 1,
        "Text": "Query ZQ_SALES_01 (instance ID ina0123456789abcdef) has been closed",
    }]
}


def kind_of(request: httpx.Request) -> str:
    """What a ``GetResponse`` POST is: ``describe``, ``submit``, ``read`` or
    ``close``."""
    body = json.loads(request.content)
    if "Metadata" in body:
        return "describe"
    analytics = body.get("Analytics", {})
    if "Actions" in analytics:
        return "close"
    if analytics.get("ProcessingDirectives") == {"ProcessingStep": "VariableSubmit"}:
        return "submit"
    return "read"


class Sap:
    """SAP behind the destination: ``GetServerInfo`` hands out a new CSRF
    token and session cookie per call; ``GetResponse`` answers from a queue
    per kind of request (``answers`` for a describe or a read,
    ``submit_answers`` for a VariableSubmit, ``close_answers`` for a Close;
    the last entry of a queue repeats). A dict is a 200 JSON answer, an
    ``httpx.Response`` is sent as it is, a callable gets the request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.server_info: Any = fixture("server_info.json")
        self.info_answer: Callable[[httpx.Request], httpx.Response] | None = None
        self.answers: list[Any] = []
        self.submit_answers: list[Any] = [fixture("submit_ok.json")]
        self.close_answers: list[Any] = [CLOSED]
        self.fetches = 0

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    @property
    def gets(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]

    def of_kind(self, kind: str) -> list[httpx.Request]:
        return [r for r in self.posts if kind_of(r) == kind]

    @property
    def reads(self) -> list[httpx.Request]:
        return self.of_kind("read")

    @property
    def submits(self) -> list[httpx.Request]:
        return self.of_kind("submit")

    @property
    def closes(self) -> list[httpx.Request]:
        return self.of_kind("close")

    @property
    def kinds(self) -> list[str]:
        """``GET`` or the kind of each request, in the order sent."""
        return ["GET" if r.method == "GET" else kind_of(r) for r in self.requests]

    def body(self, index: int = -1) -> Any:
        return json.loads(self.posts[index].content)

    def read_body(self, index: int = -1) -> Any:
        return json.loads(self.reads[index].content)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path == INFO_PATH:
            if self.info_answer is not None:
                return self.info_answer(request)
            self.fetches += 1
            return httpx.Response(
                200,
                json=self.server_info,
                headers=[
                    ("x-csrf-token", f"csrf-{self.fetches}"),
                    ("set-cookie", f"SAP_SESSIONID_XYZ_100=sess-{self.fetches}; path=/; HttpOnly"),
                ],
            )
        if request.method == "POST" and request.url.path == RESPONSE_PATH:
            queue = {"submit": self.submit_answers, "close": self.close_answers}.get(
                kind_of(request), self.answers
            )
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            if callable(answer):
                return answer(request)
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(200, json=answer)
        return httpx.Response(404)
