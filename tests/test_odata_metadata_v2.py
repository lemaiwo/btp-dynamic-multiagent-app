"""The OData V2 ``$metadata`` parser (EDMX 1.0 -> the common parsed model).

What the admin's "read metadata" preview is built from: entity sets with
their keys, labels and SAP capability annotations, navigations resolved to
entity *sets*, and function imports. Also that the parser refuses anything
that is not plain EDMX -- a DTD, an entity, an oversized body, a login page.
"""

from __future__ import annotations

import os
import re
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
    MAX_METADATA_BYTES,
    MetadataError,
    ParsedMetadata,
    SkippedElement,
    parse_metadata,
)
from agents.odata.models import KeyDef, OperationDef, ParamDef  # noqa: E402

XML = (ROOT / "tests/fixtures/odata/v2_purchasereq_reduced.xml").read_bytes()

_EDMX = "http://schemas.microsoft.com/ado/2007/06/edmx"
_M = "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
_SAP = "http://www.sap.com/Protocols/SAPData"


def _doc(schema_body: str, *, edm: str = "http://schemas.microsoft.com/ado/2008/09/edm") -> bytes:
    """A minimal EDMX 1.0 document around one schema ``NS``."""
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<edmx:Edmx Version="1.0" xmlns:edmx="{_EDMX}" xmlns:m="{_M}" xmlns:sap="{_SAP}">'
        f'<edmx:DataServices m:DataServiceVersion="2.0">'
        f'<Schema Namespace="NS" xmlns="{edm}">{schema_body}</Schema>'
        f"</edmx:DataServices></edmx:Edmx>"
    ).encode()


_ORDER = (
    '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
    '<Property Name="Id" Type="Edm.Guid" Nullable="false"/>'
    '<Property Name="Note"/>'
    "</EntityType>"
)


def _sets(md: ParsedMetadata) -> dict:
    return {e.name: e for e in md.entity_sets}


# ---------------------------------------------------------------- the fixture


def test_entity_sets_keys_labels_and_capabilities():
    md = parse_metadata(XML, "v2")
    assert md.version == "v2"
    assert md.skipped == ()
    assert [e.name for e in md.entity_sets] == [
        "A_PurchaseRequisitionHeader",
        "A_PurchaseRequisitionItem",
    ]
    item = _sets(md)["A_PurchaseRequisitionItem"]
    assert item.entity_type == "PURCHASEREQ_SRV.A_PurchaseRequisitionItemType"
    assert item.label == "Purchase requisition item"
    assert [k.name for k in item.keys] == ["PurchaseRequisition", "PurchaseRequisitionItem"]
    assert all(isinstance(k, KeyDef) and k.type == "Edm.String" for k in item.keys)
    f = {x.name: x for x in item.fields}
    assert f["PurReqnReleaseStatus"].label == "Release status"
    assert f["ItemNetAmount"].type == "Edm.Decimal"
    assert f["CreatedByUser"].filterable is False
    assert f["Material"].filterable is True
    assert f["PurReqnReleaseStatus"].creatable is False and f["Material"].creatable is True
    assert f["PurchaseRequisition"].updatable is False and f["Material"].updatable is True
    assert f["PurchaseRequisition"].nullable is False and f["Material"].nullable is True
    assert item.creatable is False and item.updatable is True and item.deletable is False
    header = _sets(md)["A_PurchaseRequisitionHeader"]
    # Only sap:deletable="false" is written on the header set.
    assert (header.creatable, header.updatable, header.deletable) == (True, True, False)


def test_navigation_resolves_to_the_target_entity_set_and_multiplicity():
    sets = _sets(parse_metadata(XML, "v2"))
    (to_items,) = sets["A_PurchaseRequisitionHeader"].navigations
    assert (to_items.name, to_items.target, to_items.collection) == (
        "to_PurchaseReqnItem",
        "A_PurchaseRequisitionItem",  # the entity SET, not the type
        True,
    )
    (to_header,) = sets["A_PurchaseRequisitionItem"].navigations
    assert (to_header.name, to_header.target, to_header.collection) == (
        "to_PurchaseReqn",
        "A_PurchaseRequisitionHeader",
        False,
    )
    # A navigation is not a field.
    assert "to_PurchaseReqn" not in {f.name for f in sets["A_PurchaseRequisitionItem"].fields}


def test_function_import_method_parameters_and_binding():
    md = parse_metadata(XML, "v2")
    assert len(md.operations) == 1
    op = md.operations[0]
    assert (op.name, op.kind, op.http_method) == ("ReleaseItem", "function_import", "POST")
    assert [p.name for p in op.parameters] == [
        "PurchaseRequisition",
        "PurchaseRequisitionItem",
        "ReleaseCode",
    ]
    assert all(isinstance(p, ParamDef) for p in op.parameters)
    assert [p.required for p in op.parameters] == [True, True, False]
    assert op.bound_to == "A_PurchaseRequisitionItem"  # from sap:action-for, else None
    assert op.label == "Release item"
    assert op.qualified_name == ""  # a V2 function import is called by its plain name


# ------------------------------------------------------------- inline variants


def test_function_import_without_http_method_defaults_to_get():
    md = parse_metadata(
        _doc(
            _ORDER + '<EntityContainer Name="C">'
            '<EntitySet Name="Orders" EntityType="NS.OrderType"/>'
            '<FunctionImport Name="CountOpen" ReturnType="Edm.Int32"/>'
            '<FunctionImport Name="Touch" m:HttpMethod="post">'
            '<Parameter Name="Id" Type="Edm.Guid" Mode="In" Nullable="false"/>'
            '<Parameter Name="Result" Type="Edm.String" Mode="Out"/>'
            "</FunctionImport></EntityContainer>"
        ),
        "v2",
    )
    count, touch = md.operations
    assert (count.name, count.http_method, count.bound_to, count.parameters) == (
        "CountOpen",
        "GET",
        None,
        (),
    )
    assert count.label == ""
    assert touch.http_method == "POST"
    # An Out parameter is not something a caller sends.
    assert [(p.name, p.type) for p in touch.parameters] == [("Id", "Edm.Guid")]


def test_missing_sap_annotations_default_to_true():
    md = parse_metadata(
        _doc(
            _ORDER
            + '<EntityContainer Name="C"><EntitySet Name="Orders" EntityType="NS.OrderType"/>'
            "</EntityContainer>"
        ),
        "v2",
    )
    (orders,) = md.entity_sets
    assert (orders.creatable, orders.updatable, orders.deletable) == (True, True, True)
    assert orders.label == "" and orders.navigations == ()
    assert [(k.name, k.type) for k in orders.keys] == [("Id", "Edm.Guid")]
    note = {f.name: f for f in orders.fields}["Note"]
    assert (note.filterable, note.creatable, note.updatable, note.nullable) == (
        True,
        True,
        True,
        True,
    )
    assert note.type == "Edm.String" and note.label == ""  # Type absent -> Edm.String
    assert md.operations == ()


@pytest.mark.parametrize(
    "edm",
    [
        "http://schemas.microsoft.com/ado/2006/04/edm",
        "http://schemas.microsoft.com/ado/2007/05/edm",
        "http://schemas.microsoft.com/ado/2009/11/edm",
    ],
)
def test_other_edm_schema_namespaces_parse_the_same(edm):
    md = parse_metadata(
        _doc(
            _ORDER + '<EntityContainer Name="C">'
            '<EntitySet Name="Orders" EntityType="NS.OrderType"/></EntityContainer>',
            edm=edm,
        ),
        "v2",
    )
    assert [e.name for e in md.entity_sets] == ["Orders"]


def test_several_schemas_alias_and_base_type():
    xml = (
        f'<edmx:Edmx Version="1.0" xmlns:edmx="{_EDMX}" xmlns:sap="{_SAP}"><edmx:DataServices>'
        '<Schema Namespace="Model" Alias="M" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">'
        '<EntityType Name="Base" Abstract="true"><Key><PropertyRef Name="Id"/></Key>'
        '<Property Name="Id" Type="Edm.Int32" Nullable="false"/></EntityType>'
        '<EntityType Name="Order" BaseType="M.Base"><Property Name="Note" Type="Edm.String"/>'
        '<NavigationProperty Name="Lines" Relationship="Model.Order_Line" FromRole="O" ToRole="L"/>'
        "</EntityType>"
        '<EntityType Name="Line"><Key><PropertyRef Name="Pos"/></Key>'
        '<Property Name="Pos" Type="Edm.Int32" Nullable="false"/></EntityType>'
        '<Association Name="Order_Line"><End Type="Model.Order" Multiplicity="1" Role="O"/>'
        '<End Type="Model.Line" Multiplicity="*" Role="L"/></Association>'
        "</Schema>"
        '<Schema Namespace="Container" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">'
        '<EntityContainer Name="C"><EntitySet Name="Orders" EntityType="M.Order"/>'
        '<EntitySet Name="Lines" EntityType="Model.Line"/>'
        '<EntitySet Name="Orphans" EntityType="Model.Missing"/>'
        '<AssociationSet Name="OL" Association="M.Order_Line">'
        '<End EntitySet="Orders" Role="O"/><End EntitySet="Lines" Role="L"/>'
        "</AssociationSet></EntityContainer>"
        "</Schema></edmx:DataServices></edmx:Edmx>"
    ).encode()
    sets = _sets(parse_metadata(xml, "v2"))
    orders = sets["Orders"]
    assert [k.name for k in orders.keys] == ["Id"]  # inherited from the base type
    assert [f.name for f in orders.fields] == ["Id", "Note"]
    # The association set is found through the schema's alias as well.
    assert [(n.name, n.target, n.collection) for n in orders.navigations] == [
        ("Lines", "Lines", True)
    ]
    # An entity set whose type is not in the document is listed, empty.
    assert sets["Orphans"].fields == () and sets["Orphans"].keys == ()


_NAV_TYPES = (
    '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
    '<Property Name="Id" Type="Edm.Guid" Nullable="false"/>'
    '<NavigationProperty Name="to_Note" Relationship="NS.A" FromRole="O" ToRole="N"/>'
    '<NavigationProperty Name="to_Gone" Relationship="NS.Nope" FromRole="O" ToRole="N"/>'
    '<NavigationProperty Name="to_NoEnd" Relationship="NS.A" FromRole="O" ToRole="X"/>'
    "</EntityType>"
    '<EntityType Name="NoteType"><Key><PropertyRef Name="Id"/></Key>'
    '<Property Name="Id" Type="Edm.Guid" Nullable="false"/></EntityType>'
    '<Association Name="A"><End Type="NS.OrderType" Multiplicity="1" Role="O"/>'
    '<End Type="NS.NoteType" Multiplicity="0..1" Role="N"/></Association>'
)


def test_an_unresolved_navigation_is_skipped_never_emitted_with_an_empty_target():
    """No association set covering this entity set means no known target.

    The one entity set of the target *type* is not taken as a stand-in: the
    association set is what says which set the other end is.
    """
    md = parse_metadata(
        _doc(
            _NAV_TYPES + '<EntityContainer Name="C">'
            '<EntitySet Name="Orders" EntityType="NS.OrderType"/>'
            '<EntitySet Name="Notes" EntityType="NS.NoteType"/>'
            "</EntityContainer>"
        ),
        "v2",
    )
    assert _sets(md)["Orders"].navigations == ()
    assert md.skipped == (
        SkippedElement("navigation", "Orders", 1, "unresolved_target"),  # no association set
        SkippedElement("navigation", "Orders", 2, "unresolved_target"),  # no such association
        SkippedElement("navigation", "Orders", 3, "unresolved_target"),  # no such ToRole end
    )


def test_the_association_set_decides_between_entity_sets_of_one_type():
    md = parse_metadata(
        _doc(
            _NAV_TYPES + '<EntityContainer Name="C">'
            '<EntitySet Name="Orders" EntityType="NS.OrderType"/>'
            '<EntitySet Name="ArchivedOrders" EntityType="NS.OrderType"/>'
            '<EntitySet Name="Notes" EntityType="NS.NoteType"/>'
            '<EntitySet Name="ArchivedNotes" EntityType="NS.NoteType"/>'
            '<AssociationSet Name="S1" Association="NS.A">'
            '<End EntitySet="Orders" Role="O"/><End EntitySet="Notes" Role="N"/></AssociationSet>'
            '<AssociationSet Name="S2" Association="NS.A">'
            '<End EntitySet="ArchivedOrders" Role="O"/><End EntitySet="ArchivedNotes" Role="N"/>'
            "</AssociationSet></EntityContainer>"
        ),
        "v2",
    )
    sets = _sets(md)
    assert [(n.name, n.target, n.collection) for n in sets["Orders"].navigations] == [
        ("to_Note", "Notes", False)
    ]
    assert [(n.name, n.target) for n in sets["ArchivedOrders"].navigations] == [
        ("to_Note", "ArchivedNotes")
    ]
    assert all(n.target for e in md.entity_sets for n in e.navigations)


def test_action_for_binds_only_when_one_entity_set_has_the_type():
    md = parse_metadata(
        _doc(
            _ORDER + '<EntityType Name="LineType"><Key><PropertyRef Name="Pos"/></Key>'
            '<Property Name="Pos" Type="Edm.Int32" Nullable="false"/></EntityType>'
            '<EntityContainer Name="C">'
            '<EntitySet Name="Orders" EntityType="NS.OrderType"/>'
            '<EntitySet Name="ArchivedOrders" EntityType="NS.OrderType"/>'
            '<EntitySet Name="Lines" EntityType="NS.LineType"/>'
            '<FunctionImport Name="Close" m:HttpMethod="POST" sap:action-for="NS.OrderType"/>'
            '<FunctionImport Name="Pinned" m:HttpMethod="POST" EntitySet="ArchivedOrders" '
            'sap:action-for="NS.OrderType"/>'
            '<FunctionImport Name="Lost" m:HttpMethod="POST" sap:action-for="NS.Nope"/>'
            '<FunctionImport Name="Split" m:HttpMethod="POST" EntitySet="Orders" '
            'sap:action-for="NS.LineType"/>'
            '<FunctionImport Name="Free" m:HttpMethod="POST" EntitySet="Orders"/>'
            "</EntityContainer>"
        ),
        "v2",
    )
    assert {o.name: o.bound_to for o in md.operations} == {
        "Close": None,  # two entity sets share the type: not guessed
        "Pinned": None,  # EntitySet is the import's RETURN set, it settles nothing
        "Lost": None,
        "Split": "Lines",  # the one set of the action-for type, not the return set
        "Free": None,
    }
    assert md.skipped == ()


# ------------------------------------------------- skipped, not failed (and why)

_SECRET = "x/../y?token=abc"


def _container(body: str) -> str:
    return f'<EntityContainer Name="C">{body}</EntityContainer>'


def test_a_property_or_navigation_with_a_bad_name_is_skipped_and_recorded():
    md = parse_metadata(
        _doc(
            '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
            '<Property Name="Id" Type="Edm.Guid"/>'
            f'<Property Name="{_SECRET}" Type="Edm.String"/>'
            '<Property Name="Note" Type="Edm.String"/>'
            '<Property Name="Note&#10;" Type="Edm.String"/>'  # fullmatch: no trailing newline
            f'<Property Name="Blob" Type="{"T" * 201}"/>'
            f'<NavigationProperty Name="{_SECRET}" Relationship="NS.A" FromRole="O" ToRole="N"/>'
            "</EntityType>" + _container('<EntitySet Name="Orders" EntityType="NS.OrderType"/>')
        ),
        "v2",
    )
    (orders,) = md.entity_sets
    assert [f.name for f in orders.fields] == ["Id", "Note"]
    assert md.skipped == (
        SkippedElement("property", "Orders", 2, "invalid_name"),
        SkippedElement("property", "Orders", 4, "invalid_name"),
        SkippedElement("property", "Orders", 5, "invalid_type"),
        SkippedElement("navigation", "Orders", 1, "invalid_name"),
    )
    # Never the offending name, nor any other text of the document.
    assert "token" not in repr(md) and "TTTT" not in repr(md.skipped)


def test_an_entity_set_that_cannot_be_represented_is_skipped_as_a_whole():
    md = parse_metadata(
        _doc(
            _ORDER
            # the key property's name is not an identifier
            + '<EntityType Name="BadKeyType"><Key><PropertyRef Name="a b"/></Key>'
            '<Property Name="a b" Type="Edm.String"/><Property Name="Ok"/></EntityType>'
            # the key names a property that does not exist
            '<EntityType Name="GhostKeyType"><Key><PropertyRef Name="Id"/>'
            '<PropertyRef Name="Ghost"/></Key><Property Name="Id" Type="Edm.Guid"/></EntityType>'
            + _container(
                f'<EntitySet Name="{_SECRET}" EntityType="NS.OrderType"/>'
                '<EntitySet Name="BadKeys" EntityType="NS.BadKeyType"/>'
                '<EntitySet Name="Orders" EntityType="NS.OrderType"/>'
                '<EntitySet Name="GhostKeys" EntityType="NS.GhostKeyType"/>'
                f'<EntitySet Name="LongType" EntityType="NS.{"T" * 200}"/>'
                '<EntitySet Name="Orders" EntityType="NS.GhostKeyType"/>'
                '<FunctionImport Name="Fix" m:HttpMethod="POST" sap:action-for="NS.BadKeyType"/>'
            )
        ),
        "v2",
    )
    assert [e.name for e in md.entity_sets] == ["Orders"]
    assert md.entity_sets[0].entity_type == "NS.OrderType"
    assert md.skipped == (
        SkippedElement("entity_set", "", 1, "invalid_name"),
        # One entry for the set; its own bad property is not listed separately.
        SkippedElement("entity_set", "BadKeys", 2, "unrepresentable_key"),
        SkippedElement("entity_set", "GhostKeys", 4, "unrepresentable_key"),
        SkippedElement("entity_set", "LongType", 5, "invalid_type"),
        SkippedElement("entity_set", "Orders", 6, "duplicate_name"),
    )
    # A skipped set is nothing an operation can be bound to.
    assert md.operations[0].bound_to is None
    assert "token" not in repr(md)


def test_a_navigation_to_a_skipped_entity_set_is_skipped_too():
    md = parse_metadata(
        _doc(
            '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
            '<Property Name="Id" Type="Edm.Guid"/>'
            '<NavigationProperty Name="to_Bad" Relationship="NS.A" FromRole="O" ToRole="B"/>'
            "</EntityType>"
            '<EntityType Name="BadType"><Key><PropertyRef Name="Ghost"/></Key>'
            '<Property Name="Id" Type="Edm.Guid"/></EntityType>'
            '<Association Name="A"><End Type="NS.OrderType" Multiplicity="1" Role="O"/>'
            '<End Type="NS.BadType" Multiplicity="*" Role="B"/></Association>'
            + _container(
                '<EntitySet Name="Orders" EntityType="NS.OrderType"/>'
                '<EntitySet Name="Bads" EntityType="NS.BadType"/>'
                '<AssociationSet Name="S" Association="NS.A">'
                '<End EntitySet="Orders" Role="O"/><End EntitySet="Bads" Role="B"/>'
                "</AssociationSet>"
            )
        ),
        "v2",
    )
    assert [e.name for e in md.entity_sets] == ["Orders"]
    assert md.entity_sets[0].navigations == ()
    assert md.skipped == (
        SkippedElement("entity_set", "Bads", 2, "unrepresentable_key"),
        SkippedElement("navigation", "Orders", 1, "unresolved_target"),
    )


@pytest.mark.parametrize("method", ["PUT", "DELETE", "MERGE", "patch", "GET POST", "&lt;x&gt;"])
def test_a_function_import_with_another_http_method_is_skipped(method):
    """Everything the parser emits fits ``OperationDef`` (GET or POST only)."""
    md = parse_metadata(
        _doc(
            _ORDER
            + _container(
                '<EntitySet Name="Orders" EntityType="NS.OrderType"/>'
                '<FunctionImport Name="Read"/>'
                f'<FunctionImport Name="Odd" m:HttpMethod="{method}"/>'
                '<FunctionImport Name="Write" m:HttpMethod=" post "/>'
            )
        ),
        "v2",
    )
    assert [(o.name, o.http_method) for o in md.operations] == [("Read", "GET"), ("Write", "POST")]
    assert md.skipped == (SkippedElement("operation", "", 2, "unsupported_http_method"),)
    for op in md.operations:  # the proof: the definition model takes it as it is
        OperationDef(
            name=op.name,
            kind=op.kind,
            http_method=op.http_method,
            bound_to=op.bound_to,
            parameters=list(op.parameters),
            title=op.label,
        )


def test_a_function_import_with_a_bad_name_or_parameter_is_skipped():
    md = parse_metadata(
        _doc(
            _ORDER
            + _container(
                '<EntitySet Name="Orders" EntityType="NS.OrderType"/>'
                f'<FunctionImport Name="{_SECRET}" m:HttpMethod="POST"/>'
                '<FunctionImport Name="BadParamName" m:HttpMethod="POST">'
                f'<Parameter Name="{_SECRET}" Type="Edm.String" Mode="In"/></FunctionImport>'
                '<FunctionImport Name="BadParamType" m:HttpMethod="POST">'
                f'<Parameter Name="P" Type="{"T" * 201}" Mode="In"/></FunctionImport>'
                '<FunctionImport Name="Fine" m:HttpMethod="POST">'
                # an Out parameter is not sent, so its name does not matter
                f'<Parameter Name="{_SECRET}" Type="Edm.String" Mode="Out"/></FunctionImport>'
                '<FunctionImport Name="Fine" m:HttpMethod="GET"/>'
            )
        ),
        "v2",
    )
    assert [(o.name, o.http_method, o.parameters) for o in md.operations] == [("Fine", "POST", ())]
    assert md.skipped == (
        SkippedElement("operation", "", 1, "invalid_name"),
        SkippedElement("operation", "", 2, "invalid_parameter"),
        SkippedElement("operation", "", 3, "invalid_parameter"),
        SkippedElement("operation", "", 5, "duplicate_name"),
    )
    assert "token" not in repr(md)


def test_labels_are_one_line_and_bounded():
    md = parse_metadata(
        _doc(
            '<EntityType Name="OrderType"><Key><PropertyRef Name="Id"/></Key>'
            f'<Property Name="Id" Type="Edm.Guid" sap:label="  A&#10;label {"x" * 300}"/>'
            "</EntityType>"
            '<EntityContainer Name="C"><EntitySet Name="Orders" EntityType="NS.OrderType"/>'
            "</EntityContainer>"
        ),
        "v2",
    )
    label = md.entity_sets[0].fields[0].label
    assert label.startswith("A label x") and len(label) == 120 and "\n" not in label


def test_labels_lose_format_and_control_characters():
    """A bidi override in a label would reorder the admin's view of the line."""
    md = parse_metadata(
        _doc(
            '<EntityType Name="OrderType" sap:label="Or&#x202E;der&#x200B;s&#xFEFF;">'
            '<Key><PropertyRef Name="Id"/></Key>'
            '<Property Name="Id" Type="Edm.Guid" sap:label="&#x2066;I&#x2069;d&#x200F;&#9;x"/>'
            "</EntityType>" + _container('<EntitySet Name="Orders" EntityType="NS.OrderType"/>')
        ),
        "v2",
    )
    assert md.entity_sets[0].label == "Orders"
    assert md.entity_sets[0].fields[0].label == "Id x"


# -------------------------------------------------------------------- refusals


def test_doctype_and_entities_are_refused():
    with pytest.raises(MetadataError):
        parse_metadata(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>', "v2")
    with pytest.raises(MetadataError):
        parse_metadata(
            b'<?xml version="1.0"?><!DOCTYPE x SYSTEM "file:///etc/passwd"><x/>',
            "v2",
        )
    # An undeclared entity reference has nothing to expand to.
    with pytest.raises(MetadataError):
        parse_metadata(XML.replace(b"Release status", b"&xxe;"), "v2")


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be", "cp037", "cp500"])
def test_a_dtd_in_an_encoding_a_byte_scan_cannot_read_is_refused(codec):
    """Whatever the encoding, a document with a DTD does not come back parsed.

    Which layer refuses differs: UTF-16 with a BOM is decoded by expat and
    stopped by the DTD handlers; UTF-16 without a BOM and EBCDIC (cp037,
    cp500) are not decodable by expat at all (it cannot read the XML
    declaration), so they end as a parse error. The handler layer on its own
    is proven by ``test_the_dtd_handlers_refuse_without_the_byte_scan``.
    """
    declared = "utf-16" if codec.startswith("utf-16") else codec
    text = (
        f'<?xml version="1.0" encoding="{declared}"?>'
        '<!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]><x>&b;</x>'
    )
    payload = text.encode(codec)
    assert b"<!DOCTYPE" not in payload  # the ASCII scan sees nothing
    with pytest.raises(MetadataError):
        parse_metadata(payload, "v2")


@pytest.mark.parametrize(
    "payload",
    [
        b'<?xml version="1.0"?><!DOCTYPE x><x/>',
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>',
        b'<?xml version="1.0"?><!DOCTYPE x SYSTEM "http://s4.internal:44300/x.dtd"><x/>',
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>',
        '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>'.encode(
            "utf-16"
        ),
        b'<?xml version="1.0" encoding="iso-8859-1"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>',
    ],
)
def test_the_dtd_handlers_refuse_without_the_byte_scan(monkeypatch, payload):
    """The expat handlers are a layer of their own, not a copy of the scan."""
    monkeypatch.setattr(odata_metadata, "_DTD_RE", re.compile(rb"(?!x)x"))  # never matches
    calls: list[int] = []
    real = odata_metadata._refuse_dtd

    def spy(*args):
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(odata_metadata, "_refuse_dtd", spy)
    with pytest.raises(MetadataError, match="DTD"):
        parse_metadata(payload, "v2")
    assert calls == [1]  # refused at the first declaration expat reported


def test_no_dtd_handler_is_reached_for_plain_edmx(monkeypatch):
    """The fixture is parsed by the same guarded parser the refusals use."""
    seen: list[str] = []
    real = odata_metadata._refuse_dtd

    def spy(*args):
        seen.append("dtd")
        return real(*args)

    monkeypatch.setattr(odata_metadata, "_refuse_dtd", spy)
    assert parse_metadata(XML, "v2").entity_sets
    assert seen == []


def test_oversized_and_non_edmx_input_is_refused():
    with pytest.raises(MetadataError, match="larger than"):
        parse_metadata(b" " * (MAX_METADATA_BYTES + 1), "v2")
    html = b"<!-- sso --><html><head><title>Logon</title></head><body><form></form></body></html>"
    with pytest.raises(MetadataError, match="not an EDMX document"):
        parse_metadata(html, "v2")
    # What a login page usually is: not even well-formed XML.
    with pytest.raises(MetadataError, match="not an EDMX document"):
        parse_metadata(b"<html><body><br><input name=user></body></html>", "v2")
    with pytest.raises(MetadataError, match="not an EDMX document"):
        parse_metadata(b"", "v2")
    with pytest.raises(MetadataError, match="not an EDMX document"):
        parse_metadata(b'{"error": {"code": "401"}}', "v2")
    # EDMX without any schema is a shell, not metadata.
    with pytest.raises(MetadataError, match="not an EDMX document"):
        parse_metadata(f'<edmx:Edmx Version="1.0" xmlns:edmx="{_EDMX}"/>'.encode(), "v2")
    with pytest.raises(MetadataError):
        parse_metadata("<x/>", "v2")  # type: ignore[arg-type]  # text, not bytes


def test_an_error_never_quotes_the_document():
    secret = b"<html><body>session=SECRETCOOKIE <br></body></html>"
    with pytest.raises(MetadataError) as exc:
        parse_metadata(secret, "v2")
    assert "SECRETCOOKIE" not in str(exc.value)
    assert exc.value.__cause__ is None  # no expat error (with its context) chained


def test_absurd_nesting_is_refused():
    deep = b"<a>" * 5000 + b"</a>" * 5000
    with pytest.raises(MetadataError):
        parse_metadata(deep, "v2")


def test_v4_is_not_available_yet_and_a_v4_document_is_not_v2():
    with pytest.raises(MetadataError, match="V4 parsing is not available yet"):
        parse_metadata(XML, "v4")
    v4 = (
        b'<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">'
        b'<edmx:DataServices><Schema Namespace="NS" xmlns="http://docs.oasis-open.org/odata/ns/edm"/>'
        b"</edmx:DataServices></edmx:Edmx>"
    )
    with pytest.raises(MetadataError, match="V4"):
        parse_metadata(v4, "v2")
    with pytest.raises(MetadataError):
        parse_metadata(XML, "v3")  # type: ignore[arg-type]


def test_real_metadata_if_present():
    files = sorted((ROOT / "docs/evidence/odata").glob("*v2*.xml"))
    if not files:
        pytest.skip("no local real $metadata (the pilot task L4 stores one)")
    for f in files:
        assert parse_metadata(f.read_bytes(), "v2").entity_sets
