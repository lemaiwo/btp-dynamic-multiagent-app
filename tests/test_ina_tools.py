"""The agent tools of ``builtin:ina`` (``agents/ina/tools.py``).

Three tools over the queries an agent's entry names: ``list_queries`` (the
catalogue lines), ``describe_query`` (the stored description, never read
from SAP here) and ``run_query`` (one ``Analytics`` read through the real
client, ``agents/ina/client.py``). The toolset runs on the real
``destination_http_client``; only the destination service and SAP behind it
are doubles (``tests/ina_helpers.py``'s ``Sap`` over
``httpx.MockTransport``).

Covered: what each tool answers and what it leaves out; a name the entry does
not hold, a query without a description and every argument the client
refuses are answered before anything is sent, without the name the model
sent; the client's answer is passed through, a truncated one with its hint,
an ordered one in SAP's order (the sort in the form SIA applied, recorded
2026-11-05), a "not assigned" row as a null key with its hint, one over the
result cap refused; failures are ``{"error": {...}}``, never an
exception; and the fixed-form activity lines.

Run:  python -m pytest tests/test_ina_tools.py
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _v in (
    "VCAP_SERVICES",
    "VCAP_APPLICATION",
    "DESTINATION_CLIENT_ID",
    "DESTINATION_CLIENT_SECRET",
    "DESTINATION_URI",
    "DESTINATION_TOKEN_URL",
    "DESTINATION_UAA_URL",
    "CONNECTIVITY_CLIENT_ID",
    "CONNECTIVITY_CLIENT_SECRET",
    "CONNECTIVITY_TOKEN_URL",
    "CONNECTIVITY_PROXY_HOST",
    "CONNECTIVITY_PROXY_PORT",
):
    os.environ.pop(_v, None)

from agents.ina import tools as ina_tools  # noqa: E402
from agents.ina.protocol import read_description  # noqa: E402
from agents.ina.tools import (  # noqa: E402
    MAX_RESULT_CHARS,
    NOT_ASSIGNED_HINT,
    TRUNCATED_HINT,
    NoUsableQueryError,
    activity_summary,
    attached_queries,
    ina_toolset,
)
from tests.ina_helpers import (  # noqa: E402
    DESTINATION,
    QUERY,
    RUN,
    SIA_URL,
    Sap,
    definition,
    fixture,
    grid,
    signed_in,
)
from tests.odata_helpers import FakeResolver  # noqa: E402

TOOLS = {"list_queries", "describe_query", "run_query"}
SALES = "sales-by-month"
STOCK = "stock-levels"
# A name of the form the model could send, and one that it must never see
# repeated in an answer.
PROBE = "ZZ_PROBE_NAME_4711"


def row(name: str = SALES, **patch: Any) -> dict[str, Any]:
    """A query as the registry hands it on (``InaQuery.to_dict()``)."""
    data: dict[str, Any] = {
        "name": name,
        "title": "Sales by month",
        "purpose": "Net sales and quantities per month and region",
        "not_for": "Forecasts",
        "destination": DESTINATION,
        "user_context": False,
        "query": QUERY,
        "package": "",
        "definition": definition().model_dump(mode="json"),
        "metadata_fetched_at": "2026-11-03T10:00:00+00:00",
        "enabled": True,
        "id": 7,
        "created_at": "2026-11-01T09:00:00+00:00",
        "updated_at": "2026-11-03T10:00:00+00:00",
        "used_by": [],
    }
    data.update(patch)
    return data


class World:
    """One toolset over a snapshot, SAP behind a mock transport."""

    def __init__(self, snapshot: dict[str, dict], names: list[str]) -> None:
        self.sap = Sap()
        self.sap.answers = [fixture("run_ok.json")]
        self.resolved: list[str] = []
        self.toolset = ina_toolset(
            {"queries": names},
            server_key="builtin:ina",
            auth_mode="destination",
            queries=snapshot,
            agent_name="analyst",
            transport=httpx.MockTransport(self.sap.handler),
            resolver_factory=self._resolver,
            connectivity=None,
        )

    def _resolver(self, name: str) -> FakeResolver:
        self.resolved.append(name)
        return FakeResolver(url=SIA_URL, name=name)

    def tool(self, name: str) -> Any:
        return self.toolset.tools[name].function


def world(*rows: dict[str, Any], names: list[str] | None = None) -> World:
    snapshot = {r["name"]: r for r in rows} or {SALES: row()}
    return World(snapshot, names if names is not None else list(snapshot))


def error_of(answer: Any) -> dict[str, Any]:
    assert isinstance(answer, dict) and set(answer) == {"error"}, answer
    return answer["error"]


# --- the factory ------------------------------------------------------------------


def test_the_toolset_has_the_three_tools():
    assert set(world().toolset.tools) == TOOLS


@pytest.mark.parametrize("mode", [None, "jwt", "app_only", "none", "oauth2"])
def test_only_destination_mode_builds(mode):
    with pytest.raises(ValueError, match="auth_mode 'destination'"):
        ina_toolset({"queries": [SALES]}, auth_mode=mode, queries={SALES: row()})


def test_an_entry_without_a_usable_query_is_its_own_error(caplog):
    caplog.set_level(logging.WARNING, logger="agents.ina.tools")
    snapshot = {SALES: row(enabled=False)}
    with pytest.raises(NoUsableQueryError):
        ina_toolset({"queries": [SALES, "gone"]}, auth_mode="destination",
                    queries=snapshot, agent_name="analyst")
    assert issubclass(NoUsableQueryError, ValueError)
    text = caplog.text
    assert f"'{SALES}', which is disabled" in text and "'gone', which is not registered" in text


def test_attached_queries_is_the_one_selection():
    snapshot = {SALES: row(), STOCK: row(STOCK, enabled=False), "other": row("other")}
    kept = attached_queries({"queries": [STOCK, SALES, SALES, 5, "gone"]}, snapshot)
    assert [q["name"] for q in kept] == [SALES]
    assert attached_queries(None, snapshot) == []
    assert attached_queries({"queries": "sales-by-month"}, snapshot) == []


def test_nothing_is_resolved_or_sent_while_the_toolset_is_built():
    w = world()
    assert w.resolved == [] and w.sap.requests == []


# --- list_queries -----------------------------------------------------------------


async def test_list_queries_answers_the_agents_enabled_queries_only():
    w = world(row(STOCK, title="Stock", purpose="Stock per plant", not_for=""),
              row(), row("hidden", enabled=False), row("not-mine"),
              names=[STOCK, "hidden", SALES])
    answer = await w.tool("list_queries")()
    assert answer == {"queries": [
        {"name": STOCK, "title": "Stock", "purpose": "Stock per plant", "not_for": ""},
        {"name": SALES, "title": "Sales by month",
         "purpose": "Net sales and quantities per month and region", "not_for": "Forecasts"},
    ]}
    text = json.dumps(answer)
    for hidden in (DESTINATION, QUERY, "not-mine", "hidden", "0AMOUNT"):
        assert hidden not in text, hidden
    assert w.sap.requests == []


async def test_a_change_of_the_snapshot_after_the_build_changes_nothing():
    snapshot = {SALES: row()}
    w = World(snapshot, [SALES])
    snapshot[SALES]["title"] = "changed behind the toolset"
    snapshot[SALES]["definition"]["measures"] = []
    assert (await w.tool("list_queries")())["queries"][0]["title"] == "Sales by month"
    described = await w.tool("describe_query")(SALES)
    assert [m["name"] for m in described["measures"]] == ["0AMOUNT", "0QUANTITY"]


# --- describe_query ---------------------------------------------------------------


async def test_describe_query_answers_the_stored_description_without_reading_sap():
    w = world()
    answer = await w.tool("describe_query")(SALES)
    assert answer == {
        "query": SALES,
        "dimensions": [
            {"name": "0CALMONTH", "label": "Calendar Year/Month"},
            {"name": "ZREGION", "label": "Sales region"},
        ],
        "measures": [
            {"name": "0AMOUNT", "label": "Net amount", "unit": "EUR"},
            {"name": "0QUANTITY", "label": "Quantity", "unit": "PC"},
        ],
        "variables": [
            {"name": "0P_CALYEAR", "label": "Calendar year", "mandatory": True,
             "type": "single"},
            {"name": "ZV_REGION", "label": "Regions", "mandatory": False, "type": "multiple"},
            {"name": "ZV_PERIOD", "label": "Period range", "mandatory": False,
             "type": "interval"},
        ],
    }
    # The wire names behind a dimension, the destination and the InA object
    # name stay with the toolset.
    text = json.dumps(answer)
    for hidden in ("0CALMONTH.KEY", DESTINATION, QUERY):
        assert hidden not in text, hidden
    assert w.sap.requests == [] and w.resolved == []


@pytest.mark.parametrize("stored", [None, {"dimensions": "not a list"}, "{"])
async def test_a_query_without_a_usable_description_is_not_described(stored):
    w = world(row(definition=stored))
    for answer in (
        await w.tool("describe_query")(SALES),
        await w.tool("run_query")(SALES, **RUN),
    ):
        error = error_of(answer)
        assert error["code"] == "not_described"
        assert error["hint"] == "ask an admin to describe the query"
    # The tool never describes a query itself: nothing went to SAP.
    assert w.sap.requests == [] and w.resolved == []


@pytest.mark.parametrize("name", [PROBE, "not-mine", "hidden", "", None, 5, ["x"]])
async def test_a_name_the_entry_does_not_hold_does_not_exist(name):
    w = world(row(), row("not-mine"), row("hidden", enabled=False), names=[SALES, "hidden"])
    for answer in (
        await w.tool("describe_query")(name),
        await w.tool("run_query")(name, **RUN),
    ):
        error = error_of(answer)
        assert error["code"] == "query_unknown"
        assert "list_queries" in error["hint"]
        assert PROBE not in json.dumps(answer) and "not-mine" not in json.dumps(answer)
    assert w.sap.requests == [] and w.resolved == []


# --- run_query --------------------------------------------------------------------


async def test_run_query_passes_the_clients_answer_through():
    w = world()
    answer = await w.tool("run_query")(SALES, **RUN)
    assert answer == {
        "columns": ["0CALMONTH", "0CALMONTH.text", "0AMOUNT"],
        "rows": [
            ["202601", "01.2026", 1200.5],
            ["202602", "02.2026", 980.0],
            ["202603", "03.2026", 1430.25],
        ],
        "units": {"0AMOUNT": "EUR"},
        "row_count": 3,
        "truncated": False,
    }
    # The stored description is the client's: one GetServerInfo, one run
    # (VariableSubmit, read, Close), no Describe; through the query's own
    # destination.
    assert w.sap.kinds == ["GET", "submit", "read", "close"]
    assert w.sap.read_body()["Analytics"]["DataSource"]["ObjectName"] == QUERY
    assert w.resolved == [DESTINATION]


async def test_run_query_with_variables_end_to_end_on_the_answers_sia_gave():
    """SIA's describe, VariableSubmit and grid answers (recorded 2026-11-04,
    sanitised): the values go in the submit, named by their dimension's key;
    the read asks for the texts the submit answer named; the rows come back
    with their texts, the instance is closed."""
    described = read_description(fixture("describe_sia.json")["Cube"]).definition
    w = world(row(definition=described.model_dump(mode="json")))
    w.sap.submit_answers = [fixture("submit_sia.json")]
    w.sap.answers = [fixture("run_sia_grid.json")]
    values = {"!CDS_P_P_DISPLAYCURRENCY": "EUR", "!CDS_P_P_DATEFUNCTION": "YEARTODATE"}
    answer = await w.tool("run_query")(
        SALES, ["ELTUIDCMP1"], ["2CIMMPURGORG", "2CI_SUPPLIER_CDS"], top=3, variables=values
    )
    assert answer == {
        "columns": ["2CIMMPURGORG", "2CIMMPURGORG.text", "2CI_SUPPLIER_CDS",
                    "2CI_SUPPLIER_CDS.text", "ELTUIDCMP1"],
        "rows": [
            ["P100", "Purchasing organisation 100", "0000000101", "Supplier A", 125.5],
            ["P100", "Purchasing organisation 100", "0000000102", "Supplier B", 2400.0],
            ["P100", "Purchasing organisation 100", "0000000103", "Supplier C", 310.25],
        ],
        "units": {"ELTUIDCMP1": "EUR"},
        "row_count": 3,
        "truncated": True,
        "hint": TRUNCATED_HINT,
    }
    assert w.sap.kinds == ["GET", "submit", "read", "close"]
    submit = json.loads(w.sap.submits[0].content)["Analytics"]
    read = w.sap.read_body()["Analytics"]
    assert [v["Values"]["Selection"]["SetOperand"]["FieldName"]
            for v in submit["Definition"]["Variables"]] == [
        "2CH0Y9FATWMXU07PABNUMVE2HHX.KEY", "2CCGCALDFVH.KEY"]
    assert submit["DataSource"]["InstanceId"] == read["DataSource"]["InstanceId"]
    assert read["Definition"]["Dimensions"][1]["Attributes"] == [
        {"Name": "2CI_SUPPLIER_CDS.KEY", "Obtainability": "Always"},
        {"Name": "2CI_SUPPLIER_CDS.LONG_TEXT", "Obtainability": "Always"},
    ]


SIA_VALUES = {"!CDS_P_P_DISPLAYCURRENCY": "EUR", "!CDS_P_P_DATEFUNCTION": "YEARTODATE"}
COSTCENTER = "2CCMMPURORDSPND-COSTCENTER"
PRODUCTTYPE = "2CCMMPURORDSPND-PRODUCTTYPE"


def sia_world(answer: str) -> World:
    described = read_description(fixture("describe_sia.json")["Cube"]).definition
    w = world(row(definition=described.model_dump(mode="json")))
    w.sap.submit_answers = [fixture("submit_sia.json")]
    w.sap.answers = [fixture(answer)]
    return w


async def test_run_query_ordered_by_the_measure_on_the_answer_sia_gave():
    """Top 5 cost centres by value (recorded 2026-11-05, sanitised): the
    sort goes out in the form SIA applied, in the submit and the read, and
    the rows come back in SAP's order, truncated, the cost centre without a
    value as a row with a null key."""
    w = sia_world("run_sia_ordered.json")
    answer = await w.tool("run_query")(
        SALES, ["ELTUIDCMP1"], [COSTCENTER], top=5,
        order=[{"by": "ELTUIDCMP1", "direction": "desc"}], variables=dict(SIA_VALUES))
    assert answer == {
        "columns": [COSTCENTER, "ELTUIDCMP1"],
        "rows": [[None, 9100.5], ["0000000111", 8200.25], ["0000000222", 7300.0],
                 ["0000000333", 6400.75], ["0000000444", 5500.0]],
        "units": {"ELTUIDCMP1": "EUR"},
        "row_count": 5,
        "truncated": True,
        "not_assigned": {COSTCENTER: 1},
        "hint": f"{TRUNCATED_HINT}; {NOT_ASSIGNED_HINT}",
    }
    sort = [{"SortType": "Measure", "MeasureName": "ELTUIDCMP1", "Direction": "Desc"}]
    assert w.sap.read_body()["Analytics"]["Definition"]["Sort"] == sort
    assert json.loads(w.sap.submits[0].content)["Analytics"]["Definition"]["Sort"] == sort
    assert w.sap.kinds == ["GET", "submit", "read", "close"]


async def test_run_query_answers_the_not_assigned_member_as_a_null_key():
    """The pilot's read that 2.37.1 refused whole (product type, recorded
    2026-11-05): SAP's no-value member is a row with a null key, counted in
    ``not_assigned``, with a hint to report it as such."""
    w = sia_world("run_sia_not_assigned.json")
    answer = await w.tool("run_query")(
        SALES, ["ELTUIDCMP1"], [PRODUCTTYPE], top=20, variables=dict(SIA_VALUES))
    assert answer == {
        "columns": [PRODUCTTYPE, "ELTUIDCMP1"],
        "rows": [["PT1", 1250.75], [None, 3400.5]],
        "units": {"ELTUIDCMP1": "EUR"},
        "row_count": 2,
        "truncated": False,
        "not_assigned": {PRODUCTTYPE: 1},
        "hint": NOT_ASSIGNED_HINT,
    }
    assert NOT_ASSIGNED_HINT == (
        "a null key is SAP's 'not assigned' member of that dimension; its number is real: "
        "report it as not assigned, never leave it out")
    assert activity_summary("run_query", answer) == "run_query: 2 rows"


def test_the_tool_description_says_how_order_and_a_null_key_read():
    description = " ".join(world().toolset.tools["run_query"].function.__doc__.split())
    # A mixed list was never recorded: sent in that order, not "applied".
    assert "sent to SAP in that order" in description
    assert "applied by SAP" not in description
    # Seen on the system with one row dimension and once with two, where SAP
    # sorted the whole result, not within the first dimension's groups.
    assert ("a measure sorts all rows by its value, across the groups of every "
            "group_by dimension") in description
    assert "not_assigned" in description and "null key" in description
    assert 'a filter on the value "" (the not assigned member) is unverified' in description


async def test_the_defaults_of_the_optional_arguments():
    w = world(row(definition=definition().model_copy(update={"variables": []})
                  .model_dump(mode="json")))
    answer = await w.tool("run_query")(SALES, ["0AMOUNT"], ["0CALMONTH"])
    assert answer["row_count"] == 3
    request = w.sap.read_body()["Analytics"]["Definition"]
    assert "Filter" not in request and "Sort" not in request and "Variables" not in request
    rows = request["ResultSetFeatureRequest"]["SubSetDescription"]["RowTo"]
    assert rows == 101  # top 100 + 1
    assert w.sap.kinds == ["GET", "read", "close"]  # no variables: no VariableSubmit


async def test_a_truncated_answer_says_so_with_a_hint():
    w = world()
    answer = await w.tool("run_query")(SALES, **RUN, top=2)
    assert answer["truncated"] is True and answer["row_count"] == 2
    assert len(answer["rows"]) == 2
    assert answer["hint"] == TRUNCATED_HINT
    assert TRUNCATED_HINT == "more rows than top: narrow the filters or raise top (max 2000)"
    # An answer that is complete carries no hint.
    assert "hint" not in await w.tool("run_query")(SALES, **RUN, top=3)


@pytest.mark.parametrize(
    "patch, where",
    [
        ({"measures": [PROBE]}, "measures[0]"),
        # Two measures are refused as such (one per run), never naming one.
        ({"measures": ["0AMOUNT", PROBE]}, "measures"),
        ({"group_by": [PROBE]}, "group_by[0]"),
        ({"filters": [{"dimension": PROBE, "operator": "=", "value": "x"}]},
         "filters[0].dimension"),
        ({"order": [{"by": PROBE, "direction": "asc"}]}, "order[0].by"),
        ({"variables": {"0P_CALYEAR": "2026", PROBE: "x"}}, "variables"),
    ],
)
async def test_an_unknown_name_is_refused_before_anything_is_sent(patch, where):
    w = world()
    answer = await w.tool("run_query")(SALES, **{**RUN, **patch})
    error = error_of(answer)
    assert error["code"] == "invalid_filter"
    assert error["message"].startswith(f"{where}:")
    assert PROBE not in json.dumps(answer)
    # Refused on the stored description alone: no destination resolved, no
    # client built, nothing sent.
    assert w.sap.requests == [] and w.resolved == [] and w.toolset.http_clients == []


async def test_two_measures_are_refused_before_anything_is_sent():
    """One measure per run until the column order of several is verified on
    SAP (every recorded grid held one); the model is told to run once per
    measure."""
    w = world()
    answer = await w.tool("run_query")(SALES, **{**RUN, "measures": ["0AMOUNT", "0QUANTITY"]})
    assert error_of(answer) == {
        "code": "invalid_filter",
        "message": "measures: one measure per run until the column order is verified",
        "hint": "run the query once per measure",
    }
    assert w.sap.requests == [] and w.resolved == [] and w.toolset.http_clients == []
    # One measure runs.
    assert RUN["measures"] == ["0AMOUNT"]
    assert (await w.tool("run_query")(SALES, **RUN))["row_count"] == 3


def test_the_tool_description_says_one_measure_per_run():
    description = world().toolset.tools["run_query"].function.__doc__
    assert "measures: exactly one measure name" in description
    assert "once per measure" in description


async def test_a_missing_mandatory_variable_is_refused_before_anything_is_sent():
    w = world()
    answer = await w.tool("run_query")(SALES, ["0AMOUNT"], ["0CALMONTH"])
    assert error_of(answer)["code"] == "variable_required"
    assert w.sap.requests == [] and w.resolved == []


@pytest.mark.parametrize("top", [0, -1, 2001])
async def test_top_outside_its_bounds_is_refused(top):
    w = world()
    code = error_of(await w.tool("run_query")(SALES, **RUN, top=top))["code"]
    assert code == ("result_too_large" if top > 2000 else "invalid_filter")
    assert w.sap.requests == [] and w.resolved == []


async def test_a_refusal_of_sap_is_an_error_object_not_an_exception():
    w = world()
    w.sap.answers = [httpx.Response(401)]
    error = error_of(await w.tool("run_query")(SALES, **RUN))
    assert error["code"] == "unauthorized" and set(error) <= {"code", "message", "hint"}


async def test_an_answer_over_the_result_cap_is_refused_not_cut():
    keys = [[f"{i:06d}"] for i in range(1500)]
    cells = [{"Value": 123456789.123, "Unit": "EUR"} for _ in keys]
    w = world()
    # The submit answer (submit_ok.json) names ZREGION's text attribute.
    w.sap.answers = [grid(["ZREGION"], keys, ["0AMOUNT"], cells,
                          texts=[[f"Region {k[0]} " + "x" * 40] for k in keys])]
    answer = await w.tool("run_query")(SALES, ["0AMOUNT"], ["ZREGION"], top=2000,
                                       variables={"0P_CALYEAR": "2026"})
    error = error_of(answer)
    assert error["code"] == "result_too_large"
    assert str(MAX_RESULT_CHARS) in error["message"]
    assert "narrow" in error["hint"]


async def test_a_query_that_runs_as_the_user_needs_a_signed_in_user():
    w = world(row(user_context=True))
    error = error_of(await w.tool("run_query")(SALES, **RUN))
    assert error["code"] == "user_token_required"
    assert w.sap.requests == []
    with signed_in("alice@example.com"):
        assert (await w.tool("run_query")(SALES, **RUN))["row_count"] == 3


async def test_one_session_per_identity_serves_every_run_of_the_toolset():
    w = world()
    await w.tool("run_query")(SALES, **RUN)
    await w.tool("run_query")(SALES, **RUN)
    assert w.sap.kinds == ["GET", *["submit", "read", "close"] * 2]
    assert w.resolved == [DESTINATION]  # one client per query, built once


async def test_each_query_goes_through_its_own_destination():
    w = world(row(), row(STOCK, destination="SIA_OTHER"))
    await w.tool("run_query")(SALES, **RUN)
    await w.tool("run_query")(STOCK, **RUN)
    assert w.resolved == [DESTINATION, "SIA_OTHER"]
    assert len(w.toolset.http_clients) == 2


@pytest.mark.parametrize("patch", [{"user_context": "true"}, {"destination": "bad name!"},
                                   {"destination": None}])
async def test_a_stored_identity_that_is_not_exact_is_not_used(patch):
    w = world(row(**patch))
    error = error_of(await w.tool("run_query")(SALES, **RUN))
    assert error["code"] == "destination_error"
    assert w.sap.requests == [] and w.resolved == []


@pytest.mark.parametrize("stored", ["", "line\nbreak", None, 5])
async def test_a_stored_query_name_that_is_not_usable_builds_no_client(stored):
    w = world(row(query=stored))
    for _ in range(2):
        assert error_of(await w.tool("run_query")(SALES, **RUN))["code"] == "query_unknown"
    assert w.toolset.http_clients == [] and w.resolved == [] and w.sap.requests == []


def test_a_stored_description_that_is_not_valid_is_logged_once_by_name(caplog):
    caplog.set_level(logging.WARNING, logger="agents.ina.tools")
    w = world(row(definition={"dimensions": "not a list"}))
    assert caplog.text.count(f"stored description of query '{SALES}' is not valid") == 1
    assert "not a list" not in caplog.text
    assert w.toolset is not None


async def test_an_unexpected_failure_is_a_fixed_error_and_one_log_line(caplog):
    def boom(name: str) -> Any:
        raise RuntimeError("https://sia.example.test/secret?token=abc")

    caplog.set_level(logging.WARNING, logger="agents.ina.tools")
    broken = ina_toolset({"queries": [SALES]}, auth_mode="destination",
                         queries={SALES: row()}, resolver_factory=boom)
    answer = await broken.tools["run_query"].function(SALES, **RUN)
    assert error_of(answer) == {"code": "destination_error",
                                "message": "the call could not be completed"}
    assert "token=abc" not in caplog.text and "RuntimeError" in caplog.text


async def test_a_missing_destination_binding_is_a_destination_error(caplog):
    # No resolver factory and no binding in the environment.
    toolset = ina_toolset({"queries": [SALES]}, auth_mode="destination",
                          queries={SALES: row()})
    error = error_of(await toolset.tools["run_query"].function(SALES, **RUN))
    assert error["code"] == "destination_error"


async def test_the_registry_can_close_every_client():
    w = world(row(), row(STOCK, destination="SIA_OTHER"))
    await w.tool("run_query")(SALES, **RUN)
    await w.tool("run_query")(STOCK, **RUN)
    await w.toolset.http_client.aclose()
    assert all(client.is_closed for client in w.toolset.http_clients)


# --- the activity line ------------------------------------------------------------


@pytest.mark.parametrize("name", ["run_query", "ina_run_query", "ina_0_run_query"])
def test_run_query_is_summarised_by_its_row_count(name):
    answer = {"columns": ["A"], "rows": [["secret-value"], ["x"]], "units": {},
              "row_count": 2, "truncated": False}
    assert activity_summary(name, answer) == "run_query: 2 rows"
    assert activity_summary(name, {**answer, "truncated": True, "hint": TRUNCATED_HINT}) == (
        "run_query: 2 rows, truncated")


def test_the_other_two_tools_have_fixed_lines():
    assert activity_summary("list_queries", {"queries": [{}, {}, {}]}) == "list_queries: 3 queries"
    assert activity_summary(
        "ina_describe_query",
        {"query": "q", "dimensions": [{}], "measures": [{}, {}], "variables": []},
    ) == "describe_query: 1 dimensions, 2 measures, 0 variables"


def test_a_refusal_is_its_code_only():
    assert activity_summary("run_query", {"error": {"code": "invalid_filter",
                                                    "message": "measures[0]: secret"}}) == (
        "error: invalid_filter")
    assert activity_summary("run_query", {"error": {"code": "Not A Code!"}}) == "error"
    assert activity_summary("run_query", "text") == "run_query: no summary"


@pytest.mark.parametrize("name", ["search_operations", "xina_run_query", "run_queries", None, 5])
def test_other_tools_get_no_fixed_line(name):
    assert activity_summary(name, {"rows": []}) is None


def test_the_module_never_imports_the_database_layer():
    source = Path(ina_tools.__file__).read_text(encoding="utf-8")
    assert "import agents.db" not in source and "from agents.db" not in source


def test_the_snapshot_given_is_not_changed_by_the_toolset():
    snapshot = {SALES: row()}
    before = copy.deepcopy(snapshot)
    World(snapshot, [SALES])
    assert snapshot == before
