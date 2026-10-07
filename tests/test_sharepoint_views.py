"""The config of a ``builtin:sharepoint`` entry (``agents/sharepoint_views.py``).

Covered: the pins (site, library, path), both kinds of view, the stored form,
and that a refusal names the field and the rule and never a value.

Run:  python -m pytest tests/test_sharepoint_views.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from agents.sharepoint_views import (  # noqa: E402
    CalendarView,
    TableView,
    ViewConfigError,
    check_pins,
    clean_views,
    column_index,
    parse_views,
    site_host,
)
from tests.sharepoint_helpers import VIEWS  # noqa: E402

PINS = {"site": "example.sharepoint.com:/sites/planning", "library": "Documents",
        "path": "Team/Planning 2026.xlsx"}


def _views(**change):
    views = copy.deepcopy(VIEWS)
    views["planning"].update(change)
    return views


def test_the_example_config_parses_into_both_kinds():
    views = parse_views(VIEWS)
    assert isinstance(views["team"], TableView)
    assert views["team"].columns == ("Name", "Team", "ID")
    planning = views["planning"]
    assert isinstance(planning, CalendarView)
    assert planning.codes["I"] == "unavailable"
    assert planning.lookup.add == ("ID",) and planning.conflict.when == ("unavailable", "half_day")
    assert column_index("A") == 1 and column_index("XFD") == 16384


def test_the_stored_form_is_stable_and_holds_only_known_keys():
    stored = clean_views(VIEWS)
    assert stored == VIEWS
    assert clean_views(stored) == stored


SECRET = "s3cret-value"


@pytest.mark.parametrize("views, field", [
    ({}, "oauth.views"),
    ([], "oauth.views"),
    ({"Bad Name": VIEWS["team"]}, "oauth.views"),
    ({"team": {"kind": "pivot"}}, "oauth.views.team.kind"),
    ({"team": {**VIEWS["team"], "range": SECRET}}, "oauth.views.team"),
    ({"team": {**VIEWS["team"], "table": "1 " + SECRET}}, "oauth.views.team.table"),
    ({"team": {**VIEWS["team"], "columns": []}}, "oauth.views.team.columns"),
    ({"team": {**VIEWS["team"], "columns": ["Name", "Name"]}}, "oauth.views.team.columns"),
    (_views(sheet="{year}{year}"), "oauth.views.planning.sheet"),
    (_views(sheet="a/" + SECRET), "oauth.views.planning.sheet"),
    (_views(date_row=True), "oauth.views.planning.date_row"),
    (_views(date_row="8"), "oauth.views.planning.date_row"),
    (_views(first_row=8), "oauth.views.planning.first_row"),
    (_views(first_date_column="d"), "oauth.views.planning.first_date_column"),
    (_views(first_date_column="XFE"), "oauth.views.planning.first_date_column"),
    (_views(labels={"team": "B"}), "oauth.views.planning.labels"),
    (_views(labels={"member": "A", "role": "B"}), "oauth.views.planning.labels"),
    (_views(labels={"member": "A", "team": "A"}), "oauth.views.planning.labels"),
    (_views(labels={"member": "D"}), "oauth.views.planning.labels"),
    (_views(codes={}), "oauth.views.planning.codes"),
    (_views(codes={"H": "not a status " + SECRET}), "oauth.views.planning.codes"),
    (_views(codes={"H": "unmapped"}), "oauth.views.planning.codes"),
    (_views(stop_at=""), "oauth.views.planning.stop_at"),
    (_views(lookup={"view": "planning", "on": "Name", "add": ["ID"]}),
     "oauth.views.planning.lookup.view"),
    (_views(lookup={"view": "team", "on": "UserID", "add": ["ID"]}),
     "oauth.views.planning.lookup"),
    (_views(lookup={"view": "team", "on": "Name", "add": ["UserID"]}),
     "oauth.views.planning.lookup"),
    (_views(conflict={"kind": "Guard", "against": "Presence", "when": ["busy"]}),
     "oauth.views.planning.conflict.when"),
    (_views(conflict={"kind": "Guard", "against": "guard", "when": ["half_day"]}),
     "oauth.views.planning.conflict"),
])
def test_a_refusal_names_the_field_and_never_a_value(views, field):
    with pytest.raises(ViewConfigError) as refused:
        parse_views(views)
    text = str(refused.value)
    assert text.startswith(field + ":"), text
    assert SECRET not in text and "Bad Name" not in text


def test_a_conflict_needs_the_kind_label():
    views = _views(labels={"member": "A"})
    with pytest.raises(ViewConfigError, match="needs labels.kind"):
        parse_views(views)


def test_at_most_ten_views():
    many = {f"v{i}": VIEWS["team"] for i in range(11)}
    with pytest.raises(ViewConfigError, match="1 to 10 views"):
        parse_views(many)


def test_pins_are_accepted_and_name_the_download_host():
    check_pins(PINS)
    assert site_host(PINS["site"]) == "example.sharepoint.com"
    check_pins({**PINS, "site": "example.sharepoint.com:/teams/a/b"})


@pytest.mark.parametrize("change, field", [
    ({"site": ""}, "oauth.site"),
    ({"site": "https://example.sharepoint.com/sites/planning"}, "oauth.site"),
    ({"site": "Example.sharepoint.com:/sites/planning"}, "oauth.site"),
    ({"site": "example.sharepoint.com.evil.test:/sites/planning"}, "oauth.site"),
    ({"site": "example.sharepoint.com:/sites/../x"}, "oauth.site"),
    ({"site": "graph.microsoft.com:/sites/planning"}, "oauth.site"),
    ({"library": ""}, "oauth.library"),
    ({"library": "a/b"}, "oauth.library"),
    ({"path": ""}, "oauth.path"),
    ({"path": "/Team/Planning.xlsx"}, "oauth.path"),
    ({"path": "Team/../Planning.xlsx"}, "oauth.path"),
    ({"path": "Team/Planning.xlsx?x=1"}, "oauth.path"),
    ({"path": "Team/Planning%20.xlsx"}, "oauth.path"),
    ({"path": "Team/Planning.docx"}, "oauth.path"),
    ({"path": "Team/" + SECRET}, "oauth.path"),
])
def test_a_bad_pin_is_refused_by_field(change, field):
    with pytest.raises(ViewConfigError) as refused:
        check_pins({**PINS, **change})
    text = str(refused.value)
    assert text.startswith(field + ":"), text
    assert SECRET not in text and "evil" not in text
