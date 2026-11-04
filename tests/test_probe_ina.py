"""The request builders of ``scripts/probe_ina.py``.

The probe is standalone (nothing of the app is imported), so its run body is
a copy of ``agents.ina.protocol.analytics_request``; these tests hold the
copy to the original, so a recorded exchange is the exchange the app makes.
They also pin the switches that try SAP's own client's form (the variable
named by its dimension's key, the ``VariableSubmit`` before the read) and
that a dump prints only a name and a size.

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
