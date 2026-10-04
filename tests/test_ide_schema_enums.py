"""Server-controlled enums of the IDE response models (B5 review follow-up).

A field whose values the server alone decides is a ``Literal`` in
``agents.ide.schemas``, so the exported schema carries an ``enum`` the UI's
contract test checks its fake backend against, and a serialiser that emits a
wrong value fails instead of reaching the UI.

Run:  python -m pytest tests/test_ide_schema_enums.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from agents.ide import schemas  # noqa: E402
from agents.ide.stages import Stage  # noqa: E402

STAGES = {s.value for s in Stage}
DOC_KINDS = {"design", "plan", "review", "note", "report"}

ENUMS = {
    ("MessageOut", "role"): {"user", "assistant", "system"},
    ("MessageOut", "stage"): STAGES,
    ("SessionOut", "stage"): STAGES,
    ("AdminSessionRowOut", "stage"): STAGES,
    ("ArtifactSummaryOut", "stage"): STAGES,
    ("ArtifactSummaryOut", "kind"): DOC_KINDS,
    ("FileSummaryOut", "state"): {"read", "modified", "new"},
    ("FileSummaryOut", "base_status"): {"sap", "absent", "unknown"},
    ("FileSummaryOut", "syntax_status"): {"ok", "errors", "unavailable"},
    ("FileRevisionOut", "syntax_status"): {"ok", "errors", "unavailable"},
    ("SyntaxResultOut", "status"): {"ok", "errors", "unavailable"},
    ("CommentOut", "kind"): DOC_KINDS,
    ("CommentOut", "state"): {"open", "sent", "addressed", "dismissed"},
}


def _enum(model: str, field: str) -> set[str]:
    prop = schemas.export_schema()["$defs"][model]["properties"][field]
    values: set[str] = set()
    for option in prop.get("anyOf", [prop]):
        values |= set(option.get("enum", []))
        if "const" in option:
            values.add(option["const"])
        if "$ref" in option:
            name = option["$ref"].rsplit("/", 1)[-1]
            values |= set(schemas.export_schema()["$defs"][name].get("enum", []))
    return values


@pytest.mark.parametrize("model,field", sorted(ENUMS))
def test_schema_lists_the_enum(model, field):
    assert _enum(model, field) == ENUMS[(model, field)]


def _message(**over):
    data = {"id": "m", "stage": "chat", "role": "user", "content": "",
            "created_at": None, "has_activity": False}
    data.update(over)
    return data


def test_wrong_values_fail():
    schemas.MessageOut.model_validate(_message())
    schemas.MessageOut.model_validate(_message(stage=Stage.design))
    for bad in (_message(role="tool"), _message(stage="deploy")):
        with pytest.raises(ValidationError):
            schemas.MessageOut.model_validate(bad)
    with pytest.raises(ValidationError):
        schemas.FileSummaryOut.model_validate({
            "path": "p", "state": "deleted", "object_type": None,
            "object_name": None, "revision": 0, "base_status": None,
            "syntax_status": None})


def test_artifact_summary_keeps_stage():
    assert "stage" in schemas.export_schema()["$defs"]["ArtifactSummaryOut"]["required"]


def test_tool_event_carries_code():
    event = schemas.ToolEventOut.model_validate(
        {"kind": "tool", "status": "error", "code": "readonly_refused"})
    assert event.model_dump()["code"] == "readonly_refused"
    assert schemas.ToolEventOut.model_validate({"kind": "note"}).code is None


# --- conventions update body and dropped stored items -----------------------


def test_conventions_update_is_a_contract_model():
    defs = schemas.export_schema()["$defs"]
    assert "clear" in defs["ConventionsUpdate"]["properties"]
    assert "clear" not in defs["ConventionsCreate"]["properties"]
    assert "clear" not in defs["ConventionsBody"]["properties"]
    ok = schemas.ConventionsUpdate.model_validate({"label": "x", "clear": ["package"]})
    assert ok.clear == ["package"]
    for bad in ({"label": "x", "clear": ["label"]}, {"clear": ["non_production"]}):
        with pytest.raises(ValidationError):
            schemas.ConventionsUpdate.model_validate(bad)
    with pytest.raises(ValidationError):
        schemas.ConventionsCreate.model_validate({"target": "DEMO", "clear": []})


def test_routes_use_the_schemas_conventions_update():
    from agents.ide import routes

    assert routes.ConventionsUpdate is schemas.ConventionsUpdate


def test_dropped_stored_items_are_logged_by_count(caplog):
    from agents.ide import routes

    secret = "SECRET-SOURCE-LINE"
    data = [{"line": 1, "message": "m", "severity": "error"}, secret,
            {"message": secret}]
    with caplog.at_level("WARNING", logger="agents.ide.routes"):
        out = routes._valid_items(data, schemas.SyntaxItemOut)
    assert len(out) == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "2" in warnings[0].getMessage()
    assert secret not in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="agents.ide.routes"):
        routes._valid_items(data[:1], schemas.SyntaxItemOut)
    assert caplog.records == []


# --- B9 carry-forward: finding kind and approval status/action are coerced ---


def _finding_row(kind):
    from types import SimpleNamespace

    return SimpleNamespace(id="f1", kind=kind, ref_id="r1", title="t",
                           program=None, include=None, line=None,
                           occurred_at=None, created_at=None)


def _approval_row(action, status):
    from types import SimpleNamespace

    return SimpleNamespace(id="a1", action=action, status=status,
                           params_json="{}", result_json=None,
                           created_at=None, decided_at=None, error_code=None)


@pytest.mark.parametrize("stored", ["bogus", None, 3])
def test_bad_stored_finding_kind_is_coerced_not_500(stored, caplog):
    from agents.ide import findings

    with caplog.at_level("WARNING", logger="agents.ide.schemas"):
        out = findings.finding_out(_finding_row(stored))
    schemas.FindingOut.model_validate(out)  # never a 500
    assert out["kind"] == "trace"
    assert "finding kind" in caplog.text
    assert findings.finding_out(_finding_row("dump"))["kind"] == "dump"


@pytest.mark.parametrize("action,status", [("bogus", "pending"),
                                           ("trace_start", "weird"),
                                           (None, None)])
def test_bad_stored_approval_values_are_coerced_not_500(action, status, caplog):
    from agents.ide import approvals

    with caplog.at_level("WARNING", logger="agents.ide.schemas"):
        out = approvals.approval_json(_approval_row(action, status))
    schemas.ApprovalOut.model_validate(out)
    if action != "trace_start":
        assert out["action"] == "trace_cancel"
    if status != "pending":
        # A value outside the contract never shows decision buttons.
        assert out["status"] == "failed"
    assert caplog.records
    good = approvals.approval_json(_approval_row("trace_start", "pending"))
    assert (good["action"], good["status"]) == ("trace_start", "pending")
