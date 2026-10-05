"""The OData V4 ``$metadata`` parser (EDMX 4.0 -> the common parsed model).

Same result shape as V2, read from V4's own vocabulary: capabilities and
labels are annotations (inline or in an ``Annotations`` block), a navigation
gets its target from a ``NavigationPropertyBinding`` and operations are
actions (POST) and functions (GET). What cannot be represented is skipped
and recorded, never guessed; the XML refusals are the V2 ones.
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

from agents.odata import metadata as odata_metadata  # noqa: E402
from agents.odata.metadata import (  # noqa: E402
    SKIP_REASONS,
    MetadataError,
    ParsedMetadata,
    SkippedElement,
    parse_metadata,
)
from agents.odata.models import KeyDef, OperationDef, ParamDef  # noqa: E402

XML = (ROOT / "tests/fixtures/odata/v4_reduced.xml").read_bytes()
V2_XML = (ROOT / "tests/fixtures/odata/v2_purchasereq_reduced.xml").read_bytes()

_EDMX = "http://docs.oasis-open.org/odata/ns/edmx"
_EDM = "http://docs.oasis-open.org/odata/ns/edm"
_COMMON = "com.sap.vocabularies.Common.v1"
_CAPABILITIES = "Org.OData.Capabilities.V1"

_REFERENCES = (
    f'<edmx:Reference Uri="https://example.com/c.xml">'
    f'<edmx:Include Namespace="{_COMMON}" Alias="Common"/></edmx:Reference>'
    f'<edmx:Reference Uri="https://example.com/p.xml">'
    f'<edmx:Include Namespace="{_CAPABILITIES}" Alias="Capabilities"/></edmx:Reference>'
)


def _doc(schema_body: str, *, references: str = _REFERENCES, more: str = "") -> bytes:
    """A minimal EDMX 4.0 document around one schema ``NS`` (alias ``Self``)."""
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<edmx:Edmx Version="4.0" xmlns:edmx="{_EDMX}">{references}<edmx:DataServices>'
        f'<Schema Namespace="NS" Alias="Self" xmlns="{_EDM}">{schema_body}</Schema>{more}'
        f"</edmx:DataServices></edmx:Edmx>"
    ).encode()


_ORDER = (
    '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
    '<Property Name="Id" Type="Edm.Guid" Nullable="false"/>'
    '<Property Name="Note" Type="Edm.String"/>'
    '<NavigationProperty Name="_Lines" Type="Collection(NS.LineType)"/>'
    "</EntityType>"
)
_LINE = (
    '<EntityType Name="LineType"><Key><PropertyRef Name="Pos"/></Key>'
    '<Property Name="Pos" Type="Edm.Int32" Nullable="false"/>'
    '<NavigationProperty Name="_Order" Type="NS.OrderType"/>'
    "</EntityType>"
)
_SETS = (
    '<EntitySet Name="Orders" EntityType="NS.OrderType">'
    '<NavigationPropertyBinding Path="_Lines" Target="Lines"/></EntitySet>'
    '<EntitySet Name="Lines" EntityType="NS.LineType">'
    '<NavigationPropertyBinding Path="_Order" Target="Orders"/></EntitySet>'
)


def _container(body: str = _SETS) -> str:
    return f'<EntityContainer Name="C">{body}</EntityContainer>'


def _sets(md: ParsedMetadata) -> dict:
    return {e.name: e for e in md.entity_sets}


def _ops(md: ParsedMetadata) -> dict:
    return {o.name: o for o in md.operations}


# ---------------------------------------------------------------- the fixture


def test_entity_sets_keys_and_types():
    md = parse_metadata(XML, "v4")
    assert md.version == "v4"
    assert md.skipped == ()
    assert [e.name for e in md.entity_sets] == ["PurchaseRequisition", "PurchaseRequisitionItem"]
    header, item = md.entity_sets
    assert header.entity_type == "com.example.pr.v0001.PurchaseRequisitionType"
    assert header.keys == (KeyDef(name="PurchaseRequisition", type="Edm.String"),)
    assert item.keys == (
        KeyDef(name="PurchaseRequisition", type="Edm.String"),
        KeyDef(name="PurchaseRequisitionItem", type="Edm.String"),
    )
    assert [(f.name, f.type, f.nullable) for f in header.fields] == [
        ("PurchaseRequisition", "Edm.String", False),
        ("PurReqnDescription", "Edm.String", True),
        ("CreatedByUser", "Edm.String", True),
        ("CreationDate", "Edm.Date", True),
    ]
    assert {f.name: f.type for f in item.fields}["RequestedQuantity"] == "Edm.Decimal"


def test_labels_from_an_inline_annotation_and_from_an_annotations_block():
    header, item = parse_metadata(XML, "v4").entity_sets
    labels = {f.name: f.label for f in header.fields}
    assert labels["PurchaseRequisition"] == "Purchase requisition"  # inline
    assert labels["PurReqnDescription"] == "Description"  # Annotations Target="alias.Type/Prop"
    assert labels["CreationDate"] == ""
    assert {f.name: f.label for f in item.fields}["PurchaseRequisitionItem"] == "Item"
    # No label on the set itself: the entity type's is taken.
    assert header.label == "Purchase requisition header"
    assert item.label == ""


def test_capability_restrictions_absent_means_true():
    header, item = parse_metadata(XML, "v4").entity_sets
    assert (header.creatable, header.updatable, header.deletable) == (True, True, False)
    assert (item.creatable, item.updatable, item.deletable) == (False, False, True)
    assert {f.name for f in header.fields if not f.filterable} == {"CreatedByUser"}
    assert all(f.filterable for f in item.fields)
    assert all(f.creatable and f.updatable for e in (header, item) for f in e.fields)


def test_navigation_target_from_the_binding_and_collection_from_the_type():
    header, item = parse_metadata(XML, "v4").entity_sets
    assert [(n.name, n.target, n.collection) for n in header.navigations] == [
        ("_Item", "PurchaseRequisitionItem", True)
    ]
    assert [(n.name, n.target, n.collection) for n in item.navigations] == [
        ("_Header", "PurchaseRequisition", False)
    ]


def test_bound_action_and_unbound_function():
    ops = _ops(parse_metadata(XML, "v4"))
    assert list(ops) == ["Release", "GetOpenCount"]
    release = ops["Release"]
    assert (release.kind, release.http_method) == ("action", "POST")
    assert release.bound_to == "PurchaseRequisition"
    assert release.qualified_name == "com.example.pr.v0001.Release"
    # The binding parameter is the entity the action is called on, not an argument.
    assert release.parameters == (ParamDef(name="Note", type="Edm.String", required=False),)
    assert release.label == "Release requisition"
    count = ops["GetOpenCount"]
    assert (count.kind, count.http_method, count.bound_to) == ("function", "GET", None)
    assert count.qualified_name == "com.example.pr.v0001.GetOpenCount"
    assert count.parameters == (ParamDef(name="Plant", type="Edm.String", required=True),)


def test_every_emitted_operation_fits_the_definition_model():
    md = parse_metadata(XML, "v4")
    for op in md.operations:
        OperationDef(
            name=op.name,
            qualified_name=op.qualified_name,
            kind=op.kind,
            http_method=op.http_method,
            bound_to=op.bound_to,
            parameters=list(op.parameters),
        )
        assert op.qualified_name  # rule 6: a V4 operation needs one


# ------------------------------------------------------------ version and XML


def test_the_document_version_must_match_the_requested_one():
    with pytest.raises(MetadataError, match="^the document is OData V2, not V4$"):
        parse_metadata(V2_XML, "v4")
    with pytest.raises(MetadataError, match="^the document is OData V4, not V2$"):
        parse_metadata(XML, "v2")
    with pytest.raises(MetadataError, match="unknown OData version"):
        parse_metadata(XML, "v3")  # type: ignore[arg-type]


def test_doctype_and_entities_are_refused():
    body = XML.split(b"?>", 1)[1]
    for prolog in (
        b'<!DOCTYPE x [<!ENTITY a "aaaa">]>',
        b'<!DOCTYPE x SYSTEM "http://example.com/x.dtd">',
        b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/hostname">]>',
    ):
        with pytest.raises(MetadataError, match="DTD"):
            parse_metadata(b'<?xml version="1.0"?>' + prolog + body, "v4")


def test_the_dtd_handlers_refuse_without_the_byte_scan(monkeypatch):
    import re

    monkeypatch.setattr(odata_metadata, "_DTD_RE", re.compile(rb"(?!)"))
    body = XML.split(b"?>", 1)[1]
    with pytest.raises(MetadataError, match="DTD"):
        parse_metadata(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]>' + body, "v4")


def test_non_edmx_oversized_and_deep_input_is_refused():
    with pytest.raises(MetadataError, match="not an EDMX document"):
        parse_metadata(b"<html><body>session=SECRETCOOKIE <br></body></html>", "v4")
    with pytest.raises(MetadataError, match="not an EDMX document"):
        parse_metadata(f'<edmx:Edmx Version="4.0" xmlns:edmx="{_EDMX}"/>'.encode(), "v4")
    with pytest.raises(MetadataError, match="larger than"):
        parse_metadata(b" " * (odata_metadata.MAX_METADATA_BYTES + 1), "v4")
    deep = "<Annotation>" * 100 + "</Annotation>" * 100
    with pytest.raises(MetadataError, match="nested deeper"):
        parse_metadata(_doc(_ORDER + deep + _container()), "v4")


# ---------------------------------------------------------------- annotations


@pytest.mark.parametrize(
    "references, prefix",
    [
        (
            f'<edmx:Reference Uri="https://example.com/c.xml">'
            f'<edmx:Include Namespace="{_COMMON}" Alias="SAP__common"/></edmx:Reference>',
            "SAP__common",
        ),
        ("", _COMMON),  # the full vocabulary namespace needs no reference
        ("", "Common"),  # the conventional alias, even when the reference is missing
    ],
)
def test_a_term_is_recognised_by_alias_or_namespace(references, prefix):
    md = parse_metadata(
        _doc(
            '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
            '<Property Name="Id" Type="Edm.Guid" Nullable="false">'
            f'<Annotation Term="{prefix}.Label" String="Order id"/></Property></EntityType>'
            + _container('<EntitySet Name="Orders" EntityType="NS.OrderType"/>'),
            references=references,
        ),
        "v4",
    )
    assert md.entity_sets[0].fields[0].label == "Order id"


def test_an_alias_declared_for_another_vocabulary_is_not_the_label():
    md = parse_metadata(
        _doc(
            '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
            '<Property Name="Id" Type="Edm.Guid" Nullable="false">'
            '<Annotation Term="Common.Label" String="Not a label"/></Property></EntityType>'
            + _container('<EntitySet Name="Orders" EntityType="NS.OrderType"/>'),
            references='<edmx:Reference Uri="https://example.com/o.xml">'
            '<edmx:Include Namespace="org.example.other" Alias="Common"/></edmx:Reference>',
        ),
        "v4",
    )
    assert md.entity_sets[0].fields[0].label == ""


def test_values_given_as_element_content_and_qualified_annotations():
    md = parse_metadata(
        _doc(
            '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
            '<Property Name="Id" Type="Edm.Guid" Nullable="false">'
            '<Annotation Term="Common.Label" Qualifier="Short" String="Qualified"/>'
            '<Annotation Term="Common.Label"><String>Order  id\n</String></Annotation>'
            "</Property>"
            '<Property Name="Note" Type="Edm.String"/>'
            '<Property Name="Hidden" Type="Edm.String"/>'
            "</EntityType>"
            + _container(
                '<EntitySet Name="Orders" EntityType="NS.OrderType">'
                '<Annotation Term="Common.Label" String="Orders (set)"/>'
                '<Annotation Term="Capabilities.InsertRestrictions"><Record>'
                '<PropertyValue Property="Insertable"><Bool> false </Bool></PropertyValue>'
                "</Record></Annotation>"
                # A qualified annotation is an alternative, not the default.
                '<Annotation Term="Capabilities.DeleteRestrictions" Qualifier="Other"><Record>'
                '<PropertyValue Property="Deletable" Bool="false"/></Record></Annotation>'
                # A value computed from a path is not a declared "false".
                '<Annotation Term="Capabilities.UpdateRestrictions"><Record>'
                '<PropertyValue Property="Updatable" Path="IsEditable"/></Record></Annotation>'
                '<Annotation Term="Capabilities.FilterRestrictions"><Record>'
                '<PropertyValue Property="NonFilterableProperties"><Collection>'
                "<PropertyPath> Hidden </PropertyPath><PropertyPath>Nope</PropertyPath>"
                "</Collection></PropertyValue></Record></Annotation>"
                "</EntitySet>"
            )
            + '<Annotations Target="NS.OrderType"><Annotation Term="Common.Label" String="Type"/>'
            "</Annotations>"
        ),
        "v4",
    )
    orders = md.entity_sets[0]
    assert orders.label == "Orders (set)"  # the set's own label wins over its type's
    assert (orders.creatable, orders.updatable, orders.deletable) == (False, True, True)
    assert {f.name: (f.label, f.filterable) for f in orders.fields} == {
        "Id": ("Order id", True),
        "Note": ("", True),
        "Hidden": ("", False),
    }


def test_filterable_false_on_the_set_covers_every_field():
    md = parse_metadata(
        _doc(
            _ORDER + _LINE + _container() + '<Annotations Target="NS.C/Orders">'
            '<Annotation Term="Capabilities.FilterRestrictions"><Record>'
            '<PropertyValue Property="Filterable" Bool="false"/></Record></Annotation>'
            "</Annotations>"
        ),
        "v4",
    )
    sets = _sets(md)
    assert not any(f.filterable for f in sets["Orders"].fields)
    assert all(f.filterable for f in sets["Lines"].fields)


def test_labels_are_one_line_bounded_and_without_invisible_characters():
    md = parse_metadata(
        _doc(
            '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
            '<Property Name="Id" Type="Edm.Guid" Nullable="false">'
            f'<Annotation Term="Common.Label"><String>{"x" * 5000}</String></Annotation>'
            "</Property>"
            '<Property Name="Note" Type="Edm.String">'
            '<Annotation Term="Common.Label" String="a&#x202E;b&#10;c&#x200B;"/></Property>'
            "</EntityType>" + _container('<EntitySet Name="Orders" EntityType="NS.OrderType"/>')
        ),
        "v4",
    )
    by_name = {f.name: f.label for f in md.entity_sets[0].fields}
    assert by_name["Id"] == "x" * 120
    assert by_name["Note"] == "ab c"


def test_character_data_is_kept_only_where_v4_needs_it_and_bounded():
    cap = odata_metadata._MAX_TEXT_CHARS
    root, _ = odata_metadata._parse_tree(
        f'<Edmx xmlns="{_EDMX}"><Documentation>free text</Documentation>'
        f"<String>{'y' * (cap + 500)}</String><PropertyPath>A&amp;B</PropertyPath>"
        f"<Bool>false<Nested/>tail</Bool></Edmx>".encode()
    )
    assert root.find("Documentation").text is None
    assert root.find("String").text == "y" * cap
    assert root.find("PropertyPath").text == "A&B"
    # An expression element with child elements is not a plain value.
    assert root.find("Bool").text is None
    assert all(e.tail is None for e in root.iter())


# ------------------------------------------------- skipped, not failed (and why)

_SECRET = "x/../y?token=abc"


def test_a_navigation_without_a_usable_binding_is_skipped():
    md = parse_metadata(
        _doc(
            '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
            '<Property Name="Id" Type="Edm.Guid" Nullable="false"/>'
            '<NavigationProperty Name="_Unbound" Type="Collection(NS.LineType)"/>'
            '<NavigationProperty Name="_Gone" Type="NS.LineType"/>'
            '<NavigationProperty Name="_Twice" Type="NS.LineType"/>'
            f'<NavigationProperty Name="{_SECRET}" Type="NS.LineType"/>'
            '<NavigationProperty Name="_Untyped"/>'
            '<NavigationProperty Name="_Path" Type="NS.LineType"/>'
            '<NavigationProperty Name="_Contained" Type="NS.LineType"/>'
            '<NavigationProperty Name="_Ok" Type="Collection(NS.LineType)"/>'
            "</EntityType>"
            + _LINE
            + _container(
                '<EntitySet Name="Orders" EntityType="NS.OrderType">'
                '<NavigationPropertyBinding Path="_Gone" Target="Nowhere"/>'
                '<NavigationPropertyBinding Path="_Twice" Target="Lines"/>'
                '<NavigationPropertyBinding Path="_Twice" Target="OtherLines"/>'
                '<NavigationPropertyBinding Path="_Untyped" Target="Lines"/>'
                '<NavigationPropertyBinding Path="_Path" Target="Self.C/Lines"/>'
                '<NavigationPropertyBinding Path="_Contained" Target="Lines/_Order"/>'
                '<NavigationPropertyBinding Path="_Ok" Target="Lines"/>'
                "</EntitySet>"
                '<EntitySet Name="Lines" EntityType="NS.LineType"/>'
                '<EntitySet Name="OtherLines" EntityType="NS.LineType"/>'
            )
        ),
        "v4",
    )
    orders = _sets(md)["Orders"]
    assert [(n.name, n.target, n.collection) for n in orders.navigations] == [
        ("_Path", "Lines", False),  # Target as a container-qualified path
        ("_Ok", "Lines", True),
    ]
    assert [s for s in md.skipped if s.entity_set == "Orders"] == [
        SkippedElement("navigation", "Orders", 1, "unresolved_target"),  # no binding
        SkippedElement("navigation", "Orders", 2, "unresolved_target"),  # no such set
        SkippedElement("navigation", "Orders", 3, "unresolved_target"),  # two targets
        SkippedElement("navigation", "Orders", 4, "invalid_name"),
        SkippedElement("navigation", "Orders", 5, "invalid_type"),  # multiplicity unknown
        SkippedElement("navigation", "Orders", 7, "unresolved_target"),  # a path, not a set
    ]
    # Lines' own navigation has no binding either.
    assert [s.reason for s in md.skipped if s.entity_set != "Orders"] == ["unresolved_target"] * 2
    assert "token" not in repr(md)
    names = {e.name for e in md.entity_sets}
    assert all(n.target in names for e in md.entity_sets for n in e.navigations)


def test_properties_and_entity_sets_are_skipped_like_in_v2():
    md = parse_metadata(
        _doc(
            '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
            '<Property Name="Id" Type="Edm.Guid" Nullable="false"/>'
            f'<Property Name="{_SECRET}" Type="Edm.String"/>'
            f'<Property Name="Long" Type="{"T" * 201}"/>'
            "</EntityType>"
            # A key through a complex property cannot be a KeyDef.
            '<EntityType Name="OddType"><Key><PropertyRef Name="Part/Id" Alias="PartId"/></Key>'
            '<Property Name="Part" Type="NS.PartType"/></EntityType>'
            + _container(
                '<EntitySet Name="Orders" EntityType="NS.OrderType"/>'
                f'<EntitySet Name="{_SECRET}" EntityType="NS.OrderType"/>'
                '<EntitySet Name="Orders" EntityType="NS.OrderType"/>'
                '<EntitySet Name="Odd" EntityType="NS.OddType"/>'
            )
        ),
        "v4",
    )
    assert [e.name for e in md.entity_sets] == ["Orders"]
    assert [f.name for f in md.entity_sets[0].fields] == ["Id"]
    assert md.skipped == (
        SkippedElement("property", "Orders", 2, "invalid_name"),
        SkippedElement("property", "Orders", 3, "invalid_type"),
        SkippedElement("entity_set", "", 2, "invalid_name"),
        SkippedElement("entity_set", "Orders", 3, "duplicate_name"),
        SkippedElement("entity_set", "Odd", 4, "unrepresentable_key"),
    )
    assert "token" not in repr(md)


def test_base_types_and_a_type_shared_by_two_sets():
    md = parse_metadata(
        _doc(
            '<EntityType Name="BaseType" Abstract="true"><Key><PropertyRef Name="Id"/></Key>'
            '<Property Name="Id" Type="Edm.Guid" Nullable="false"/></EntityType>'
            '<EntityType Name="OrderType" BaseType="Self.BaseType">'
            '<Property Name="Note" Type="Edm.String"/></EntityType>'
            + _container(
                '<EntitySet Name="Orders" EntityType="Self.OrderType"/>'
                '<EntitySet Name="ArchivedOrders" EntityType="NS.OrderType"/>'
            )
            + '<Annotations Target="NS.BaseType/Id">'
            '<Annotation Term="Common.Label" String="Identifier"/></Annotations>'
        ),
        "v4",
    )
    for entity_set in md.entity_sets:
        assert entity_set.entity_type == "NS.OrderType"  # the alias is resolved
        assert [(f.name, f.label) for f in entity_set.fields] == [
            ("Id", "Identifier"),
            ("Note", ""),
        ]
        assert entity_set.keys == (KeyDef(name="Id", type="Edm.Guid"),)
    assert md.skipped == ()


# ----------------------------------------------------------------- operations


def test_operations_that_cannot_be_called_as_modelled_are_skipped():
    md = parse_metadata(
        _doc(
            _ORDER + _LINE + '<Action Name="Close" IsBound="true">'  # 1: two sets have the type
            '<Parameter Name="_it" Type="NS.OrderType"/></Action>'
            '<Action Name="Renumber" IsBound="true">'  # 2: bound to a collection
            '<Parameter Name="_it" Type="Collection(NS.LineType)"/></Action>'
            '<Action Name="Orphan" IsBound="true">'  # 3: no entity set of that type
            '<Parameter Name="_it" Type="NS.NoType"/></Action>'
            '<Action Name="Empty" IsBound="true"/>'  # 4: no binding parameter
            '<Function Name="Internal">'  # 5: no import, so no URL
            '<ReturnType Type="Edm.Int32"/></Function>'
            f'<Action Name="{_SECRET}"/>'  # 6
            '<Action Name="Bad" IsBound="true">'  # 7
            '<Parameter Name="_it" Type="NS.LineType"/>'
            f'<Parameter Name="{_SECRET}" Type="Edm.String"/></Action>'
            '<Action Name="Split" IsBound="true">'  # 8: fine
            # The binding parameter's name is never used.
            f'<Parameter Name="{_SECRET}" Type="Self.LineType"/>'
            '<Parameter Name="At" Type="Edm.Int32" Nullable="false"/></Action>'
            '<Action Name="Split" IsBound="true">'  # 9: an overload; names are unique
            '<Parameter Name="_it" Type="NS.LineType"/></Action>'
            '<Function Name="Count"><ReturnType Type="Edm.Int32"/></Function>'  # 10: fine
            '<Function Name="Renamed"><ReturnType Type="Edm.Int32"/></Function>'  # 11: bad import
            + _container(
                _SETS + '<EntitySet Name="ArchivedOrders" EntityType="NS.OrderType"/>'
                '<FunctionImport Name="CountAll" Function="Self.Count"/>'
                f'<FunctionImport Name="{_SECRET}" Function="NS.Renamed"/>'
                '<ActionImport Name="Dangling" Action="NS.Nope"/>'
                # An import of the other kind does not make a function callable.
                '<ActionImport Name="Internal" Action="NS.Internal"/>'
            )
        ),
        "v4",
    )
    ops = _ops(md)
    assert list(ops) == ["Split", "CountAll"]
    assert (ops["Split"].bound_to, ops["Split"].qualified_name) == ("Lines", "NS.Split")
    assert ops["Split"].parameters == (ParamDef(name="At", type="Edm.Int32", required=True),)
    # An unbound operation is called through its import: that is its name.
    assert (ops["CountAll"].qualified_name, ops["CountAll"].bound_to) == ("NS.Count", None)
    assert [s for s in md.skipped if s.kind == "operation"] == [
        SkippedElement("operation", "", 1, "unsupported_binding"),
        SkippedElement("operation", "", 2, "unsupported_binding"),
        SkippedElement("operation", "", 3, "unsupported_binding"),
        SkippedElement("operation", "", 4, "unsupported_binding"),
        SkippedElement("operation", "", 5, "not_imported"),
        SkippedElement("operation", "", 6, "invalid_name"),
        SkippedElement("operation", "", 7, "invalid_parameter"),
        SkippedElement("operation", "", 9, "duplicate_name"),
        SkippedElement("operation", "", 11, "invalid_name"),
    ]
    assert {s.reason for s in md.skipped} <= set(SKIP_REASONS)
    assert "token" not in repr(md)
    for op in md.operations:
        OperationDef(
            name=op.name,
            qualified_name=op.qualified_name,
            kind=op.kind,
            http_method=op.http_method,
            bound_to=op.bound_to,
            parameters=list(op.parameters),
        )


def test_an_action_is_not_bound_to_a_skipped_entity_set():
    md = parse_metadata(
        _doc(
            '<EntityType Name="OddType"><Key><PropertyRef Name="Missing"/></Key>'
            '<Property Name="Id" Type="Edm.Guid"/></EntityType>'
            '<Action Name="Fix" IsBound="true"><Parameter Name="_it" Type="NS.OddType"/></Action>'
            + _container('<EntitySet Name="Odd" EntityType="NS.OddType"/>')
        ),
        "v4",
    )
    assert md.entity_sets == () and md.operations == ()
    assert md.skipped == (
        SkippedElement("entity_set", "Odd", 1, "unrepresentable_key"),
        SkippedElement("operation", "", 1, "unsupported_binding"),
    )


def test_a_namespace_that_is_no_edm_name_gives_no_qualified_name():
    xml = _doc(
        '<Function Name="Count"><ReturnType Type="Edm.Int32"/></Function>'
        + _container('<FunctionImport Name="Count" Function="my-ns.Count"/>')
    ).replace(b'Namespace="NS"', b'Namespace="my-ns"')
    md = parse_metadata(xml, "v4")
    assert md.operations == ()
    assert md.skipped == (SkippedElement("operation", "", 1, "invalid_name"),)


def test_which_parameters_are_required():
    md = parse_metadata(
        _doc(
            '<Action Name="Post">'
            '<Parameter Name="Must" Type="Edm.String" Nullable="false"/>'
            '<Parameter Name="May" Type="Edm.String"/></Action>'
            '<Function Name="Find">'
            '<Parameter Name="Must" Type="Edm.String"/>'  # nullable, but still part of the URL
            '<Parameter Name="May" Type="Edm.String">'
            '<Annotation Term="Core.OptionalParameter"/></Parameter>'
            '<ReturnType Type="Edm.String"/></Function>'
            + _container(
                '<ActionImport Name="Post" Action="NS.Post"/>'
                '<FunctionImport Name="Find" Function="NS.Find"/>'
            )
        ),
        "v4",
    )
    ops = _ops(md)
    assert {p.name: p.required for p in ops["Post"].parameters} == {"Must": True, "May": False}
    assert {p.name: p.required for p in ops["Find"].parameters} == {"Must": True, "May": False}
    assert (ops["Post"].kind, ops["Post"].http_method) == ("action", "POST")
    assert (ops["Find"].kind, ops["Find"].http_method) == ("function", "GET")


def test_operation_labels_from_the_import_the_operation_or_a_block():
    md = parse_metadata(
        _doc(
            _ORDER.replace('<NavigationProperty Name="_Lines" Type="Collection(NS.LineType)"/>', "")
            + '<Action Name="Close" IsBound="true"><Parameter Name="_it" Type="NS.OrderType"/>'
            "</Action>"
            '<Function Name="Count"><Annotation Term="Common.Label" String="From function"/>'
            '<ReturnType Type="Edm.Int32"/></Function>'
            '<Function Name="Sum"><Annotation Term="Common.Label" String="From function"/>'
            '<ReturnType Type="Edm.Int32"/></Function>'
            + _container(
                '<EntitySet Name="Orders" EntityType="NS.OrderType"/>'
                '<FunctionImport Name="Count" Function="NS.Count"/>'
                '<FunctionImport Name="Sum" Function="NS.Sum">'
                '<Annotation Term="Common.Label" String="From import"/></FunctionImport>'
            )
            + '<Annotations Target="Self.Close(Self.OrderType)">'
            '<Annotation Term="Common.Label" String="Close order"/></Annotations>'
        ),
        "v4",
    )
    assert {o.name: o.label for o in md.operations} == {
        "Close": "Close order",
        "Count": "From function",
        "Sum": "From import",
    }


def test_several_schemas():
    md = parse_metadata(
        _doc(
            _ORDER + _LINE,
            more=f'<Schema Namespace="NS.Service" xmlns="{_EDM}">{_container()}'
            '<Annotations Target="NS.Service.C/Lines">'
            '<Annotation Term="Capabilities.DeleteRestrictions"><Record>'
            '<PropertyValue Property="Deletable" Bool="false"/></Record></Annotation>'
            "</Annotations></Schema>",
        ),
        "v4",
    )
    sets = _sets(md)
    assert sets["Orders"].navigations[0].target == "Lines"
    assert (sets["Lines"].deletable, sets["Orders"].deletable) == (False, True)
    assert md.skipped == ()


def test_real_metadata_if_present():
    files = sorted((ROOT / "docs/evidence/odata").glob("*v4*.xml"))
    if not files:
        pytest.skip("no local real V4 $metadata (the pilot task stores one)")
    for f in files:
        assert parse_metadata(f.read_bytes(), "v4").entity_sets
