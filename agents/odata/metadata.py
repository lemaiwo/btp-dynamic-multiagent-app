"""``$metadata`` of an OData service, parsed into one version-neutral shape.

The admin's "read metadata" step shows what a service offers so that a
catalogue definition can be curated from it. This module turns the EDMX
document into :class:`ParsedMetadata`: entity sets (not entity types -- an
agent addresses a set) with keys, fields, labels and the SAP capability
annotations, navigations resolved to the *entity set* they lead to, and the
callable operations. Nothing here decides what an agent may do; the parsed
flags are what SAP *says*, the catalogue's flags are what the admin allows.

The document comes from a remote system, so it is untrusted input:

* A DTD is never accepted. Input containing ``<!DOCTYPE`` or ``<!ENTITY`` is
  refused before parsing, and the parser itself refuses a doctype or entity
  declaration when expat reports one. The second check is the one that
  holds for an encoding a byte scan cannot read (UTF-16, or a single-byte
  codec such as EBCDIC that expat decodes through Python's codecs). Without
  a DTD there is no entity to expand (no "billion laughs") and no external
  entity to resolve; an external entity handler is installed anyway and
  refuses.
* Size and nesting depth are capped.
* An error never quotes the document: what came back instead of EDMX is
  often a login page, and a refusal is echoed to the admin UI and logged.

Elements and attributes are matched by local name, so every EDMX 1.0 / EDM
schema namespace revision and the ``sap:`` / ``m:`` annotation namespaces
parse the same way.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal
from xml.parsers import expat

from pydantic import ValidationError

from .models import EDM_NAME_RE, KeyDef, ParamDef

MAX_METADATA_BYTES = 20_000_000
# EDMX is shallow (Edmx > DataServices > Schema > EntityType > Key >
# PropertyRef; V4 annotations nest a little deeper). A cap keeps a hostile
# document from building a tree that is expensive to walk or free.
MAX_DEPTH = 64
# What `FieldDef.label` / `EntitySetDef.title` accept, so a parsed label can
# be taken over as it is.
MAX_LABEL_CHARS = 120
_MAX_TYPE_CHARS = 200  # models.EdmType

_DTD_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_NAME_RE = re.compile(EDM_NAME_RE)
_NOT_EDMX = "not an EDMX document"


class MetadataError(ValueError):
    """The document is not usable ``$metadata``. The message is safe to show."""


@dataclass(frozen=True)
class ParsedField:
    name: str
    type: str
    label: str
    filterable: bool
    creatable: bool
    updatable: bool
    nullable: bool


@dataclass(frozen=True)
class ParsedNavigation:
    name: str
    target: str  # entity SET name ("" when the target type has none)
    collection: bool


@dataclass(frozen=True)
class ParsedEntitySet:
    name: str
    entity_type: str
    label: str
    keys: tuple[KeyDef, ...]
    fields: tuple[ParsedField, ...]
    navigations: tuple[ParsedNavigation, ...]
    creatable: bool
    updatable: bool
    deletable: bool


@dataclass(frozen=True)
class ParsedOperation:
    name: str
    qualified_name: str
    kind: str
    http_method: str
    bound_to: str | None
    parameters: tuple[ParamDef, ...]
    label: str


@dataclass(frozen=True)
class ParsedMetadata:
    version: str
    entity_sets: tuple[ParsedEntitySet, ...]
    operations: tuple[ParsedOperation, ...]


# --------------------------------------------------------------- safe parsing


def _refuse_dtd(*_args: object) -> None:
    raise MetadataError("the document contains a DTD or an entity declaration; refused")


def _local(name: str) -> str:
    # With a namespace separator expat reports "<uri> <local>".
    return name.rpartition(" ")[2]


def _parse_tree(xml: bytes) -> tuple[ET.Element, str]:
    """The document as a tree of local names, plus the root's namespace.

    expat is driven directly instead of through ``ET.XMLParser`` so that the
    DTD handlers are ours: the C ``XMLParser`` does not expose its expat
    parser, and its default is to accept an internal DTD.
    """
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator=" ")
    depth = 0
    root_namespace: list[str] = []

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth
        depth += 1
        if depth > MAX_DEPTH:
            raise MetadataError(f"the document is nested deeper than {MAX_DEPTH} levels")
        if depth == 1:
            root_namespace.append(name.rpartition(" ")[0])
        # Annotations by local name (sap:label -> label); an attribute of the
        # element's own vocabulary (no namespace) wins over an annotation of
        # the same local name.
        flat = {_local(k): v for k, v in attrs.items() if " " in k}
        flat.update((k, v) for k, v in attrs.items() if " " not in k)
        builder.start(_local(name), flat)

    def end(name: str) -> None:
        nonlocal depth
        depth -= 1
        builder.end(_local(name))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    # Character data is not kept: EDMX 1.0 carries everything in attributes.
    # Looked up at call time, so the guard stays one function.
    parser.StartDoctypeDeclHandler = lambda *a: _refuse_dtd(*a)
    parser.EntityDeclHandler = lambda *a: _refuse_dtd(*a)
    parser.UnparsedEntityDeclHandler = lambda *a: _refuse_dtd(*a)
    parser.ExternalEntityRefHandler = lambda *a: _refuse_dtd(*a)
    try:
        parser.Parse(xml, True)
        root = builder.close()
    except MetadataError:
        raise
    except Exception:
        # expat's message carries a line/column and, chained, the context;
        # neither is needed and the input may be a page with a session in it.
        raise MetadataError(_NOT_EDMX) from None
    return root, (root_namespace[0] if root_namespace else "")


# -------------------------------------------------------------------- helpers


def _flag(element: ET.Element, name: str) -> bool:
    """A SAP capability annotation; absent means ``true`` (the V2 default)."""
    value = element.get(name)
    return True if value is None else value.strip().lower() != "false"


def _label(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join((element.get("label") or "").split())[:MAX_LABEL_CHARS]


def _valid_name(value: str | None) -> bool:
    return bool(value) and _NAME_RE.match(value or "") is not None


def _type_name(element: ET.Element, who: str) -> str:
    value = (element.get("Type") or "").strip() or "Edm.String"
    if len(value) > _MAX_TYPE_CHARS:
        raise MetadataError(f"{who} has a type name longer than {_MAX_TYPE_CHARS} characters")
    return value


class _Schemas:
    """Qualified-name lookup over every ``Schema`` of the document.

    A reference may use the schema's namespace or its alias; both resolve to
    the canonical ``Namespace.Name``.
    """

    def __init__(self, schemas: list[ET.Element]) -> None:
        self.entity_types: dict[str, tuple[str, ET.Element]] = {}
        self.associations: dict[str, tuple[str, ET.Element]] = {}
        self.containers: list[ET.Element] = []
        for schema in schemas:
            namespace = (schema.get("Namespace") or "").strip()
            prefixes = [p for p in (namespace, (schema.get("Alias") or "").strip()) if p]
            for child in schema:
                if child.tag == "EntityContainer":
                    self.containers.append(child)
                    continue
                table = {"EntityType": self.entity_types, "Association": self.associations}.get(
                    child.tag
                )
                name = child.get("Name")
                if table is None or not name:
                    continue
                canonical = f"{namespace}.{name}" if namespace else name
                for prefix in prefixes or [""]:
                    table.setdefault(f"{prefix}.{name}" if prefix else name, (canonical, child))

    def entity_type(self, ref: str | None) -> tuple[str, ET.Element] | None:
        return self.entity_types.get((ref or "").strip())

    def canonical_type(self, ref: str | None) -> str:
        found = self.entity_type(ref)
        return found[0] if found else (ref or "").strip()

    def chain(self, element: ET.Element) -> list[ET.Element]:
        """The type and its base types, base first."""
        chain = [element]
        seen = {id(element)}
        while True:
            base = self.entity_type(chain[0].get("BaseType"))
            if base is None or id(base[1]) in seen:
                return chain
            seen.add(id(base[1]))
            chain.insert(0, base[1])


# ------------------------------------------------------------------------- V2


def _v2_fields(chain: list[ET.Element], who: str) -> tuple[ParsedField, ...]:
    fields: dict[str, ParsedField] = {}
    for entity_type in chain:
        for prop in entity_type.findall("Property"):
            name = prop.get("Name")
            if not _valid_name(name):
                raise MetadataError(f"{who} has a property whose name is not a valid identifier")
            assert name is not None
            fields.setdefault(
                name,
                ParsedField(
                    name=name,
                    type=_type_name(prop, f"property {name!r} of {who}"),
                    label=_label(prop),
                    filterable=_flag(prop, "filterable"),
                    creatable=_flag(prop, "creatable"),
                    updatable=_flag(prop, "updatable"),
                    nullable=(prop.get("Nullable") or "").strip().lower() != "false",
                ),
            )
    return tuple(fields.values())


def _v2_keys(
    chain: list[ET.Element], fields: tuple[ParsedField, ...], who: str
) -> tuple[KeyDef, ...]:
    types = {f.name: f.type for f in fields}
    for entity_type in chain:  # the key is declared once, on the root of the hierarchy
        key = entity_type.find("Key")
        if key is None:
            continue
        names = list(dict.fromkeys(ref.get("Name") or "" for ref in key.findall("PropertyRef")))
        try:
            return tuple(KeyDef(name=n, type=types.get(n, "Edm.String")) for n in names)
        except ValidationError:
            raise MetadataError(f"{who} has a key whose name is not a valid identifier") from None
    return ()


def _v2_navigations(
    chain: list[ET.Element],
    set_name: str,
    schemas: _Schemas,
    association_sets: list[tuple[str, dict[str, str]]],
    sets_by_type: dict[str, list[str]],
    who: str,
) -> tuple[ParsedNavigation, ...]:
    navigations: dict[str, ParsedNavigation] = {}
    for entity_type in chain:
        for nav in entity_type.findall("NavigationProperty"):
            name = nav.get("Name")
            if not _valid_name(name):
                raise MetadataError(f"{who} has a navigation whose name is not a valid identifier")
            assert name is not None
            association = schemas.associations.get((nav.get("Relationship") or "").strip())
            to_role, from_role = nav.get("ToRole") or "", nav.get("FromRole") or ""
            target, collection = "", False
            if association is not None:
                canonical, element = association
                end = next((e for e in element.findall("End") if e.get("Role") == to_role), None)
                if end is not None:
                    collection = (end.get("Multiplicity") or "").strip() == "*"
                    # The association set says which entity SET the other end
                    # is; several sets can share one entity type.
                    for assoc_name, roles in association_sets:
                        if assoc_name == canonical and roles.get(from_role) == set_name:
                            target = roles.get(to_role, "")
                            break
                    if not target:
                        candidates = sets_by_type.get(schemas.canonical_type(end.get("Type")), [])
                        target = candidates[0] if len(candidates) == 1 else ""
            navigations.setdefault(
                name, ParsedNavigation(name=name, target=target, collection=collection)
            )
    return tuple(navigations.values())


def _v2_operation(
    element: ET.Element,
    schemas: _Schemas,
    set_names: set[str],
    sets_by_type: dict[str, list[str]],
) -> ParsedOperation:
    name = element.get("Name")
    if not _valid_name(name):
        raise MetadataError("a function import name is not a valid identifier")
    assert name is not None
    who = f"function import {name!r}"
    parameters: dict[str, ParamDef] = {}
    for param in element.findall("Parameter"):
        # Out parameters are part of the answer, not of the call.
        if (param.get("Mode") or "In").strip().lower() == "out":
            continue
        try:
            parsed = ParamDef(
                name=param.get("Name") or "",
                type=_type_name(param, f"a parameter of {who}"),
                # A parameter is optional only when the service says it may
                # be null; SAP Gateway writes Nullable="false" on mandatory ones.
                required=(param.get("Nullable") or "").strip().lower() == "false",
            )
        except ValidationError:
            raise MetadataError(
                f"{who} has a parameter whose name is not a valid identifier"
            ) from None
        parameters.setdefault(parsed.name, parsed)

    bound_to: str | None = None
    action_for = element.get("action-for")
    if action_for:
        candidates = sets_by_type.get(schemas.canonical_type(action_for), [])
        own_set = (element.get("EntitySet") or "").strip()
        if len(candidates) == 1:
            bound_to = candidates[0]
        elif own_set in candidates and own_set in set_names:
            bound_to = own_set  # several sets share the type: only an explicit one counts
    return ParsedOperation(
        name=name,
        qualified_name="",  # a V2 function import is addressed by its plain name
        kind="function_import",
        http_method=(element.get("HttpMethod") or "GET").strip().upper() or "GET",
        bound_to=bound_to,
        parameters=tuple(parameters.values()),
        label=_label(element),
    )


def _parse_v2(schemas: _Schemas) -> ParsedMetadata:
    set_elements: dict[str, ET.Element] = {}
    for container in schemas.containers:
        for element in container.findall("EntitySet"):
            name = element.get("Name")
            if not _valid_name(name):
                raise MetadataError("an entity set name is not a valid identifier")
            assert name is not None
            set_elements.setdefault(name, element)

    sets_by_type: dict[str, list[str]] = {}
    for name, element in set_elements.items():
        sets_by_type.setdefault(schemas.canonical_type(element.get("EntityType")), []).append(name)

    association_sets: list[tuple[str, dict[str, str]]] = []
    for container in schemas.containers:
        for element in container.findall("AssociationSet"):
            association = schemas.associations.get((element.get("Association") or "").strip())
            if association is None:
                continue
            roles = {
                end.get("Role") or "": end.get("EntitySet") or ""
                for end in element.findall("End")
                if (end.get("EntitySet") or "") in set_elements
            }
            association_sets.append((association[0], roles))

    entity_sets: list[ParsedEntitySet] = []
    for name, element in set_elements.items():
        who = f"entity set {name!r}"
        resolved = schemas.entity_type(element.get("EntityType"))
        # A set whose type is not in the document is still listed (empty), so
        # the admin sees it exists instead of wondering where it went.
        chain = schemas.chain(resolved[1]) if resolved else []
        fields = _v2_fields(chain, who)
        entity_sets.append(
            ParsedEntitySet(
                name=name,
                entity_type=schemas.canonical_type(element.get("EntityType")),
                label=_label(element) or _label(resolved[1] if resolved else None),
                keys=_v2_keys(chain, fields, who),
                fields=fields,
                navigations=_v2_navigations(
                    chain, name, schemas, association_sets, sets_by_type, who
                ),
                creatable=_flag(element, "creatable"),
                updatable=_flag(element, "updatable"),
                deletable=_flag(element, "deletable"),
            )
        )

    operations: dict[str, ParsedOperation] = {}
    for container in schemas.containers:
        for element in container.findall("FunctionImport"):
            operation = _v2_operation(element, schemas, set(set_elements), sets_by_type)
            operations.setdefault(operation.name, operation)

    return ParsedMetadata(
        version="v2", entity_sets=tuple(entity_sets), operations=tuple(operations.values())
    )


# ------------------------------------------------------------------ entry point


def parse_metadata(xml: bytes, version: Literal["v2", "v4"]) -> ParsedMetadata:
    """Parse a ``$metadata`` document; :class:`MetadataError` when it is not one."""
    if version == "v4":
        raise MetadataError("V4 parsing is not available yet")
    if version != "v2":
        raise MetadataError("unknown OData version (expected 'v2' or 'v4')")
    if not isinstance(xml, (bytes, bytearray, memoryview)):
        # Text would have lost the document's own encoding declaration.
        raise MetadataError("the $metadata document must be passed as bytes")
    xml = bytes(xml)
    if len(xml) > MAX_METADATA_BYTES:
        raise MetadataError(f"the $metadata document is larger than {MAX_METADATA_BYTES} bytes")
    if _DTD_RE.search(xml):
        _refuse_dtd()

    root, namespace = _parse_tree(xml)
    if root.tag != "Edmx":
        raise MetadataError(_NOT_EDMX)
    if (root.get("Version") or "").strip().startswith("4") or "oasis-open.org" in namespace:
        raise MetadataError("the document is OData V4 metadata, not V2")
    schemas = list(root.iter("Schema"))
    if not schemas:
        raise MetadataError(_NOT_EDMX)
    return _parse_v2(_Schemas(schemas))
