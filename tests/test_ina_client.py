"""The InA client (``agents/ina/client.py``): what it sends, what it reads back.

The client runs on the real ``destination_http_client`` (``DestinationAuth``,
and for the OnPremise case the ``OnPremiseRouter``); only the destination
service, the connectivity tokens and SAP behind them are doubles
(``httpx.MockTransport``). The answers are the synthetic fixtures in
``tests/fixtures/ina/``, shaped after what SIA answered the probe
(``scripts/probe_ina.py``): ``GetServerInfo`` with its services and
capabilities, a query description, a result grid, and the message of an
unknown query.

Covered (the CSRF token and the SAP session per destination and user are
``tests/test_ina_sessions.py``): ``describe`` maps SAP's description onto the save-time gate's
``InaDefinition``; ``run`` sends the ``Analytics`` request exactly as pinned
and reads rows and units from the grid; every cap and every argument check
refuses before anything is sent; and every failure is one code of the closed
list with a text in which nothing of SAP's answer but a cut, masked message
text travels.

Run:  python -m pytest tests/test_ina_client.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

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
from tests.ina_helpers import (  # noqa: E402, F401 - re-exported for the other suites
    CLOSED,
    FIXTURES,
    INFO_PATH,
    RESPONSE_PATH,
    Sap,
    fixture,
    grid,
    kind_of,
)
from tests.odata_helpers import FakeConnectivity, FakeResolver, OnPremiseResolver  # noqa: E402

DESTINATION = "SIA_INA"
QUERY = "ZQ_SALES_01"
SIA_URL = "https://sia.example.test:44300"
CAPABILITIES = [
    "AggregationNOPNULL",
    "ClientCapabilities",
    "ResultSetCellFormatString",
    "SupportsComplexFilters",
    "Variables",
]
ALICE = "alice@example.com"
BOB = "bob@example.com"


def definition() -> InaDefinition:
    """The definition ``describe`` reads from ``describe_ok.json``."""
    return InaDefinition.model_validate(
        {
            "measure_structure": "Measures",
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


def make(
    sap: Sap,
    *,
    user_context: bool = False,
    destination: str = DESTINATION,
    resolver: Any = None,
    store: InaSessionStore | None = None,
    known: InaDefinition | None = None,
    package: str = "",
) -> InaClient:
    resolver = resolver or FakeResolver(url=SIA_URL, name=destination)
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
        destination=destination,
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
# The instance a run opens in the SAP session: "ina" and 16 hex digits.
INSTANCE = re.compile(r"ina[0-9a-f]{16}")
HIDE_TOTALS = [
    {"Result": "Members", "Visibility": "Visible"},
    {"Result": "Total", "Visibility": "Hidden"},
]


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
    body = sap.read_body()
    assert INSTANCE.fullmatch(body["Analytics"]["DataSource"].pop("InstanceId"))
    assert body == {
        "ClientInfo": ina_client.CLIENT_INFO,
        "Analytics": {
            "Capabilities": CAPABILITIES,
            "DataSource": {"ObjectName": QUERY, "PackageName": "ZPKG", "Type": "Query"},
            "Definition": {
                "Dimensions": [
                    {
                        "Name": "0CALMONTH",
                        "Axis": "Rows",
                        # The text attribute the submit answer named (submit_ok.json).
                        "Attributes": [
                            {"Name": "0CALMONTH.KEY", "Obtainability": "Always"},
                            {"Name": "0CALMONTH.LONG_TEXT", "Obtainability": "Always"},
                        ],
                        "ResultStructure": HIDE_TOTALS,
                    },
                    {
                        "Name": "ZREGION",
                        "Axis": "Rows",
                        "Attributes": [
                            {"Name": "ZREGION.KEY", "Obtainability": "Always"},
                            {"Name": "ZREGION.LONG_TEXT", "Obtainability": "Always"},
                        ],
                        "ResultStructure": HIDE_TOTALS,
                    },
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
                                    "Elements": [{
                                        "Comparison": "=",
                                        "Low": "2026",
                                        "IsExcluding": False,
                                    }],
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
                                    "Elements": [{
                                        "Comparison": "=",
                                        "Low": "NORTH",
                                        "IsExcluding": False,
                                    }],
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
                                        {
                                            "Comparison": "BETWEEN",
                                            "Low": "01",
                                            "High": "06",
                                            "IsExcluding": False,
                                        }
                                    ],
                                }
                            }
                        },
                    },
                ],
                "ResultSetFeatureRequest": {
                    "ResultFormat": "Version2",
                    "ResultEncoding": "None",
                    # One row more than top: it tells "there is more" from "that is
                    # all"; no MaxResultRecords (with it SIA added total rows).
                    "SubSetDescription": {
                        "RowFrom": 0,
                        "RowTo": 51,
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
    request = sap.read_body()["Analytics"]["Definition"]
    assert "Filter" not in request and "Sort" not in request and "Variables" not in request
    assert "MaxResultRecords" not in request["ResultSetFeatureRequest"]
    assert request["ResultSetFeatureRequest"]["SubSetDescription"]["RowTo"] == 101
    # No variables, no VariableSubmit: the read and the Close.
    assert sap.kinds == ["GET", "read", "close"]


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
    assert sap.kinds == ["GET", "describe", "submit", "read", "close"]


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


async def test_a_refused_destination_keeps_its_admin_text_out_of_the_answer():
    from agents.destination_auth import DestinationRefused

    class Refusing(FakeResolver):
        async def resolve(self, **kwargs: Any) -> Any:
            raise DestinationRefused(
                "refused at https://dest.example.test/?x=1",
                admin_text="destination 'SIA_INA' is not usable as configured",
            )

    sap = Sap()
    error = await refused(make(sap, resolver=Refusing(url=SIA_URL, name=DESTINATION)).describe())
    assert error.code == "destination_error"
    assert error.admin_text == "destination 'SIA_INA' is not usable as configured"
    assert "admin_text" not in error.to_dict() and "usable" not in json.dumps(error.to_dict())


async def test_another_destination_failure_has_no_admin_text():
    class Broken(FakeResolver):
        async def resolve(self, **kwargs: Any) -> Any:
            raise DestinationError("boom")

    error = await refused(make(Sap(), resolver=Broken(url=SIA_URL, name=DESTINATION)).describe())
    assert error.code == "destination_error" and error.admin_text is None


def test_the_repr_of_an_answer_holds_neither_headers_nor_body():
    from agents.ina.client import _Answer

    answer = _Answer(
        status=200,
        headers=httpx.Headers({"x-csrf-token": "secret-token"}),
        body=b"secret-body",
        cookies={"SAP_SESSIONID": "secret-cookie"},
        proxied=False,
    )
    assert "secret" not in repr(answer) and "200" in repr(answer)


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
