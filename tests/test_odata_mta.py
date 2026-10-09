"""mta.yaml: the connectivity resource and the OData env vars.

Run:  python -m pytest tests/test_odata_mta.py -q
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def mta() -> dict:
    return yaml.safe_load((ROOT / "mta.yaml").read_text())


def _app() -> dict:
    return next(m for m in mta()["modules"] if m["name"] == "pydantic-agent")


def test_version_is_2_24_0():
    assert mta()["version"] == "2.24.0"


def test_connectivity_resource_is_lite_and_bound_to_the_app():
    res = {r["name"]: r for r in mta()["resources"]}["agent-connectivity"]
    assert res["type"] == "org.cloudfoundry.managed-service"
    assert res["parameters"] == {"service": "connectivity", "service-plan": "lite"}
    names = [r["name"] for r in _app()["requires"]]
    assert "agent-connectivity" in names
    assert names.index("agent-connectivity") > names.index("agent-destination")


def test_odata_env_vars_are_quoted_strings():
    props = _app()["properties"]
    assert props["ODATA_AUDIT_RETENTION_DAYS"] == "365"


def test_descriptor_names_no_landscape():
    # CONNECTIVITY_PP_MODE is set per landscape in an .mtaext, not here.
    assert "CONNECTIVITY_PP_MODE" not in (ROOT / "mta.yaml").read_text()
