"""The Describe and Test routes of the InA admin API:
``/admin/api/ina/queries/{name}/describe`` and ``.../test``.

Split from ``tests/test_ina_admin.py`` (which holds the CRUD, reload, health
and scope cases and the shared fixtures, imported from there): what a
Describe stores and answers, what a Test sends and answers, the refusals of
both, the two-call slot limit, and the reload a Describe of a query in use
causes. SAP is the mock transport of ``tests/ina_helpers.py`` behind the
seams of ``agents.ina.admin_calls``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.db import InaQuery, SessionLocal, update_ina_query  # noqa: E402
from agents.ina import admin_calls, admin_routes  # noqa: E402
from agents.ina.errors import ERROR_CODES  # noqa: E402
from tests import test_ina_admin as admin_suite  # noqa: E402
from tests.ina_helpers import DESTINATION, definition, fixture  # noqa: E402
from tests.odata_helpers import OnPremiseResolver  # noqa: E402
from tests.test_ina_admin import (  # noqa: E402
    BASE,
    NAME,
    NAMES,
    ONE,
    SECRET,
    VALUE,
    add_agent,
    bearer,
    described,
    optional_variables,
    query,
    total,
)

# The admin suite's fixtures, bound by name so pytest finds them in this
# module too (imported, every test argument would read as a redefinition).
_clean_tables = admin_suite._clean_tables
_every_slot_is_given_back = admin_suite._every_slot_is_given_back
client = admin_suite.client
created = admin_suite.created
live = admin_suite.live
rebuilt = admin_suite.rebuilt
remote = admin_suite.remote


# --- describe -----------------------------------------------------------------


async def test_describe_reads_sap_stores_the_definition_and_answers_names(client, remote, created):
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "dimensions": 2,
        "measures": 2,
        "variables": 3,
        "names": NAMES,
        "truncated": False,
        "skipped": 0,
        "metadata_fetched_at": body["metadata_fetched_at"],
        "reloaded": False,
        "reload_failed": False,
    }
    assert body["metadata_fetched_at"]
    stored = (await client.get(ONE)).json()
    assert stored["described"] is True
    assert stored["definition"] == definition().model_dump(mode="json")
    assert stored["metadata_fetched_at"] == body["metadata_fetched_at"]
    # The row moved: a form loaded before the describe must reload.
    assert stored["updated_at"] != created["updated_at"]
    assert remote.asked_for == [DESTINATION]
    # GetServerInfo once, then the Metadata request; nothing else.
    assert [(q.method, q.url.path) for q in remote.requests] == [
        ("GET", "/sap/bw/ina/GetServerInfo"),
        ("POST", "/sap/bw/ina/GetResponse"),
    ]
    assert "Metadata" in json.loads(remote.requests[1].content)


async def test_describe_takes_an_empty_object_and_refuses_any_key(client, remote, created):
    assert (await client.post(f"{ONE}/describe", json={})).status_code == 200
    r = await client.post(f"{ONE}/describe", json={"destination": SECRET})
    assert r.status_code == 422 and SECRET not in r.text
    assert r.json() == {"detail": "destination: Extra inputs are not permitted"}


async def test_a_describe_sap_refuses_stores_nothing_and_names_the_code(client, remote, created):
    remote.sap.answers = [fixture("query_unknown.json")]
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 502
    assert r.headers["x-ina-error"] == "query_unknown"
    assert r.json() == {"detail": "SAP does not know the InA query of this registration"}
    stored = (await client.get(ONE)).json()
    assert stored["described"] is False and stored["updated_at"] == created["updated_at"]


@pytest.mark.parametrize(
    "answer, status, code",
    [
        (httpx.Response(401), 502, "unauthorized"),
        (httpx.Response(403), 502, "forbidden"),
        (httpx.Response(200, text=f"<html>{SECRET}</html>"), 502, "not_json"),
    ],
)
async def test_a_refused_server_info_is_the_clients_code(
    client, remote, created, answer, status, code
):
    remote.sap.info_answer = lambda request: answer
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == status and r.headers["x-ina-error"] == code
    assert code in ERROR_CODES and SECRET not in r.text


async def test_describe_as_the_signed_in_user_needs_one(client, remote):
    assert (await client.post(BASE, json=query(user_context=True))).status_code == 201
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 424 and r.headers["x-ina-error"] == "user_token_required"
    assert remote.asked_for == [] and remote.requests == []
    # With the admin's own token, the destination is asked as that user.
    headers = bearer("alice")
    r = await client.post(f"{ONE}/describe", headers=headers)
    assert r.status_code == 200, r.text
    ((token, principal),) = {c for c in remote.resolver.calls}
    assert token == headers["Authorization"].removeprefix("Bearer ") and principal


async def test_a_refused_destination_answers_its_admin_text(client, remote, created):
    # OnPremise without a connectivity binding: refused before anything is sent.
    remote.resolver = OnPremiseResolver(name=DESTINATION)
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 502 and r.headers["x-ina-error"] == "destination_error"
    assert DESTINATION in r.json()["detail"] and "connectivity" in r.json()["detail"]
    assert remote.requests == []


async def test_a_describe_over_the_budget_is_504(client, remote, created, monkeypatch):
    monkeypatch.setattr(admin_calls, "CALL_BUDGET_SECONDS", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return remote.sap.handler(request)

    remote.handler = slow
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 504 and r.headers["x-ina-error"] == "timeout"
    assert (await client.get(ONE)).json()["described"] is False


async def test_a_query_changed_while_it_was_described_is_not_overwritten(
    client, remote, created, monkeypatch
):
    real = admin_calls.read_description

    async def read_and_edit(stored: dict[str, Any]):
        description = await real(stored)
        async with SessionLocal() as s:
            await update_ina_query(s, NAME, {**query(), "destination": "OTHER_DEST"})
        return description

    monkeypatch.setattr(admin_calls, "read_description", read_and_edit)
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 409
    assert r.json() == {
        "detail": f"Query '{NAME}' was changed while it was described; describe it again"
    }
    stored = (await client.get(ONE)).json()
    assert stored["destination"] == "OTHER_DEST" and stored["described"] is False


async def test_a_query_deleted_while_it_was_described_is_404(client, remote, created, monkeypatch):
    real = admin_calls.read_description

    async def read_and_delete(stored: dict[str, Any]):
        description = await real(stored)
        async with SessionLocal() as s:
            await s.execute(delete(InaQuery))
            await s.commit()
        return description

    monkeypatch.setattr(admin_calls, "read_description", read_and_delete)
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 404 and r.json() == {"detail": "Query not found"}


async def test_describe_logs_one_line_without_content(client, remote, created, caplog):
    with caplog.at_level(logging.INFO, logger=admin_calls.logger.name):
        await client.post(f"{ONE}/describe")
    lines = [r.getMessage() for r in caplog.records if r.name == admin_calls.logger.name]
    (line,) = [ln for ln in lines if "describe" in ln]
    assert NAME in line and "code=-" in line and "Net amount" not in line


# --- test ---------------------------------------------------------------------


async def test_the_test_runs_the_first_measure_with_top_1_and_answers_no_value(client, remote):
    assert (
        await client.post(BASE, json=query(definition=optional_variables()))
    ).status_code == 201
    remote.sap.answers = [total()]
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "status", "duration_ms", "columns"}
    assert body["ok"] is True and body["status"] == 200
    assert isinstance(body["duration_ms"], int) and body["columns"] == ["0AMOUNT"]
    assert str(VALUE) not in r.text and "EUR" not in r.text
    # No variable is given: the read and the Close, no VariableSubmit.
    assert remote.sap.kinds == ["GET", "read", "close"]
    (read,) = remote.sap.reads
    sent = json.dumps(json.loads(read.content))
    assert "0AMOUNT" in sent and "0QUANTITY" not in sent
    assert '"RowTo": 2' in sent  # top 1, plus the one that says "more"


async def test_the_test_of_an_undescribed_query_describes_first_and_stores_nothing(
    client, remote, created
):
    patched = fixture("describe_ok.json")
    remote.sap.answers = [patched, total()]
    # The recorded description has a mandatory variable; make it optional.
    for variable in patched["Cube"]["Variables"]:
        variable["Mandatory"] = False
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 200, r.text
    assert r.json()["columns"] == ["0AMOUNT"]
    assert remote.sap.kinds == ["GET", "describe", "read", "close"]
    stored = (await client.get(ONE)).json()
    assert stored["described"] is False and stored["updated_at"] == created["updated_at"]


async def test_a_mandatory_variable_is_a_refusal_before_the_run_is_sent(client, remote):
    defined = definition().model_dump(mode="json")
    assert (await client.post(BASE, json=query(definition=defined))).status_code == 201
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 422 and r.headers["x-ina-error"] == "variable_required"
    assert r.json() == {"detail": admin_calls.VARIABLE_REQUIRED_TEXT}
    assert remote.sap.posts == []


async def test_a_sia_description_makes_the_test_refuse_before_the_run_is_sent(client, remote):
    # SIA's describe answer (2026-11-04) marks its mandatory variables with
    # ``InputType``; a Test of the described query sent a run without them,
    # and SAP refused it (``run_sia_variables_missing.json``).
    remote.sap.answers = [fixture("describe_sia.json"), fixture("run_sia_variables_missing.json")]
    assert (await client.post(BASE, json=query())).status_code == 201
    assert (await client.post(f"{ONE}/describe")).status_code == 200
    stored = (await client.get(ONE)).json()["definition"]["variables"]
    assert [v["name"] for v in stored if v["mandatory"]] == [
        "!CDS_P_P_DISPLAYCURRENCY",
        "!CDS_P_P_DATEFUNCTION",
    ]
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 422 and r.headers["x-ina-error"] == "variable_required"
    assert r.json() == {"detail": admin_calls.VARIABLE_REQUIRED_TEXT}
    assert len(remote.sap.posts) == 1  # the describe; no run was sent


async def test_a_test_sap_refuses_is_the_clients_code(client, remote):
    assert (
        await client.post(BASE, json=query(definition=optional_variables()))
    ).status_code == 201
    remote.sap.answers = [httpx.Response(500, json={"Messages": [
        {"Type": 2, "Text": f"Fehler {SECRET} at https://sia.internal/x?sap-password=p"}
    ]})]
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 502 and r.headers["x-ina-error"] == "sap_error"
    assert "sia.internal" not in r.text and "sap-password" not in r.text


async def test_a_disabled_query_may_be_tested(client, remote):
    defined = optional_variables()
    body = query(definition=defined, enabled=False)
    assert (await client.post(BASE, json=body)).status_code == 201
    remote.sap.answers = [total()]
    assert (await client.post(f"{ONE}/test")).json()["ok"] is True


async def test_the_test_refuses_a_body_key(client, remote, created):
    r = await client.post(f"{ONE}/test", json={"variables": {"0P_CALYEAR": SECRET}})
    assert r.status_code == 422 and SECRET not in r.text
    assert remote.requests == []


# --- reload of a query in use ----------------------------------------------------


async def test_a_describe_of_a_query_in_use_reloads(client, remote, created, live, rebuilt):
    await add_agent("analyst", NAME)
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and live.reloads == 1
    assert rebuilt == [[("ina", NAME)]]


# --- the SAP calls: slots, one client builder, refusals (review round 1) ----------


async def test_a_third_call_while_two_run_is_busy_and_sends_nothing(client, remote, created):
    gate = asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return remote.sap.handler(request)

    remote.handler = held
    first = asyncio.create_task(client.post(f"{ONE}/describe"))
    second = asyncio.create_task(client.post(f"{ONE}/test"))
    for _ in range(200):
        if admin_calls._slots.locked():
            break
        await asyncio.sleep(0.01)
    assert admin_calls._slots.locked()
    for path in (f"{ONE}/describe", f"{ONE}/test"):
        r = await client.post(path)
        assert r.status_code == 429 and r.headers["x-ina-error"] == "busy"
        assert r.json() == {"detail": admin_calls.BUSY_TEXT}
    asked = len(remote.asked_for)
    gate.set()
    assert (await first).status_code == 200
    await second
    assert asked == 2, "the refused calls resolved nothing"


async def test_a_test_as_the_signed_in_user_needs_one(client, remote):
    body = query(user_context=True, definition=optional_variables())
    assert (await client.post(BASE, json=body)).status_code == 201
    r = await client.post(f"{ONE}/test")
    assert r.status_code == 424 and r.headers["x-ina-error"] == "user_token_required"
    assert r.json() == {"detail": admin_calls.USER_REQUIRED_TEXT}
    assert remote.asked_for == [] and remote.requests == []


async def test_a_stray_value_error_inside_the_client_is_no_destination_text(
    client, remote, created, monkeypatch, caplog
):
    async def broken(self):
        raise ValueError(f"field {SECRET} at https://sia.internal/x")

    monkeypatch.setattr(admin_calls.InaClient, "describe", broken)
    with caplog.at_level(logging.WARNING):
        r = await client.post(f"{ONE}/describe")
    assert r.status_code == 502 and r.headers["x-ina-error"] == "destination_error"
    assert r.json() == {"detail": admin_calls._FAILED_TEXT}
    assert SECRET not in caplog.text and "sia.internal" not in caplog.text
    assert "ValueError" in caplog.text


async def test_a_destination_that_cannot_be_built_is_a_destination_error(
    client, remote, created, monkeypatch, caplog
):
    from agents.destination import DestinationError

    def refused(name: str) -> Any:
        raise DestinationError(f"no binding at https://dest.internal/?token={SECRET}")

    monkeypatch.setattr(admin_calls, "_resolver", refused)
    with caplog.at_level(logging.WARNING):
        r = await client.post(f"{ONE}/describe")
    assert r.status_code == 502 and r.headers["x-ina-error"] == "destination_error"
    assert SECRET not in r.text and SECRET not in caplog.text


def test_the_tools_and_the_admin_calls_build_one_client():
    import ast

    from agents.ina import client as ina_client
    from agents.ina import tools as ina_tools

    assert admin_calls.build_ina_http_client is ina_client.build_ina_http_client
    assert ina_tools.build_ina_http_client is ina_client.build_ina_http_client
    for module in ("tools.py", "admin_calls.py"):
        tree = ast.parse((ROOT / "agents" / "ina" / module).read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "destination_http_client" not in names and "NoCookieJar" not in names, module


async def test_the_one_builder_keeps_no_cookie_and_refuses_onpremise_without_a_binding():
    from agents.ina.client import build_ina_http_client
    from agents.odata.session import NoCookieJar

    sent: list[httpx.Request] = []
    http = build_ina_http_client(
        OnPremiseResolver(name=DESTINATION),
        user_context=False,
        server_key="builtin:ina/x",
        connectivity=None,
        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)),
    )
    async with http:
        assert isinstance(http.cookies.jar, NoCookieJar)
        with pytest.raises(Exception) as info:
            await http.get("/sap/bw/ina/GetServerInfo")
    assert "connectivity service binding" in getattr(info.value, "admin_text", "")
    assert sent == []


async def test_a_description_the_gate_refuses_is_logged_by_rule_and_stores_nothing(
    client, remote, created, monkeypatch, caplog
):
    from agents.ina.models import InaDefinition, InaDimension
    from agents.ina.protocol import InaDescription

    async def twice(stored: dict[str, Any]) -> InaDescription:
        bad = InaDefinition.model_construct(
            dimensions=[InaDimension(name=SECRET), InaDimension(name=SECRET)],
            measures=[],
            variables=[],
        )
        return InaDescription(definition=bad)

    monkeypatch.setattr(admin_calls, "read_description", twice)
    with caplog.at_level(logging.WARNING, logger=admin_routes.logger.name):
        r = await client.post(f"{ONE}/describe")
    assert r.status_code == 502 and r.headers["x-ina-error"] == "sap_error"
    assert r.json() == {"detail": admin_routes._STORE_TEXT}
    (line,) = [rec.getMessage() for rec in caplog.records if rec.name == admin_routes.logger.name]
    assert "definition.dimensions" in line and "distinct" in line and SECRET not in line
    stored = (await client.get(ONE)).json()
    assert stored["described"] is False and stored["updated_at"] == created["updated_at"]


async def test_describe_answers_what_was_cut_and_left_out(client, remote, created, monkeypatch):
    from agents.ina.protocol import InaDescription

    async def cut(stored: dict[str, Any]) -> InaDescription:
        return InaDescription(definition=definition(), skipped=["0ODD", "0ODDER"], truncated=True)

    monkeypatch.setattr(admin_calls, "read_description", cut)
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 200, r.text
    assert r.json()["truncated"] is True and r.json()["skipped"] == 2


async def test_a_describe_that_changes_nothing_reloads_nothing(client, remote, live, rebuilt):
    await described(client, remote)
    await add_agent("analyst", NAME)
    rebuilt.clear()
    before = (await client.get(ONE)).json()
    r = await client.post(f"{ONE}/describe")
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    assert live.reloads == 0 and rebuilt == []
    after = (await client.get(ONE)).json()
    # The stamp says when SAP was read; the definition is the same.
    assert after["definition"] == before["definition"]
    assert after["metadata_fetched_at"] != before["metadata_fetched_at"]
