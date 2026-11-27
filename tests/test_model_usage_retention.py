"""Retention of the model usage ledger (cost in euros): its own clock, 0 keeps."""

from __future__ import annotations

import importlib
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

import app as app_module  # noqa: E402
from agents import model_usage_store  # noqa: E402
from agents.db import ModelUsage, SessionLocal, init_db  # noqa: E402


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()
    async with SessionLocal() as db:
        await db.execute(ModelUsage.__table__.delete())
        await db.commit()


async def _add(ref: str, age_days: int) -> None:
    async with SessionLocal() as db:
        db.add(ModelUsage(
            source="job", ref_id=ref, model="m",
            created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        ))
        await db.commit()


async def _refs() -> set[str]:
    async with SessionLocal() as db:
        return set((await db.execute(select(ModelUsage.ref_id))).scalars())


async def test_purge_deletes_only_older_rows():
    await _add("old", 500)
    await _add("new", 10)
    async with SessionLocal() as db:
        assert await model_usage_store.purge_model_usage(db, 400) == 1
    assert await _refs() == {"new"}


@pytest.mark.parametrize("days", [0, -3, True, "5"])
async def test_zero_or_bad_days_delete_nothing(days):
    await _add("old", 5000)
    async with SessionLocal() as db:
        assert await model_usage_store.purge_model_usage(db, days) == 0
    assert await _refs() == {"old"}


def test_setting_defaults_and_falls_back(monkeypatch, caplog):
    monkeypatch.delenv("MODEL_USAGE_RETENTION_DAYS", raising=False)
    importlib.reload(model_usage_store)
    assert model_usage_store.MODEL_USAGE_RETENTION_DAYS == 400
    monkeypatch.setenv("MODEL_USAGE_RETENTION_DAYS", "0")
    importlib.reload(model_usage_store)
    assert model_usage_store.MODEL_USAGE_RETENTION_DAYS == 0
    monkeypatch.setenv("MODEL_USAGE_RETENTION_DAYS", "a year")
    with caplog.at_level(logging.WARNING):
        importlib.reload(model_usage_store)
    assert model_usage_store.MODEL_USAGE_RETENTION_DAYS == 400
    assert "a year" not in caplog.text
    monkeypatch.delenv("MODEL_USAGE_RETENTION_DAYS")
    importlib.reload(model_usage_store)


async def test_daily_pass_purges_the_ledger(monkeypatch):
    await _add("old", 900)
    monkeypatch.setattr(app_module.model_usage_store, "MODEL_USAGE_RETENTION_DAYS", 400)
    await app_module._purge_ide_sessions()
    assert await _refs() == set()


async def test_daily_pass_keeps_ledger_when_zero(monkeypatch):
    await _add("old", 900)
    monkeypatch.setattr(app_module.model_usage_store, "MODEL_USAGE_RETENTION_DAYS", 0)
    await app_module._purge_ide_sessions()
    assert await _refs() == {"old"}
