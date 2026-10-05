"""``$metadata`` of an OData service, parsed into one version-neutral shape.

The admin's "read metadata" step shows what a service offers so that a
catalogue definition can be curated from it. This module turns the EDMX
document into :class:`ParsedMetadata`: entity sets (not entity types -- an
agent addresses a set) with keys, fields, labels and the SAP capability
annotations, navigations resolved to the *entity set* they lead to, and the
callable operations. Nothing here decides what an agent may do; the parsed
flags are what SAP *says*, the catalogue's flags are what the admin allows.

The document comes from a remote system, so it is untrusted input:

* A DTD is never accepted, by two independent layers. Input containing
  ``<!DOCTYPE`` or ``<!ENTITY`` is refused before parsing (an ASCII byte
  scan), and the parser itself refuses a doctype or entity declaration when
  expat reports one. The handlers are the layer that holds for input the
  scan cannot read but expat can decode: UTF-16 with a byte order mark.
  Input expat cannot decode at all (EBCDIC, UTF-16 without a BOM) is not
  stopped by either layer -- it simply does not parse and is refused as
  "not an EDMX document". Without a DTD there is no entity to expand (no
  "billion laughs") and no external entity to resolve; an external entity
  handler is installed anyway and refuses.
* Size and nesting depth are capped.
* An error never quotes the document: what came back instead of EDMX is
  often a login page, and a refusal is echoed to the admin UI and logged.

Elements and attributes are matched by local name, so every EDMX 1.0 / EDM
schema namespace revision and the ``sap:`` / ``m:`` annotation namespaces
parse the same way.

Everything in the result fits the definition models (``agents.odata.models``):
names match ``EDM_NAME_RE``, an operation's method is GET or POST, a
navigation always has a target entity set. What the document declares but
cannot be represented that way is left out and listed in
``ParsedMetadata.skipped`` (kind, owning entity set, position, a reason code
from ``SKIP_REASONS`` -- never its name or other text of the document); one
odd element does not make the whole service unreadable.

``parse_metadata`` is synchronous CPU work (up to ``MAX_METADATA_BYTES`` of
XML): a route or tool must call it through ``asyncio.to_thread`` and not on
the event loop.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal
from xml.parsers import expat

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
_HTTP_METHODS = ("GET", "POST")  # models.OperationDef.http_method

# Why an element is in `ParsedMetadata.skipped`. `position` counts, 1-based
# and in document order: EntitySet elements over all containers
# ("entity_set"), FunctionImport elements over all containers ("operation"),
# and the Property / NavigationProperty elements of the entity set's type,
# base types first ("property" / "navigation").
SKIP_REASONS = (
    "invalid_name",  # the Name is not an EDM_NAME_RE identifier
    "invalid_type",  # a type name longer than the models accept
    "duplicate_name",  # entity_set / operation: an earlier one has the name
    "unrepresentable_key",  # entity_set: a key part is no (parsed) property of it
    "unresolved_target",  # navigation: no association set gives its target entity set
    "unsupported_http_method",  # operation: m:HttpMethod is not GET or POST
    "invalid_parameter",  # operation: an In parameter with a bad name or type
)


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
    target: str  # entity SET name, always one of ParsedMetadata.entity_sets
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
    http_method: str  # "GET" | "POST"
    bound_to: str | None
    parameters: tuple[ParamDef, ...]
    label: str


@dataclass(frozen=True)
class SkippedElement:
    """Something the document declares that the parser left out, and why.

    Deliberately without the element's own name or any other text of the
    document: an element is skipped because its name or type is not
    something the definition models accept, so that text is exactly what
    must not travel on into a preview or a log. ``position`` is how the
    admin finds it in the ``$metadata``.
    """

    kind: str  # "entity_set" | "property" | "navigation" | "operation"
    entity_set: str  # the owning (or, for kind "entity_set", the own) set; "" when none or invalid
    position: int  # 1-based, see SKIP_REASONS for what is counted
    reason: str  # one of SKIP_REASONS


@dataclass(frozen=True)
class ParsedMetadata:
    version: str
    entity_sets: tuple[ParsedEntitySet, ...]
    operations: tuple[ParsedOperation, ...]
    skipped: tuple[SkippedElement, ...] = ()


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
    """A SAP capability annotation; absent means ``true`` (the V2 default).

    This is what SAP *declares*, not a permission: the catalogue enables
    nothing from it.
    """
    value = element.get(name)
    return True if value is None else value.strip().lower() != "false"


def _label(element: ET.Element | None) -> str:
    """A one-line label without invisible characters.

    Format characters (category Cf: bidi overrides and isolates, zero-width
    characters, BOM) are dropped: in an admin table they can reorder or hide
    what the line says. Other control characters become a space.
    """
    if element is None:
        return ""
    text = "".join(
        " " if unicodedata.category(ch) == "Cc" else ch
        for ch in element.get("label") or ""
        if unicodedata.category(ch) != "Cf"
    )
    return " ".join(text.split())[:MAX_LABEL_CHARS]


def _name(element: ET.Element) -> str | None:
    """The element's ``Name`` when the definition models accept it, else ``None``.

    ``fullmatch``: with ``match`` the pattern's ``$`` also accepts a trailing
    newline.
    """
    value = element.get("Name")
    return value if value and _NAME_RE.fullmatch(value) else None


def _type_name(element: ET.Element) -> str | None:
    """The ``Type`` (default ``Edm.String``), or ``None`` when it is too long."""
    value = (element.get("Type") or "").strip() or "Edm.String"
    return value if len(value) <= _MAX_TYPE_CHARS else None


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


@dataclass
class _SetDraft:
    """An entity set that survived the first pass (name, type, fields, key)."""

    name: str
    element: ET.Element
    type_element: ET.Element | None
    entity_type: str
    chain: list[ET.Element]
    fields: tuple[ParsedField, ...]
    keys: tuple[KeyDef, ...]


def _v2_fields(
    chain: list[ET.Element], set_name: str
) -> tuple[tuple[ParsedField, ...], list[SkippedElement]]:
    fields: dict[str, ParsedField] = {}
    skipped: list[SkippedElement] = []
    position = 0
    for entity_type in chain:
        for prop in entity_type.findall("Property"):
            position += 1
            name, type_name = _name(prop), _type_name(prop)
            if name is None or type_name is None:
                reason = "invalid_name" if name is None else "invalid_type"
                skipped.append(SkippedElement("property", set_name, position, reason))
                continue
            fields.setdefault(
                name,
                ParsedField(
                    name=name,
                    type=type_name,
                    label=_label(prop),
                    filterable=_flag(prop, "filterable"),
                    creatable=_flag(prop, "creatable"),
                    updatable=_flag(prop, "updatable"),
                    nullable=(prop.get("Nullable") or "").strip().lower() != "false",
                ),
            )
    return tuple(fields.values()), skipped


def _v2_keys(chain: list[ET.Element], fields: tuple[ParsedField, ...]) -> tuple[KeyDef, ...] | None:
    """The key, ``()`` for a type that declares none, ``None`` when it cannot be represented.

    A key part must be one of the parsed fields: a part that names no
    property, or a property that was skipped, would leave an entity set
    whose rows cannot be addressed -- and guessing a type for it would send
    wrongly quoted keys to SAP.
    """
    types = {f.name: f.type for f in fields}
    for entity_type in chain:  # the key is declared once, on the root of the hierarchy
        key = entity_type.find("Key")
        if key is None:
            continue
        names = list(dict.fromkeys(ref.get("Name") or "" for ref in key.findall("PropertyRef")))
        if any(name not in types for name in names):
            return None
        return tuple(KeyDef(name=name, type=types[name]) for name in names)
    return ()


def _v2_navigations(
    draft: _SetDraft,
    schemas: _Schemas,
    targets: dict[tuple[str, str, str], str],
) -> tuple[tuple[ParsedNavigation, ...], list[SkippedElement]]:
    navigations: dict[str, ParsedNavigation] = {}
    skipped: list[SkippedElement] = []
    position = 0
    for entity_type in draft.chain:
        for nav in entity_type.findall("NavigationProperty"):
            position += 1
            name = _name(nav)
            if name is None:
                skipped.append(SkippedElement("navigation", draft.name, position, "invalid_name"))
                continue
            association = schemas.associations.get((nav.get("Relationship") or "").strip())
            to_role, from_role = nav.get("ToRole") or "", nav.get("FromRole") or ""
            end = (
                next((e for e in association[1].findall("End") if e.get("Role") == to_role), None)
                if association is not None
                else None
            )
            # Only an association set says which entity SET the other end is
            # (several sets can share one entity type); without one for this
            # set the navigation has no known target and is not offered.
            target = (
                targets.get((association[0], from_role, draft.name))
                if association is not None and end is not None
                else None
            )
            if end is None or not target:
                skipped.append(
                    SkippedElement("navigation", draft.name, position, "unresolved_target")
                )
                continue
            navigations.setdefault(
                name,
                ParsedNavigation(
                    name=name,
                    target=target,
                    collection=(end.get("Multiplicity") or "").strip() == "*",
                ),
            )
    return tuple(navigations.values()), skipped


def _v2_operation(
    element: ET.Element, schemas: _Schemas, sets_by_type: dict[str, list[str]]
) -> ParsedOperation | str:
    """The function import, or the reason code it is skipped for."""
    name = _name(element)
    if name is None:
        return "invalid_name"
    http_method = (element.get("HttpMethod") or "").strip().upper() or "GET"
    if http_method not in _HTTP_METHODS:
        # PUT / DELETE / MERGE exist in V2, but the tools call an operation
        # with GET or POST only, and `OperationDef` stores nothing else.
        return "unsupported_http_method"
    parameters: dict[str, ParamDef] = {}
    for param in element.findall("Parameter"):
        # Out parameters are part of the answer, not of the call.
        if (param.get("Mode") or "In").strip().lower() == "out":
            continue
        param_name, type_name = _name(param), _type_name(param)
        if param_name is None or type_name is None:
            # Not callable as declared: a call without one of its parameters
            # would be a different call.
            return "invalid_parameter"
        parameters.setdefault(
            param_name,
            ParamDef(
                name=param_name,
                type=type_name,
                # A parameter is optional only when the service says it may
                # be null; SAP Gateway writes Nullable="false" on mandatory ones.
                required=(param.get("Nullable") or "").strip().lower() == "false",
            ),
        )

    # sap:action-for names an entity TYPE. It is a binding only when exactly
    # one entity set has that type; the import's own EntitySet attribute is
    # the set of its RETURN value and decides nothing here.
    candidates = sets_by_type.get(schemas.canonical_type(element.get("action-for")), [])
    return ParsedOperation(
        name=name,
        qualified_name="",  # a V2 function import is addressed by its plain name
        kind="function_import",
        http_method=http_method,
        bound_to=candidates[0] if element.get("action-for") and len(candidates) == 1 else None,
        parameters=tuple(parameters.values()),
        label=_label(element),
    )


def _v2_entity_set_drafts(schemas: _Schemas, skipped: list[SkippedElement]) -> dict[str, _SetDraft]:
    drafts: dict[str, _SetDraft] = {}
    position = 0
    for container in schemas.containers:
        for element in container.findall("EntitySet"):
            position += 1
            name = _name(element)
            if name is None:
                skipped.append(SkippedElement("entity_set", "", position, "invalid_name"))
                continue
            if name in drafts:
                skipped.append(SkippedElement("entity_set", name, position, "duplicate_name"))
                continue
            resolved = schemas.entity_type(element.get("EntityType"))
            entity_type = schemas.canonical_type(element.get("EntityType"))
            if len(entity_type) > _MAX_TYPE_CHARS:
                skipped.append(SkippedElement("entity_set", name, position, "invalid_type"))
                continue
            # A set whose type is not in the document is still listed (empty),
            # so the admin sees it exists instead of wondering where it went.
            chain = schemas.chain(resolved[1]) if resolved else []
            fields, skipped_fields = _v2_fields(chain, name)
            keys = _v2_keys(chain, fields)
            if keys is None:
                # One entry for the whole set; its properties are not listed.
                skipped.append(SkippedElement("entity_set", name, position, "unrepresentable_key"))
                continue
            skipped.extend(skipped_fields)
            drafts[name] = _SetDraft(
                name=name,
                element=element,
                type_element=resolved[1] if resolved else None,
                entity_type=entity_type,
                chain=chain,
                fields=fields,
                keys=keys,
            )
    return drafts


def _v2_navigation_targets(
    schemas: _Schemas, drafts: dict[str, _SetDraft]
) -> dict[tuple[str, str, str], str]:
    """``(association, from role, from entity set) -> to entity set``, built once.

    The from *set* is part of the key because one association can have
    several association sets, one per pair of entity sets. Only entity sets
    that are part of the result appear, on either side.
    """
    targets: dict[tuple[str, str, str], str] = {}
    for container in schemas.containers:
        for element in container.findall("AssociationSet"):
            association = schemas.associations.get((element.get("Association") or "").strip())
            if association is None:
                continue
            ends = [
                (end.get("Role") or "", end.get("EntitySet") or "")
                for end in element.findall("End")
            ]
            for from_role, from_set in ends:
                for to_role, to_set in ends:
                    if from_role != to_role and from_set in drafts and to_set in drafts:
                        targets.setdefault((association[0], from_role, from_set), to_set)
    return targets


def _parse_v2(schemas: _Schemas) -> ParsedMetadata:
    skipped: list[SkippedElement] = []
    # Pass 1: which entity sets exist in the result. Navigations and bindings
    # are resolved afterwards, against these only.
    drafts = _v2_entity_set_drafts(schemas, skipped)
    sets_by_type: dict[str, list[str]] = {}
    for draft in drafts.values():
        sets_by_type.setdefault(draft.entity_type, []).append(draft.name)
    targets = _v2_navigation_targets(schemas, drafts)

    entity_sets: list[ParsedEntitySet] = []
    for draft in drafts.values():
        navigations, skipped_navigations = _v2_navigations(draft, schemas, targets)
        skipped.extend(skipped_navigations)
        entity_sets.append(
            ParsedEntitySet(
                name=draft.name,
                entity_type=draft.entity_type,
                label=_label(draft.element) or _label(draft.type_element),
                keys=draft.keys,
                fields=draft.fields,
                navigations=navigations,
                creatable=_flag(draft.element, "creatable"),
                updatable=_flag(draft.element, "updatable"),
                deletable=_flag(draft.element, "deletable"),
            )
        )

    operations: dict[str, ParsedOperation] = {}
    position = 0
    for container in schemas.containers:
        for element in container.findall("FunctionImport"):
            position += 1
            operation = _v2_operation(element, schemas, sets_by_type)
            if isinstance(operation, str):
                skipped.append(SkippedElement("operation", "", position, operation))
            elif operation.name in operations:
                skipped.append(SkippedElement("operation", "", position, "duplicate_name"))
            else:
                operations[operation.name] = operation

    return ParsedMetadata(
        version="v2",
        entity_sets=tuple(entity_sets),
        operations=tuple(operations.values()),
        skipped=tuple(skipped),
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
