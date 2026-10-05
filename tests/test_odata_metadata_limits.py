"""The bounds of ``agents.odata.metadata.parse_metadata``.

A remote system chooses the ``$metadata`` document, so the work it can make
the parser do must not be "entity sets x properties". Asserted on what was
built and on the parser's own work counter, never on wall time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

from agents.odata import metadata  # noqa: E402
from agents.odata.metadata import (  # noqa: E402
    MAX_PARSED_ENTITY_SETS,
    MAX_PARSED_OPERATIONS,
    MetadataError,
    SkippedElement,
    parse_metadata,
)
from agents.odata.models import MAX_ENTITY_SETS, MAX_OPERATIONS  # noqa: E402

V2_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" '
    'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">'
    '<edmx:DataServices m:DataServiceVersion="2.0">'
    '<Schema Namespace="NS" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">'
)
V2_TAIL = "</Schema></edmx:DataServices></edmx:Edmx>"
V4_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">'
    '<edmx:DataServices><Schema Namespace="NS" xmlns="http://docs.oasis-open.org/odata/ns/edm">'
)
V4_TAIL = "</Schema></edmx:DataServices></edmx:Edmx>"


def properties(count: int, bad: int = 0) -> str:
    good = "".join(f'<Property Name="F{i}" Type="Edm.String"/>' for i in range(count))
    return good + "".join(f'<Property Name="bad name {i}" Type="Edm.String"/>' for i in range(bad))


def v2(sets: int, fields: int, *, bad: int = 0, operations: str = "", extra: str = "") -> bytes:
    entity_sets = "".join(f'<EntitySet Name="S{i}" EntityType="NS.T"/>' for i in range(sets))
    return (
        V2_HEAD
        + f'<EntityType Name="T"><Key><PropertyRef Name="F0"/></Key>{properties(fields, bad)}'
        + f"</EntityType>{extra}"
        + f'<EntityContainer Name="C" m:IsDefaultEntityContainer="true">{entity_sets}{operations}'
        + f"</EntityContainer>{V2_TAIL}"
    ).encode()


def v4(sets: int, fields: int, *, bad: int = 0, set_body: str = "", extra: str = "") -> bytes:
    entity_sets = "".join(
        f'<EntitySet Name="S{i}" EntityType="NS.T">{set_body}</EntitySet>' for i in range(sets)
    )
    return (
        V4_HEAD
        + f'<EntityType Name="T"><Key><PropertyRef Name="F0"/></Key>{properties(fields, bad)}'
        + f"</EntityType>{extra}"
        + f'<EntityContainer Name="C">{entity_sets}</EntityContainer>{V4_TAIL}'
    ).encode()


@pytest.mark.parametrize("build", [v2, v4])
def test_many_entity_sets_on_one_large_type_share_its_fields(build):
    """The reviewer's document in small: every set past the first costs
    nothing per property."""
    parsed = parse_metadata(build(300, 2_000), "v2" if build is v2 else "v4")
    assert len(parsed.entity_sets) == 300
    first = parsed.entity_sets[0]
    assert len(first.fields) == 2_000
    # One tuple of fields and one key, shared by all 300 sets ...
    assert all(e.fields is first.fields and e.keys is first.keys for e in parsed.entity_sets)
    # ... and work proportional to properties + sets, not to their product.
    assert parsed.work < 3 * 2_000 + 10 * 300
    assert parsed.truncated is False


@pytest.mark.parametrize("build", [v2, v4])
def test_a_shared_types_skipped_properties_are_recorded_once(build):
    parsed = parse_metadata(build(50, 3, bad=40), "v2" if build is v2 else "v4")
    assert len(parsed.entity_sets) == 50
    assert len(parsed.skipped) == 40
    # Under the first set of the type, at their positions in its type chain.
    assert parsed.skipped[0] == SkippedElement("property", "S0", 4, "invalid_name")
    assert {s.entity_set for s in parsed.skipped} == {"S0"}


def test_v4_sets_with_their_own_restrictions_get_their_own_fields():
    restricted = (
        '<Annotation Term="Capabilities.FilterRestrictions"><Record>'
        '<PropertyValue Property="NonFilterableProperties"><Collection>'
        "<PropertyPath>F1</PropertyPath></Collection></PropertyValue></Record></Annotation>"
    )
    document = v4(1, 3).replace(
        b"</EntityContainer>",
        f'<EntitySet Name="R0" EntityType="NS.T">{restricted}</EntitySet>'
        f'<EntitySet Name="R1" EntityType="NS.T">{restricted}</EntitySet>'
        "</EntityContainer>".encode(),
    )
    plain, r0, r1 = parse_metadata(document, "v4").entity_sets
    assert [f.filterable for f in plain.fields] == [True, True, True]
    assert [f.filterable for f in r0.fields] == [True, False, True]
    # The same restrictions on the same type: one tuple.
    assert r1.fields is r0.fields and r0.fields is not plain.fields


@pytest.mark.parametrize("build", [v2, v4])
def test_entity_sets_past_the_cap_are_counted_not_built(build):
    parsed = parse_metadata(build(MAX_PARSED_ENTITY_SETS + 250, 3), "v2" if build is v2 else "v4")
    assert MAX_PARSED_ENTITY_SETS > MAX_ENTITY_SETS
    assert [e.name for e in parsed.entity_sets] == [f"S{i}" for i in range(MAX_PARSED_ENTITY_SETS)]
    assert parsed.truncated is True
    assert parsed.entity_sets_declared == MAX_PARSED_ENTITY_SETS + 250


def test_v2_operations_past_the_cap_are_counted_not_built():
    imports = "".join(
        f'<FunctionImport Name="Op{i}" m:HttpMethod="POST"/>'
        for i in range(MAX_PARSED_OPERATIONS + 20)
    )
    parsed = parse_metadata(v2(1, 2, operations=imports), "v2")
    assert MAX_PARSED_OPERATIONS > MAX_OPERATIONS
    assert len(parsed.operations) == MAX_PARSED_OPERATIONS and parsed.truncated is True
    assert parsed.operations_declared == MAX_PARSED_OPERATIONS + 20
    assert parsed.entity_sets_declared == 1


def test_v4_operations_past_the_cap_are_counted_not_built():
    actions = "".join(
        f'<Action Name="A{i}" IsBound="true"><Parameter Name="_it" Type="NS.T"/></Action>'
        for i in range(MAX_PARSED_OPERATIONS + 20)
    )
    parsed = parse_metadata(v4(1, 2, extra=actions), "v4")
    assert len(parsed.operations) == MAX_PARSED_OPERATIONS and parsed.truncated is True
    assert parsed.operations_declared == MAX_PARSED_OPERATIONS + 20


def test_a_document_within_the_caps_reports_what_it_declares():
    parsed = parse_metadata(v2(3, 2, operations='<FunctionImport Name="Op"/>'), "v2")
    assert parsed.truncated is False
    assert (parsed.entity_sets_declared, parsed.operations_declared) == (3, 1)
    assert 0 < parsed.work < 50


def refused(document: bytes, version: str, budget: int, monkeypatch) -> None:
    monkeypatch.setattr(metadata, "MAX_PARSE_WORK", budget)
    with pytest.raises(MetadataError) as caught:
        parse_metadata(document, version)
    assert str(caught.value) == (
        f"the $metadata document is too large to read: its entity sets and operations "
        f"have more than {budget} properties, navigations and parameters in all"
    )


def test_the_work_budget_ends_a_parse_of_too_many_properties(monkeypatch):
    refused(v2(1, 500), "v2", 400, monkeypatch)
    refused(v4(1, 500), "v4", 400, monkeypatch)


def test_the_work_budget_covers_navigations_per_set(monkeypatch):
    """Navigations are resolved per set: sets x navigations is real work."""
    navigations = "".join(
        f'<NavigationProperty Name="N{i}" Relationship="NS.A" FromRole="a" ToRole="b"/>'
        for i in range(100)
    )
    document = v2(100, 1).replace(b"</EntityType>", navigations.encode() + b"</EntityType>")
    assert parse_metadata(document, "v2").work > 100 * 100
    refused(document, "v2", 5_000, monkeypatch)


def test_the_work_budget_covers_skipped_and_duplicate_elements(monkeypatch):
    duplicates = '<EntitySet Name="S0" EntityType="NS.T"/>' * 2_000
    document = v2(1, 1).replace(b"</EntityContainer>", duplicates.encode() + b"</EntityContainer>")
    assert len(parse_metadata(document, "v2").skipped) == 2_000
    refused(document, "v2", 1_000, monkeypatch)


def test_the_work_budget_covers_v4_variants_and_parameters(monkeypatch):
    restricted = "".join(
        '<Annotation Term="Capabilities.FilterRestrictions"><Record>'
        '<PropertyValue Property="NonFilterableProperties"><Collection>'
        "<PropertyPath>F1</PropertyPath></Collection></PropertyValue></Record></Annotation>"
    )
    # Each set restricts a different property: 60 variants of 100 fields.
    sets = "".join(
        f'<EntitySet Name="V{i}" EntityType="NS.T">'
        + restricted.replace("F1", f"F{i}")
        + "</EntitySet>"
        for i in range(60)
    )
    document = v4(0, 100).replace(b"</EntityContainer>", sets.encode() + b"</EntityContainer>")
    parsed = parse_metadata(document, "v4")
    assert len(parsed.entity_sets) == 60 and parsed.work > 60 * 100
    refused(document, "v4", 3_000, monkeypatch)
    parameters = "".join(f'<Parameter Name="P{i}" Type="Edm.String"/>' for i in range(300))
    refused(
        v2(1, 1, operations=f'<FunctionImport Name="Op">{parameters}</FunctionImport>'),
        "v2",
        200,
        monkeypatch,
    )


def test_a_long_inheritance_chain_is_walked_once_and_charged(monkeypatch):
    depth = 300
    types = '<EntityType Name="T0"><Key><PropertyRef Name="Id"/></Key>'
    types += '<Property Name="Id" Type="Edm.String"/></EntityType>'
    types += "".join(
        f'<EntityType Name="T{i}" BaseType="NS.T{i - 1}"/>' for i in range(1, depth)
    )
    sets = "".join(f'<EntitySet Name="S{i}" EntityType="NS.T{depth - 1}"/>' for i in range(100))
    document = (
        V2_HEAD + types + f'<EntityContainer Name="C">{sets}</EntityContainer>' + V2_TAIL
    ).encode()
    parsed = parse_metadata(document, "v2")
    assert len(parsed.entity_sets) == 100 and parsed.entity_sets[0].keys[0].name == "Id"
    assert parsed.work < 2 * depth + 10 * 100
    refused(document, "v2", depth - 10, monkeypatch)


def test_a_bytearray_is_parsed_without_being_copied_first():
    document = bytearray(v2(1, 2))
    assert len(parse_metadata(document, "v2").entity_sets) == 1
    assert len(parse_metadata(memoryview(bytes(document)), "v2").entity_sets) == 1
