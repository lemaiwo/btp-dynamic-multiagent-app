"""The run of the InA client (``InaClient.run``): three requests on one
query instance in one SAP session.

What SIA answered on 2026-11-04 (runs 5 to 8): values reach SAP only through
a VariableSubmit of their own (SIA has no ``DirectVariableTransfer``); the
read then runs on the same ``InstanceId`` and asks for the text attributes
the submit answer named; a Close releases the instance. Covered: the order
and the shape of the three requests, one new instance per run, the cookies a
submit answer sets travel with the read and the Close and are kept; a 403 at
either step drops the session and redoes the whole sequence once; a refusal
of the submit or the read is the run's error and the instance is still
closed; a Close that fails never fails the run and is logged by exception
class only; the total deadline covers the whole sequence; a run cancelled
during the read raises CancelledError and sends no Close; a definition
described before 2.37.1 sends and reads the ``Measures`` structure.

Same doubles as ``tests/test_ina_client.py`` (real ``destination_http_client``
over ``httpx.MockTransport``, ``tests/ina_helpers.py``).

Run:  python -m pytest tests/test_ina_run.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ina import client as ina_client  # noqa: E402
from agents.ina.models import InaDefinition  # noqa: E402
from agents.ina.protocol import close_request, read_description  # noqa: E402
from tests.ina_helpers import Sap, fixture, grid  # noqa: E402
from tests.test_ina_client import INSTANCE, RUN, definition, make, refused  # noqa: E402

SIA_VALUES = {"!CDS_P_P_DISPLAYCURRENCY": "EUR", "!CDS_P_P_DATEFUNCTION": "YEARTODATE"}


def instance_of(request: httpx.Request) -> str:
    body = json.loads(request.content)["Analytics"]
    if "Actions" in body:
        (action,) = body["Actions"]
        (instance,) = action["DataSources"]
        return instance
    return body["DataSource"]["InstanceId"]


def rows_of(request: httpx.Request) -> list[dict[str, Any]]:
    dimensions = json.loads(request.content)["Analytics"]["Definition"]["Dimensions"]
    return [d for d in dimensions if d["Axis"] == "Rows"]


def with_cookie(answer: Any, cookie: str) -> httpx.Response:
    response = httpx.Response(200, json=answer)
    response.headers["set-cookie"] = cookie
    return response


def required_403() -> httpx.Response:
    return httpx.Response(403, headers={"x-csrf-token": "Required"}, text="CSRF token required")


# ------------------------------------------------------------------- the sequence


async def test_a_run_with_variables_is_submit_read_and_close_on_one_instance():
    sap = Sap()
    sap.answers = [fixture("run_ok.json")]
    out = await make(sap, known=definition()).run(**RUN)
    assert out["row_count"] == 3
    assert sap.kinds == ["GET", "submit", "read", "close"]
    (instance,) = {instance_of(r) for r in sap.posts}
    assert INSTANCE.fullmatch(instance)
    assert json.loads(sap.closes[0].content) == close_request(instance)
    submit, read = (json.loads(r.content)["Analytics"] for r in (sap.submits[0], sap.reads[0]))
    assert submit["Definition"]["Variables"] == read["Definition"]["Variables"]
    assert "ProcessingDirectives" not in read


async def test_a_run_without_variables_is_the_read_and_the_close():
    sap = Sap()
    sap.answers = [fixture("run_ok.json")]
    known = definition().model_copy(update={"variables": []})
    out = await make(sap, known=known).run(["0AMOUNT"], ["0CALMONTH"])
    assert sap.kinds == ["GET", "read", "close"]
    # No submit answer named a text: the key alone is asked for, and the
    # text SAP sends anyway is not passed on.
    assert rows_of(sap.reads[0])[0]["Attributes"] == [
        {"Name": "0CALMONTH.KEY", "Obtainability": "Always"}]
    assert out["columns"] == ["0CALMONTH", "0AMOUNT"]


async def test_each_run_opens_an_instance_of_its_own():
    sap = Sap()
    sap.answers = [fixture("run_ok.json")]
    client = make(sap, known=definition())
    await client.run(**RUN)
    await client.run(**RUN)
    first, second = ({instance_of(r) for r in sap.posts[i:i + 3]} for i in (0, 3))
    assert len(first) == len(second) == 1 and first != second


async def test_the_read_asks_for_the_text_the_submit_answer_names_and_nothing_else():
    sap = Sap()
    submitted = fixture("submit_ok.json")
    submitted["Cube"]["Dimensions"][1]["TextAttribute"] = "0CALMONTH.SECRET"  # ZREGION's
    sap.submit_answers = [submitted]
    sap.answers = [grid(["0CALMONTH", "ZREGION"], [["202601", "N"]], ["0AMOUNT"],
                        [{"Value": 1}], texts=[["01.2026", "North"]])]
    out = await make(sap, known=definition()).run(
        ["0AMOUNT"], ["0CALMONTH", "ZREGION"], variables={"0P_CALYEAR": "2026"})
    assert [r["Attributes"] for r in rows_of(sap.reads[0])] == [
        [{"Name": "0CALMONTH.KEY", "Obtainability": "Always"},
         {"Name": "0CALMONTH.LONG_TEXT", "Obtainability": "Always"}],
        [{"Name": "ZREGION.KEY", "Obtainability": "Always"}],
    ]
    assert out["columns"] == ["0CALMONTH", "0CALMONTH.text", "ZREGION", "0AMOUNT"]


async def test_the_cookies_of_the_submit_answer_travel_with_the_read_and_the_close():
    sap = Sap()
    sap.submit_answers = [
        with_cookie(fixture("submit_ok.json"), "SAP_SESSIONID_XYZ_100=sess-submit; path=/"),
        fixture("submit_ok.json"),
    ]
    sap.answers = [fixture("run_ok.json")]
    store = ina_client.InaSessionStore()
    await make(sap, store=store, known=definition()).run(**RUN)
    await make(sap, store=store, known=definition()).run(**RUN)
    assert [p.headers["Cookie"] for p in sap.posts] == [
        "SAP_SESSIONID_XYZ_100=sess-1",
        *["SAP_SESSIONID_XYZ_100=sess-submit"] * 5,
    ]
    assert len(sap.gets) == 1


# ------------------------------------------------------------------ the 403 redo


async def test_a_403_on_the_submit_redoes_the_whole_sequence_once():
    sap = Sap()
    sap.submit_answers = [required_403(), fixture("submit_ok.json")]
    sap.answers = [fixture("run_ok.json")]
    out = await make(sap, known=definition()).run(**RUN)
    assert out["row_count"] == 3
    assert sap.kinds == ["GET", "submit", "GET", "submit", "read", "close"]
    assert [p.headers["X-CSRF-Token"] for p in sap.posts] == ["csrf-1", *["csrf-2"] * 3]


async def test_a_403_on_the_read_redoes_the_whole_sequence_once():
    sap = Sap()
    sap.answers = [required_403(), fixture("run_ok.json")]
    out = await make(sap, known=definition()).run(**RUN)
    assert out["row_count"] == 3
    assert sap.kinds == ["GET", "submit", "read", "GET", "submit", "read", "close"]
    assert [p.headers["X-CSRF-Token"] for p in sap.posts] == [*["csrf-1"] * 2, *["csrf-2"] * 3]
    assert len({instance_of(r) for r in sap.posts}) == 1


@pytest.mark.parametrize(
    ("second", "code"),
    [(required_403, "csrf_failed"), (lambda: httpx.Response(403), "forbidden")],
)
async def test_a_second_403_ends_the_run_and_closes_nothing(second, code):
    sap = Sap()
    sap.submit_answers = [required_403(), second()]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == code
    assert sap.kinds == ["GET", "submit", "GET", "submit"]


# --------------------------------------------------------- refusals and the Close


async def test_a_refused_submit_is_the_runs_error_and_the_instance_is_closed():
    sap = Sap()
    sap.submit_answers = [fixture("run_sia_variables_missing.json")]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "sap_error"
    assert error.message.startswith("Query kann nur dann ausgeführt werden")
    assert sap.kinds == ["GET", "submit", "close"]


async def test_a_refused_read_is_the_runs_error_and_the_instance_is_closed():
    sap = Sap()
    sap.answers = [{"Messages": [{"Type": 2, "Text": "Dump in the read"}]}]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "sap_error" and error.message == "Dump in the read"
    assert sap.kinds == ["GET", "submit", "read", "close"]


async def test_a_submit_answer_over_the_byte_cap_is_result_too_large(monkeypatch):
    monkeypatch.setattr(ina_client, "MAX_RESPONSE_BYTES", 1000)
    sap = Sap()
    sap.submit_answers = [{"Cube": {"Dimensions": []}, "padding": "z" * 5000}]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "result_too_large"
    assert sap.kinds == ["GET", "submit", "close"]


def _hangs(seconds: float):
    async def answer(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(seconds)
        return httpx.Response(200, json={})

    return answer


def _fails(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused https://sia.internal/?sap-password=hunter2")


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, text="<html>Dump at https://sia.internal/x</html>"),
        httpx.Response(403, headers={"x-csrf-token": "Required"}),
        {"Messages": [{"Type": 2, "Text": "Close refused at https://sia.internal"}]},
        _fails,
        _hangs(5),
    ],
    ids=["500", "403", "error message", "connection error", "hangs"],
)
async def test_a_close_that_fails_never_fails_the_run_and_logs_the_class_only(
    answer, monkeypatch, caplog
):
    monkeypatch.setattr(ina_client, "CLOSE_DEADLINE_SECONDS", 0.05)
    sap = Sap()
    sap.answers = [fixture("run_ok.json")]
    sap.close_answers = [answer]
    with caplog.at_level("INFO", logger="agents.ina.client"):
        out = await make(sap, known=definition()).run(**RUN)
    assert out["row_count"] == 3 and sap.kinds[-1] == "close"
    lines = [r.getMessage() for r in caplog.records]
    assert any("query instance was not closed" in line for line in lines), lines
    for line in lines:
        for leaked in ("sia.internal", "hunter2", "Dump", "refused", "csrf-", "sess-"):
            assert leaked not in line


async def test_the_total_deadline_covers_the_whole_sequence(monkeypatch):
    """Each request alone is inside the deadline; the sequence is not."""
    monkeypatch.setattr(ina_client, "CALL_DEADLINE_SECONDS", 0.3)

    async def slow(answer: Any) -> Any:
        async def respond(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.2)
            return httpx.Response(200, json=answer)

        return respond

    sap = Sap()
    sap.submit_answers = [await slow(fixture("submit_ok.json"))]
    sap.answers = [await slow(fixture("run_ok.json"))]
    error = await refused(make(sap, known=definition()).run(**RUN))
    assert error.code == "timeout"
    assert sap.kinds == ["GET", "submit", "read"]  # no time is left for a Close


async def test_a_run_cancelled_during_the_read_propagates_and_sends_no_close():
    """A cancelled caller (the admin Test route's budget, a chat turn that is
    stopped) gets its CancelledError; no Close is sent while the task is
    being cancelled, so the instance lives until ICF ends the SAP session."""
    reading = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        reading.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json=fixture("run_ok.json"))

    sap = Sap()
    sap.answers = [hang]
    task = asyncio.create_task(make(sap, known=definition()).run(**RUN))
    await asyncio.wait_for(reading.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sap.kinds == ["GET", "submit", "read"]
    assert sap.closes == []


# --------------------------------------------------------- definitions old and new


async def test_an_older_definition_sends_and_reads_the_measures_structure():
    """A row described before 2.37.1 names no structure and no key field."""
    old = InaDefinition.model_validate({
        "dimensions": [{"name": "0CALMONTH"}],
        "measures": [{"name": "0AMOUNT"}],
        "variables": [{"name": "0P_CALYEAR", "mandatory": True}],
    })
    sap = Sap()
    sap.answers = [grid(["0CALMONTH"], [["202601"]], ["0AMOUNT"], [{"Value": 7, "Unit": "EUR"}])]
    out = await make(sap, known=old).run(["0AMOUNT"], ["0CALMONTH"],
                                         variables={"0P_CALYEAR": "2026"})
    read = json.loads(sap.reads[0].content)["Analytics"]["Definition"]
    assert read["Dimensions"][-1]["Name"] == "Measures"
    assert read["Variables"][0]["Values"]["Selection"]["SetOperand"]["FieldName"] == "0P_CALYEAR"
    assert out["rows"] == [["202601", 7]] and out["units"] == {"0AMOUNT": "EUR"}


async def test_the_recorded_sia_run_end_to_end():
    """SIA's describe, submit and grid answers (sanitised, runs 5 and 8)."""
    described = read_description(fixture("describe_sia.json")["Cube"]).definition
    sap = Sap()
    sap.submit_answers = [fixture("submit_sia.json")]
    sap.answers = [fixture("run_sia_grid.json")]
    out = await make(sap, known=described).run(
        ["ELTUIDCMP1"], ["2CIMMPURGORG", "2CI_SUPPLIER_CDS"], top=3, variables=SIA_VALUES)
    assert out["columns"] == ["2CIMMPURGORG", "2CIMMPURGORG.text", "2CI_SUPPLIER_CDS",
                              "2CI_SUPPLIER_CDS.text", "ELTUIDCMP1"]
    assert out["row_count"] == 3 and out["truncated"] is True
    assert sap.kinds == ["GET", "submit", "read", "close"]
    submit = json.loads(sap.submits[0].content)["Analytics"]["Definition"]
    assert [d.get("Attributes") for d in submit["Dimensions"]] == [None, None, None]
