"""The one reading of a ``builtin:hana`` entry (``agents/hana/config.py``)."""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from agents.hana import BUILTIN_HANA_URL
from agents.hana.config import (
    ENTRY_KEYS,
    HANA_SINGLE_ENTRY_MESSAGE,
    MAX_SYSTEMS,
    SYSTEM_KEYS,
    HanaConfigError,
    Pins,
    check_entry,
    clean_entry,
    pins_of,
)

VALID = {
    "systems": [
        {"name": "sib_systemdb", "destination": "HANA_SIB_SYSTEMDB"},
        {"name": "sib_tenant", "destination": "HANA_SIB_TENANT"},
    ],
    "allow_select": True,
}


def _with(**changes):
    cfg = {"systems": [dict(s) for s in VALID["systems"]], "allow_select": True}
    cfg.update(changes)
    return cfg


def _refused(cfg, field: str, *secrets: str) -> str:
    with pytest.raises(HanaConfigError) as exc:
        clean_entry(cfg)
    text = str(exc.value)
    assert text.startswith(field), text
    for secret in secrets:
        assert secret not in text, text
    return text


def test_constants():
    assert BUILTIN_HANA_URL == "builtin:hana"
    assert ENTRY_KEYS == ("systems", "allow_select")
    assert SYSTEM_KEYS == ("name", "destination")
    assert MAX_SYSTEMS == 10
    assert HANA_SINGLE_ENTRY_MESSAGE == (
        "an agent may have at most one builtin:hana entry; list every system it "
        "reads in that entry's oauth.systems")
    assert issubclass(HanaConfigError, ValueError)


def test_valid_entry_is_stored_exactly():
    assert clean_entry(VALID) == VALID
    check_entry(VALID)


def test_clean_entry_returns_a_copy():
    cfg = _with()
    cleaned = clean_entry(cfg)
    cleaned["systems"][0]["name"] = "other"
    assert cfg["systems"][0]["name"] == "sib_systemdb"


def test_allow_select_absent_or_false_is_stored_absent():
    cfg = _with()
    del cfg["allow_select"]
    assert clean_entry(cfg) == {"systems": VALID["systems"]}
    assert clean_entry(_with(allow_select=False)) == {"systems": VALID["systems"]}
    assert clean_entry(_with(allow_select=None)) == {"systems": VALID["systems"]}


def test_key_order_is_fixed():
    cfg = {"allow_select": True,
           "systems": [{"destination": "HANA_X", "name": "x"}]}
    cleaned = clean_entry(cfg)
    assert list(cleaned) == ["systems", "allow_select"]
    assert list(cleaned["systems"][0]) == ["name", "destination"]


@pytest.mark.parametrize("cfg", [None, "systems", ["x"], 1, True])
def test_not_a_dict(cfg):
    _refused(cfg, "oauth:")


@pytest.mark.parametrize("systems", [None, [], "HANA_SECRET_X", {"name": "x"}, 5])
def test_systems_missing_empty_or_not_a_list(systems):
    cfg = _with(systems=systems)
    if systems is None:
        del cfg["systems"]
    _refused(cfg, "oauth.systems:", "HANA_SECRET_X")


def test_more_than_ten_systems():
    systems = [{"name": f"s{i}", "destination": f"HANA_{i}"} for i in range(11)]
    _refused(_with(systems=systems), "oauth.systems:")
    assert len(clean_entry(_with(systems=systems[:10]))["systems"]) == 10


@pytest.mark.parametrize("item", ["HANA_SECRET_X", ["x"], None, 3])
def test_item_not_a_dict(item):
    _refused(_with(systems=[VALID["systems"][0], item]),
             "oauth.systems[1]:", "HANA_SECRET_X")


def test_item_with_a_stray_key_does_not_name_it():
    item = {"name": "x", "destination": "HANA_X", "password_secret_key": "hunter2"}
    text = _refused(_with(systems=[item]), "oauth.systems[0]:",
                    "password_secret_key", "hunter2")
    assert "name" in text and "destination" in text


@pytest.mark.parametrize("missing", ["name", "destination"])
def test_item_without_name_or_destination(missing):
    item = {"name": "x", "destination": "HANA_X"}
    del item[missing]
    _refused(_with(systems=[item]), f"oauth.systems[0].{missing}:")


@pytest.mark.parametrize("name", [
    "Sib", "1sib", "_sib", "sib-tenant", "sib tenant", " sib", "sib ", "sib\n",
    "a" * 33, "", "sïb", 7, None, True])
def test_bad_name(name):
    text = _refused(_with(systems=[{"name": name, "destination": "HANA_X"}]),
                    "oauth.systems[0].name:")
    # A name of fewer than three visible characters could stand in the fixed
    # text by chance; every longer one must not be repeated.
    if isinstance(name, str) and len(name.strip()) >= 3:
        assert name not in text, text
        assert name.strip() not in text, text


def test_good_names():
    for name in ("a", "sib_systemdb", "a" * 32, "x9_"):
        clean_entry(_with(systems=[{"name": name, "destination": "HANA_X"}]))


def test_duplicate_names():
    systems = [{"name": "sib", "destination": "HANA_A"},
               {"name": "sib", "destination": "HANA_B"}]
    text = _refused(_with(systems=systems), "oauth.systems[1].name: duplicate", "HANA_A")
    assert "sib" not in text.split(":", 1)[1]


def test_names_are_case_sensitive_only_lower_case_allowed():
    # Upper case is refused by the pattern, so a case-folded duplicate cannot exist.
    _refused(_with(systems=[{"name": "SIB", "destination": "HANA_A"}]),
             "oauth.systems[0].name:")


@pytest.mark.parametrize("destination", [
    "HANA SECRETX", " HANA_SECRETX", "HANA_SECRETX ", "HANA/SECRETX", "HANA_SECRETX\n",
    "", "H" * 201, 5, None, ["HANA_SECRETX"], True])
def test_bad_destination(destination):
    _refused(_with(systems=[{"name": "x", "destination": destination}]),
             "oauth.systems[0].destination:", "SECRETX")


def test_good_destinations():
    for destination in ("A", "HANA.SIB-1_x", "H" * 200):
        clean_entry(_with(systems=[{"name": "x", "destination": destination}]))


def test_destination_pattern_equals_the_odata_gate():
    from agents.hana import config
    from agents.odata.models import DESTINATION_NAME_RE

    assert "^" + config.DESTINATION_PATTERN + "$" == DESTINATION_NAME_RE


@pytest.mark.parametrize("value", ["true", 1, 0, "false", [True], {"x": 1}])
def test_allow_select_must_be_a_json_boolean(value):
    _refused(_with(allow_select=value), "oauth.allow_select:")


def test_user_context_true_is_refused():
    _refused(_with(user_context=True), "oauth.user_context:")


def test_user_context_false_echo_is_dropped():
    assert clean_entry(_with(user_context=False)) == VALID


def test_has_client_secret_false_echo_is_dropped():
    assert clean_entry(_with(has_client_secret=False)) == VALID
    assert clean_entry(_with(has_client_secret=None)) == VALID


def test_has_client_secret_true_is_refused():
    _refused(_with(has_client_secret=True), "oauth:")


def test_other_top_level_key_is_refused_without_its_name():
    _refused(_with(secret_token_key="hunter2"), "oauth:", "secret_token_key", "hunter2")


def test_top_level_destination_is_refused():
    text = _refused(_with(destination="HANA_TOPSECRET"), "oauth.destination:",
                    "HANA_TOPSECRET")
    assert "per system" in text


def test_pins_of():
    pins = pins_of(VALID)
    assert pins == Pins(
        systems=(("sib_systemdb", "HANA_SIB_SYSTEMDB"),
                 ("sib_tenant", "HANA_SIB_TENANT")),
        allow_select=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        pins.allow_select = False  # type: ignore[misc]
    cfg = _with()
    del cfg["allow_select"]
    assert pins_of(cfg).allow_select is False


def test_pins_of_refuses_what_check_refuses():
    with pytest.raises(HanaConfigError):
        pins_of({"systems": []})


def test_standard_library_only():
    from agents.hana import config

    source = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            # The package itself holds only BUILTIN_HANA_URL.
            roots.add(node.module if node.module == "agents.hana"
                      else node.module.split(".")[0])
    assert roots <= {"__future__", "re", "dataclasses", "typing", "agents.hana"}, roots


def test_the_package_init_imports_nothing():
    """``agents/db.py`` reads the entry through ``agents.hana.config``: the
    package itself must stay free of imports, or that read would pull them."""
    import agents.hana

    tree = ast.parse(pathlib.Path(agents.hana.__file__).read_text(encoding="utf-8"))
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert imports == []
