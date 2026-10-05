"""The OData catalogue and audit tables and their helpers in ``agents.db``.

Section 1.3 of the OData services plan. SQLite only: the Postgres side
(column widths, the unique constraint, timezone-aware timestamps) is
verified live.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
for _v in (
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
    "CONNECTIVITY_PP_MODE",
):
    os.environ.pop(_v, None)

from agents import db as agents_db  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ODataAuditLog,
    ODataService,
    SessionLocal,
    create_odata_service,
    delete_odata_service,
    engine,
    get_odata_service,
    init_db,
    list_odata_services,
    odata_entries,
    odata_service_referrers,
    purge_odata_audit,
    update_odata_service,
    upsert_agent,
    validate_odata_service,
)
from agents.odata.models import ServiceDefinition  # noqa: E402
from agents.odata.models import validate_odata_service as models_validate  # noqa: E402

GOOD: dict[str, Any] = {
    "name": "purchase-requisitions",
    "title": "Purchase requisitions",
    "purpose": "Read requisitions and their items",
    "destination": "S4_ODATA_USER",
    "user_context": True,
    "odata_version": "v2",
    "service_path": "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV",
    "definition": {
        "entity_sets": [
            {
                "name": "A_PurchaseRequisitionItem",
                "title": "Requisition item",
                "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
                "operations": ["list", "get"],
                "fields": [
                    {"name": "PurchaseRequisition", "selectable": True, "filterable": True},
                    {"name": "PurchaseRequisitionItem", "selectable": True},
                    {
                        "name": "PurReqnReleaseStatus",
                        "label": "Release status",
                        "selectable": True,
                        "filterable": True,
                        "writable": True,
                    },
                ],
            }
        ],
        "operations": [
            {
                "name": "ReleaseItem",
                "kind": "function_import",
                "http_method": "POST",
                "bound_to": "A_PurchaseRequisitionItem",
                "parameters": [{"name": "ReleaseCode"}],
            }
        ],
    },
}

ODATA_SERVER = {
    "url": "builtin:odata",
    "auth_mode": "destination",
    "oauth": {"services": ["purchase-requisitions"], "allow_write": True},
}

PLAIN_SERVER = {"url": "https://mcp.example.com/mcp", "auth_mode": "jwt"}


def _odata_server(services: Any) -> dict[str, Any]:
    return {"url": "builtin:odata", "auth_mode": "destination", "oauth": {"services": services}}


def good(**patch: Any) -> dict[str, Any]:
    data = copy.deepcopy(GOOD)
    data.update(patch)
    return data


@pytest.fixture(autouse=True)
async def _clean_tables():
    """Each test starts with empty OData tables and no agents.

    The database is the session's, shared with every other suite in the
    process, so the tests never assume a fresh file.
    """
    await init_db()
    async with SessionLocal() as s:
        for model in (ODataService, ODataAuditLog, AgentConfig):
            await s.execute(delete(model))
        await s.commit()
    yield


def _agent_row(
    name: str, *, enabled: int, primary: dict, extras: list[dict] | None = None
) -> AgentConfig:
    """An agent row as storage holds it, written without the save-time cleaner."""
    return AgentConfig(
        name=name,
        description="d",
        instructions="i",
        mcp_url=primary["url"],
        auth_mode=primary["auth_mode"],
        oauth_json=json.dumps(primary["oauth"]) if primary.get("oauth") else None,
        extra_servers_json=json.dumps(extras) if extras else None,
        enabled=enabled,
    )


def test_validate_odata_service_is_reexported_not_copied():
    assert validate_odata_service is models_validate


async def test_init_db_creates_both_tables_and_is_idempotent():
    await init_db()
    await init_db()
    async with engine.connect() as c:
        names = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    assert {"odata_services", "odata_audit_log"} <= names


async def test_create_get_list_update_delete():
    async with SessionLocal() as s:
        row = await create_odata_service(s, validate_odata_service(GOOD))
        assert row.to_dict()["counts"] == {"entity_sets": 1, "operations": 1}
        assert row.to_dict()["has_write"] is False
        got = await get_odata_service(s, "purchase-requisitions")
        assert got.definition["entity_sets"][0]["name"] == "A_PurchaseRequisitionItem"
        assert [r.name for r in await list_odata_services(s)] == ["purchase-requisitions"]
        await update_odata_service(s, row, validate_odata_service({**GOOD, "title": "PR"}))
        assert "definition" not in row.to_summary()
        assert row.to_export() == validate_odata_service({**GOOD, "title": "PR"})
        assert await delete_odata_service(s, row) is True
        assert await get_odata_service(s, "purchase-requisitions") is None


async def test_to_dict_shape_and_stored_types():
    async with SessionLocal() as s:
        row = await create_odata_service(s, validate_odata_service(GOOD))
        d = row.to_dict()
        assert set(d) == {
            "id", "name", "title", "purpose", "not_for", "destination", "user_context",
            "odata_version", "service_path", "enabled", "definition", "metadata_fetched_at",
            "created_at", "updated_at", "counts", "has_write", "used_by",
        }
        assert d["user_context"] is True and d["enabled"] is True
        assert d["metadata_fetched_at"] is None and d["used_by"] == []
        assert isinstance(d["id"], int) and d["created_at"] and d["updated_at"]
        assert set(row.to_summary()) == set(d) - {"definition"}
        used = [{"agent_id": 1, "agent": "a", "enabled": True, "expose_api": False,
                 "api_slug": None, "allow_write": False}]
        assert row.to_dict(used_by=used)["used_by"] == used
        assert row.to_summary(used_by=used)["used_by"] == used
        # Booleans are Integer 0/1 in the table, like AgentConfig.expose_chat.
        assert (row.user_context, row.enabled) == (1, 1)


async def test_definition_is_stored_as_the_models_own_serialisation():
    """The size cap is measured on ``model_dump_json()``; store exactly that."""
    data = validate_odata_service(GOOD)
    async with SessionLocal() as s:
        row = await create_odata_service(s, data)
        expected = ServiceDefinition.model_validate(data["definition"]).model_dump_json()
        assert row.definition_json == expected


async def test_list_is_ordered_by_name_and_disabled_round_trips():
    async with SessionLocal() as s:
        await create_odata_service(s, validate_odata_service(good(name="zz-last")))
        await create_odata_service(s, validate_odata_service(good(name="aa-first", enabled=False)))
        rows = await list_odata_services(s)
        assert [r.name for r in rows] == ["aa-first", "zz-last"]
        assert rows[0].to_dict()["enabled"] is False
        assert rows[0].to_export()["enabled"] is False


async def test_metadata_fetched_at_round_trips_through_export():
    """SQLite drops the offset; the export must still be the payload's dump."""
    stamp = "2026-10-05T09:30:00+02:00"
    data = validate_odata_service(good(metadata_fetched_at=stamp))
    async with SessionLocal() as s:
        await create_odata_service(s, data)
    async with SessionLocal() as s:
        row = await get_odata_service(s, "purchase-requisitions")
        exported = row.to_export()
        assert exported["metadata_fetched_at"] == "2026-10-05T07:30:00Z"
        assert row.to_dict()["metadata_fetched_at"] == "2026-10-05T07:30:00Z"
        # An export is importable as it is, and stable from then on.
        assert validate_odata_service(exported) == exported
        assert datetime.fromisoformat(stamp) == datetime.fromisoformat(
            exported["metadata_fetched_at"].replace("Z", "+00:00")
        )


async def test_duplicate_name_is_a_value_error():
    async with SessionLocal() as s:
        await create_odata_service(s, validate_odata_service(GOOD))
        with pytest.raises(ValueError, match="already exists"):
            await create_odata_service(s, validate_odata_service(good(title="Other")))
        assert len(await list_odata_services(s)) == 1


async def test_duplicate_name_without_commit_keeps_the_callers_transaction():
    """An import adds several rows in one transaction; one taken name must
    not throw away the rows before it."""
    async with SessionLocal() as s:
        await create_odata_service(s, validate_odata_service(good(name="first")), commit=False)
        with pytest.raises(ValueError, match="already exists"):
            await create_odata_service(s, validate_odata_service(good(name="first")), commit=False)
        await s.commit()
    async with SessionLocal() as s:
        assert [r.name for r in await list_odata_services(s)] == ["first"]


async def test_unique_constraint_backs_the_name_check():
    data = validate_odata_service(GOOD)
    async with SessionLocal() as s:
        await create_odata_service(s, data)
    async with SessionLocal() as s:
        s.add(ODataService(
            name=data["name"], title="t", purpose="p", destination="S4_ODATA_TECH",
            odata_version="v2", service_path="/x", definition_json="{}",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_update_refuses_another_name():
    """Agents attach a service by name; a rename would detach them silently."""
    async with SessionLocal() as s:
        row = await create_odata_service(s, validate_odata_service(GOOD))
        with pytest.raises(ValueError, match="name cannot be changed"):
            await update_odata_service(s, row, validate_odata_service(good(name="other")))
        assert row.name == "purchase-requisitions"


async def test_update_replaces_every_payload_field():
    changed = good(
        title="PR", purpose="Other", not_for="Contracts", destination="S4_ODATA_TECH",
        user_context=False, odata_version="v2", service_path="/sap/opu/odata/sap/OTHER_SRV",
        enabled=False, metadata_fetched_at="2026-10-05T07:30:00Z",
    )
    changed["definition"] = {"entity_sets": [], "operations": []}
    async with SessionLocal() as s:
        row = await create_odata_service(s, validate_odata_service(GOOD))
        await update_odata_service(s, row, validate_odata_service(changed))
    async with SessionLocal() as s:
        row = await get_odata_service(s, "purchase-requisitions")
        assert row.to_export() == validate_odata_service(changed)
        assert row.to_dict()["counts"] == {"entity_sets": 0, "operations": 0}


async def test_has_write_true_for_an_enabled_changing_operation_or_a_write_op():
    by_op = good(name="by-op")
    by_op["definition"]["operations"][0]["enabled"] = True
    by_entity = good(name="by-entity")
    by_entity["definition"]["entity_sets"][0]["operations"] = ["list", "get", "update"]
    read_only_op = good(name="read-only-op")
    read_only_op["definition"]["operations"][0].update(enabled=True, changes_data=False)
    async with SessionLocal() as s:
        cases = ((by_op, True), (by_entity, True), (read_only_op, False), (good(), False))
        for data, expected in cases:
            row = await create_odata_service(s, validate_odata_service(data))
            assert row.to_dict()["has_write"] is expected, data["name"]
            assert row.to_summary()["has_write"] is expected, data["name"]


def test_has_write_fails_towards_write_on_an_unreadable_definition():
    """``has_write`` labels a service in the admin list; a definition this
    code cannot read must not be shown as read-only."""
    row = ODataService(name="x", definition_json="not json")
    assert row.definition == {}
    assert row.to_summary()["has_write"] is True
    assert row.to_summary()["counts"] == {"entity_sets": 0, "operations": 0}


def test_odata_entries_picks_only_builtin_odata_blocks():
    servers = [
        {"url": "https://mcp.example.com/mcp", "auth_mode": "jwt"},
        {"url": "builtin:jira", "auth_mode": "destination", "oauth": {"destination": "JIRA"}},
        ODATA_SERVER,
        {"url": " BUILTIN:OData/ ", "auth_mode": "destination", "oauth": {"services": ["b"]}},
        {"url": "builtin:odata", "auth_mode": "destination"},
        {"url": "builtin:odata", "auth_mode": "destination", "oauth": "services"},
        {"url": None},
        "builtin:odata",
    ]
    assert odata_entries(servers) == [ODATA_SERVER["oauth"], {"services": ["b"]}]
    assert odata_entries([]) == []


async def test_referrers_from_stored_rows():
    other = {"url": "builtin:odata", "auth_mode": "destination",
             "oauth": {"services": ["stock", "purchase-requisitions"], "allow_write": "true"}}
    async with SessionLocal() as s:
        s.add(_agent_row("buyer", enabled=1, primary=ODATA_SERVER))
        s.add(_agent_row(
            "reader", enabled=0,
            primary=PLAIN_SERVER, extras=[other],
        ))
        s.add(_agent_row("plain", enabled=1, primary=PLAIN_SERVER))
        # Malformed blocks are skipped, never a 500 on the service list.
        s.add(_agent_row("broken", enabled=1, primary=_odata_server("purchase-requisitions")))
        s.add(_agent_row("mixed", enabled=1, primary=_odata_server([7, None, "stock", "stock"])))
        await s.commit()
        refs = await odata_service_referrers(s)
        assert set(refs) == {"purchase-requisitions", "stock"}
        pr = refs["purchase-requisitions"]
        assert [(r["agent"], r["enabled"], r["allow_write"]) for r in pr] == [
            ("buyer", True, True),
            # "true" is a string: only a real true opens writes.
            ("reader", False, False),
        ]
        assert set(pr[0]) == {
            "agent_id", "agent", "enabled", "expose_api", "api_slug", "allow_write",
        }
        assert isinstance(pr[0]["agent_id"], int)
        assert pr[0]["expose_api"] is False and pr[0]["api_slug"] is None
        assert [r["agent"] for r in refs["stock"]] == ["mixed", "reader"]
        assert await odata_service_referrers(s, "stock") == {"stock": refs["stock"]}
        assert await odata_service_referrers(s, "nobody-uses-this") == {}


async def test_referrers_merge_two_entries_of_one_agent():
    """One line per agent and service; write access on any entry counts."""
    read = {"url": "builtin:odata", "auth_mode": "destination",
            "oauth": {"services": ["purchase-requisitions"]}}
    async with SessionLocal() as s:
        s.add(_agent_row("buyer", enabled=1, primary=read, extras=[ODATA_SERVER]))
        await s.commit()
        refs = await odata_service_referrers(s, "purchase-requisitions")
        got = [(r["agent"], r["allow_write"]) for r in refs["purchase-requisitions"]]
        assert got == [("buyer", True)]


async def test_referrers_lists_enabled_and_disabled_agents_with_allow_write():
    """Through ``upsert_agent``, i.e. the storage cleaner's own shape of the
    ``builtin:odata`` block (it names no destination)."""
    async with SessionLocal() as s:
        # `upsert_agent` refuses a service the catalogue does not have.
        await create_odata_service(s, validate_odata_service(copy.deepcopy(GOOD)))
        for name, enabled in (("buyer", True), ("reader", False)):
            await upsert_agent(
                s, name=name, description="d", instructions="i",
                mcp_servers=[copy.deepcopy(ODATA_SERVER)], enabled=enabled,
            )
        refs = await odata_service_referrers(s, "purchase-requisitions")
        got = [(r["agent"], r["enabled"], r["allow_write"]) for r in refs["purchase-requisitions"]]
        assert got == [("buyer", True, True), ("reader", False, True)]


def _audit_row(**patch: Any) -> ODataAuditLog:
    values: dict[str, Any] = {
        "call_id": "a" * 32, "agent": "buyer", "run_id": "r1", "sent_as": "alice@example.com",
        "run_principal": "alice@example.com", "token_digest": "d" * 64,
        "service": "purchase-requisitions", "target": "A_PurchaseRequisitionItem",
        "operation": "update", "key_json": json.dumps({"PurchaseRequisition": "10"}),
        "body_fields_json": json.dumps(["PurReqnReleaseStatus"]), "phase": "write",
        "http_status": 204, "outcome": "ok",
    }
    values.update(patch)
    return ODataAuditLog(**values)


async def test_audit_row_insert_and_purge():
    """The table itself; how rows get there is tests/test_odata_audit.py."""
    now = datetime.now(timezone.utc)
    async with SessionLocal() as s:
        s.add(_audit_row())
        s.add(_audit_row(
            call_id="b" * 32, created_at=now - timedelta(days=400), run_id=None,
            sent_as="technical:S4_ODATA_TECH", run_principal=None, token_digest=None,
            operation="create", key_json=None, phase="token", http_status=None, outcome="intent",
        ))
        await s.commit()
        query = select(ODataAuditLog).where(ODataAuditLog.run_id == "r1")
        fresh = (await s.execute(query)).scalar_one()
        assert fresh.created_at is not None and fresh.http_status == 204
        assert fresh.finished_at is None and fresh.created_key_json is None
        shown = fresh.to_dict()
        assert shown["key"] == {"PurchaseRequisition": "10"} and shown["status"] == 204
        assert shown["fields"] == ["PurReqnReleaseStatus"] and shown["created_key"] is None
        assert shown["created_at"].endswith("+00:00") and shown["finished_at"] is None
        assert await purge_odata_audit(s, 0) == 0
        assert await purge_odata_audit(s, -5) == 0
        assert await agents_db.purge_odata_audit(s, 365) == 1
        assert await purge_odata_audit(s, 365) == 0
        left = (await s.execute(select(ODataAuditLog))).scalars().all()
        assert [r.run_id for r in left] == ["r1"]
        assert (await s.execute(select(func.count()).select_from(ODataAuditLog))).scalar_one() == 1


async def test_a_call_id_is_stored_once():
    async with SessionLocal() as s:
        s.add(_audit_row())
        await s.commit()
        s.add(_audit_row(outcome="intent"))
        with pytest.raises(IntegrityError):
            await s.commit()


def test_column_widths_fit_what_validation_accepts():
    """SQLite ignores VARCHAR limits; Postgres does not. A value the models
    accept must fit its column, or the first save on CF fails."""
    cols = ODataService.__table__.c
    assert cols.name.type.length >= 64
    assert cols.title.type.length >= 120
    assert cols.purpose.type.length >= 200 and cols.not_for.type.length >= 200
    assert cols.destination.type.length >= 200
    assert cols.service_path.type.length >= 512
    audit = ODataAuditLog.__table__.c
    assert audit.service.type.length >= 64 and audit.target.type.length >= 128
    assert audit.agent.type.length >= AgentConfig.__table__.c.name.type.length
    assert audit.call_id.type.length >= 32 and audit.token_digest.type.length >= 64
    for value in ("create", "update", "delete", "call"):
        assert len(value) <= audit.operation.type.length
    for value in agents_db.ODATA_AUDIT_OUTCOMES:
        assert len(value) <= audit.outcome.type.length
    assert set(agents_db.ODATA_AUDIT_OUTCOMES) == {
        "intent", "ok", "refused", "sap_error", "unknown", "cancelled",
    }
    # "technical:<destination>" with the longest destination name allowed.
    assert len("technical:") + 200 <= audit.sent_as.type.length
    assert audit.run_principal.type.length >= 255
    assert audit.created_at.index is True
    assert "principal" not in audit  # two identities, never one ambiguous column


async def test_losing_the_name_race_is_a_value_error_and_keeps_the_transaction(monkeypatch):
    """Two saves of one name can both pass the lookup; the constraint then
    refuses the second, and that must read like the lookup's refusal."""
    async def nobody_there(session, name):
        return None

    async with SessionLocal() as s:
        await create_odata_service(s, validate_odata_service(good(name="kept")), commit=False)
        await create_odata_service(s, validate_odata_service(good(name="raced")), commit=False)
        monkeypatch.setattr(agents_db, "get_odata_service", nobody_there)
        with pytest.raises(ValueError, match="already exists") as refused:
            await create_odata_service(s, validate_odata_service(good(name="raced")), commit=False)
        # Not chained: an IntegrityError carries the statement and its parameters.
        assert refused.value.__cause__ is None and refused.value.__suppress_context__
        await s.commit()
    monkeypatch.undo()
    async with SessionLocal() as s:
        assert [r.name for r in await list_odata_services(s)] == ["kept", "raced"]
