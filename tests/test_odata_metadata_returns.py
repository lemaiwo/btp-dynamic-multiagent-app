"""What an operation returns, as ``parse_metadata`` reads it (task M1).

The return entity set is what lets a call's result be cut to the catalogue's
field allowlist, so it is emitted only when the document ties the operation
to ONE entity set of the result: named by the import and of the declared
type, or the only set of that type. Anything else is absent, never guessed,
and the operation itself is still listed.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

from agents.odata.metadata import ParsedReturn, parse_metadata  # noqa: E402

_TYPES = (
    '<EntityType Name="Item"><Key><PropertyRef Name="Id"/></Key>'
    '<Property Name="Id" Type="Edm.String" Nullable="false"/></EntityType>'
    '<EntityType Name="Supplier"><Key><PropertyRef Name="Id"/></Key>'
    '<Property Name="Id" Type="Edm.String" Nullable="false"/></EntityType>'
    '<EntityType Name="Shared"><Key><PropertyRef Name="Id"/></Key>'
    '<Property Name="Id" Type="Edm.String" Nullable="false"/></EntityType>'
    # Its key names no property: the set of this type is skipped.
    '<EntityType Name="Broken"><Key><PropertyRef Name="Nope"/></Key>'
    '<Property Name="Id" Type="Edm.String" Nullable="false"/></EntityType>'
)
_SETS = (
    '<EntitySet Name="Items" EntityType="NS.Item"/>'
    '<EntitySet Name="Suppliers" EntityType="NS.Supplier"/>'
    '<EntitySet Name="SharedA" EntityType="NS.Shared"/>'
    '<EntitySet Name="SharedB" EntityType="NS.Shared"/>'
    '<EntitySet Name="BrokenSet" EntityType="NS.Broken"/>'
)


def v2(imports: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" '
        'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">'
        '<edmx:DataServices m:DataServiceVersion="2.0">'
        '<Schema Namespace="NS" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">'
        f'{_TYPES}<EntityContainer Name="C" m:IsDefaultEntityContainer="true">{_SETS}{imports}'
        "</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
    ).encode()


def v4(operations: str, imports: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">'
        '<edmx:DataServices><Schema Namespace="NS" Alias="Self" '
        'xmlns="http://docs.oasis-open.org/odata/ns/edm">'
        f'{_TYPES}{operations}<EntityContainer Name="C">{_SETS}{imports}</EntityContainer>'
        "</Schema></edmx:DataServices></edmx:Edmx>"
    ).encode()


V2_CASES = {
    # name -> (attributes, expected)
    "GetItem": ('ReturnType="NS.Item" EntitySet="Items"', ParsedReturn("Items", False, "")),
    "ListItems": (
        'ReturnType="Collection(NS.Item)" EntitySet="Items"', ParsedReturn("Items", True, ""),
    ),
    # No EntitySet attribute, and exactly one kept set has the type.
    "OnlySet": ('ReturnType="NS.Supplier"', ParsedReturn("Suppliers", False, "")),
    # Two sets share the type: never guessed from the type alone.
    "SharedUnsaid": ('ReturnType="Collection(NS.Shared)"', ParsedReturn(None, True, "")),
    "SharedNamed": (
        'ReturnType="Collection(NS.Shared)" EntitySet="SharedB"',
        ParsedReturn("SharedB", True, ""),
    ),
    # The set named is not of the declared type, or is not in the result.
    "Mismatch": ('ReturnType="NS.Item" EntitySet="Suppliers"', ParsedReturn(None, False, "")),
    "UnknownSet": ('ReturnType="NS.Item" EntitySet="Nope"', ParsedReturn(None, False, "")),
    "SkippedSet": ('ReturnType="NS.Broken" EntitySet="BrokenSet"', ParsedReturn(None, False, "")),
    "SkippedOnly": ('ReturnType="NS.Broken"', ParsedReturn(None, False, "")),
    "Count": ('ReturnType="Edm.Int32"', ParsedReturn(None, False, "Edm.Int32")),
    "Texts": ('ReturnType="Collection(Edm.String)"', ParsedReturn(None, True, "Edm.String")),
    "Complex": ('ReturnType="NS.Address"', ParsedReturn(None, False, "")),
    "Nothing": ("", None),
    "Half": ('ReturnType="Collection(NS.Item" EntitySet="Items"', None),
    "Empty": ('ReturnType="Collection()" EntitySet="Items"', None),
    "BadPrimitive": ('ReturnType="Edm.Str ing"', None),
}


def test_v2_return_type_and_entity_set_of_a_function_import():
    imports = "".join(
        f'<FunctionImport Name="{name}" m:HttpMethod="GET" {attributes}/>'
        for name, (attributes, _) in V2_CASES.items()
    )
    parsed = parse_metadata(v2(imports), "v2")
    # Unresolved is absent, not skipped: every import is still an operation.
    assert [o.name for o in parsed.operations] == list(V2_CASES)
    assert {o.name: o.returns for o in parsed.operations} == {
        name: expected for name, (_, expected) in V2_CASES.items()
    }
    assert [s for s in parsed.skipped if s.kind == "operation"] == []
    # Reading it enables nothing and binds nothing.
    assert all(o.bound_to is None for o in parsed.operations)


def test_v2_the_only_set_of_a_type_is_not_trusted_when_sets_were_left_unread(monkeypatch):
    from agents.odata import metadata

    monkeypatch.setattr(metadata, "MAX_PARSED_ENTITY_SETS", 2)
    imports = (
        '<FunctionImport Name="OnlySet" ReturnType="NS.Supplier"/>'
        '<FunctionImport Name="Named" ReturnType="NS.Item" EntitySet="Items"/>'
    )
    parsed = parse_metadata(v2(imports), "v2")
    assert parsed.truncated is True and [e.name for e in parsed.entity_sets] == [
        "Items", "Suppliers",
    ]
    by_name = {o.name: o.returns for o in parsed.operations}
    # A set past the cap could have the type too.
    assert by_name["OnlySet"] == ParsedReturn(None, False, "")
    assert by_name["Named"] == ParsedReturn("Items", False, "")


def test_v4_return_type_of_actions_and_functions_and_the_imports_entity_set():
    operations = (
        '<Function Name="GetItem"><ReturnType Type="NS.Item"/></Function>'
        '<Function Name="ListShared"><ReturnType Type="Collection(Self.Shared)"/></Function>'
        '<Function Name="CountOpen"><ReturnType Type="Edm.Int32"/></Function>'
        '<Action Name="Refresh"/>'
        # Bound: the entity it is called on comes back (EntitySetPath = the binding).
        '<Action Name="Release" IsBound="true" EntitySetPath="_it">'
        '<Parameter Name="_it" Type="NS.Item"/><ReturnType Type="NS.Item"/></Action>'
        # Bound, returns the only set of another type.
        '<Function Name="SupplierOf" IsBound="true"><Parameter Name="_it" Type="NS.Item"/>'
        '<ReturnType Type="NS.Supplier"/></Function>'
        # Bound, a path that is more than the binding parameter: not followed.
        '<Function Name="Related" IsBound="true" EntitySetPath="_it/to_Shared">'
        '<Parameter Name="_it" Type="NS.Item"/><ReturnType Type="Collection(NS.Shared)"/>'
        "</Function>"
    )
    imports = (
        '<FunctionImport Name="GetItem" Function="NS.GetItem" EntitySet="Items"/>'
        '<FunctionImport Name="GetItemUnsaid" Function="NS.GetItem"/>'
        '<FunctionImport Name="GetItemElsewhere" Function="NS.GetItem" EntitySet="Suppliers"/>'
        '<FunctionImport Name="GetItemByPath" Function="Self.GetItem" EntitySet="NS.C/Items"/>'
        '<FunctionImport Name="GetItemOther" Function="NS.GetItem" EntitySet="NS.Other/Items"/>'
        '<FunctionImport Name="ListSharedB" Function="NS.ListShared" EntitySet="SharedB"/>'
        '<FunctionImport Name="ListShared" Function="NS.ListShared"/>'
        '<FunctionImport Name="CountOpen" Function="NS.CountOpen"/>'
        '<ActionImport Name="Refresh" Action="NS.Refresh"/>'
    )
    parsed = parse_metadata(v4(operations, imports), "v4")
    assert {o.name: o.returns for o in parsed.operations} == {
        "GetItem": ParsedReturn("Items", False, ""),
        "GetItemUnsaid": ParsedReturn("Items", False, ""),  # the only set of the type
        "GetItemElsewhere": ParsedReturn(None, False, ""),  # a set of another type
        "GetItemByPath": ParsedReturn("Items", False, ""),
        "GetItemOther": ParsedReturn(None, False, ""),  # a set in a container not in here
        "ListSharedB": ParsedReturn("SharedB", True, ""),
        "ListShared": ParsedReturn(None, True, ""),
        "CountOpen": ParsedReturn(None, False, "Edm.Int32"),
        "Refresh": None,
        "Release": ParsedReturn("Items", False, ""),
        "SupplierOf": ParsedReturn("Suppliers", False, ""),
        "Related": ParsedReturn(None, True, ""),
    }
    assert [s for s in parsed.skipped if s.kind == "operation"] == []
    bound = {o.name: o.bound_to for o in parsed.operations if o.bound_to}
    assert bound == {"Release": "Items", "SupplierOf": "Items", "Related": "Items"}


def test_reading_what_operations_return_costs_no_work_per_entity_set():
    """One unit-free dictionary lookup per operation: the work a parse spends
    does not depend on how many sets there are to resolve against."""
    imports = "".join(
        f'<FunctionImport Name="F{i}" ReturnType="NS.Item" EntitySet="Items"/>' for i in range(300)
    )
    plain = "".join(f'<FunctionImport Name="F{i}"/>' for i in range(300))
    assert parse_metadata(v2(imports), "v2").work == parse_metadata(v2(plain), "v2").work


def test_the_preview_passes_the_return_on_as_a_suggestion_only():
    from agents.odata.preview import build_preview

    imports = (
        '<FunctionImport Name="ListItems" m:HttpMethod="GET" '
        'ReturnType="Collection(NS.Item)" EntitySet="Items"/>'
        '<FunctionImport Name="Count" m:HttpMethod="GET" ReturnType="Edm.Int32"/>'
        '<FunctionImport Name="SharedUnsaid" m:HttpMethod="GET" ReturnType="NS.Shared"/>'
        '<FunctionImport Name="Nothing" m:HttpMethod="POST"/>'
    )
    preview = build_preview(parse_metadata(v2(imports), "v2"))
    by_name = {o["name"]: o for o in preview["operations"]}
    assert by_name["ListItems"]["suggested"] == {
        "changes_data": True,
        "known": False,
        "returns": {"entity_set": "Items", "collection": True, "type": ""},
    }
    assert by_name["Count"]["suggested"]["returns"] == {
        "entity_set": None, "collection": False, "type": "Edm.Int32",
    }
    assert by_name["SharedUnsaid"]["suggested"]["returns"] == {
        "entity_set": None, "collection": False, "type": "",
    }
    assert by_name["Nothing"]["suggested"]["returns"] is None
    # A suggestion: the operation itself carries no `returns` and no switch.
    for operation in preview["operations"]:
        assert "returns" not in operation and "enabled" not in operation


def test_a_skipped_set_of_the_type_means_the_type_does_not_decide():
    """"The only set of this type" counts the sets the document DECLARES: one
    that was skipped (a bad name, a duplicate) is still a second set."""
    skipped = (
        '<EntitySet Name="bad name" EntityType="NS.Supplier"/>'
        '<EntitySet Name="Items" EntityType="NS.Item"/>'  # a duplicate name
    )
    imports = (
        '<FunctionImport Name="Supplier" ReturnType="NS.Supplier"/>'
        '<FunctionImport Name="Item" ReturnType="NS.Item"/>'
        '<FunctionImport Name="Named" ReturnType="NS.Supplier" EntitySet="Suppliers"/>'
    )
    parsed = parse_metadata(v2(skipped + imports), "v2")
    assert sorted(s.reason for s in parsed.skipped) == [
        "duplicate_name", "invalid_name", "unrepresentable_key",
    ]
    by_name = {o.name: o.returns for o in parsed.operations}
    assert by_name["Supplier"] == ParsedReturn(None, False, "")
    assert by_name["Item"] == ParsedReturn(None, False, "")
    assert by_name["Named"] == ParsedReturn("Suppliers", False, "")
    # A skipped set whose type cannot be read could be of any type.
    unreadable = f'<EntitySet Name="Odd" EntityType="{"X" * 300}"/>'
    parsed = parse_metadata(v2(unreadable + imports), "v2")
    assert {o.name: o.returns for o in parsed.operations}["Supplier"] == ParsedReturn(
        None, False, ""
    )
