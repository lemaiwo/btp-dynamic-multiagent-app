"""What a function import of a ``generic`` service returns: the review round of
task 3c (addendum (k), point 4b, of the web chat design).

Beside ``tests/test_odata_generic.py``, whose helpers and audit fixture are
used here: the real toolset on a mock SAP, recording through the real
``StoredWriteRecorder``. No network.

* An import bound to a catalogue entity set and naming no ``returns``: only
  rows that positively say they are of a complex type are passed on whole;
  rows without a type are for the entity rule, which cuts or withholds.
* Server paging (``__next``) of a generic READ is refused also where the
  rows are entities of a catalogue set; an ``odata`` service, and every
  write, keeps its answer as before.
* An object without a property is ``nothing``.
* A flat object stays withheld wherever the whole-result rule does not apply.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agents.odata.client import (  # noqa: E402
    WHOLE_COLLECTION,
    WHOLE_RETURNS,
    WHOLE_STRUCTURE,
)
from tests.test_odata_generic import (  # noqa: E402
    BOUND_CALL,
    CALL,
    LH,
    MONTHS,
    NEXT,
    PAGED_HINT,
    RESULT_TYPE,
    SCARR,
    Sap,
    _bound_import,
    _definition,
    _empty_audit_table,  # noqa: F401
    _get_import,
    _withheld,
    _writes,
    all_rows,
    code,
    execute,
    service,
    toolset,
)

FLAT = {"MNR": "01", "LTX": "January"}
CUT = {"CARRID": "LH", "CARRNAME": "Lufthansa"}  # CURRCODE is no catalogue field of SCARRSet
RETURNS = {"entity_set": "SCARRSet", "collection": True}


def test_the_two_words_are_named_constants():
    assert (WHOLE_STRUCTURE, WHOLE_COLLECTION) == ("structure", "collection")
    assert WHOLE_RETURNS == (WHOLE_STRUCTURE, WHOLE_COLLECTION)


# -- a bound import without `returns` ----------------------------------------------


def _bound_tools(sap: Sap) -> Any:
    return toolset({"agent-data": service(definition=_definition([_bound_import()]))}, sap)


async def test_typed_complex_rows_of_a_bound_import_come_whole():
    """What says it is of a complex type is the module's answer, bound or not."""
    sap = Sap()
    tools = _bound_tools(sap)
    row = {"__metadata": {"type": RESULT_TYPE}, **FLAT}
    sap.answer = {"d": {"results": [row]}}
    out = await execute(tools, **BOUND_CALL)
    assert (out["returned"], out["result"]) == ("collection", [FLAT])
    sap.answer = {"d": {MONTHS: row}}
    out = await execute(tools, **BOUND_CALL)
    assert (out["returned"], out["result"]) == ("structure", FLAT)


@pytest.mark.parametrize(
    "rows",
    [
        [LH],  # no `__metadata` at all
        [{"__metadata": {}, **LH}],  # one without a type
        [{"__metadata": {"type": ""}, **LH}],  # an empty type is no type
        [{"__metadata": {"type": RESULT_TYPE}, **FLAT}, LH],  # one row of two says nothing
    ],
)
async def test_untyped_rows_of_a_bound_import_never_pass_the_set_s_field_cut(rows):
    sap = Sap()
    sap.answer = {"d": {"results": rows}}
    out = await execute(_bound_tools(sap), **BOUND_CALL)
    assert out == _withheld()
    assert "EUR" not in json.dumps(out) and "Lufthansa" not in json.dumps(out)


# -- server paging of catalogue entities -------------------------------------------


def _scarr(**meta: Any) -> dict[str, Any]:
    return {"__metadata": {"type": SCARR, **meta}, **LH}


PAGED_ENTITIES = {"d": {"results": [_scarr(uri="SCARRSet('LH')")], "__next": NEXT}}


@pytest.mark.parametrize(
    "operation, call",
    [
        (_get_import(returns=RETURNS), {"service": "agent-data", "target": MONTHS,
                                        "operation": "call", "params": {"LANGUAGE": "E"}}),
        (_bound_import(), BOUND_CALL),
    ],
)
async def test_paged_entities_of_a_generic_read_are_refused(operation, call):
    sap = Sap()
    tools = toolset({"agent-data": service(definition=_definition([operation]))}, sap)
    for answer in (PAGED_ENTITIES, {"d": {MONTHS: PAGED_ENTITIES["d"]}}):
        sap.answer = answer
        out = await execute(tools, **call)
        assert set(out) == {"error"} and code(out) == "result_too_large"
        assert out["error"]["hint"] == PAGED_HINT
        assert "Lufthansa" not in json.dumps(out) and "skiptoken" not in json.dumps(out)
    assert len(sap.requests) == 2  # one per call: the next page is not fetched
    # Without `__next` the same rows are the set's entities, cut as before.
    sap.answer = {"d": {"results": [_scarr(uri="SCARRSet('LH')")]}}
    out = await execute(tools, **call)
    assert (out["returned"], out["result"], out["truncated"]) == ("entities", [CUT], False)


@pytest.mark.parametrize(
    "operation, call",
    [
        (_get_import(changes_data=False, returns=RETURNS), CALL),
        (_get_import(changes_data=False, bound_to="SCARRSet",
                     parameters=[{"name": "CARRID", "type": "Edm.String", "required": True}]),
         BOUND_CALL),
    ],
)
async def test_paged_entities_of_an_odata_service_are_answered_as_before(operation, call):
    """Unchanged for `odata`: a separate decision (tracked on the board)."""
    sap = Sap()
    tools = toolset({"agent-data": service("odata", definition=_definition([operation]))}, sap)
    sap.answer = PAGED_ENTITIES
    out = await execute(tools, **call)
    assert out == {"ok": True, "status": 200, "returned": "entities", "result": [CUT],
                   "truncated": False}


async def test_paged_entities_after_a_generic_write_are_its_confirmation_as_before():
    """A POST import is a write: no refusal may follow a change SAP made."""
    sap = Sap()
    posted = _get_import(http_method="POST", changes_data=False, returns=RETURNS)
    services = {"agent-data": service(definition=_definition([posted]))}
    sap.answer = PAGED_ENTITIES
    out = await execute(toolset(services, sap, _writes(list(services))), **CALL)
    assert (out["returned"], out["result"]) == ("entities", [CUT])
    assert [r.outcome for r in await all_rows()] == ["ok"]


# -- an object without a property ----------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [{"d": {}}, {"d": {MONTHS: {}}}, {"d": {"__metadata": {"type": RESULT_TYPE}}}],
)
async def test_an_empty_structure_is_nothing(answer):
    sap = Sap()
    for tools, call in ((toolset({"agent-data": service()}, sap), CALL),
                        (_bound_tools(sap), BOUND_CALL)):
        sap.answer = answer
        out = await execute(tools, **call)
        assert out == {"ok": True, "status": 200, "returned": "nothing", "result": None,
                       "truncated": False}


async def test_an_empty_table_is_still_an_empty_collection():
    sap = Sap()
    sap.answer = {"d": {"results": []}}
    out = await execute(toolset({"agent-data": service()}, sap), **CALL)
    assert (out["returned"], out["result"]) == ("collection", [])


# -- a flat object where the whole-result rule does not apply ------------------------


@pytest.mark.parametrize(
    "kind, operation, call, allow_write",
    [
        ("odata", _get_import(), CALL, True),  # a write of an `odata` service
        # A POST import of a generic service is a write: its answer is the confirmation.
        ("generic", _get_import(http_method="POST", changes_data=False), CALL, True),
        # `odata` reads that name the set they return, or are bound to.
        ("odata", _get_import(changes_data=False, returns=RETURNS), CALL, False),
        ("odata", _get_import(changes_data=False, bound_to="SCARRSet",
                              parameters=[{"name": "CARRID", "type": "Edm.String",
                                           "required": True}]),
         BOUND_CALL, False),
    ],
)
async def test_a_flat_object_is_withheld_where_nothing_is_passed_on_whole(
    kind, operation, call, allow_write
):
    sap = Sap()
    services = {"agent-data": service(kind, definition=_definition([operation]))}
    tools = toolset(services, sap, _writes(list(services)) if allow_write else None)
    for answer in ({"d": FLAT}, {"d": {MONTHS: FLAT}}):
        sap.answer = answer
        out = await execute(tools, **call)
        assert out == _withheld(), answer
        assert "January" not in json.dumps(out)
    assert [r.outcome for r in await all_rows()] == (["ok", "ok"] if allow_write else [])
