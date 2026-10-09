"""mta.yaml and requirements.txt: SAP HANA as the second database choice.

Run:  python -m pytest tests/test_hana_mta.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "mta.yaml").read_text()


def mta() -> dict:
    return yaml.safe_load(TEXT)


def _app() -> dict:
    return next(m for m in mta()["modules"] if m["name"] == "pydantic-agent")


def _resources() -> dict:
    return {r["name"]: r for r in mta()["resources"]}


def test_version_is_2_23_0():
    assert mta()["version"] == "2.23.0"


def test_the_hdi_container_is_declared_and_switched_off():
    resource = _resources()["agent-registry-hana"]
    assert resource["type"] == "org.cloudfoundry.managed-service"
    assert resource["parameters"] == {"service": "hana", "service-plan": "hdi-shared"}
    # Off by default: a deploy without an extension creates no HANA
    # container and binds none.
    assert resource["active"] is False


def test_postgres_stays_the_default():
    resource = _resources()["agent-registry-db"]
    assert resource["parameters"]["service"] == "postgresql-db"
    assert resource.get("active", True) is True


def test_the_app_requires_both_so_an_extension_only_flips_active():
    names = [r["name"] for r in _app()["requires"]]
    assert "agent-registry-db" in names and "agent-registry-hana" in names
    assert names.index("agent-registry-hana") == names.index("agent-registry-db") + 1


def test_no_other_module_requires_the_container():
    for module in mta()["modules"]:
        if module["name"] != "pydantic-agent":
            assert "agent-registry-hana" not in [r["name"] for r in module.get("requires", [])]


def test_the_descriptor_shows_the_extension_that_switches():
    """As a comment: an .mtaext is landscape-specific and not in the repo."""
    comment = "\n".join(
        line.split("#", 1)[1] for line in TEXT.splitlines() if line.lstrip().startswith("#")
    )
    snippet = re.search(
        r"resources:\s*\n\s*- name: agent-registry-db\s*\n\s*active: false\s*\n"
        r"\s*- name: agent-registry-hana\s*\n\s*active: true",
        comment,
    )
    assert snippet, "the mtaext snippet is missing from the mta.yaml comment"
    assert "DB_KIND" in comment


def test_db_kind_is_not_set_in_the_descriptor():
    # It only matters when a landscape binds both; then its .mtaext sets it.
    assert "DB_KIND" not in _app()["properties"]


def test_the_hana_driver_and_dialect_are_pinned():
    pins = dict(
        line.split("==", 1)
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if "==" in line and not line.startswith("#")
    )
    assert pins["sqlalchemy-hana"] == "5.0.0"
    assert pins["hdbcli"] == "2.30.27"
    # Still there, at the versions they had.
    assert pins["asyncpg"] == "0.31.0" and pins["aiosqlite"] == "0.22.1"


def test_the_pins_are_what_is_installed():
    from importlib.metadata import version

    assert version("sqlalchemy-hana") == "5.0.0"
    assert version("hdbcli") == "2.30.27"
