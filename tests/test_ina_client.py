"""The InA client (``agents/ina/client.py``): what it sends, what it reads back.

The client runs on the real ``destination_http_client`` (``DestinationAuth``,
and for the OnPremise case the ``OnPremiseRouter``); only the destination
service, the connectivity tokens and SAP behind them are doubles
(``httpx.MockTransport``). The answers are the synthetic fixtures in
``tests/fixtures/ina/``, shaped after what SIA answered the probe
(``scripts/probe_ina.py``): ``GetServerInfo`` with its services and
capabilities, a query description, a result grid, and the message of an
unknown query.

Covered: the CSRF token is fetched once per (destination, user) with the
``GetServerInfo`` call and sent with every ``GetResponse``; a 403 re-fetches it
once; ``describe`` maps SAP's description onto the save-time gate's
``InaDefinition``; ``run`` sends the ``Analytics`` request exactly as pinned
and reads rows and units from the grid; every cap and every argument check
refuses before anything is sent; and every failure is one code of the closed
list with a text in which nothing of SAP's answer but a cut, masked message
text travels.

Run:  python -m pytest tests/test_ina_client.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.destination import DestinationError  # noqa: E402
from agents.destination_auth import destination_http_client  # noqa: E402
from agents.ina import client as ina_client  # noqa: E402
from agents.ina.client import (  # noqa: E402
    MAX_CELLS,
    MAX_DIMENSIONS,
    MAX_FILTERS,
    MAX_MEASURES,
    MAX_TOP,
    InaClient,
    InaSessionStore,
)
from agents.ina.errors import ERROR_CODES, InaError  # noqa: E402
from agents.ina.models import InaDefinition, validate_ina_query  # noqa: E402
from tests.odata_helpers import FakeConnectivity, FakeResolver, OnPremiseResolver  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ina"
DESTINATION = "SIA_INA"
QUERY = "ZQ_SALES_01"
SIA_URL = "https://sia.example.test:44300"
INFO_PATH = "/sap/bw/ina/GetServerInfo"
RESPONSE_PATH = "/sap/bw/ina/GetResponse"
CAPABILITIES = [
    "AggregationNOPNULL",
    "ClientCapabilities",
    "ResultSetCellFormatString",
    "SupportsComplexFilters",
    "Variables",
]
ALICE = "alice@example.com"
BOB = "bob@example.com"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def definition() -> InaDefinition:
    """The definition ``describe`` reads from ``describe_ok.json``."""
    return InaDefinition.model_validate(
        {
            "dimensions": [
                {"name": "0CALMONTH", "label": "Calendar Year/Month", "key_field": "0CALMONTH.KEY"},
                {"name": "ZREGION", "label": "Sales region", "key_field": "ZREGION.KEY"},
            ],
            "measures": [
                {"name": "0AMOUNT", "label": "Net amount", "unit": "EUR"},
                {"name": "0QUANTITY", "label": "Quantity", "unit": "PC"},
            ],
            "variables": [
                {
                    "name": "0P_CALYEAR",
                    "label": "Calendar year",
                    "mandatory": True,
                    "type": "single",
                },
                {"name": "ZV_REGION", "label": "Regions", "mandatory": False, "type": "multiple"},
                {
                    "name": "ZV_PERIOD",
                    "label": "Period range",
                    "mandatory": False,
                    "type": "interval",
                },
            ],
        }
    )


def grid(
    dimensions: list[str],
    rows: list[list[str]],
    measures: list[str],
    cells: list[dict[str, Any]],
) -> dict[str, Any]:
    """A result grid in the shape of ``run_ok.json``."""
    return {
        "Grids": [
            {
                "Axes": [
                    {
                        "Name": "Rows",
                        "Type": 1,
                        "Dimensions": [{"Name": d} for d in dimensions],
                        "Tuples": [{"Members": [{"Name": key} for key in row]} for row in rows],
                    },
                    {
                        "Name": "Columns",
                        "Type": 2,
                        "Dimensions": [{"Name": "Measures"}],
                        "Tuples": [{"Members": [{"Name": m}]} for m in measures],
                    },
                ],
                "Cells": cells,
            }
        ],
        "Messages": [],
    }


class Sap:
    """SAP behind the destination: ``GetServerInfo`` hands out a new CSRF
    token and session cookie per call; ``GetResponse`` answers from a queue
    (a dict is a 200 JSON answer, an ``httpx.Response`` is sent as it is, a
    callable gets the request)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.server_info: Any = fixture("server_info.json")
        self.info_answer: Callable[[httpx.Request], httpx.Response] | None = None
        self.answers: list[Any] = []
        self.fetches = 0

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    @property
    def gets(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]

    def body(self, index: int = -1) -> Any:
        return json.loads(self.posts[index].content)

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
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            if callable(answer):
                return answer(request)
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(200, json=answer)
        return httpx.Response(404)


def make(
    sap: Sap,
    *,
    user_context: bool = False,
    resolver: Any = None,
    store: InaSessionStore | None = None,
    known: InaDefinition | None = None,
    package: str = "",
) -> InaClient:
    resolver = resolver or FakeResolver(url=SIA_URL, name=DESTINATION)
    http = destination_http_client(
        resolver,
        user_context=user_context,
        server_key="builtin:ina",
        expected_hosts=(),
        connectivity=None,
        transport=httpx.MockTransport(sap.handler),
    )
    return InaClient(
        http,
        store if store is not None else InaSessionStore(),
        destination=DESTINATION,
        user_context=user_context,
        query=QUERY,
        package=package,
        definition=known,
    )


class signed_in:
    """A bound user, as the request middleware leaves it."""

    def __init__(self, principal: str) -> None:
        self.principal = principal

    def __enter__(self) -> None:
        self._jwt = current_jwt.set(f"jwt-of-{self.principal}")
        self._principal = current_principal.set(self.principal)

    def __exit__(self, *exc: Any) -> None:
        current_jwt.reset(self._jwt)
        current_principal.reset(self._principal)


RUN = {
    "measures": ["0AMOUNT", "0QUANTITY"],
    "group_by": ["0CALMONTH"],
    "variables": {"0P_CALYEAR": "2026"},
}


async def refused(coro: Any) -> InaError:
    with pytest.raises(InaError) as info:
        await coro
    return info.value


# ----------------------------------------------------------------- the error list


def test_the_error_codes_are_the_closed_list_of_the_design():
    assert ERROR_CODES == (
        "destination_error",
        "proxy_refused",
        "user_token_required",
        "unauthorized",
        "forbidden",
        "csrf_failed",
        "query_unknown",
        "variable_required",
        "invalid_filter",
        "result_too_large",
        "sap_error",
        "unreachable",
        "timeout",
        "not_json",
    )


def test_an_error_outside_the_list_cannot_be_made():
    with pytest.raises(ValueError):
        InaError("not_a_code", "x")
    error = InaError("sap_error", "the text", hint="a hint")
    assert error.to_dict() == {"code": "sap_error", "message": "the text", "hint": "a hint"}
    assert InaError("timeout", "t").to_dict() == {"code": "timeout", "message": "t"}


def test_the_caps_are_the_designs():
    assert (MAX_CELLS, MAX_DIMENSIONS, MAX_MEASURES, MAX_FILTERS, MAX_TOP) == (
        5000,
        20,
        20,
        30,
        2000,
    )


# --------------------------------------------------------------- CSRF and session


async def test_server_info_is_fetched_once_per_destination_and_user_and_its_token_reused():
    sap = Sap()
    sap.answers = [fixture("describe_ok.json"), fixture("run_ok.json")]
    store = InaSessionStore()
    await make(sap, store=store).describe()
    await make(sap, store=store, known=definition()).run(**RUN)
    (get,) = sap.gets
    assert get.url.path == INFO_PATH and get.headers["X-CSRF-Token"] == "Fetch"
    assert "cookie" not in get.headers
    assert len(sap.posts) == 2
    for post in sap.posts:
        assert post.url.path == RESPONSE_PATH
        assert post.headers["X-CSRF-Token"] == "csrf-1"
        assert post.headers["Cookie"] == "SAP_SESSIONID_XYZ_100=sess-1"
        assert post.headers["Content-Type"] == "application/json"
        # The destination's credential travels as for any destination request.
        assert post.headers["Authorization"] == "Bearer dest-token"
        assert post.url.host == "sia.example.test"


async def test_each_signed_in_user_has_a_session_of_their_own():
    sap = Sap()
    sap.answers = [fixture("run_ok.json")]
    store = InaSessionStore()
    with signed_in(ALICE):
        await make(sap, user_context=True, store=store, known=definition()).run(**RUN)
        await make(sap, user_context=True, store=store, known=definition()).run(**RUN)
    with signed_in(BOB):
        await make(sap, user_context=True, store=store, known=definition()).run(**RUN)
    assert len(sap.gets) == 2
    tokens = [p.headers["X-CSRF-Token"] for p in sap.posts]
    assert tokens == ["csrf-1", "csrf-1", "csrf-2"]
    assert [p.headers["Authorization"] for p in sap.posts] == [
        f"Bearer user-token-of-{ALICE}",
        f"Bearer user-token-of-{ALICE}",
        f"Bearer user-token-of-{BOB}",
    ]


async def test_the_shared_http_client_keeps_no_cookie():
    sap = Sap()
    sap.answers = [fixture("describe_ok.json")]
    client = make(sap)
    await client.describe()
    assert len(client._http.cookies.jar) == 0


async def test_a_403_on_the_post_fetches_the_token_again_once():
    sap = Sap()
    sap.answers = [
        httpx.Response(403, headers={"x-csrf-token": "Required"}, text="CSRF validation failed"),
        fixture("run_ok.json"),
    ]
    out = await make(sap, known=definition()).run(**RUN)
    assert out["row_count"] == 3
    assert [r.method for r in sap.requests] == ["GET", "POST", "GET", "POST"]
    assert [p.headers["X-CSRF-Token"] for p in sap.posts] == ["csrf-1", "csrf-2"]


async def test_a_second_403_is_csrf_failed():
    sap = Sap()
    sap.answers = [httpx.Response(403, headers={"x-csrf-token": "Required"})]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "csrf_failed"
    assert [r.method for r in sap.requests] == ["GET", "POST", "GET", "POST"]


async def test_a_403_that_is_no_csrf_refusal_is_forbidden_after_the_one_retry():
    sap = Sap()
    sap.answers = [httpx.Response(403, text="<html>no authorisation</html>")]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "forbidden"
    assert "html" not in error.message and "authorisation" not in error.message
    assert len(sap.posts) == 2


async def test_server_info_without_a_token_is_csrf_failed():
    sap = Sap()
    sap.info_answer = lambda request: httpx.Response(200, json=fixture("server_info.json"))
    error = await refused(make(sap).describe())
    assert error.code == "csrf_failed"
    assert sap.posts == []


async def test_server_info_without_the_analytics_service_is_sap_error():
    sap = Sap()
    sap.server_info = {"Services": [{"Service": "Catalog", "Capabilities": []}]}
    error = await refused(make(sap).describe())
    assert error.code == "sap_error"
    assert sap.posts == []


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "unauthorized"), (403, "forbidden"), (404, "sap_error"), (500, "sap_error")],
)
async def test_a_refused_server_info_maps_to_its_code(status, code):
    sap = Sap()
    sap.info_answer = lambda request: httpx.Response(status, text="<html>secret page</html>")
    error = await refused(make(sap).describe())
    assert error.code == code
    assert "secret" not in error.message
    assert sap.posts == []


async def test_a_redirect_is_not_followed():
    sap = Sap()
    sap.info_answer = lambda request: httpx.Response(
        302, headers={"location": "https://idp.example.test/login?token=abc"}
    )
    error = await refused(make(sap).describe())
    assert error.code == "sap_error"
    assert len(sap.requests) == 1
    assert "idp" not in error.message and "abc" not in error.message


# ---------------------------------------------------------------------- describe


async def test_describe_maps_the_answer_to_the_definition():
    sap = Sap()
    sap.answers = [fixture("describe_ok.json")]
    described = await make(sap, package="ZPKG").describe()
    assert described.definition == definition()
    assert described.skipped == [] and described.truncated is False
    # What the save-time gate takes: the description is stored through it.
    stored = validate_ina_query(
        {
            "name": "sales",
            "title": "Sales",
            "purpose": "Sales by month",
            "destination": DESTINATION,
            "query": QUERY,
            "definition": described.definition.model_dump(),
        }
    )
    assert stored.definition == definition()
    body = sap.body()
    assert body == {
        "ClientInfo": ina_client.CLIENT_INFO,
        "Metadata": {
            "Capabilities": CAPABILITIES,
            "Context": "Analytics",
            "DataSource": {"ObjectName": QUERY, "PackageName": "ZPKG", "Type": "Query"},
            "Expand": ina_client.DESCRIBE_EXPAND,
            "Language": "EN",
        },
        "Options": ["StatefulServer"],
    }


async def test_describe_of_an_unknown_query_is_query_unknown():
    sap = Sap()
    sap.answers = [fixture("query_unknown.json")]
    error = await refused(make(sap).describe())
    assert error.code == "query_unknown"
    assert "unbekannt" not in error.message


async def test_describe_of_an_answer_without_a_cube_is_sap_error():
    sap = Sap()
    sap.answers = [{"Something": "else"}]
    error = await refused(make(sap).describe())
    assert error.code == "sap_error"


# --------------------------------------------------------------------------- run


async def test_run_sends_the_analytics_request_exactly():
    sap = Sap()
    sap.answers = [
        grid(
            ["0CALMONTH", "ZREGION"],
            [["202601", "NORTH"], ["202601", "SOUTH"]],
            ["0AMOUNT"],
            [{"Value": 1.5, "Unit": "EUR"}, {"Value": 2, "Unit": "EUR"}],
        )
    ]
    await make(sap, known=definition(), package="ZPKG").run(
        measures=["0AMOUNT"],
        group_by=["0CALMONTH", "ZREGION"],
        filters=[
            {"dimension": "ZREGION", "operator": "in", "value": ["NORTH", "SOUTH"]},
            {"dimension": "0CALMONTH", "operator": "between", "value": ["202601", "202603"]},
            {"dimension": "ZREGION", "operator": "<>", "value": "WEST"},
            {"dimension": "0CALMONTH", "operator": ">=", "value": 202601},
        ],
        top=50,
        order=[{"by": "0AMOUNT", "direction": "desc"}, {"by": "0CALMONTH"}],
        variables={"0P_CALYEAR": "2026", "ZV_REGION": ["NORTH"], "ZV_PERIOD": ["01", "06"]},
    )
    assert sap.body() == {
        "ClientInfo": ina_client.CLIENT_INFO,
        "Analytics": {
            "Capabilities": CAPABILITIES,
            "DataSource": {"ObjectName": QUERY, "PackageName": "ZPKG", "Type": "Query"},
            "Definition": {
                "Dimensions": [
                    {"Name": "0CALMONTH", "Axis": "Rows"},
                    {"Name": "ZREGION", "Axis": "Rows"},
                    {
                        "Name": "Measures",
                        "Axis": "Columns",
                        "Members": [{"Name": "0AMOUNT", "Visibility": "Visible"}],
                    },
                ],
                "Filter": {
                    "Selection": {
                        "Operator": {
                            "Code": "And",
                            "SubSelections": [
                                {
                                    "SetOperand": {
                                        "FieldName": "ZREGION.KEY",
                                        "Elements": [
                                            {"Comparison": "=", "Low": "NORTH"},
                                            {"Comparison": "=", "Low": "SOUTH"},
                                        ],
                                    }
                                },
                                {
                                    "SetOperand": {
                                        "FieldName": "0CALMONTH.KEY",
                                        "Elements": [
                                            {
                                                "Comparison": "BETWEEN",
                                                "Low": "202601",
                                                "High": "202603",
                                            },
                                        ],
                                    }
                                },
                                {
                                    "SetOperand": {
                                        "FieldName": "ZREGION.KEY",
                                        "Elements": [
                                            {"Comparison": "<>", "Low": "WEST"},
                                        ],
                                    }
                                },
                                {
                                    "SetOperand": {
                                        "FieldName": "0CALMONTH.KEY",
                                        "Elements": [
                                            {"Comparison": ">=", "Low": "202601"},
                                        ],
                                    }
                                },
                            ],
                        }
                    }
                },
                "Sort": [
                    {
                        "Dimension": "Measures",
                        "SortType": "Measure",
                        "MeasureName": "0AMOUNT",
                        "Direction": "Desc",
                    },
                    {"Dimension": "0CALMONTH", "SortType": "MemberKey", "Direction": "Asc"},
                ],
                "Variables": [
                    {
                        "Name": "0P_CALYEAR",
                        "Values": {
                            "Selection": {
                                "SetOperand": {
                                    "FieldName": "0P_CALYEAR",
                                    "Elements": [{"Comparison": "=", "Low": "2026"}],
                                }
                            }
                        },
                    },
                    {
                        "Name": "ZV_REGION",
                        "Values": {
                            "Selection": {
                                "SetOperand": {
                                    "FieldName": "ZV_REGION",
                                    "Elements": [{"Comparison": "=", "Low": "NORTH"}],
                                }
                            }
                        },
                    },
                    {
                        "Name": "ZV_PERIOD",
                        "Values": {
                            "Selection": {
                                "SetOperand": {
                                    "FieldName": "ZV_PERIOD",
                                    "Elements": [
                                        {"Comparison": "BETWEEN", "Low": "01", "High": "06"}
                                    ],
                                }
                            }
                        },
                    },
                ],
                "ResultSetFeatureRequest": {
                    "ResultFormat": "Version2",
                    "ResultEncoding": "None",
                    "MaxResultRecords": 50,
                    "SubSetDescription": {
                        "RowFrom": 0,
                        "RowTo": 50,
                        "ColumnFrom": 0,
                        "ColumnTo": -1,
                    },
                    "UseDefaultAttributeKey": False,
                },
            },
            "Language": "EN",
        },
        "Options": ["StatefulServer"],
    }


async def test_run_without_filters_order_or_variables_sends_none_of_them():
    sap = Sap()
    sap.answers = [fixture("run_ok.json")]
    known = definition().model_copy(update={"variables": []})
    await make(sap, known=known).run(measures=["0AMOUNT", "0QUANTITY"], group_by=["0CALMONTH"])
    request = sap.body()["Analytics"]["Definition"]
    assert "Filter" not in request and "Sort" not in request and "Variables" not in request
    assert request["ResultSetFeatureRequest"]["MaxResultRecords"] == 100


async def test_run_reads_rows_and_units_from_the_grid():
    sap = Sap()
    sap.answers = [fixture("run_ok.json")]
    out = await make(sap, known=definition()).run(**RUN)
    assert out == {
        "columns": ["0CALMONTH", "0CALMONTH.text", "0AMOUNT", "0QUANTITY"],
        "rows": [
            ["202601", "01.2026", 1200.5, 10],
            ["202602", "02.2026", 980.0, 8],
            ["202603", "03.2026", 1430.25, 12],
        ],
        "units": {"0AMOUNT": "EUR", "0QUANTITY": "PC"},
        "row_count": 3,
        "truncated": False,
    }


async def test_run_without_a_definition_describes_first():
    sap = Sap()
    sap.answers = [fixture("describe_ok.json"), fixture("run_ok.json")]
    out = await make(sap).run(**RUN)
    assert out["row_count"] == 3
    assert "Metadata" in sap.body(0) and "Analytics" in sap.body(1)


# ----------------------------------------------- refused before anything is sent


@pytest.mark.parametrize(
    "args",
    [
        {"group_by": ["0CALMONTH"] * (MAX_DIMENSIONS + 1)},
        {"filters": [{"dimension": "ZREGION", "operator": "=", "value": "x"}] * (MAX_FILTERS + 1)},
        {"top": MAX_TOP + 1},
        {"measures": ["0UNKNOWN"]},
        {"variables": {}},
    ],
)
async def test_a_refused_argument_sends_nothing(args):
    """Each cap and check refuses before a request; the full list of rules
    is in ``tests/test_ina_protocol.py``."""
    sap = Sap()
    sap.answers = [fixture("run_ok.json")]
    error = await refused(make(sap, known=definition()).run(**{**RUN, **args}))
    assert error.code in ("result_too_large", "invalid_filter", "variable_required")
    assert sap.requests == []


async def test_a_cap_is_refused_before_an_undescribed_query_is_read():
    sap = Sap()
    error = await refused(make(sap).run(**{**RUN, "top": MAX_TOP + 1}))
    assert error.code == "result_too_large"
    assert sap.requests == []


async def test_a_missing_mandatory_variable_names_it():
    sap = Sap()
    error = await refused(
        make(sap, known=definition()).run(measures=["0AMOUNT"], group_by=["0CALMONTH"])
    )
    assert error.code == "variable_required"
    assert "0P_CALYEAR" in error.message
    assert sap.requests == []


async def test_no_signed_in_user_is_user_token_required_and_sends_nothing():
    sap = Sap()
    resolver = FakeResolver(url=SIA_URL, name=DESTINATION)
    error = await refused(make(sap, user_context=True, resolver=resolver).describe())
    assert error.code == "user_token_required"
    assert sap.requests == [] and resolver.calls == []


async def test_a_client_whose_connection_names_another_destination_is_refused():
    sap = Sap()
    http = destination_http_client(
        FakeResolver(url=SIA_URL, name="OTHER"),
        server_key="builtin:ina",
        connectivity=None,
        transport=httpx.MockTransport(sap.handler),
    )
    client = InaClient(
        http,
        InaSessionStore(),
        destination=DESTINATION,
        user_context=False,
        query=QUERY,
        package="",
    )
    error = await refused(client.describe())
    assert error.code == "destination_error"
    assert sap.requests == []


def test_the_client_needs_an_ina_session_store():
    http = destination_http_client(
        FakeResolver(url=SIA_URL, name=DESTINATION), server_key="builtin:ina", connectivity=None
    )
    with pytest.raises(TypeError):
        InaClient(
            http, object(), destination=DESTINATION, user_context=False, query=QUERY, package=""
        )


# --------------------------------------------------------- what SAP's answer says


async def test_an_unknown_query_is_query_unknown():
    sap = Sap()
    sap.answers = [fixture("query_unknown.json")]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "query_unknown"


async def test_another_sap_message_is_sap_error_cut_and_masked():
    text = (
        "Fehler bei https://sia.internal:44300/sap/bw/ina?sap-password=hunter2 "
        "Authorization: Bearer abc.def.ghi " + "y" * 900
    )
    sap = Sap()
    sap.answers = [
        {
            "Messages": [
                {"Type": 0, "Text": "an information"},
                {"Type": 2, "Text": text},
            ]
        }
    ]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "sap_error"
    assert len(error.message) <= 500
    assert "[url]" in error.message
    for leaked in ("hunter2", "sia.internal", "abc.def.ghi", "information"):
        assert leaked not in error.message


async def test_a_warning_alone_does_not_fail_the_run():
    answer = fixture("run_ok.json")
    answer["Messages"] = [{"Type": 1, "Text": "a warning"}]
    sap = Sap()
    sap.answers = [answer]
    out = await make(sap, known=definition()).run(**RUN)
    assert out["row_count"] == 3 and "warning" not in json.dumps(out)


async def test_a_non_json_200_is_not_json():
    sap = Sap()
    sap.answers = [
        httpx.Response(200, text="<html>Logon</html>", headers={"content-type": "text/html"})
    ]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "not_json"
    assert "Logon" not in error.message


async def test_an_error_status_with_a_message_envelope_passes_the_masked_text():
    sap = Sap()
    sap.answers = [httpx.Response(500, json={"Messages": [{"Type": 2, "Text": "Dump ABC"}]})]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "sap_error" and "Dump ABC" in error.message


async def test_an_answer_over_the_byte_cap_is_result_too_large(monkeypatch):
    monkeypatch.setattr(ina_client, "MAX_RESPONSE_BYTES", 1000)
    sap = Sap()
    sap.answers = [{"Grids": [], "padding": "z" * 5000}]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "result_too_large"


async def test_a_401_is_unauthorized():
    sap = Sap()
    sap.answers = [httpx.Response(401, text="secret")]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "unauthorized"


# ------------------------------------------------------------ transport failures


async def test_a_407_of_the_connectivity_proxy_is_proxy_refused():
    sap = Sap()

    def proxy(request: httpx.Request) -> httpx.Response:
        sap.requests.append(request)
        return httpx.Response(407, json={"error": "tenant-zone-9"})

    http = destination_http_client(
        OnPremiseResolver(url="http://sia.internal:44300", name=DESTINATION),
        server_key="builtin:ina",
        connectivity=FakeConnectivity(),
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        proxy_transport=httpx.MockTransport(proxy),
    )
    client = InaClient(
        http,
        InaSessionStore(),
        destination=DESTINATION,
        user_context=False,
        query=QUERY,
        package="",
    )
    error = await refused(client.describe())
    assert error.code == "proxy_refused"
    assert "tenant-zone-9" not in json.dumps(error.to_dict())
    assert all(r.method == "GET" for r in sap.requests)


async def test_an_onpremise_destination_is_reached_through_the_proxy():
    sap = Sap()
    sap.answers = [fixture("describe_ok.json")]
    http = destination_http_client(
        OnPremiseResolver(url="http://sia.internal:44300", name=DESTINATION),
        server_key="builtin:ina",
        connectivity=FakeConnectivity(),
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        proxy_transport=httpx.MockTransport(sap.handler),
    )
    client = InaClient(
        http,
        InaSessionStore(),
        destination=DESTINATION,
        user_context=False,
        query=QUERY,
        package="",
    )
    described = await client.describe()
    assert described.definition == definition()
    for request in sap.requests:
        assert request.url.scheme == "http" and request.headers["Proxy-Authorization"]
        assert request.url.params["sap-client"] == "100"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (httpx.ReadTimeout("timed out at https://sia.internal/?token=abc"), "timeout"),
        (httpx.ConnectError("refused https://sia.internal/?token=abc"), "unreachable"),
    ],
)
async def test_a_transport_failure_maps_to_its_code(exc, code):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    http = destination_http_client(
        FakeResolver(url=SIA_URL, name=DESTINATION),
        server_key="builtin:ina",
        connectivity=None,
        transport=httpx.MockTransport(handler),
    )
    client = InaClient(
        http,
        InaSessionStore(),
        destination=DESTINATION,
        user_context=False,
        query=QUERY,
        package="",
    )
    error = await refused(client.describe())
    assert error.code == code
    assert "abc" not in error.message and "sia.internal" not in error.message


async def test_the_call_deadline_is_timeout(monkeypatch):
    import asyncio

    monkeypatch.setattr(ina_client, "CALL_DEADLINE_SECONDS", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    http = destination_http_client(
        FakeResolver(url=SIA_URL, name=DESTINATION),
        server_key="builtin:ina",
        connectivity=None,
        transport=httpx.MockTransport(slow),
    )
    client = InaClient(
        http,
        InaSessionStore(),
        destination=DESTINATION,
        user_context=False,
        query=QUERY,
        package="",
    )
    error = await refused(client.describe())
    assert error.code == "timeout"


async def test_a_destination_failure_is_destination_error_with_a_fixed_text():
    class Broken(FakeResolver):
        async def resolve(self, **kwargs: Any) -> Any:
            raise DestinationError("lookup of https://dest.example.test/?client_secret=s3 failed")

    sap = Sap()
    error = await refused(make(sap, resolver=Broken(url=SIA_URL, name=DESTINATION)).describe())
    assert error.code == "destination_error"
    assert "s3" not in error.message and "dest.example" not in error.message
    assert sap.requests == []


async def test_the_repr_of_the_client_holds_no_credential():
    sap = Sap()
    sap.answers = [fixture("describe_ok.json")]
    store = InaSessionStore()
    client = make(sap, store=store)
    await client.describe()
    shown = repr(client) + str(client) + repr(store)
    for secret in ("dest-token", "csrf-1", "sess-1", "Bearer"):
        assert secret not in shown
    assert DESTINATION in repr(client)
