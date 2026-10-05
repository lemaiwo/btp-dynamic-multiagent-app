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


# ------------------------------------------------- fix round 2: inner scans
#
# One unit of the work budget must not hide a scan whose size the document
# chooses. Two kinds of assertion, neither on wall time: the budget error
# where the work is charged per item examined, and -- where the fix is an
# index or a memo, which is not charged -- the number of tree accesses the
# parse made, counted by `_Probe` elements.

import random  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402


class _Probe(ET.Element):
    """An element that counts what is read of it: attribute reads, and the
    children a ``find`` / ``findall`` / iteration walks over."""

    counts = {"reads": 0, "scanned": 0}

    def get(self, key, default=None):
        _Probe.counts["reads"] += 1
        return super().get(key, default)

    def find(self, path, namespaces=None):
        _Probe.counts["scanned"] += len(self)
        return super().find(path, namespaces)

    def findall(self, path, namespaces=None):
        _Probe.counts["scanned"] += len(self)
        return super().findall(path, namespaces)

    def __iter__(self):
        _Probe.counts["scanned"] += len(self)
        return iter(self[:])


@pytest.fixture
def steps(monkeypatch):
    """Tree accesses of the parses in this test, as a callable."""
    counts = {"reads": 0, "scanned": 0}
    monkeypatch.setattr(_Probe, "counts", counts)
    real = ET.TreeBuilder
    monkeypatch.setattr(metadata.ET, "TreeBuilder", lambda: real(element_factory=_Probe))
    return lambda: counts["reads"] + counts["scanned"]


def linear(document: bytes) -> int:
    """A generous bound for work that is linear in the document: 40 tree
    accesses per tag. The scans these tests are about are 10 to 100 times that."""
    return 40 * document.count(b"<")


EMPTY_LABEL = '<Annotation Term="Common.Label" String=""/>'


def test_v2_association_ends_are_indexed_once(steps):
    """N1: the `End` list of an association was scanned per navigation per set."""
    ends = "".join(f'<End Role="r{i}" Type="NS.T"/>' for i in range(2_000))
    navigations = "".join(
        f'<NavigationProperty Name="N{i}" Relationship="NS.A" FromRole="a" ToRole="z"/>'
        for i in range(20)
    )
    document = v2(20, 1, extra=f'<Association Name="A">{ends}</Association>').replace(
        b"</EntityType>", navigations.encode() + b"</EntityType>"
    )
    parsed = parse_metadata(document, "v2")
    assert len(parsed.entity_sets) == 20
    assert [s.reason for s in parsed.skipped] == ["unresolved_target"] * 400
    assert steps() < linear(document)


def test_v4_sets_of_a_type_without_a_usable_key_do_not_rescan_annotations(steps):
    """N1: a set whose type has an unrepresentable key is never kept, so its
    name can repeat; each repeat read the set's restrictions again."""
    restricted = (
        '<Annotation Term="Capabilities.FilterRestrictions"><Record>'
        '<PropertyValue Property="Filterable" Bool="true"/></Record></Annotation>'
    )
    document = (
        V4_HEAD
        + '<EntityType Name="T"><Key><PropertyRef Name="Ghost"/></Key>'
        + '<Property Name="F0" Type="Edm.String"/></EntityType>'
        + f'<Annotations Target="NS.C/S">{restricted * 300}</Annotations>'
        + '<EntityContainer Name="C">'
        + '<EntitySet Name="S" EntityType="NS.T"/>' * 300
        + "</EntityContainer>"
        + V4_TAIL
    ).encode()
    parsed = parse_metadata(document, "v4")
    assert parsed.entity_sets == ()
    assert [s.reason for s in parsed.skipped] == ["unrepresentable_key"] * 300
    assert steps() < linear(document)


def duplicate_properties(block: str, count: int = 300) -> bytes:
    """A V4 type whose property `P` is declared `count` times, with `block`
    as the content of the `Annotations` block that targets it."""
    return (
        V4_HEAD
        + '<EntityType Name="T"><Key><PropertyRef Name="Id"/></Key>'
        + '<Property Name="Id" Type="Edm.String"/>'
        + '<Property Name="P" Type="Edm.String"/>' * count
        + f'</EntityType><Annotations Target="NS.T/P">{block}</Annotations>'
        + '<EntityContainer Name="C"><EntitySet Name="S" EntityType="NS.T"/></EntityContainer>'
        + V4_TAIL
    ).encode()


def test_v4_duplicate_property_names_are_charged_for_their_annotations(monkeypatch):
    """N1: properties of one name share the target `Type/Prop`; each read
    the whole block again for one unit."""
    refused(duplicate_properties(EMPTY_LABEL * 300), "v4", 5_000, monkeypatch)


def test_v4_annotations_that_are_not_read_cost_nothing_per_lookup(steps):
    """The same shape with annotations the parser has no use for, and a
    label whose value sits behind many other children."""
    junk = '<Annotation Term="Custom.Thing" String="x"/>' * 300
    label = f'<Annotation Term="Common.Label">{"<Junk/>" * 300}<String>Real</String></Annotation>'
    document = duplicate_properties(junk + label)
    parsed = parse_metadata(document, "v4")
    assert [(f.name, f.label) for f in parsed.entity_sets[0].fields] == [("Id", ""), ("P", "Real")]
    assert steps() < linear(document)


def test_v4_overloads_are_charged_for_the_label_scan(monkeypatch):
    """N1: overloads of one name are skipped at one unit each, after each
    of them read the operation's `Annotations` block."""
    overloads = (
        '<Action Name="A" IsBound="true"><Parameter Name="it" Type="NS.T"/></Action>' * 300
    )
    block = f'<Annotations Target="NS.A">{EMPTY_LABEL * 300}</Annotations>'
    refused(v4(1, 2, extra=overloads + block), "v4", 5_000, monkeypatch)


def test_v4_the_type_label_is_read_once_per_type(steps):
    """N1: the set label's fallback, the type's label, was read per set."""
    block = f'<Annotations Target="NS.T">{EMPTY_LABEL * 300}</Annotations>'
    document = v4(300, 2, extra=block)
    parsed = parse_metadata(document, "v4")
    assert len(parsed.entity_sets) == 300 and parsed.entity_sets[-1].label == ""
    assert steps() < linear(document)
    labelled = v4(3, 2, extra='<Annotations Target="NS.T"><Annotation Term="Common.Label" '
                  'String="Order"/></Annotations>')
    assert [e.label for e in parse_metadata(labelled, "v4").entity_sets] == ["Order"] * 3


def test_v4_restriction_records_are_charged_per_value_and_path(monkeypatch):
    values = '<PropertyValue Property="Filterable" Bool="true"/>' * 2_000
    by_value = (
        f'<Annotation Term="Capabilities.FilterRestrictions"><Record>{values}</Record></Annotation>'
    )
    refused(v4(1, 2, set_body=by_value), "v4", 1_500, monkeypatch)
    paths = "<PropertyPath>F1</PropertyPath>" * 2_000
    by_path = (
        '<Annotation Term="Capabilities.FilterRestrictions"><Record>'
        f'<PropertyValue Property="NonFilterableProperties"><Collection>{paths}</Collection>'
        "</PropertyValue></Record></Annotation>"
    )
    refused(v4(1, 2, set_body=by_path), "v4", 1_500, monkeypatch)


def test_v4_one_action_with_many_imports_stops_at_the_operation_cap():
    """N3: the cap is on operations BUILT, not on `Action` elements read."""
    imports = "".join(
        f'<ActionImport Name="I{i}" Action="NS.Do"/>' for i in range(MAX_PARSED_OPERATIONS + 20)
    )
    document = v4(1, 2, extra='<Action Name="Do"/>').replace(
        b"</EntityContainer>", imports.encode() + b"</EntityContainer>"
    )
    parsed = parse_metadata(document, "v4")
    assert len(parsed.operations) == MAX_PARSED_OPERATIONS and parsed.truncated is True
    assert parsed.operations[-1].name == f"I{MAX_PARSED_OPERATIONS - 1}"
    exact = v4(1, 2, extra='<Action Name="Do"/>').replace(
        b"</EntityContainer>",
        "".join(
            f'<ActionImport Name="I{i}" Action="NS.Do"/>' for i in range(MAX_PARSED_OPERATIONS)
        ).encode()
        + b"</EntityContainer>",
    )
    parsed = parse_metadata(exact, "v4")
    assert len(parsed.operations) == MAX_PARSED_OPERATIONS and parsed.truncated is False


@pytest.mark.parametrize("version", ["v2", "v4"])
def test_a_shared_base_types_labels_are_cleaned_once(version, monkeypatch):
    """A long label was cleaned character by character for every derived
    type that inherits the property."""
    calls = [0]
    real = metadata.unicodedata.category

    def category(ch):
        calls[0] += 1
        return real(ch)

    monkeypatch.setattr(metadata.unicodedata, "category", category)
    long_label = "​" * 4_000 + "Amount"  # zero-width, then the label
    if version == "v2":
        sap = 'xmlns:sap="http://www.sap.com/Protocols/SAPData"'
        head = V2_HEAD.replace("<Schema ", f"<Schema {sap} ")
        base = (
            '<EntityType Name="B"><Key><PropertyRef Name="Id"/></Key>'
            f'<Property Name="Id" Type="Edm.String" sap:label="{long_label}"/></EntityType>'
        )
        tail = V2_TAIL
    else:
        head = V4_HEAD
        base = (
            '<EntityType Name="B"><Key><PropertyRef Name="Id"/></Key>'
            f'<Property Name="Id" Type="Edm.String"><Annotation Term="Common.Label" '
            f'String="{long_label}"/></Property></EntityType>'
        )
        tail = V4_TAIL
    types = "".join(f'<EntityType Name="D{i}" BaseType="NS.B"/>' for i in range(60))
    sets = "".join(f'<EntitySet Name="S{i}" EntityType="NS.D{i}"/>' for i in range(60))
    document = (
        head + base + types + f'<EntityContainer Name="C">{sets}</EntityContainer>' + tail
    ).encode()
    parsed = parse_metadata(document, version)
    assert len(parsed.entity_sets) == 60
    assert {e.fields[0].label for e in parsed.entity_sets} == {"Amount"}
    assert calls[0] < 4 * len(long_label)


def test_a_long_shared_name_is_charged_where_it_is_copied(monkeypatch):
    """A name that many elements share (container, namespace) is copied into
    a target or a qualified name per element: charged by its length."""
    name = "C" * 50_000
    types = "".join(
        f'<EntityType Name="T{i}"><Key><PropertyRef Name="Ghost"/></Key></EntityType>'
        for i in range(300)
    )
    sets = "".join(f'<EntitySet Name="S{i}" EntityType="NS.T{i}"/>' for i in range(300))
    document = (
        V4_HEAD + types + f'<EntityContainer Name="{name}">{sets}</EntityContainer>' + V4_TAIL
    ).encode()
    refused(document, "v4", 2_000, monkeypatch)
    namespace = "N" * 50_000
    actions = '<Action Name="A"/>' * 300
    document = (
        V4_HEAD.replace('Namespace="NS"', f'Namespace="{namespace}"') + actions + V4_TAIL
    ).encode()
    refused(document, "v4", 2_000, monkeypatch)


def test_a_qualified_name_resolves_as_before():
    """`_Schemas.canonical` no longer tries every namespace per reference;
    the rule it replaces is spelled out here and compared on many names."""
    schemas = (
        '<Schema Namespace="com.sap.one" Alias="one"/><Schema Namespace="com.sap" Alias="s"/>'
        '<Schema Namespace="com.sap.one.more" Alias="one.x"/><Schema Namespace="z" Alias="one"/>'
        '<Schema Namespace="" Alias="none"/><Schema Namespace="s.t"/>'
    )
    root, _ = metadata._parse_tree(
        f'<Edmx xmlns="http://docs.oasis-open.org/odata/ns/edmx">{schemas}</Edmx>'.encode()
    )
    elements = list(root.iter("Schema"))
    resolver = metadata._Schemas(elements)
    namespaces = sorted(
        (e.get("Namespace") for e in elements if e.get("Namespace")), key=len, reverse=True
    )
    aliases = sorted(
        ((e.get("Alias"), e.get("Namespace")) for e in elements
         if e.get("Namespace") and e.get("Alias")),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )

    def before(ref):
        ref = (ref or "").strip()
        if any(ref.startswith(f"{namespace}.") for namespace in namespaces):
            return ref
        for alias, namespace in aliases:
            if ref.startswith(f"{alias}."):
                return namespace + ref[len(alias):]
        return ref

    parts = ["com", "sap", "one", "more", "s", "t", "x", "z", "none", "Order", ""]
    rng = random.Random(7)
    refs = [None, "", " one.Order ", "one", "one.", ".one", "one.x.Order", "s.t.Order"]
    refs += [".".join(rng.choice(parts) for _ in range(rng.randint(1, 6))) for _ in range(3_000)]
    for ref in refs:
        assert resolver.canonical(ref) == before(ref), ref
