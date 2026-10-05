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
parse the same way. Character data is kept only for the three V4 expression
elements the parser reads (``String``, ``Bool``, ``PropertyPath``), and at
most ``_MAX_TEXT_CHARS`` of it per element.

V2 (EDMX 1.0) and V4 (EDMX 4.0) give the same result shape. V4 says the same
things elsewhere: labels and capabilities are annotations (inline, or in an
``Annotations`` block that names its target), a navigation's target entity
set is a ``NavigationPropertyBinding`` of the entity set, and operations are
actions (POST) and functions (GET), bound to an entity or imported into the
container.

Everything in the result fits the definition models (``agents.odata.models``):
names match ``EDM_NAME_RE``, an operation's method is GET or POST, a
navigation always has a target entity set. What the document declares but
cannot be represented that way is left out and listed in
``ParsedMetadata.skipped`` (kind, owning entity set, position, a reason code
from ``SKIP_REASONS`` -- never a name that failed the EDM rule or other
document text); one odd element does not make the whole service unreadable.
A property or navigation of a type that several entity sets share is
recorded once per set, with a position relative to that set's type chain.

``parse_metadata`` is synchronous CPU work (up to ``MAX_METADATA_BYTES`` of
XML): a route or tool must call it through ``asyncio.to_thread`` and not on
the event loop.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
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
# Character data is kept for these elements only: the V4 annotation values
# that may be written as element content. Everything else in EDMX is
# attributes, and free text (documentation, a login page) is not wanted.
_TEXT_ELEMENTS = frozenset({"String", "Bool", "PropertyPath"})
# Far above a label (cut to MAX_LABEL_CHARS anyway), a boolean or a property
# name (EDM_NAME_RE allows 128). A longer text is cut, which can only make a
# property path match nothing.
_MAX_TEXT_CHARS = 1024

_DTD_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_NAME_RE = re.compile(EDM_NAME_RE)
_NOT_EDMX = "not an EDMX document"
_HTTP_METHODS = ("GET", "POST")  # models.OperationDef.http_method

# Why an element is in `ParsedMetadata.skipped`. `position` counts, 1-based
# and in document order: EntitySet elements over all containers
# ("entity_set"); for "operation" the FunctionImport elements over all
# containers in V2 and the Action / Function elements over all schemas in V4
# (every overload counts); and the Property / NavigationProperty elements of
# the entity set's type, base types first ("property" / "navigation").
SKIP_REASONS = (
    # The Name is not an EDM_NAME_RE identifier. V4 operation: also its
    # namespace-qualified name, or the name of the import it is called by.
    "invalid_name",
    # A type name longer than the models accept. V4 navigation: no Type at
    # all, so nothing says whether it leads to one entity or to many.
    "invalid_type",
    "duplicate_name",  # entity_set / operation: an earlier one has the name
    "unrepresentable_key",  # entity_set: a key part is no (parsed) property of it
    # navigation: no association set (V2) / navigation property binding (V4)
    # gives one target entity set that is part of the result.
    "unresolved_target",
    "unsupported_http_method",  # operation (V2): m:HttpMethod is not GET or POST
    "invalid_parameter",  # operation: a parameter of the call with a bad name or type
    # operation (V4), bound: the binding parameter is not a single entity of
    # exactly one entity set of the result (missing, a collection, a type no
    # set or several sets have).
    "unsupported_binding",
    "not_imported",  # operation (V4), unbound: no action / function import, so no URL
)

_EDMX_V4_NAMESPACE = "http://docs.oasis-open.org/odata/ns/edmx"
# The vocabularies the V4 parser reads, with the alias each one
# conventionally gets. A document's own `edmx:Include` aliases come on top
# (and replace these); SAP services declare e.g. `SAP__common`.
_COMMON = "com.sap.vocabularies.Common.v1"
_CAPABILITIES = "Org.OData.Capabilities.V1"
_CORE = "Org.OData.Core.V1"
_DEFAULT_ALIASES = {"Common": _COMMON, "Capabilities": _CAPABILITIES, "Core": _CORE}
_TERM_LABEL = f"{_COMMON}.Label"
_TERM_INSERT = f"{_CAPABILITIES}.InsertRestrictions"
_TERM_UPDATE = f"{_CAPABILITIES}.UpdateRestrictions"
_TERM_DELETE = f"{_CAPABILITIES}.DeleteRestrictions"
_TERM_FILTER = f"{_CAPABILITIES}.FilterRestrictions"
_TERM_OPTIONAL = f"{_CORE}.OptionalParameter"


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

    Never a name that failed the EDM rule or other document text: an
    element is skipped because its name or type is not something the
    definition models accept, so that text is exactly what must not travel
    on into a preview or a log. ``entity_set`` is a name that passed the
    rule (for kind ``entity_set`` the element's own, already valid, name).
    ``position`` is how the admin finds the element in the ``$metadata``. A
    property or navigation of a type that several entity sets share is
    recorded once per set, with a position relative to that set's type
    chain.
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
    # Per open element: the text collected for it, or None when its text is
    # not kept. `room` is what the innermost element may still take.
    texts: list[list[str] | None] = []
    room = 0

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth, room
        depth += 1
        if depth > MAX_DEPTH:
            raise MetadataError(f"the document is nested deeper than {MAX_DEPTH} levels")
        if depth == 1:
            root_namespace.append(name.rpartition(" ")[0])
        if texts and texts[-1] is not None:
            # An expression element with child elements is not a plain value.
            texts[-1] = None
        texts.append([] if _local(name) in _TEXT_ELEMENTS else None)
        room = _MAX_TEXT_CHARS
        # Annotations by local name (sap:label -> label); an attribute of the
        # element's own vocabulary (no namespace) wins over an annotation of
        # the same local name.
        flat = {_local(k): v for k, v in attrs.items() if " " in k}
        flat.update((k, v) for k, v in attrs.items() if " " not in k)
        builder.start(_local(name), flat)

    def end(name: str) -> None:
        nonlocal depth
        depth -= 1
        chunks = texts.pop()
        if chunks:
            # Handed over in one piece and before any child could start, so it
            # becomes the element's `text`; no element ever gets a `tail`.
            builder.data("".join(chunks))
        builder.end(_local(name))

    def data(text: str) -> None:
        nonlocal room
        chunks = texts[-1] if texts else None
        if chunks is None or room <= 0:
            return
        chunks.append(text[:room])
        room -= len(text)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    # EDMX 1.0 carries everything in attributes; text is kept for the few V4
    # expression elements in _TEXT_ELEMENTS only, and bounded.
    parser.CharacterDataHandler = data
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


def _clean_label(text: str | None) -> str:
    """A one-line label without invisible characters.

    Format characters (category Cf: bidi overrides and isolates, zero-width
    characters, BOM) are dropped: in an admin table they can reorder or hide
    what the line says. Other control characters become a space.
    """
    text = "".join(
        " " if unicodedata.category(ch) == "Cc" else ch
        for ch in text or ""
        if unicodedata.category(ch) != "Cf"
    )
    return " ".join(text.split())[:MAX_LABEL_CHARS]


def _label(element: ET.Element | None) -> str:
    """The ``sap:label`` of a V2 element."""
    return "" if element is None else _clean_label(element.get("label"))


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
        self.elements = schemas
        self.entity_types: dict[str, tuple[str, ET.Element]] = {}
        self.associations: dict[str, tuple[str, ET.Element]] = {}
        self.containers: list[ET.Element] = []
        # id(element) -> canonical name, for entity types and containers. The
        # elements live as long as the tree, which outlives this object's use.
        self._names: dict[int, str] = {}
        self._namespaces: list[str] = []
        self._aliases: list[tuple[str, str]] = []
        for schema in schemas:
            namespace = (schema.get("Namespace") or "").strip()
            alias = (schema.get("Alias") or "").strip()
            prefixes = [p for p in (namespace, alias) if p]
            if namespace:
                self._namespaces.append(namespace)
                if alias:
                    self._aliases.append((alias, namespace))
            for child in schema:
                if child.tag == "EntityContainer":
                    self.containers.append(child)
                    container = child.get("Name") or ""
                    self._names[id(child)] = f"{namespace}.{container}" if namespace else container
                    continue
                table = {"EntityType": self.entity_types, "Association": self.associations}.get(
                    child.tag
                )
                name = child.get("Name")
                if table is None or not name:
                    continue
                canonical = f"{namespace}.{name}" if namespace else name
                if child.tag == "EntityType":
                    self._names[id(child)] = canonical
                for prefix in prefixes or [""]:
                    table.setdefault(f"{prefix}.{name}" if prefix else name, (canonical, child))
        # Longest first, so that a namespace which extends another one wins.
        self._namespaces.sort(key=len, reverse=True)
        self._aliases.sort(key=lambda pair: len(pair[0]), reverse=True)

    def name_of(self, element: ET.Element | None) -> str:
        """The canonical name of an entity type or container of the document."""
        return "" if element is None else self._names.get(id(element), "")

    def canonical(self, ref: str | None) -> str:
        """A qualified name with a schema alias replaced by its namespace.

        Purely textual, so it also serves names that have no lookup table
        here (actions, functions, containers).
        """
        ref = (ref or "").strip()
        if any(ref.startswith(f"{namespace}.") for namespace in self._namespaces):
            return ref
        for alias, namespace in self._aliases:
            if ref.startswith(f"{alias}."):
                return namespace + ref[len(alias) :]
        return ref

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


# ---------------------------------------------------- entity sets, both versions


@dataclass
class _SetDraft:
    """An entity set that survived the first pass (name, type, fields, key)."""

    name: str
    element: ET.Element
    container: ET.Element
    type_element: ET.Element | None
    entity_type: str
    chain: list[ET.Element]
    fields: tuple[ParsedField, ...]
    keys: tuple[KeyDef, ...]


# (type chain, entity set name, container, EntitySet element) -> fields, skipped
_FieldsOf = Callable[
    [list[ET.Element], str, ET.Element, ET.Element],
    tuple[tuple[ParsedField, ...], list[SkippedElement]],
]


def _keys(chain: list[ET.Element], fields: tuple[ParsedField, ...]) -> tuple[KeyDef, ...] | None:
    """The key, ``()`` for a type that declares none, ``None`` when it cannot be represented.

    A key part must be one of the parsed fields: a part that names no
    property (in V4 also a path into a complex property), or a property that
    was skipped, would leave an entity set whose rows cannot be addressed --
    and guessing a type for it would send wrongly quoted keys to SAP.
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


def _entity_set_drafts(
    schemas: _Schemas, skipped: list[SkippedElement], fields_of: _FieldsOf
) -> dict[str, _SetDraft]:
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
            fields, skipped_fields = fields_of(chain, name, container, element)
            keys = _keys(chain, fields)
            if keys is None:
                # One entry for the whole set; its properties are not listed.
                skipped.append(SkippedElement("entity_set", name, position, "unrepresentable_key"))
                continue
            skipped.extend(skipped_fields)
            drafts[name] = _SetDraft(
                name=name,
                element=element,
                container=container,
                type_element=resolved[1] if resolved else None,
                entity_type=entity_type,
                chain=chain,
                fields=fields,
                keys=keys,
            )
    return drafts


def _sets_by_type(drafts: dict[str, _SetDraft]) -> dict[str, list[str]]:
    """Entity type -> the entity sets of the result that have it."""
    sets_by_type: dict[str, list[str]] = {}
    for draft in drafts.values():
        sets_by_type.setdefault(draft.entity_type, []).append(draft.name)
    return sets_by_type


# ------------------------------------------------------------------------- V2


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
    # `sets_by_type` holds kept sets only: a skipped set is never a binding.
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


def _v2_navigation_targets(
    schemas: _Schemas, drafts: dict[str, _SetDraft]
) -> dict[tuple[str, str, str], str]:
    """``(association, from role, from entity set) -> to entity set``, built once.

    The from *set* is part of the key because one association can have
    several association sets, one per pair of entity sets. Only entity sets
    that are part of the result appear, on either side. A key for which two
    association sets name different target sets is ambiguous: it maps to
    ``""``, which the caller treats as no target, instead of letting the
    first association set in the document win.
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
                    if from_role == to_role or from_set not in drafts or to_set not in drafts:
                        continue
                    key = (association[0], from_role, from_set)
                    if targets.setdefault(key, to_set) != to_set:
                        targets[key] = ""  # stays ambiguous whatever follows
    return targets


def _parse_v2(schemas: _Schemas) -> ParsedMetadata:
    skipped: list[SkippedElement] = []
    # Pass 1: which entity sets exist in the result. Navigations and bindings
    # are resolved afterwards, against these only.
    drafts = _entity_set_drafts(
        schemas, skipped, lambda chain, name, _container, _element: _v2_fields(chain, name)
    )
    sets_by_type = _sets_by_type(drafts)
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


# ------------------------------------------------------------------------- V4


class _Annotations:
    """The annotations of a V4 document, found by term and by what they annotate.

    An annotation is either a child of the element it describes or sits in an
    ``Annotations`` block that names its target (``ns.Type``, ``ns.Type/Prop``,
    ``ns.Container/EntitySet``, ``ns.Action(...)``). A term is compared by its
    full name, so the alias a document chose for a vocabulary does not matter.
    Annotations with a ``Qualifier`` are alternatives for special consumers
    and are not read.
    """

    def __init__(self, root: ET.Element, schemas: _Schemas) -> None:
        self._schemas = schemas
        self._aliases = dict(_DEFAULT_ALIASES)
        for include in root.iter("Include"):
            namespace = (include.get("Namespace") or "").strip()
            alias = (include.get("Alias") or "").strip()
            if namespace and alias:
                self._aliases[alias] = namespace
        self._blocks: dict[str, list[ET.Element]] = {}
        for schema in schemas.elements:
            for block in schema.findall("Annotations"):
                if not block.get("Qualifier"):
                    self._blocks.setdefault(self._target(block.get("Target")), []).append(block)

    def _target(self, raw: str | None) -> str:
        """``alias.Name(signature)/rest`` -> ``namespace.Name/rest``."""
        head, slash, rest = (raw or "").strip().partition("/")
        return self._schemas.canonical(head.partition("(")[0]) + slash + rest

    def _term(self, raw: str | None) -> str:
        prefix, _, name = (raw or "").strip().rpartition(".")
        return f"{self._aliases.get(prefix, prefix)}.{name}"

    def find(self, term: str, element: ET.Element | None, target: str = "") -> ET.Element | None:
        """The annotation with that term on the element, else in a block for ``target``."""
        sources = [] if element is None else [element]
        if target:
            sources.extend(self._blocks.get(target, ()))
        for source in sources:
            for annotation in source.findall("Annotation"):
                if not annotation.get("Qualifier") and self._term(annotation.get("Term")) == term:
                    return annotation
        return None

    def label(self, element: ET.Element | None, target: str = "") -> str:
        annotation = self.find(_TERM_LABEL, element, target)
        return "" if annotation is None else _clean_label(_expression(annotation, "String"))

    def declared_false(
        self, term: str, prop: str, element: ET.Element | None, target: str = ""
    ) -> bool:
        """Whether a restriction record sets ``prop`` to a constant ``false``.

        Absent, or computed from a path, is not a declared "no": the flag
        stays ``True`` like a missing ``sap:`` annotation in V2. What SAP
        declares is information for the admin, not a permission.
        """
        value = _record_value(self.find(term, element, target), prop)
        text = None if value is None else _expression(value, "Bool")
        return text is not None and text.strip().lower() == "false"


def _expression(element: ET.Element, kind: str) -> str | None:
    """A constant, given as attribute (``String="x"``) or as child (``<String>x</String>``)."""
    value = element.get(kind)
    if value is not None:
        return value
    child = element.find(kind)
    return None if child is None else child.text or ""


def _record_value(annotation: ET.Element | None, prop: str) -> ET.Element | None:
    record = None if annotation is None else annotation.find("Record")
    if record is None:
        return None
    return next((v for v in record.findall("PropertyValue") if v.get("Property") == prop), None)


def _v4_fields(
    chain: list[ET.Element],
    set_name: str,
    set_target: str,
    set_element: ET.Element,
    schemas: _Schemas,
    annotations: _Annotations,
) -> tuple[tuple[ParsedField, ...], list[SkippedElement]]:
    # Filter restrictions belong to the entity SET, not to the type.
    restrictions = annotations.find(_TERM_FILTER, set_element, set_target)
    nothing_filterable = annotations.declared_false(
        _TERM_FILTER, "Filterable", set_element, set_target
    )
    paths = _record_value(restrictions, "NonFilterableProperties")
    collection = None if paths is None else paths.find("Collection")
    non_filterable = (
        set()
        if collection is None
        else {(path.text or "").strip() for path in collection.findall("PropertyPath")}
    )

    fields: dict[str, ParsedField] = {}
    skipped: list[SkippedElement] = []
    position = 0
    for entity_type in chain:
        declaring_type = schemas.name_of(entity_type)
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
                    # A block annotates the property on the type that declares it.
                    label=annotations.label(prop, f"{declaring_type}/{name}"),
                    filterable=not nothing_filterable and name not in non_filterable,
                    # V4 has no per-property flag the set-level restrictions
                    # above would not already cover; absent means true.
                    creatable=True,
                    updatable=True,
                    nullable=(prop.get("Nullable") or "").strip().lower() != "false",
                ),
            )
    return tuple(fields.values()), skipped


def _v4_bindings(
    draft: _SetDraft, schemas: _Schemas, drafts: dict[str, _SetDraft]
) -> dict[str, str]:
    """Navigation property name -> target entity set (``""`` when ambiguous).

    Only a binding whose path is the navigation property itself and whose
    target is an entity set of the result counts. A target may be written as
    ``Container/EntitySet``; a longer path (a contained entity) is no set.
    """
    containers = {schemas.name_of(container) for container in schemas.containers}
    bindings: dict[str, str] = {}
    for binding in draft.element.findall("NavigationPropertyBinding"):
        path = (binding.get("Path") or "").strip()
        target = (binding.get("Target") or "").strip()
        head, slash, rest = target.partition("/")
        if slash:
            target = rest if schemas.canonical(head) in containers else ""
        if not path or target not in drafts:
            continue
        if bindings.setdefault(path, target) != target:
            bindings[path] = ""  # two different targets: not guessed
    return bindings


def _v4_navigations(
    draft: _SetDraft, bindings: dict[str, str]
) -> tuple[tuple[ParsedNavigation, ...], list[SkippedElement]]:
    navigations: dict[str, ParsedNavigation] = {}
    skipped: list[SkippedElement] = []
    position = 0
    for entity_type in draft.chain:
        for nav in entity_type.findall("NavigationProperty"):
            position += 1
            name = _name(nav)
            type_ref = (nav.get("Type") or "").strip()
            target = bindings.get(name or "")
            if name is None:
                reason = "invalid_name"
            elif not type_ref:
                reason = "invalid_type"  # without a Type the multiplicity would be a guess
            elif not target:
                reason = "unresolved_target"
            else:
                reason = ""
            if name is None or not target or reason:
                skipped.append(SkippedElement("navigation", draft.name, position, reason))
                continue
            navigations.setdefault(
                name,
                ParsedNavigation(
                    name=name,
                    target=target,
                    collection=type_ref.startswith("Collection(") and type_ref.endswith(")"),
                ),
            )
    return tuple(navigations.values()), skipped


def _v4_parameters(
    element: ET.Element, kind: str, bound: bool, annotations: _Annotations
) -> tuple[ParamDef, ...] | None:
    """The parameters of the call, ``None`` when one cannot be represented.

    The first parameter of a bound operation is the entity it is called on
    (part of the URL), not an argument.
    """
    parameters: dict[str, ParamDef] = {}
    for param in element.findall("Parameter")[1 if bound else 0 :]:
        name, type_name = _name(param), _type_name(param)
        if name is None or type_name is None:
            return None
        if kind == "action":
            # An action's nullable parameter may be left out of the body.
            required = (param.get("Nullable") or "").strip().lower() == "false"
        else:
            # A function's parameters are part of the URL: all of them, unless
            # the service marks one as optional.
            required = annotations.find(_TERM_OPTIONAL, param) is None
        parameters.setdefault(name, ParamDef(name=name, type=type_name, required=required))
    return tuple(parameters.values())


def _v4_operation(
    element: ET.Element,
    kind: str,
    namespace: str,
    schemas: _Schemas,
    annotations: _Annotations,
    sets_by_type: dict[str, list[str]],
    imports: dict[tuple[str, str], list[tuple[ET.Element, ET.Element]]],
) -> list[ParsedOperation | str]:
    """What one ``Action`` / ``Function`` gives: operations, or reason codes.

    A bound one is one operation, addressed by its qualified name on an
    entity. An unbound one is callable only through an import in the
    container and is addressed by the import's name -- one operation per
    import, none without.
    """
    name = _name(element)
    qualified = f"{namespace}.{name}" if namespace else name or ""
    # models.OperationDef holds the qualified name to the same EDM rule.
    if name is None or not _NAME_RE.fullmatch(qualified):
        return ["invalid_name"]
    bound = (element.get("IsBound") or "").strip().lower() == "true"
    parameters = _v4_parameters(element, kind, bound, annotations)
    if parameters is None:
        return ["invalid_parameter"]
    http_method = "POST" if kind == "action" else "GET"
    label = annotations.label(element, qualified)

    if bound:
        binding = element.find("Parameter")
        type_ref = "" if binding is None else (binding.get("Type") or "").strip()
        # `sets_by_type` holds kept sets only. Several sets of the type, or a
        # collection binding (called on the set, without a key), have no
        # place in `bound_to` + key; not guessed.
        candidates = sets_by_type.get(schemas.canonical_type(type_ref), [])
        if len(candidates) != 1:
            return ["unsupported_binding"]
        return [
            ParsedOperation(
                name=name,
                qualified_name=qualified,
                kind=kind,
                http_method=http_method,
                bound_to=candidates[0],
                parameters=parameters,
                label=label,
            )
        ]

    outcomes: list[ParsedOperation | str] = []
    for container, imported in imports.get((kind, qualified), []):
        import_name = _name(imported)
        if import_name is None:
            outcomes.append("invalid_name")
            continue
        outcomes.append(
            ParsedOperation(
                name=import_name,
                qualified_name=qualified,
                kind=kind,
                http_method=http_method,
                bound_to=None,
                parameters=parameters,
                label=annotations.label(imported, f"{schemas.name_of(container)}/{import_name}")
                or label,
            )
        )
    return outcomes or ["not_imported"]


def _parse_v4(root: ET.Element, schemas: _Schemas) -> ParsedMetadata:
    annotations = _Annotations(root, schemas)
    skipped: list[SkippedElement] = []

    def set_target(container: ET.Element, name: str) -> str:
        return f"{schemas.name_of(container)}/{name}"

    # Pass 1: which entity sets exist in the result. Navigations and bindings
    # are resolved afterwards, against these only.
    drafts = _entity_set_drafts(
        schemas,
        skipped,
        lambda chain, name, container, element: _v4_fields(
            chain, name, set_target(container, name), element, schemas, annotations
        ),
    )
    sets_by_type = _sets_by_type(drafts)

    entity_sets: list[ParsedEntitySet] = []
    for draft in drafts.values():
        target = set_target(draft.container, draft.name)
        navigations, skipped_navigations = _v4_navigations(
            draft, _v4_bindings(draft, schemas, drafts)
        )
        skipped.extend(skipped_navigations)
        entity_sets.append(
            ParsedEntitySet(
                name=draft.name,
                entity_type=draft.entity_type,
                label=annotations.label(draft.element, target)
                or annotations.label(draft.type_element, draft.entity_type),
                keys=draft.keys,
                fields=draft.fields,
                navigations=navigations,
                creatable=not annotations.declared_false(
                    _TERM_INSERT, "Insertable", draft.element, target
                ),
                updatable=not annotations.declared_false(
                    _TERM_UPDATE, "Updatable", draft.element, target
                ),
                deletable=not annotations.declared_false(
                    _TERM_DELETE, "Deletable", draft.element, target
                ),
            )
        )

    # (kind, qualified operation name) -> its imports, in document order. An
    # import that names nothing in the document offers nothing and is not
    # listed anywhere.
    imports: dict[tuple[str, str], list[tuple[ET.Element, ET.Element]]] = {}
    for container in schemas.containers:
        for child in container:
            if child.tag == "ActionImport":
                key = ("action", schemas.canonical(child.get("Action")))
            elif child.tag == "FunctionImport":
                key = ("function", schemas.canonical(child.get("Function")))
            else:
                continue
            imports.setdefault(key, []).append((container, child))

    operations: dict[str, ParsedOperation] = {}
    position = 0
    for schema in schemas.elements:
        namespace = (schema.get("Namespace") or "").strip()
        for element in schema:
            kind = {"Action": "action", "Function": "function"}.get(element.tag)
            if kind is None:
                continue
            position += 1
            for outcome in _v4_operation(
                element, kind, namespace, schemas, annotations, sets_by_type, imports
            ):
                if isinstance(outcome, str):
                    skipped.append(SkippedElement("operation", "", position, outcome))
                elif outcome.name in operations:
                    # `OperationDef` names are unique: of several overloads
                    # (or bound actions of one name on different entities)
                    # only the first is offered.
                    skipped.append(SkippedElement("operation", "", position, "duplicate_name"))
                else:
                    operations[outcome.name] = outcome

    return ParsedMetadata(
        version="v4",
        entity_sets=tuple(entity_sets),
        operations=tuple(operations.values()),
        skipped=tuple(skipped),
    )


# ------------------------------------------------------------------ entry point


def parse_metadata(xml: bytes, version: Literal["v2", "v4"]) -> ParsedMetadata:
    """Parse a ``$metadata`` document; :class:`MetadataError` when it is not one.

    ``version`` is what the catalogue service says it is; a document of the
    other version is refused instead of being read as something it is not.
    """
    if version not in ("v2", "v4"):
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
    # EDMX 4.0 has its own (OASIS) namespace; every older one is Microsoft's.
    is_v4 = namespace == _EDMX_V4_NAMESPACE or (root.get("Version") or "").strip().startswith("4")
    if is_v4 and version == "v2":
        raise MetadataError("the document is OData V4, not V2")
    if not is_v4 and version == "v4":
        raise MetadataError("the document is OData V2, not V4")
    schemas = list(root.iter("Schema"))
    if not schemas:
        raise MetadataError(_NOT_EDMX)
    if is_v4:
        return _parse_v4(root, _Schemas(schemas))
    return _parse_v2(_Schemas(schemas))
