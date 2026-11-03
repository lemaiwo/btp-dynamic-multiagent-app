"""The generic kind of a catalogue service (addendum (k), part 3, of the web chat design).

A ``generic`` service is the ZAGENT_DATA registration: a V2 service whose
objects come from SAP's own allow-list. Two things differ from ``odata``:

* the write rule: a ``GET`` function import is a read whatever its stored
  ``changes_data`` says (the allow-list owner vouches that a listed module
  only reads), so it runs without ``allow_write`` and is not audited. Every
  other call, and every entity write, keeps the rule of ``odata``;
* the gate: a generic service is OData V2, refused at ``kind`` otherwise.

And one thing is the same for both, pinned here on a document of that
service: the ``sap:label`` of an entity set and of a function import is read
as its label, which the import takes over as the title.

The real toolset on a mock SAP (``httpx.MockTransport`` behind the real
``destination_http_client``), recording through the real
``StoredWriteRecorder`` into this process's test database. No network.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents.data_catalog.importer import plan_service  # noqa: E402
from agents.db import ODataAuditLog, ODataService, SessionLocal, init_db  # noqa: E402
from agents.odata.audit import StoredWriteRecorder  # noqa: E402
from agents.odata.calls import stored_call_refusal  # noqa: E402
from agents.odata.client import ODataClient, call_changes_data, call_refusal  # noqa: E402
from agents.odata.metadata import ParsedReturn, parse_metadata  # noqa: E402
from agents.odata.models import (  # noqa: E402
    ODataServicePayload,
    OperationDef,
    ServiceDefinition,
    operation_is_write,
    validate_odata_service,
)
from agents.odata.preview import build_preview  # noqa: E402
from agents.odata.search import operation_changes_data, search_catalogue  # noqa: E402
from agents.odata.tools import odata_toolset  # noqa: E402
from agents.odata.v2 import V2Dialect  # noqa: E402
from tests.odata_helpers import FakeResolver  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "odata" / "zagent_data_metadata.xml"
SERVICE_PATH = "/sap/opu/odata/sap/ZAGENT_DATA_SRV"
CTX = SimpleNamespace(run_id="run-1")
MONTHS = "MONTH_NAMES_GET"


def _get_import(name: str = MONTHS, **kw: Any) -> dict[str, Any]:
    """A GET function import as an admin enables it: ``changes_data`` left at
    the models' default (True), so "whatever it says" is the worst case."""
    return {
        "name": name,
        "title": "Month names (smoke test)",
        "kind": "function_import",
        "http_method": "GET",
        "enabled": True,
        "parameters": [{"name": "LANGUAGE", "type": "Edm.String", "required": True}],
        **kw,
    }


def _definition(operations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "entity_sets": [
            {
                "name": "SCARRSet",
                "title": "Demo airlines (smoke test)",
                "entity_type": "ZAGENT_DATA_SRV.SCARR",
                "keys": [{"name": "CARRID"}],
                "operations": ["list", "get"],
                "fields": [
                    {"name": "CARRID", "label": "Airline", "selectable": True, "filterable": True},
                    {"name": "CARRNAME", "label": "Airline name", "selectable": True},
                ],
            }
        ],
        "operations": [_get_import()] if operations is None else operations,
    }


def payload(kind: str = "generic", **patch: Any) -> dict[str, Any]:
    """One catalogue service as the admin API takes it."""
    return {
        "name": "agent-data",
        "kind": kind,
        "title": "SAP allow-listed data",
        "purpose": "Tables and function modules SAP lists for agents",
        "destination": "AGENT_DATA_TECH",
        "user_context": False,
        "odata_version": "v2",
        "service_path": SERVICE_PATH,
        "definition": _definition(),
        **patch,
    }


def service(kind: str = "generic", **patch: Any) -> dict[str, Any]:
    """The same service as ``ODataService.to_dict()`` hands it on."""
    clean = validate_odata_service(payload(kind, **patch))
    return {**clean, "id": 1, "counts": {}, "has_write": False, "used_by": []}


async def all_rows() -> list[ODataAuditLog]:
    async with SessionLocal() as s:
        return list((await s.execute(select(ODataAuditLog).order_by(ODataAuditLog.id))).scalars())


@pytest.fixture(autouse=True)
async def _empty_audit_table():
    """Another suite may own the engine: never assume a fresh file."""
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(ODataAuditLog))
        await s.commit()
    yield


def code(out: dict) -> str | None:
    return out.get("error", {}).get("code")


# -- the rule ---------------------------------------------------------------------


def test_a_get_function_import_of_a_generic_service_is_a_read_whatever_it_says():
    for changes_data in (True, False, None, "false"):
        assert operation_is_write(changes_data, "GET", "generic", "function_import") is False


def test_the_same_call_of_an_odata_service_keeps_the_rule():
    assert operation_is_write(True, "GET", "odata", "function_import") is True
    assert operation_is_write(None, "GET", "odata", "function_import") is True
    assert operation_is_write(False, "GET", "odata", "function_import") is False
    # The default kind is `odata`: the two-value form is the rule of before.
    assert operation_is_write(True, "GET") is True
    assert operation_is_write(False, "GET") is False


@pytest.mark.parametrize(
    "changes_data, method, operation_kind",
    [
        (False, "POST", "function_import"),  # a POST is a write on every kind
        (True, "POST", "function_import"),
        (True, "GET", None),  # what kind of operation is not known: the rule
        (True, "GET", "function"),  # a V4 function is no function import
        (True, "GET", "action"),
        (True, "get", "function_import"),  # exactly GET
        (True, None, "function_import"),
    ],
)
def test_everything_else_of_a_generic_service_keeps_the_rule(changes_data, method, operation_kind):
    assert operation_is_write(changes_data, method, "generic", operation_kind) is True


@pytest.mark.parametrize("kind", ["Generic", "generic ", "zagent", None, 1, True])
def test_only_the_exact_kind_relaxes_the_rule(kind):
    assert operation_is_write(True, "GET", kind, "function_import") is True


def test_the_model_and_the_stored_dict_decide_alike():
    op = OperationDef(name=MONTHS, kind="function_import", http_method="GET", enabled=True)
    assert op.changes_data is True  # the import's default
    assert op.is_write() is True and op.is_write("odata") is True
    assert op.is_write("generic") is False
    stored = op.model_dump()
    assert operation_changes_data(stored) is True
    assert operation_changes_data(stored, "generic") is False
    assert call_changes_data(op) is True and call_changes_data(op, "generic") is False
    post = OperationDef(name="X", kind="function_import", http_method="POST", enabled=True,
                        changes_data=False)
    assert post.is_write("generic") is True and operation_changes_data(
        post.model_dump(), "generic"
    ) is True


def test_call_refusal_applies_the_write_switch_by_kind():
    op = OperationDef.model_validate(_get_import())
    dialect = V2Dialect()
    assert call_refusal(op, None, dialect, allow_write=False) == "write_not_allowed"
    assert call_refusal(op, None, dialect, allow_write=False, kind="generic") is None
    stored = _get_import()
    assert stored_call_refusal(stored, {}, "v2", allow_write=False) == "write_not_allowed"
    assert stored_call_refusal(stored, {}, "v2", allow_write=False, kind="generic") is None


def test_has_write_follows_the_kind():
    generic = ODataServicePayload.model_validate(payload())
    odata = ODataServicePayload.model_validate(payload("odata"))
    assert generic.has_write() is False and odata.has_write() is True
    definition = ServiceDefinition.model_validate(_definition())
    assert definition.has_write() is True and definition.has_write("generic") is False
    # An entity write is a write on a generic service too.
    with_create = _definition()
    with_create["entity_sets"][0]["operations"].append("create")
    with_create["entity_sets"][0]["fields"][1]["writable"] = True
    assert ServiceDefinition.model_validate(with_create).has_write("generic") is True


def test_the_client_takes_the_kind_from_the_service():
    for kind, changes in (("generic", False), ("odata", True)):
        rules = ODataClient(None, service(kind), V2Dialect())  # type: ignore[arg-type]
        plan = rules.check_call(
            ServiceDefinition.model_validate(_definition()).operation(MONTHS),
            params={"LANGUAGE": "E"},
        )
        assert plan.changes is changes


def _row(kind: str, operations: list[dict[str, Any]]) -> ODataService:
    data = payload(kind, definition=_definition(operations))
    return ODataService(
        name=data["name"],
        kind=kind,
        title=data["title"],
        purpose=data["purpose"],
        not_for="",
        destination=data["destination"],
        user_context=0,
        odata_version="v2",
        service_path=SERVICE_PATH,
        definition_json=json.dumps(data["definition"]),
        enabled=1,
    )


def test_the_service_list_flags_writes_by_kind():
    assert _row("generic", [_get_import()]).to_summary()["has_write"] is False
    assert _row("odata", [_get_import()]).to_summary()["has_write"] is True
    post = _get_import("POSTED", http_method="POST", changes_data=False)
    assert _row("generic", [post]).to_summary()["has_write"] is True


def test_the_catalogue_import_reads_a_generic_get_import_as_readable():
    def function_node(kind: str) -> dict[str, Any]:
        planned, _ = plan_service(_row(kind, [_get_import()]))
        (node,) = [p for p in planned if p.kind == "function"]
        source = planned[0]
        return {"node": node.attrs, "source": source.attrs}

    generic, odata = function_node("generic"), function_node("odata")
    assert (generic["node"]["readable"], generic["node"]["writable"]) == (1, 0)
    assert generic["source"]["writable"] == 0
    assert (odata["node"]["readable"], odata["node"]["writable"]) == (0, 1)
    assert odata["source"]["writable"] == 1


# -- the gate ---------------------------------------------------------------------


def test_a_generic_service_validates():
    p = ODataServicePayload.model_validate(payload())
    assert p.kind == "generic"
    assert validate_odata_service(payload())["kind"] == "generic"


def test_another_kind_is_refused_at_kind():
    with pytest.raises(ValidationError) as refused:
        ODataServicePayload.model_validate({**payload(), "kind": "other"})
    assert [e["loc"] for e in refused.value.errors()] == [("kind",)]


def test_a_generic_service_is_odata_v2():
    data = payload()
    data.update(odata_version="v4", definition={"entity_sets": [], "operations": []})
    with pytest.raises(ValidationError) as refused:
        ODataServicePayload.model_validate(data)
    assert [(e["loc"], e["msg"]) for e in refused.value.errors()] == [
        (("kind",), "a generic service is OData V2")
    ]
    with pytest.raises(ValueError) as err:
        validate_odata_service(data)
    assert str(err.value) == "kind: a generic service is OData V2"
    # Before the operations are looked at: the version is the reason.
    data["definition"] = _definition()
    with pytest.raises(ValueError) as err:
        validate_odata_service(data)
    assert str(err.value) == "kind: a generic service is OData V2"
    data["kind"] = "odata"
    with pytest.raises(ValueError) as err:
        validate_odata_service(data)
    assert "a v4 service allows only" in str(err.value)


# -- through the toolset ----------------------------------------------------------


class Sap:
    """SAP: answers the function import, and anything else with an error."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer: Any = {
            "d": {
                "results": [
                    {
                        "__metadata": {"type": "ZAGENT_DATA_SRV.MONTH_NAMES_GET_RESULT"},
                        "SPRAS": "E",
                        "MNR": "01",
                        "KTX": "Jan",
                        "LTX": "January",
                    }
                ]
            }
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("X-CSRF-Token") == "Fetch":
            return httpx.Response(200, headers={"X-CSRF-Token": "T"}, json={"d": {}})
        if request.url.path == f"{SERVICE_PATH}/{MONTHS}":
            return httpx.Response(200, json=self.answer)
        return httpx.Response(404, json={"error": {"code": "X", "message": {"value": "no"}}})


def toolset(services: dict[str, dict], sap: Sap, oauth: dict[str, Any] | None = None) -> Any:
    return odata_toolset(
        oauth if oauth is not None else {"services": list(services)},
        auth_mode="destination",
        services=services,
        agent_name="analyst",
        transport=httpx.MockTransport(sap.handler),
        resolver_factory=lambda destination: FakeResolver(name=destination),
        recorder=StoredWriteRecorder(),
    )


async def search(tools: Any, **args: Any) -> dict:
    return await tools.tools["search_operations"].function(**args)


async def execute(tools: Any, **args: Any) -> dict:
    return await tools.tools["execute_operation"].function(CTX, **args)


CALL = {"service": "agent-data", "target": MONTHS, "operation": "call",
        "params": {"LANGUAGE": "E"}}


async def test_without_allow_write_a_generic_get_import_is_listed_and_runs_unrecorded():
    sap = Sap()
    tools = toolset({"agent-data": service()}, sap)
    found = await search(tools, query="month names", detail="full")
    (match,) = [m for m in found["matches"] if m["target"] == MONTHS]
    assert match["kind"] == "operation" and match["changes_data"] is False
    assert match["title"] == "Month names (smoke test)"

    out = await execute(tools, **CALL)
    assert out["ok"] is True and out["status"] == 200
    # A complex type is not tied to a catalogue entity set: not passed on.
    assert out["returned"] == "withheld" and out["result"] is None
    (sent,) = sap.requests  # no CSRF token request: sent as a read
    assert sent.method == "GET" and sent.url.path == f"{SERVICE_PATH}/{MONTHS}"
    assert dict(sent.url.params) == {"LANGUAGE": "'E'", "$format": "json"}
    assert "X-CSRF-Token" not in sent.headers
    assert await all_rows() == []  # a read is not recorded


async def test_the_same_service_as_odata_needs_allow_write():
    sap = Sap()
    tools = toolset({"agent-data": service("odata")}, sap)
    found = await search(tools, query="month names", detail="full")
    assert [m for m in found["matches"] if m["target"] == MONTHS] == []
    out = await execute(tools, **CALL)
    assert code(out) == "write_not_allowed"
    assert sap.requests == [] and await all_rows() == []


async def test_a_posted_import_of_a_generic_service_is_still_a_write():
    sap = Sap()
    posted = _get_import(http_method="POST", changes_data=False)
    tools = toolset({"agent-data": service(definition=_definition([posted]))}, sap)
    found = await search(tools, query="month names")
    assert [m for m in found["matches"] if m["target"] == MONTHS] == []
    assert code(await execute(tools, **CALL)) == "write_not_allowed"
    assert sap.requests == [] and await all_rows() == []


def test_search_over_the_snapshot_lists_by_kind():
    def listed(kind: str) -> list[str]:
        out = search_catalogue([service(kind)], "", allow_call=True)
        return [m["target"] for m in out["matches"] if m["kind"] == "operation"]

    assert listed("generic") == [MONTHS] and listed("odata") == []


# -- the $metadata of a generic service -------------------------------------------


def _parsed() -> Any:
    return parse_metadata(FIXTURE.read_bytes(), "v2")


def test_the_fixture_parses_with_set_and_function_labels():
    md = _parsed()
    assert md.skipped == () and md.truncated is False
    sets = {e.name: e for e in md.entity_sets}
    assert list(sets) == ["SCARRSet", "T000Set"]
    # The set's own label, not the entity type's ("Airline").
    assert sets["SCARRSet"].label == "Demo airlines (smoke test)"
    assert sets["T000Set"].label == "Clients of the system (smoke test)"
    assert {f.name: f.label for f in sets["SCARRSet"].fields} == {
        "CARRID": "Airline", "CARRNAME": "Airline name", "CURRCODE": "Airline currency",
    }
    assert not any(e.creatable or e.updatable or e.deletable for e in sets.values())
    (op,) = md.operations
    assert (op.name, op.kind, op.http_method, op.bound_to) == (
        MONTHS, "function_import", "GET", None,
    )
    assert op.label == "Month names (smoke test)"
    assert [(p.name, p.type, p.required) for p in op.parameters] == [
        ("LANGUAGE", "Edm.String", True)
    ]
    # A complex collection: many come back, of no catalogue entity set.
    assert op.returns == ParsedReturn(entity_set=None, collection=True, type="")


def test_the_preview_carries_the_set_and_function_labels():
    preview = build_preview(_parsed())
    assert [(e["name"], e["label"]) for e in preview["entity_sets"]] == [
        ("SCARRSet", "Demo airlines (smoke test)"),
        ("T000Set", "Clients of the system (smoke test)"),
    ]
    (op,) = preview["operations"]
    assert op["label"] == "Month names (smoke test)"
    # What a V2 GET import changes is not known from the document: the safe
    # suggestion. On a generic service it does not matter (the rule above).
    assert op["suggested"]["changes_data"] is True and op["suggested"]["known"] is False


def test_a_long_or_invisible_set_or_function_label_is_one_line_of_at_most_120():
    xml = FIXTURE.read_text(encoding="utf-8")
    long = "Airlines " + "x" * 300
    xml = xml.replace('sap:label="Demo airlines (smoke test)"', f'sap:label="{long}"')
    xml = xml.replace(
        'sap:label="Month names (smoke test)"', 'sap:label="Month&#x202E; names&#10;(x)"'
    )
    md = parse_metadata(xml.encode("utf-8"), "v2")
    label = md.entity_sets[0].label
    assert label.startswith("Airlines x") and len(label) == 120
    assert md.operations[0].label == "Month names (x)"


def test_the_labels_become_titles_the_gate_and_the_catalogue_take():
    """What the import dialog stores from the preview (``title`` = SAP's
    label): a valid generic service whose titles reach the catalogue nodes."""
    preview = build_preview(_parsed())
    definition = {
        "entity_sets": [
            {
                "name": e["name"],
                "title": e["label"],
                "entity_type": e["entity_type"],
                "keys": e["keys"],
                "operations": ["list"],
                "fields": [
                    {"name": f["name"], "type": f["type"], "label": f["label"],
                     "selectable": True}
                    for f in e["fields"]
                ],
            }
            for e in preview["entity_sets"]
        ],
        "operations": [
            {
                "name": o["name"],
                "title": o["label"],
                "kind": o["kind"],
                "http_method": o["http_method"],
                "parameters": o["parameters"],
                "enabled": True,
                "changes_data": o["suggested"]["changes_data"],
            }
            for o in preview["operations"]
        ],
    }
    clean = validate_odata_service(payload(definition=copy.deepcopy(definition)))
    titles = [e["title"] for e in clean["definition"]["entity_sets"]]
    assert titles == ["Demo airlines (smoke test)", "Clients of the system (smoke test)"]
    assert clean["definition"]["operations"][0]["title"] == "Month names (smoke test)"
    planned, _ = plan_service(SimpleNamespace(**{**clean, "not_for": ""}))
    labels = {(p.kind, p.name): p.attrs["label"] for p in planned}
    assert labels[("entity", "SCARRSet")] == "Demo airlines (smoke test)"
    assert labels[("function", MONTHS)] == "Month names (smoke test)"
    assert next(p for p in planned if p.kind == "function").attrs["readable"] == 1
