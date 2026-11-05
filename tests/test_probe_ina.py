"""The request builders of ``scripts/probe_ina.py``.

The probe is standalone (nothing of the app is imported), so its read, its
VariableSubmit, its Close and its reading of the submit answer's text
attributes are copies of ``agents.ina.protocol``'s ``analytics_request``,
``variable_submit_request``, ``close_request`` and ``text_attributes``;
these tests hold the copies to the originals, so a recorded exchange is the
exchange the app makes. They also pin the switches that try SAP's own
client's form (the variable named by its dimension's key, the
``VariableSubmit`` before the read) and that a dump prints only a name and a
size.

Run:  python -m pytest tests/test_probe_ina.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import probe_ina as probe  # noqa: E402

from agents.ina.models import InaDefinition  # noqa: E402
from agents.ina.protocol import analytics_request, data_source, plan_run  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ina"
CAPABILITIES = ["ClientCapabilities", "StatefulServer"]


def sia_cube() -> dict:
    return json.loads((FIXTURES / "describe_sia.json").read_text(encoding="utf-8"))["Cube"]


def without_client_info(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "ClientInfo"}


def app_body(variables: dict[str, str] | None = None) -> dict:
    definition = InaDefinition.model_validate({
        "measures": [{"name": "ELTUIDCMP1"}],
        "variables": [{"name": n, "mandatory": True, "type": "single"} for n in variables or {}],
    })
    plan = plan_run(definition, measures=["ELTUIDCMP1"], group_by=[], filters=None, top=1,
                    order=None, variables=variables)
    return analytics_request(CAPABILITIES, data_source("2CCMMPURORDQUERY", ""), plan)


def test_the_probes_run_is_the_apps_run():
    body = probe._run_request(CAPABILITIES, "2CCMMPURORDQUERY", "ELTUIDCMP1", [])
    assert without_client_info(body) == without_client_info(app_body())


def test_the_probes_variables_are_the_apps_variables():
    values = {"!CDS_P_P_DISPLAYCURRENCY": "EUR", "!CDS_P_P_DATEFUNCTION": "YEARTODATE"}
    body = probe._run_request(CAPABILITIES, "2CCMMPURORDQUERY", "ELTUIDCMP1", list(values.items()))
    assert without_client_info(body) == without_client_info(app_body(values))


def test_the_first_measure_and_the_variable_fields_of_the_sia_describe():
    cube = sia_cube()
    assert probe._first_measure(cube) == "ELTUIDCMP1"
    fields = probe._variable_fields(cube)
    assert fields["!CDS_P_P_DISPLAYCURRENCY"] == "2CH0Y9FATWMXU07PABNUMVE2HHX.KEY"
    assert fields["!CDS_P_P_DATEFUNCTION"] == "2CCGCALDFVH.KEY"


def test_the_sap_form_names_the_value_by_the_dimension_key():
    entry = probe._variable_entry("!V", "EUR", "DIM.KEY")
    operand = entry["Values"]["Selection"]["SetOperand"]
    assert operand == {
        "FieldName": "DIM.KEY",
        "Elements": [{"Comparison": "=", "Low": "EUR", "IsExcluding": False}],
    }


def test_the_submit_is_a_copy_of_the_read_on_one_instance():
    body = probe._run_request(CAPABILITIES, "Q", "M", [("!V", "EUR")], structure="S")
    submit, read = probe._batch_with_submit(body, "probe1")["Batch"]
    assert submit["Analytics"]["ProcessingDirectives"] == {"ProcessingStep": "VariableSubmit"}
    assert "ProcessingDirectives" not in read["Analytics"]
    assert {submit["Analytics"]["DataSource"]["InstanceId"],
            read["Analytics"]["DataSource"]["InstanceId"]} == {"probe1"}
    assert read["Analytics"]["Definition"]["Dimensions"][0]["Name"] == "S"
    assert "InstanceId" not in body["Analytics"]["DataSource"]  # the original is untouched


def test_a_dump_prints_its_name_and_size_only(tmp_path, capsys):
    raw = b'{"Grids": [{"Cells": [4711]}]}'
    probe._dump(str(tmp_path / "d"), "run.response.json", raw)
    assert (tmp_path / "d" / "run.response.json").read_bytes() == raw
    out = capsys.readouterr().out
    assert out == f"DUMP run.response.json bytes={len(raw)}\n" and "4711" not in out
    probe._dump("", "x.json", b"{}")
    assert capsys.readouterr().out == ""


GROUP_BY = ["2CIMMPURGORG", "2CIPLANT", "2CCMMPURORDSPND-COMPANYCODE"]
VALUES = {"!CDS_P_P_DISPLAYCURRENCY": "EUR", "!CDS_P_P_DATEFUNCTION": "YEARTODATE"}


def sia_plan():
    from agents.ina.protocol import read_description

    definition = read_description(sia_cube()).definition
    return plan_run(definition, measures=["ELTUIDCMP1"], group_by=GROUP_BY,
                    filters=None, top=3, order=None, variables=VALUES)


def probe_body(**kw) -> dict:
    return probe._run_request(
        CAPABILITIES, "2CCMMPURORDQUERY", "ELTUIDCMP1", list(VALUES.items()), top=3,
        structure="ELTUIDSTRCMMPURORDQUERY", fields=probe._variable_fields(sia_cube()),
        group_by=GROUP_BY, instance="probe1", **kw,
    )


def submit_cube() -> dict:
    return json.loads((FIXTURES / "submit_sia.json").read_text(encoding="utf-8"))["Cube"]


def test_the_probes_texts_are_the_apps_texts():
    from agents.ina.protocol import text_attributes

    texts = probe._texts(submit_cube(), GROUP_BY)
    assert texts == text_attributes(submit_cube(), GROUP_BY)
    assert texts == {"2CIMMPURGORG": "2CIMMPURGORG.LONG_TEXT", "2CIPLANT": "2CIPLANT.LONG_TEXT"}


def test_the_probes_grouped_read_on_an_instance_is_the_apps_read():
    """The form runs 5 to 8 answered (the variables named by their
    dimension's key, the real structure, each row's key and text, totals
    hidden) on an InstanceId: the app's body."""
    from dataclasses import replace

    texts = probe._texts(submit_cube(), GROUP_BY)
    plan = replace(sia_plan(), texts=texts)
    app = analytics_request(CAPABILITIES, data_source("2CCMMPURORDQUERY", "", "probe1"), plan)
    assert without_client_info(probe_body(texts=texts)) == without_client_info(app)


def test_the_probes_submit_is_the_apps_submit():
    from agents.ina.protocol import variable_submit_request

    app = variable_submit_request(
        CAPABILITIES, data_source("2CCMMPURORDQUERY", "", "probe1"), sia_plan())
    assert without_client_info(probe_body(submit=True)) == without_client_info(app)


def test_the_probes_close_is_the_apps_close():
    from agents.ina.protocol import close_request

    assert probe._close_request("probe1") == close_request("probe1")


def test_a_dump_outside_the_temp_directory_is_refused(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(probe.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    for directory in (str(tmp_path / "elsewhere"), str(tmp_path / "tmp"),
                      str(tmp_path / "tmp" / ".." / "out")):
        with pytest.raises(probe.ProbeError) as info:
            probe._dump(directory, "x.json", b"{}")
        assert str(info.value) == probe.DUMP_REFUSED
    assert not (tmp_path / "elsewhere").exists() and not (tmp_path / "out").exists()


def test_a_dump_is_private_to_its_owner(tmp_path, capsys):
    target = tmp_path / "dumps"
    probe._dump(str(target), "run.response.json", b"{}")
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / "run.response.json").stat().st_mode & 0o777 == 0o600


def test_a_label_is_a_plain_file_name_stem():
    import argparse

    import pytest

    assert probe._label("run-6_b") == "run-6_b"
    for bad in ("", "../x", "a/b", "a.b", "x" * 41, "run 6"):
        with pytest.raises(argparse.ArgumentTypeError):
            probe._label(bad)


COSTCENTER = "2CCMMPURORDSPND-COSTCENTER"
ORDERS = [
    [("ELTUIDCMP1", "desc")],
    [(COSTCENTER, "desc")],
    [(COSTCENTER, "asc"), ("ELTUIDCMP1", "desc")],
]


def ordered(order: list[tuple[str, str]], *, submit: bool) -> tuple[dict, dict]:
    """The probe's and the app's request of one ordered run (2026-11-05: top
    5 cost centres of the SIA query), the read or the submit."""
    from agents.ina.protocol import read_description, variable_submit_request

    plan = plan_run(read_description(sia_cube()).definition, measures=["ELTUIDCMP1"],
                    group_by=[COSTCENTER], filters=None, top=5,
                    order=[{"by": n, "direction": d} for n, d in order], variables=VALUES)
    build = variable_submit_request if submit else analytics_request
    app = build(CAPABILITIES, data_source("2CCMMPURORDQUERY", "", "probe1"), plan)
    body = probe._run_request(
        CAPABILITIES, "2CCMMPURORDQUERY", "ELTUIDCMP1", list(VALUES.items()), top=5,
        structure="ELTUIDSTRCMMPURORDQUERY", fields=probe._variable_fields(sia_cube()),
        group_by=[COSTCENTER], instance="probe1", submit=submit, order=order,
    )
    return body, app


def test_the_probes_order_is_the_apps_sort():
    """``--order`` sends the ``Sort`` ``plan_run`` builds (a measure without
    ``Dimension``, a dimension by its key), in the read and in the submit."""
    for order in ORDERS:
        for submit in (False, True):
            body, app = ordered(order, submit=submit)
            assert without_client_info(body) == without_client_info(app), (order, submit)


def test_the_other_sort_forms_name_a_dimension_for_the_measure():
    """The readings the probe tried besides the app's: the measure structure
    as ``Dimension`` (2.37.1's form, ignored by SIA) and the row dimension."""
    order = [("M", "desc")]
    assert probe._sort(order, "M", "S", ["D"], "structure") == [
        {"SortType": "Measure", "MeasureName": "M", "Direction": "Desc", "Dimension": "S"}]
    assert probe._sort(order, "M", "S", ["D"], "rows") == [
        {"SortType": "Measure", "MeasureName": "M", "Direction": "Desc", "Dimension": "D"}]


def test_an_order_is_name_equals_asc_or_desc():
    import pytest

    assert probe._parse_order(["M=desc", "D=asc"]) == [("M", "desc"), ("D", "asc")]
    for bad in ("M", "=desc", "M=down", "M=DESC"):
        with pytest.raises(probe.ProbeError):
            probe._parse_order([bad])


def test_the_order_line_says_how_the_rows_came_back_never_a_value():
    payload = {"Grids": [{
        "Axes": [{"Type": "Rows", "TupleCount": 3, "TupleCountTotal": 352}],
        "CellArraySizes": [3, 1],
        "Cells": {"Values": {"Encoding": "None", "Size": 3, "Values": [9471.5, 812.25, 3.75]}},
    }]}
    line = probe._seen_order(payload)
    assert line == "order=desc rows=3 tuple_count=3 tuple_count_total=352"
    assert "9471" not in line
    payload["Grids"][0]["Cells"]["Values"]["Values"] = [3.75, 9471.5, 812.25]
    assert probe._seen_order(payload).startswith("order=unordered ")
    assert probe._seen_order({}) == "order=- rows=0 tuple_count= tuple_count_total="
