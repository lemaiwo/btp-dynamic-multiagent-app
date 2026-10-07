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
    del views["planning"]["kinds"]
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


# --- fix round 1: the gate checks the value that is stored -------------------

NFD_PATH = "Team/Plané.xlsx"  # "e" + combining acute: not composed (NFC)


@pytest.mark.parametrize("change", [
    {"path": "Team/Plan.xlsx\n"},
    {"path": " Team/Plan.xlsx"},
    {"path": "Team/Plan.xlsx "},
    {"path": NFD_PATH},
    {"library": " Documents "},
    {"library": "Documents\n"},
    {"library": "Libraryé"},
    {"site": " example.sharepoint.com:/sites/planning"},
    {"site": "example.sharepoint.com:/sites/planning\n"},
])
def test_a_pin_that_is_not_in_its_checked_form_is_refused(change):
    """`check_pins` returns nothing and the caller stores the raw value, so a
    value the check would have trimmed or composed must not pass."""
    (key, value), = change.items()
    with pytest.raises(ViewConfigError) as refused:
        check_pins({**PINS, **change})
    text = str(refused.value)
    assert text.startswith(f"oauth.{key}:"), text
    assert value not in text and value.strip() not in text


@pytest.mark.parametrize("cfg", [None, [], "site", 0, [("site", "x")]])
def test_pins_that_are_not_an_object_are_refused(cfg):
    with pytest.raises(ViewConfigError) as refused:
        check_pins(cfg)
    assert str(refused.value).startswith("oauth:")


@pytest.mark.parametrize("separator", [" ", " "])
def test_a_line_or_paragraph_separator_is_not_one_line(separator):
    for change, field in [({"path": f"Team/Pl{separator}an.xlsx"}, "oauth.path"),
                          ({"library": f"Docu{separator}ments"}, "oauth.library")]:
        with pytest.raises(ViewConfigError) as refused:
            check_pins({**PINS, **change})
        assert str(refused.value).startswith(field + ":")
    for views, field in [
        ({"team": {**VIEWS["team"], "columns": ["Name", f"Te{separator}am"]}},
         "oauth.views.team.columns"),
        (_views(stop_at=f"Sum{separator}mary"), "oauth.views.planning.stop_at"),
    ]:
        with pytest.raises(ViewConfigError) as refused:
            parse_views(views)
        assert str(refused.value).startswith(field + ":")


@pytest.mark.parametrize("column", ["status", "member", "Team", "KIND", "from", "To"])
def test_a_lookup_cannot_add_a_column_named_like_a_run_field(column):
    """The added columns sit in the run object beside member, team, kind,
    status, from and to: a table column of such a name would replace one."""
    views = _views(lookup={"view": "team", "on": "Name", "add": [column]})
    views["team"] = {"kind": "table", "table": "TeamMembers", "columns": ["Name", column]}
    with pytest.raises(ViewConfigError) as refused:
        parse_views(views)
    text = str(refused.value)
    assert text.startswith("oauth.views.planning.lookup.add:"), text
    assert "run field" in text


# --- fix round 1: rules that had no test ------------------------------------

@pytest.mark.parametrize("change, field", [
    ({"path": "Team\\Planning.xlsx"}, "oauth.path"),
    ({"path": "Team/Planning#1.xlsx"}, "oauth.path"),
    ({"path": "Team/Planning:1.xlsx"}, "oauth.path"),
    ({"path": "Team/Planning*.xlsx"}, "oauth.path"),
    ({"path": 'Team/"Planning".xlsx'}, "oauth.path"),
    ({"path": "Team/<Planning.xlsx"}, "oauth.path"),
    ({"path": "Team/Planning>.xlsx"}, "oauth.path"),
    ({"path": "Team/Plan|ning.xlsx"}, "oauth.path"),
    ({"path": "Team/./Planning.xlsx"}, "oauth.path"),
    ({"path": "Team//Planning.xlsx"}, "oauth.path"),
    ({"path": "Team/ Planning.xlsx"}, "oauth.path"),
    ({"path": "Team/Plan\x07ning.xlsx"}, "oauth.path"),
    ({"path": "Team/Plan\tning.xlsx"}, "oauth.path"),
    ({"path": "a" * 396 + ".xlsx"}, "oauth.path"),
    ({"path": 7}, "oauth.path"),
    ({"site": "example.sharepoint.com:/a/b/c/d/e"}, "oauth.site"),
    ({"site": "example.sharepoint.com:/sites/..."}, "oauth.site"),
    ({"site": "example.sharepoint.com:/sites/planning/"}, "oauth.site"),
    ({"site": "example.sharepoint.com:"}, "oauth.site"),
    ({"site": None}, "oauth.site"),
    ({"library": "a\\" + SECRET}, "oauth.library"),
    ({"library": "a/" + SECRET}, "oauth.library"),
    ({"library": SECRET * 11}, "oauth.library"),
    ({"library": None}, "oauth.library"),
])
def test_more_bad_pins_are_refused_by_field(change, field):
    with pytest.raises(ViewConfigError) as refused:
        check_pins({**PINS, **change})
    text = str(refused.value)
    assert text.startswith(field + ":"), text
    assert SECRET not in text and "Planning" not in text


def test_pins_at_their_limits_are_accepted():
    check_pins({**PINS, "path": "a" * 395 + ".xlsx"})          # 400 characters
    check_pins({**PINS, "path": "Team/PLANNING.XLSX"})        # the suffix in any case
    check_pins({**PINS, "library": "L" * 128})
    check_pins({**PINS, "site": "example.sharepoint.com:/a/b/c/d"})  # four segments


def _codes(n):
    return {f"C{i}": "unavailable" for i in range(n)}


@pytest.mark.parametrize("views, field", [
    (_views(codes={"H": "unavailable", " H": "available"}), "oauth.views.planning.codes"),
    (_views(codes=_codes(101)), "oauth.views.planning.codes"),
    (_views(codes={"H" * 17: "unavailable"}), "oauth.views.planning.codes"),
    (_views(codes={"H": 1}), "oauth.views.planning.codes"),
    (_views(lookup=False), "oauth.views.planning.lookup"),
    (_views(lookup=0), "oauth.views.planning.lookup"),
    (_views(lookup={"view": "team", "on": "Name", "add": ["ID"], "more": SECRET}),
     "oauth.views.planning.lookup"),
    (_views(conflict=False), "oauth.views.planning.conflict"),
    (_views(conflict=0), "oauth.views.planning.conflict"),
    (_views(first_date_column=4), "oauth.views.planning.first_date_column"),
    (_views(first_date_column=None), "oauth.views.planning.first_date_column"),
    (_views(sheet="{year}" + "s" * 28), "oauth.views.planning.sheet"),
    (_views(sheet="s" * 32), "oauth.views.planning.sheet"),
    (_views(sheet="a[" + SECRET), "oauth.views.planning.sheet"),
    (_views(sheet="a]" + SECRET), "oauth.views.planning.sheet"),
    (_views(sheet="a:" + SECRET), "oauth.views.planning.sheet"),
    (_views(sheet="{month}"), "oauth.views.planning.sheet"),
    (_views(first_row=1.5), "oauth.views.planning.first_row"),
    (_views(date_row=0), "oauth.views.planning.date_row"),
    ({"team": {**VIEWS["team"], "columns": [f"c{i}" for i in range(51)]}},
     "oauth.views.team.columns"),
    ({"team": {**VIEWS["team"], "columns": "Name"}}, "oauth.views.team.columns"),
    ({"team": {**VIEWS["team"], "table": "Team " + SECRET}}, "oauth.views.team.table"),
    ({"team": {**VIEWS["team"], "table": "Team\x07Members"}}, "oauth.views.team.table"),
    ({"team": {**VIEWS["team"], "table": "TeamMembers\n"}}, "oauth.views.team.table"),
    ({"team": SECRET}, "oauth.views.team"),
])
def test_more_refusals_name_the_field_and_never_a_value(views, field):
    with pytest.raises(ViewConfigError) as refused:
        parse_views(views)
    text = str(refused.value)
    assert text.startswith(field + ":"), text
    assert SECRET not in text


def test_views_at_their_limits_are_accepted():
    assert parse_views(_views(sheet="{year}" + "s" * 27))["planning"].sheet.endswith("s")
    assert len(parse_views(_views(sheet="s" * 31))["planning"].sheet) == 31
    assert len(parse_views(_views(codes={**_codes(98), "H": "unavailable", "½H": "half_day"}))
               ["planning"].codes) == 100
    columns = [f"c{i}" for i in range(50)]
    assert parse_views({"team": {**VIEWS["team"], "columns": columns}})["team"].columns \
        == tuple(columns)


# --- AMENDMENT 1: pinned row kinds ------------------------------------------

def _without(*keys, **change):
    views = _views(**change)
    for key in keys:
        del views["planning"][key]
    return views


def test_the_kinds_of_a_calendar_view_are_parsed_and_stored():
    planning = parse_views(VIEWS)["planning"]
    assert planning.kinds == ("Presence", "Guard")
    assert clean_views(VIEWS)["planning"]["kinds"] == ["Presence", "Guard"]
    # Stored in the checked form, as every other configured name.
    assert clean_views(_views(kinds=[" Presence", "Guard "]))["planning"]["kinds"] \
        == ["Presence", "Guard"]


def test_a_view_without_a_kind_label_has_no_kinds():
    views = _without("kinds", "conflict", labels={"member": "A", "team": "B"})
    assert parse_views(views)["planning"].kinds == ()
    assert "kinds" not in clean_views(views)["planning"]
    assert clean_views(views) == views


@pytest.mark.parametrize("views, field", [
    (_without("kinds"), "oauth.views.planning.kinds"),
    (_views(kinds=None), "oauth.views.planning.kinds"),
    (_views(kinds=[]), "oauth.views.planning.kinds"),
    (_views(kinds="Guard " + SECRET), "oauth.views.planning.kinds"),
    (_views(kinds={"Guard": SECRET}), "oauth.views.planning.kinds"),
    (_views(kinds=["Presence", "Guard", 7]), "oauth.views.planning.kinds"),
    (_views(kinds=["Presence", "Guard", True]), "oauth.views.planning.kinds"),
    (_views(kinds=["Presence", "Guard", ""]), "oauth.views.planning.kinds"),
    (_views(kinds=["Presence", "Guard", "Guard"]), "oauth.views.planning.kinds"),
    (_views(kinds=["Presence", "Guard", " Guard "]), "oauth.views.planning.kinds"),
    (_views(kinds=["Presence", "Guard", "x" * 61]), "oauth.views.planning.kinds"),
    (_views(kinds=["Presence", "Guard", SECRET + "\nline"]), "oauth.views.planning.kinds"),
    (_views(kinds=["Presence", "Guard", SECRET + "\u2028"  + "x"]),
     "oauth.views.planning.kinds"),
    (_views(kinds=["Presence", "Guard"] + [f"k{i}" for i in range(9)]),
     "oauth.views.planning.kinds"),
    # No kind label: the list would pin nothing.
    (_without("conflict", labels={"member": "A", "team": "B"}), "oauth.views.planning.kinds"),
    (_without("conflict", labels={"member": "A"}, kinds=None), "oauth.views.planning.kinds"),
    # A conflict compares two pinned kinds.
    (_views(kinds=["Presence", SECRET]), "oauth.views.planning.conflict.kind"),
    (_views(kinds=["Guard", SECRET]), "oauth.views.planning.conflict.against"),
    (_views(kinds=["Presence", "guard"]), "oauth.views.planning.conflict.kind"),
])
def test_a_kinds_refusal_names_the_field_and_never_a_value(views, field):
    with pytest.raises(ViewConfigError) as refused:
        parse_views(views)
    text = str(refused.value)
    assert text.startswith(field + ":"), text
    assert SECRET not in text


def test_kinds_at_their_limits_are_accepted():
    kinds = ["Presence", "Guard"] + [f"{i}" + "k" * 59 for i in range(8)]
    assert parse_views(_views(kinds=kinds))["planning"].kinds == tuple(kinds)
