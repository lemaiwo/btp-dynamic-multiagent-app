"""URL confinement of the OData built-in (Task O2).

The destination's credential travels with every request, so everything a
model or a SAP answer can influence must stay a path below the service root:
path segments, the paging link SAP returns, and the ``$filter`` expression.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
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

from agents.odata.models import FieldDef  # noqa: E402
from agents.odata.urls import (  # noqa: E402
    MAX_FILTER_CHARS,
    FilterError,
    check_filter,
    confine_next_link,
    join_path,
)

SP = "/sap/opu/odata/sap/SRV"


# -- join_path ---------------------------------------------------------------


def test_join_path_confines_every_segment():
    assert join_path(SP, "Set('1')") == "/sap/opu/odata/sap/SRV/Set('1')"
    assert join_path(SP, "Set('1')", "to_Item") == "/sap/opu/odata/sap/SRV/Set('1')/to_Item"
    for bad in ("../x", "a/b", "a?b", "a#b", "", "a\nb"):
        with pytest.raises(ValueError):
            join_path(SP, bad)


@pytest.mark.parametrize(
    "bad",
    [
        "..",
        ".",
        "a..b",
        "a\\b",
        "a b",
        "a\tb",
        "a\x7fb",
        "café",
        "%2e%2e",
        "%2E%2E",
        "a%2Fb",
        "a%2fb",
        "a%5Cb",
        "a%3Fb",
        "a%23b",
        "a%0Ab",
        "a%00b",
        "%252e%252e",
        "a%252Fb",
        "50%25",  # an escaped '%' is one decode from anything
        "a%",
        "a%2",
        "a%zz",  # not an escape at all
        "x" * 2000,
    ],
)
def test_join_path_refuses_what_a_decoding_server_would_read_differently(bad):
    with pytest.raises(ValueError) as excinfo:
        join_path(SP, bad)
    assert bad not in str(excinfo.value) or len(bad) < 3


def test_join_path_keeps_harmless_escapes_and_checks_the_root():
    assert join_path(SP, "Set('a%20b')") == SP + "/Set('a%20b')"
    assert join_path(SP, "Set('caf%C3%A9')") == SP + "/Set('caf%C3%A9')"
    assert join_path(SP) == SP
    for bad_root in ("sap/opu", "/sap/../x", "https://evil.example/x", "/sap?x=1", ""):
        with pytest.raises(ValueError):
            join_path(bad_root, "Set")
    with pytest.raises(ValueError):
        join_path(SP, 5)  # type: ignore[arg-type]


def test_join_path_refusal_does_not_echo_the_segment():
    with pytest.raises(ValueError) as excinfo:
        join_path(SP, "Set?token=s3cr3t-value")
    assert "s3cr3t" not in str(excinfo.value)


# -- confine_next_link -------------------------------------------------------


def test_next_link_only_under_the_same_service_path():
    assert (
        confine_next_link("http://s4.internal:44300/sap/opu/odata/sap/SRV/Set?$skiptoken=50", SP)
        == "/sap/opu/odata/sap/SRV/Set?$skiptoken=50"
    )
    assert confine_next_link("Set?$skiptoken=50", SP) == "/sap/opu/odata/sap/SRV/Set?$skiptoken=50"
    assert confine_next_link(SP + "/Set?$skiptoken=50", SP) == SP + "/Set?$skiptoken=50"
    for bad in (
        "/sap/opu/odata/sap/OTHER/Set",
        "/sap/opu/odata/sap/SRV/../OTHER",
        "//evil.example/sap/opu/odata/sap/SRV/Set",
    ):
        assert confine_next_link(bad, SP) is None


@pytest.mark.parametrize(
    "bad",
    [
        "",
        SP,  # the root itself is no entity set
        SP + "/",
        SP + "X/Set",  # a sibling service sharing the prefix
        SP + "/Set/../../OTHER/Set",
        SP + "/%2e%2e/OTHER/Set",
        SP + "/Set%2F..%2F..%2FOTHER",
        SP + "/%252e%252e/OTHER",
        SP + "//Set",
        SP + "/Set#frag",
        SP + "/Set?$skiptoken=1#frag",
        SP + "/Set?$skiptoken=1\r\nX-Injected: 1",
        SP + "/Set?a=b c",
        SP + "\\..\\OTHER",
        "../OTHER/Set?$skiptoken=1",
        "Set/../../OTHER",
        "ftp://s4.internal/sap/opu/odata/sap/SRV/Set",
        "javascript://s4.internal/sap/opu/odata/sap/SRV/Set",
        "https://user:pw@s4.internal/sap/opu/odata/sap/SRV/Set",
        "https://s4.internal",
        "https://s4.internal?x=/sap/opu/odata/sap/SRV/Set",
        "/sap/opu/odata/sap/SRV/Set?" + "x" * 5000,
        None,
        42,
    ],
)
def test_next_link_refusals(bad):
    assert confine_next_link(bad, SP) is None


def test_next_link_needs_a_confined_service_path():
    assert confine_next_link("/x/../y/Set", "/x/../y") is None
    assert confine_next_link("Set", "relative/root") is None


def test_next_link_keeps_sap_escapes_in_path_and_query():
    link = "https://s4.internal:44300" + SP + "/Set(%271%27)/to_Item?$skiptoken=%27A%20B%27&$top=5"
    assert confine_next_link(link, SP) == SP + "/Set(%271%27)/to_Item?$skiptoken=%27A%20B%27&$top=5"


# -- check_filter ------------------------------------------------------------


def _fields():
    return {
        "Plant": FieldDef(name="Plant", selectable=True, filterable=True),
        "Quantity": FieldDef(name="Quantity", type="Edm.Decimal", selectable=True, filterable=True),
        "CreationDate": FieldDef(
            name="CreationDate", type="Edm.DateTime", selectable=True, filterable=True
        ),
        "Id": FieldDef(name="Id", type="Edm.Guid", selectable=True, filterable=True),
        "CreatedByUser": FieldDef(name="CreatedByUser"),
    }


def test_filter_fields_must_be_filterable():
    fields = {
        "Plant": FieldDef(name="Plant", selectable=True, filterable=True),
        "CreatedByUser": FieldDef(name="CreatedByUser"),
    }
    check_filter("Plant eq '1000' and startswith(Plant,'1')", fields)
    check_filter("Plant eq 'CreatedByUser'", fields)  # a string literal is not an identifier
    with pytest.raises(FilterError) as e:
        check_filter("CreatedByUser eq 'X'", fields)
    assert e.value.code == "field_not_filterable"
    with pytest.raises(FilterError) as e:
        check_filter("Nope eq 1", fields)
    assert e.value.code == "unknown_field"
    with pytest.raises(FilterError):
        check_filter("Plant eq '1'" + " " * 2000, fields)  # MAX_FILTER_CHARS
    with pytest.raises(FilterError):
        check_filter("Plant eq '1'\n&$expand=x", fields)  # control characters


@pytest.mark.parametrize(
    "expr",
    [
        "Plant eq '1000'",
        "not (Plant eq '1000') or Plant ne null",
        "not(Plant eq '1000')",
        "Plant eq 'O''Neil and CreatedByUser eq ''x'''",
        "Plant eq 'a&$top=1 /?#%'",
        "Quantity gt 10.5m and Quantity le -3 and Quantity lt 1e3d",
        "CreationDate ge datetime'2026-01-01T00:00:00' "
        "and CreationDate lt datetimeoffset'2026-02-01T00:00:00Z'",
        "Id eq guid'01234567-89ab-cdef-0123-456789abcdef'",
        "substringof('00',Plant) and length(tolower(Plant)) eq 4",
        "year(CreationDate) eq 2026 and indexof(Plant, '1') ge 0",
        "Plant in ('1000', '2000')",
        "  Plant   eq   '1'  ",
    ],
)
def test_filter_accepts_the_supported_grammar(expr):
    check_filter(expr, _fields())


@pytest.mark.parametrize(
    "expr, code",
    [
        ("CreatedByUser eq 'X'", "field_not_filterable"),
        ("substringof('X',CreatedByUser)", "field_not_filterable"),
        ("Plant eq '1' or (tolower(CreatedByUser) eq 'x')", "field_not_filterable"),
        ("Plant eq CreatedByUser", "field_not_filterable"),
        ("plant eq '1'", "unknown_field"),  # names are case-sensitive
        ("startswith eq 1", "unknown_field"),  # a function name without a call
        ("to_Item/Plant eq '1'", "invalid_argument"),  # no navigation paths
        ("Plant eq '1'&$top=9999", "invalid_argument"),
        ("Plant eq '1' $expand", "invalid_argument"),
        ("Plant eq '1';", "invalid_argument"),
        ('Plant eq "1"', "invalid_argument"),
        ("Plant eq '1", "invalid_argument"),  # unterminated literal
        ("Plant eq 1abc", "invalid_argument"),
        ("Plant eq %27x%27", "invalid_argument"),
        ("Plant eq café", "invalid_argument"),
        ("Plant eq '1'\t", "invalid_argument"),
        ("Plant eq '1\x00'", "invalid_argument"),
        ("Plant eq '‮1'", "invalid_argument"),  # a format character inside a literal
        ("evil(Plant) eq 1", "invalid_argument"),  # not an allowed function
        ("Plant add 1 eq 2", "unknown_field"),  # arithmetic is not offered
        ("Plant eq binary'00'", "unknown_field"),
        ("Quantity eq 1 - 2", "invalid_argument"),
    ],
)
def test_filter_refusals(expr, code):
    with pytest.raises(FilterError) as excinfo:
        check_filter(expr, _fields())
    assert excinfo.value.code == code
    assert excinfo.value.message and len(excinfo.value.message) <= 200


def test_filter_length_limit_and_types():
    fields = _fields()
    padding = MAX_FILTER_CHARS - len("Plant eq ''")
    check_filter("Plant eq '" + "x" * padding + "'", fields)
    with pytest.raises(FilterError) as excinfo:
        check_filter("Plant eq '" + "x" * (padding + 1) + "'", fields)
    assert excinfo.value.code == "invalid_argument"
    for not_text in (None, 5, ["Plant eq '1'"]):
        with pytest.raises(FilterError):
            check_filter(not_text, fields)  # type: ignore[arg-type]
    check_filter("", fields)  # nothing to refuse
    check_filter("   ", fields)


def test_filter_refusal_names_the_field_but_never_a_literal():
    with pytest.raises(FilterError) as excinfo:
        check_filter("CreatedByUser eq 'alice@example.com'", _fields())
    assert "CreatedByUser" in excinfo.value.message
    assert "alice" not in excinfo.value.message
    with pytest.raises(FilterError) as excinfo:
        check_filter("Plant eq 's3cr3t'&x", _fields())
    assert "s3cr3t" not in excinfo.value.message
    with pytest.raises(FilterError) as excinfo:
        check_filter("A" * 300 + " eq 1", _fields())
    assert len(excinfo.value.message) <= 200


# -- review follow-up --------------------------------------------------------


def test_a_field_named_like_an_operator_word_is_still_a_field():
    fields = {
        "Plant": FieldDef(name="Plant", selectable=True, filterable=True),
        "in": FieldDef(name="in"),
        "null": FieldDef(name="null"),
        "and": FieldDef(name="and"),
        "has": FieldDef(name="has", selectable=True, filterable=True),
        "length": FieldDef(name="length"),
    }
    check_filter("Plant eq '1' or has eq 1", fields)
    for expr in (
        "in eq 'x'",
        "Plant eq '1' or in eq 'x'",
        "Plant eq null",
        "Plant eq '1' and Plant eq '2'",
        "Plant in ('1')",
        "length(Plant) eq 1",
    ):
        with pytest.raises(FilterError) as excinfo:
            check_filter(expr, fields)
        assert excinfo.value.code == "field_not_filterable"
    # Without such fields the words are operators, as before.
    check_filter("Plant eq null and Plant in ('1') and length(Plant) eq 1", _fields())


# -- OData V4 (Task V42) -----------------------------------------------------

V4_GUID = "01234567-89ab-cdef-0123-456789abcdef"


def _v4_fields():
    def field(name, edm="Edm.String", filterable=True):
        return FieldDef(name=name, type=edm, selectable=filterable, filterable=filterable)

    return {
        "Plant": field("Plant"),
        "Quantity": field("Quantity", "Edm.Decimal"),
        "Day": field("Day", "Edm.Date"),
        "ChangedAt": field("ChangedAt", "Edm.DateTimeOffset"),
        "StartTime": field("StartTime", "Edm.TimeOfDay"),
        "Lead": field("Lead", "Edm.Duration"),
        "Id": field("Id", "Edm.Guid"),
        "Released": field("Released", "Edm.Boolean"),
        "Status": field("Status", "SRV.Status"),
        "Address": field("Address", "SRV.Address"),
        "Tags": field("Tags", "Collection(Edm.String)"),
        "Blob": field("Blob", "Edm.Binary"),
        "abcdef01": field("abcdef01"),  # a name that starts like a GUID
        "CreatedByUser": field("CreatedByUser", filterable=False),
    }


@pytest.mark.parametrize(
    "expr",
    [
        "Plant eq '1000'",
        "not (Plant eq '1000') or Plant ne null",
        "contains(Plant,'00') and startswith(Plant,'1') and endswith(Plant,'0')",
        "length(tolower(trim(Plant))) eq 4 and indexof(Plant,'1') ge 0",
        "Plant in ('1000','2000')",
        "Plant eq 'O''Neil and CreatedByUser eq ''x'''",
        "Plant eq 'a&$top=1 /?#% $it any(d:d/Plant)'",
        "Quantity gt 10.5 and Quantity le -3 and Quantity lt 1e3 and Quantity ne 1.5E-2",
        f"Id eq {V4_GUID}",
        "Id eq ABCDEF01-89AB-CDEF-0123-456789ABCDEF",
        "abcdef01 eq 'x'",
        "Day ge 2026-01-01 and Day lt 2026-02-01 and year(Day) eq 2026",
        "ChangedAt ge 2026-10-05T10:00:00Z and ChangedAt lt 2026-10-05T10:00:00.123+02:00",
        "date(ChangedAt) eq 2026-10-05 and time(ChangedAt) lt 12:30:00",
        "StartTime ge 08:00 and StartTime lt 17:30:00.5",
        "Lead gt duration'PT12H' and Lead le duration'P1DT2H3M4.5S'",
        "Status eq SRV.Status'Open' or Status has Some.Deep.Namespace.Flags'A,B'"
        " or Status eq Some.Deep.Namespace.Flags'A'",
        "Released eq true",
        "  Plant   eq   '1'  ",
        "",
    ],
)
def test_v4_filter_accepts_the_v4_grammar(expr):
    fields = _v4_fields()
    fields["Status"] = FieldDef(
        name="Status",
        type="Some.Deep.Namespace.Flags" if "Deep" in expr and "SRV" not in expr else "SRV.Status",
        selectable=True,
        filterable=True,
    )
    if "SRV.Status'" in expr and "Deep" in expr:
        # Two enum types in one expression: each field needs its own.
        fields["Status"] = FieldDef(
            name="Status", type="SRV.Status", selectable=True, filterable=True
        )
    check_filter(expr, fields, version="v4")


@pytest.mark.parametrize(
    "expr, code",
    [
        ("CreatedByUser eq 'X'", "field_not_filterable"),
        ("contains(CreatedByUser,'X')", "field_not_filterable"),
        ("Plant eq CreatedByUser", "field_not_filterable"),
        ("plant eq '1'", "unknown_field"),
        ("Plant EQ '1'", "unknown_field"),
        ("Plant eq '1' AND Plant eq '2'", "unknown_field"),
        ("contains eq 1", "unknown_field"),
        # The V2 way of writing a value or a function.
        (f"Id eq guid'{V4_GUID}'", "invalid_argument"),
        ("ChangedAt ge datetime'2026-01-01T00:00:00'", "invalid_argument"),
        ("ChangedAt ge datetimeoffset'2026-01-01T00:00:00Z'", "invalid_argument"),
        ("StartTime eq time'PT8H'", "invalid_argument"),
        ("Plant eq binary'00'", "invalid_argument"),
        ("Plant eq X'00'", "invalid_argument"),
        ("substringof('00',Plant)", "invalid_argument"),
        ("Quantity gt 10.5m", "invalid_argument"),
        ("Quantity gt 10.5M", "invalid_argument"),
        ("Quantity gt 5L", "invalid_argument"),
        ("Quantity gt 1.5d", "invalid_argument"),
        # Half a literal is no literal.
        ("Day eq 2026-01", "invalid_argument"),
        ("Day eq 2026-01-01T", "invalid_argument"),
        ("Day eq 2026-01-01x", "invalid_argument"),
        (f"Id eq {V4_GUID}-00", "invalid_argument"),
        (f"Id eq {V4_GUID[:-1]}", "invalid_argument"),
        ("ChangedAt eq 2026-10-05T10:00:00Z'x'", "invalid_argument"),
        ("Quantity eq 1 - 2", "invalid_argument"),
        ("Quantity eq 1abc", "invalid_argument"),
        ("Lead eq duration'P1D", "invalid_argument"),
        ("Lead eq duration'1 day'", "invalid_argument"),
        ("Status eq SRV.Status'Open", "invalid_argument"),
        ("Status eq SRV.Status''", "invalid_argument"),
        ("Status eq SRV.Status'Open' or Plant eq Edm.String'x'", "invalid_argument"),
        # Lambda operators, path and system segments, aliases, type functions.
        ("Items/any(d:d/Plant eq '1')", "invalid_argument"),
        ("Items/all(d:d/Plant eq '1')", "invalid_argument"),
        ("any(Plant)", "invalid_argument"),
        ("all(Plant)", "invalid_argument"),
        ("Items/$count gt 1", "invalid_argument"),
        ("$count gt 1", "invalid_argument"),
        ("$it eq '1'", "invalid_argument"),
        ("$it/Plant eq '1'", "invalid_argument"),
        ("$root/Plants('1')/Plant eq Plant", "invalid_argument"),
        ("Address/City eq 'x'", "invalid_argument"),
        ("Plant eq @p", "invalid_argument"),
        ("cast(Plant,Edm.Int32) eq 1", "invalid_argument"),
        ("isof(Plant,Edm.String)", "invalid_argument"),
        ("geo.distance(Plant,Plant) lt 1", "invalid_argument"),
        ("evil(Plant) eq 1", "invalid_argument"),
        ("Plant eq '1'&$top=9999", "invalid_argument"),
        ("Plant eq '1';", "invalid_argument"),
        ('Plant eq "1"', "invalid_argument"),
        ("Plant eq '1", "invalid_argument"),
        ("Plant eq %27x%27", "invalid_argument"),
        ("Plant eq '1'\t", "invalid_argument"),
        ("Plant add 1 eq 2", "unknown_field"),
        ("Quantity div 2 eq 1", "unknown_field"),
        # A type that is not positively recognised is no filter target.
        ("Address eq 'x'", "field_not_filterable"),
        ("Address eq null", "field_not_filterable"),
        ("Tags eq 'x'", "field_not_filterable"),
        ("Blob eq 'AAEC'", "field_not_filterable"),
        ("Status eq 'Open'", "field_not_filterable"),
        ("Status eq SRV.Other'Open'", "field_not_filterable"),
        ("Status eq SRV.Status'Open' and Address eq null", "field_not_filterable"),
    ],
)
def test_v4_filter_refusals(expr, code):
    with pytest.raises(FilterError) as excinfo:
        check_filter(expr, _v4_fields(), version="v4")
    assert excinfo.value.code == code, excinfo.value.message
    assert excinfo.value.message and len(excinfo.value.message) <= 260
    assert V4_GUID not in excinfo.value.message


def test_the_two_filter_grammars_are_not_mixed():
    fields = _v4_fields()
    for v4_only in (f"Id eq {V4_GUID}", "StartTime ge 08:00", "Lead gt duration'PT1H'"):
        check_filter(v4_only, fields, version="v4")
        with pytest.raises(FilterError):
            check_filter(v4_only, fields)  # V2 is the default
    for v2_only in (f"Id eq guid'{V4_GUID}'", "Quantity gt 1.5m", "substringof('0',Plant)"):
        check_filter(v2_only, fields, version="v2")
        with pytest.raises(FilterError):
            check_filter(v2_only, fields, version="v4")
    # V2 does not look at a field's type; V4 does.
    check_filter("Address eq 'x'", fields)
    for version in ("v3", "", None, 4, "V4"):
        with pytest.raises(FilterError) as excinfo:
            check_filter("Plant eq '1'", fields, version=version)  # type: ignore[arg-type]
        assert excinfo.value.code == "invalid_argument"


def test_v4_filter_refusal_never_repeats_a_literal():
    for expr in (
        "CreatedByUser eq 'alice@example.com'",
        "Plant eq 's3cr3t'&x",
        "Plant eq guid's3cr3t'",
        "Address eq SRV.Other's3cr3t'",
    ):
        with pytest.raises(FilterError) as excinfo:
            check_filter(expr, _v4_fields(), version="v4")
        assert "alice" not in excinfo.value.message and "s3cr3t" not in excinfo.value.message
    with pytest.raises(FilterError) as excinfo:
        check_filter("A" * 300 + " eq 1", _v4_fields(), version="v4")
    assert len(excinfo.value.message) <= 200
    with pytest.raises(FilterError):
        check_filter("Plant eq '" + "x" * MAX_FILTER_CHARS + "'", _v4_fields(), version="v4")
    # Pathological input is refused in linear time (no nested quantifier).
    for hostile in ("1" * 999 + "x", "A." * 400 + "'", "-" * 999, "0-" * 499):
        with pytest.raises(FilterError):
            check_filter("Plant eq " + hostile, _v4_fields(), version="v4")
