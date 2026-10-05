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
elements the parser reads (``String``, ``Bool``, ``PropertyPath``); a text
longer than ``_MAX_TEXT_CHARS`` is treated as absent, never cut.

V2 (EDMX 1.0) and V4 (EDMX 4.0) give the same result shape. V4 says the same
things elsewhere: labels and capabilities are annotations (inline, or in an
``Annotations`` block that names its target), a navigation's target entity
set is a ``NavigationPropertyBinding`` of the entity set, and operations are
actions (POST) and functions (GET), bound to an entity or imported into the
container. A document is V4 when its root element is in the OASIS EDMX
namespace, whatever its ``Version`` attribute says.

The capability flags are what the service declares and can only go down:
absent means ``True``, and where a V4 document says the same thing twice
with different answers, the restriction wins. Per field, V4 lowers
``creatable`` / ``updatable`` for ``Core.Computed`` (neither),
``Core.Immutable`` (not updatable) and the entity set's
``NonInsertableProperties`` / ``NonUpdatableProperties``.
``Capabilities.ReadRestrictions`` is not read: the result has no "readable"
flag to lower.

For whoever builds a catalogue definition from the result:
``ParsedOperation`` carries no ``changes_data``. Set it from the kind -- a V4
action changes data, a V4 function does not, and for a V2 function import it
is unknown, so it is treated as changing data unless it is called with GET
and the admin says otherwise.

Everything in the result fits the definition models (``agents.odata.models``):
names match ``EDM_NAME_RE``, an operation's method is GET or POST, a
navigation always has a target entity set. What the document declares but
cannot be represented that way is left out and listed in
``ParsedMetadata.skipped`` (kind, owning entity set, position, a reason code
from ``SKIP_REASONS`` -- never a name that failed the EDM rule or other
document text); one odd element does not make the whole service unreadable.
A skipped PROPERTY of a type that several entity sets share is recorded once,
under the first entity set of the result that has the type; a skipped
navigation is recorded per set (its target depends on the set). Positions
are relative to the set's type chain. Every entry also names the set's
entity type (``SkippedElement.entity_type``), which is what explains a
field that is missing from the OTHER sets of that type.

A remote system chooses this document, so the work it can cause is bounded
here and not by its size alone: fields and keys are built once per entity
type and shared by the sets that have it, at most ``MAX_PARSED_ENTITY_SETS``
entity sets and ``MAX_PARSED_OPERATIONS`` operations are built (the rest are
only counted: ``ParsedMetadata.truncated``, ``entity_sets_declared``,
``operations_declared``), and one budget (``MAX_PARSE_WORK``: properties,
navigations, parameters and skipped entries examined) ends the parse with a
fixed-text ``MetadataError`` instead of letting it run on.

The budget is charged where the work happens, so that one unit never hides
a scan whose size the document chooses. The rules the code keeps to:

* An element's children are never searched with ``find`` / ``findall``:
  ``_Work.kids`` groups them by tag once per element (linear in the
  document, like reading it), and every later lookup is a dict access.
* What is read of ONE element -- a property's name, type, label and
  annotations, a navigation's association and end, an import's label -- is
  read once and kept, however many derived types, entity sets or overloads
  come back to it. The per-set and per-type loops then only combine those
  results, one unit per item.
* A lookup that returns a list (annotations of a term for a target, the
  values of a restriction record, its property paths, the parts of a key)
  costs one unit per item returned, each time it is asked.
* A string that many elements share (a namespace, a container or type
  name, a role) is charged by its length where it is copied into a target
  or compared again: ``_Work.spend_text``, nothing for the first
  ``_TEXT_UNIT`` characters, so a real document pays nothing here.

Those rules bound the loops. The INPUT of every loop is bounded once, where
the document is read (``_parse_tree``), so that no later code can be handed
a string or a tree whose size the document chose:

* An attribute value longer than its cap is not kept: the attribute is
  absent, as over-long element text is, and counted
  (``ParsedMetadata.attributes_dropped``). The cap is ``_MAX_TEXT_CHARS``
  in general (a label is cut to ``MAX_LABEL_CHARS`` anyway) and tighter for
  the attributes that carry NAMES (``_ATTRIBUTE_CAPS``): a name the
  definition models cannot hold makes its element unrepresentable, which
  the skip-and-record paths report. So a megabyte of ``Bool="..."`` is not
  stripped and lowered per lookup, and a megabyte of namespace is not copied
  into the qualified name of every element of its schema.
* Where "absent" has a meaning of its own -- a V2 ``Type`` is
  ``Edm.String``, ``m:HttpMethod`` is ``GET``, no ``Qualifier`` is the plain
  annotation, no ``EntitySet`` lets the type decide -- a dropped attribute
  is told apart (``_dropped``) and never gets that default.
* A document of more than ``MAX_ELEMENTS`` elements is refused.

``parse_metadata`` is synchronous CPU work (up to ``MAX_METADATA_BYTES`` of
XML): a route or tool must call it through ``asyncio.to_thread`` and not on
the event loop.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from xml.parsers import expat

from .models import EDM_NAME_RE, MAX_ENTITY_SETS, MAX_OPERATIONS, KeyDef, ParamDef

MAX_METADATA_BYTES = 20_000_000
# How many entity sets and operations are BUILT: twice what a catalogue
# service can hold, so a consumer that shows the catalogue's limit can still
# say "there are more". Elements past the cap are counted, not read.
MAX_PARSED_ENTITY_SETS = 2 * MAX_ENTITY_SETS
MAX_PARSED_OPERATIONS = 2 * MAX_OPERATIONS
# The work budget of one parse, in elements examined after the tree is
# built: properties (once per entity type and set-level variant),
# navigations and bindings (per set), parameters, inheritance links, and
# every skipped entry recorded. A large real service is a few thousand; the
# cap is what keeps "sets x properties" from being chosen by the document.
# Read at call time.
MAX_PARSE_WORK = 200_000
# How many elements a document may have. The largest SAP `$metadata`
# documents have a few hundred thousand; an element of a real service
# (`<Property Name=".." Type=".."/>`, `<PropertyRef Name=".."/>`) is tens of
# bytes at the very least, so a real document under `MAX_METADATA_BYTES`
# stays well below this. Without it the same 20 MB can be five million
# four-byte elements, each a tree node and, for a schema's own children, up
# to three qualified names kept in the lookup tables. With the attribute
# caps this is what bounds what one document can make a parse retain:
# elements x a few hundred bytes. Read at call time.
MAX_ELEMENTS = 500_000
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
# name (EDM_NAME_RE allows 128). A longer text is dropped as a whole: cut, it
# would be a value the document never gave ("false" followed by padding
# would read as false).
_MAX_TEXT_CHARS = 1024
# An attribute value longer than its cap is dropped where the document is
# read. The general cap is the text cap; attributes that carry names are held
# to what the models can store, with a little room so that a name or type
# just over ITS limit still reaches the check that names the reason: an EDM
# name is at most 128 characters (`EDM_NAME_RE`), a type 200
# (`_MAX_TYPE_CHARS`) or `Collection(` + 200 + `)`. A namespace or alias is
# part of every qualified name of its schema, and a qualified name is itself
# held to those limits. An annotation target is two names, or an operation
# with its signature.
_MAX_ATTRIBUTE_CHARS = _MAX_TEXT_CHARS
_MAX_NAME_ATTRIBUTE_CHARS = 256
_MAX_NAMESPACE_CHARS = 128
_MAX_TARGET_CHARS = 512
_ATTRIBUTE_CAPS = {
    **dict.fromkeys(
        (
            "Name", "Type", "EntityType", "EntitySet", "BaseType", "ReturnType",
            "Association", "Relationship", "Role", "FromRole", "ToRole", "Partner",
            "Action", "Function", "EntitySetPath", "action-for", "Term", "Qualifier",
            "Property", "Path", "PropertyPath", "HttpMethod", "Mode",
        ),  # fmt: skip
        _MAX_NAME_ATTRIBUTE_CHARS,
    ),
    "Namespace": _MAX_NAMESPACE_CHARS,
    "Alias": _MAX_NAMESPACE_CHARS,
    "Target": _MAX_TARGET_CHARS,
}
# The key under which the tree builder notes a dropped attribute `X` on its
# element: " X". No XML attribute has a space in its name.
_DROPPED_PREFIX = " "
# `_Work.spend_text`: one unit of the budget per this many characters of a
# shared string that is copied or compared once more.
_TEXT_UNIT = 4096

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
    # A type name longer than the models accept. V4 also: a property without
    # a Type (no type is assumed for it), and a navigation without one or
    # with a malformed `Collection(...)`, so that nothing says whether it
    # leads to one entity or to many.
    "invalid_type",
    "duplicate_name",  # entity_set / operation: an earlier one has the name
    "unrepresentable_key",  # entity_set: a key part is no (parsed) property of it
    # navigation: no association set (V2) / navigation property binding (V4)
    # gives one target entity set that is part of the result.
    "unresolved_target",
    "unsupported_http_method",  # operation (V2): m:HttpMethod is not GET or POST
    # operation: a parameter of the call with a bad name or type (V4: or
    # without a type).
    "invalid_parameter",
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
_TERM_COMPUTED = f"{_CORE}.Computed"
_TERM_IMMUTABLE = f"{_CORE}.Immutable"
# Last segment of every term the parser reads -> (vocabulary, full name).
# An annotation with any other term is never looked at again.
_TERMS = {
    term.rpartition(".")[2]: (term.rpartition(".")[0], term)
    for term in (
        _TERM_LABEL,
        _TERM_INSERT,
        _TERM_UPDATE,
        _TERM_DELETE,
        _TERM_FILTER,
        _TERM_OPTIONAL,
        _TERM_COMPUTED,
        _TERM_IMMUTABLE,
    )
}


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
class ParsedReturn:
    """What the document says an operation returns.

    ``entity_set`` is the NAME of an entity set of the result, given only
    when the document ties the return value to exactly one: the set the
    import names, when it is part of the result and of the declared type, or
    the only set of the result with that type (and then only when every
    entity set of the document was read). ``None`` means "not known": an
    operation whose return set cannot be resolved is still an operation.
    ``collection`` is whether ``Collection(...)`` is declared. ``type`` is
    the EDM name of a PRIMITIVE return type (``Edm.Int32``), ``""`` for
    anything else.
    """

    entity_set: str | None
    collection: bool
    type: str


@dataclass(frozen=True)
class ParsedOperation:
    name: str
    qualified_name: str
    kind: str
    http_method: str  # "GET" | "POST"
    bound_to: str | None
    parameters: tuple[ParamDef, ...]
    label: str
    # None when the document declares no (readable) return type. Not part of
    # what makes two parsed operations the same operation.
    returns: ParsedReturn | None = field(default=None, compare=False)


@dataclass(frozen=True)
class SkippedElement:
    """Something the document declares that the parser left out, and why.

    Never a name that failed the EDM rule or other document text: an
    element is skipped because its name or type is not something the
    definition models accept, so that text is exactly what must not travel
    on into a preview or a log. ``entity_set`` is a name that passed the
    rule (for kind ``entity_set`` the element's own, already valid, name).
    ``position`` is how the admin finds the element in the ``$metadata``. A
    property of a type that several entity sets share is recorded once
    (under the first set of the result with that type), a navigation once
    per set; the position is relative to the set's type chain.

    ``entity_type`` is the entity type of that set (for kind ``entity_set``
    the element's own), given only when it has the form of an EDM name, else
    ``""``; always ``""`` for an operation. It explains, it does not
    identify: two entries are equal when kind, entity set, position and
    reason are.
    """

    kind: str  # "entity_set" | "property" | "navigation" | "operation"
    entity_set: str  # the owning (or, for kind "entity_set", the own) set; "" when none or invalid
    position: int  # 1-based, see SKIP_REASONS for what is counted
    reason: str  # one of SKIP_REASONS
    entity_type: str = field(default="", compare=False)


@dataclass(frozen=True)
class ParsedMetadata:
    version: str
    entity_sets: tuple[ParsedEntitySet, ...]
    operations: tuple[ParsedOperation, ...]
    skipped: tuple[SkippedElement, ...] = ()
    # True when the document declares more entity sets or operations than
    # are built (`MAX_PARSED_ENTITY_SETS` / `MAX_PARSED_OPERATIONS`). What
    # lies past the cap was not read: it is in none of the tuples above, a
    # navigation to such a set is skipped as `unresolved_target`, and an
    # operation bound to its type is not bound.
    truncated: bool = False
    # How many `EntitySet` elements, and how many operation elements
    # (`FunctionImport` in V2, `Action` + `Function` in V4), the document
    # declares -- including those skipped and those past the cap.
    entity_sets_declared: int = 0
    operations_declared: int = 0
    # The work the parse spent, in the units of `MAX_PARSE_WORK`.
    work: int = 0
    # How many attribute values were longer than their cap and therefore not
    # kept (`_ATTRIBUTE_CAPS`); more than 0 means the result may lack what
    # those attributes said (a label, a skipped element, a namespace).
    attributes_dropped: int = 0


class _Work:
    """The work budget of one parse (`MAX_PARSE_WORK`)."""

    def __init__(self) -> None:
        self.spent = 0
        self.truncated = False
        self.attributes_dropped = 0
        # id(element) -> its children by tag. The elements live as long as
        # the tree, which outlives every use of this object.
        self._kids: dict[int, dict[str, list[ET.Element]]] = {}
        self._labels: dict[str, str] = {}

    def kids(self, element: ET.Element | None, tag: str) -> list[ET.Element] | tuple[()]:
        """The children of ``element`` with that tag, in document order.

        Grouped once per element and not charged: that is one pass over
        children the tree builder has already made, and it happens once.
        The caller pays for the items it then looks at.
        """
        if element is None or not len(element):
            return ()
        index = self._kids.get(id(element))
        if index is None:
            index = self._kids[id(element)] = {}
            for child in element:
                index.setdefault(child.tag, []).append(child)
        return index.get(tag, ())

    def first(self, element: ET.Element | None, tag: str) -> ET.Element | None:
        found = self.kids(element, tag)
        return found[0] if found else None

    def label(self, text: str | None) -> str:
        """``_clean_label``, once per distinct text."""
        if not text:
            return ""
        known = self._labels.get(text)
        if known is None:
            known = self._labels[text] = _clean_label(text)
        return known

    def spend_text(self, *texts: str) -> None:
        """Charge for strings that many elements share and that are copied
        or compared once more: one unit per ``_TEXT_UNIT`` characters."""
        units = sum(len(text) for text in texts) // _TEXT_UNIT
        if units:
            self.spend(units)

    def spend(self, units: int = 1) -> None:
        self.spent += units
        if self.spent > MAX_PARSE_WORK:
            raise MetadataError(
                f"the $metadata document is too large to read: its entity sets and "
                f"operations have more than {MAX_PARSE_WORK} properties, navigations "
                f"and parameters in all"
            )


class _Skips(list):  # of SkippedElement
    """A list of skipped elements that charges the work budget per entry:
    a document of nothing but invalid or duplicate elements is bounded too."""

    def __init__(self, work: _Work) -> None:
        super().__init__()
        self._work = work

    def append(self, item: SkippedElement) -> None:
        self._work.spend()
        super().append(item)

    def extend(self, items) -> None:  # type: ignore[no-untyped-def]
        items = list(items)
        self._work.spend(len(items))
        super().extend(items)


# --------------------------------------------------------------- safe parsing


def _refuse_dtd(*_args: object) -> None:
    raise MetadataError("the document contains a DTD or an entity declaration; refused")


def _local(name: str) -> str:
    # With a namespace separator expat reports "<uri> <local>".
    return name.rpartition(" ")[2]


def _parse_tree(xml: bytes, counts: dict[str, int] | None = None) -> tuple[ET.Element, str]:
    """The document as a tree of local names, plus the root's namespace.

    expat is driven directly instead of through ``ET.XMLParser`` so that the
    DTD handlers are ours: the C ``XMLParser`` does not expose its expat
    parser, and its default is to accept an internal DTD.

    This is where the input is bounded, once: at most ``MAX_ELEMENTS``
    elements, and no attribute value longer than its cap (``_ATTRIBUTE_CAPS``,
    else ``_MAX_ATTRIBUTE_CHARS``). A longer value is not kept -- never cut:
    cut, it would be a value the document never gave -- and its name is
    noted on the element (``_dropped``). ``counts["attributes_dropped"]`` is
    how many there were.
    """
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator=" ")
    depth = 0
    elements = 0
    dropped = 0
    limit = MAX_ELEMENTS
    root_namespace: list[str] = []
    # Per open element: the text collected for it, or None when its text is
    # not kept (any more). `room` is what the innermost element may still take.
    texts: list[list[str] | None] = []
    room = 0

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth, room, elements, dropped
        depth += 1
        if depth > MAX_DEPTH:
            raise MetadataError(f"the document is nested deeper than {MAX_DEPTH} levels")
        elements += 1
        if elements > limit:
            raise MetadataError(
                f"the $metadata document is too large to read: it has more than "
                f"{limit} elements"
            )
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
        for key in [
            k for k, v in flat.items() if len(v) > _ATTRIBUTE_CAPS.get(k, _MAX_ATTRIBUTE_CHARS)
        ]:
            # Absent, and noted: an annotation of the same local name that
            # was overwritten above is gone either way.
            del flat[key]
            flat[_DROPPED_PREFIX + key] = ""
            dropped += 1
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
        if chunks is None:
            return
        room -= len(text)
        if room < 0:
            texts[-1] = None  # too long to be a value: absent, not cut
        else:
            chunks.append(text)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    # EDMX 1.0 carries everything in attributes; text is kept for the few V4
    # expression elements in _TEXT_ELEMENTS only, and only up to the cap.
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
    if counts is not None:
        counts["attributes_dropped"] = dropped
    return root, (root_namespace[0] if root_namespace else "")


# -------------------------------------------------------------------- helpers


def _dropped(element: ET.Element, name: str) -> bool:
    """Whether the element HAD attribute ``name`` with a value too long to
    keep. Asked only where a missing attribute has a meaning of its own."""
    return element.get(_DROPPED_PREFIX + name) is not None


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
    what the line says. Other control characters become a space, and runs
    of white space one space. Reading stops with the character that fills
    ``MAX_LABEL_CHARS``: the rest of a long text decides nothing.
    """
    out: list[str] = []
    gap = False
    for ch in text or "":
        category = unicodedata.category(ch)
        if category == "Cf":
            continue
        if category == "Cc" or ch.isspace():
            gap = bool(out)
            continue
        if gap:
            out.append(" ")
            gap = False
        out.append(ch)
        if len(out) >= MAX_LABEL_CHARS:
            break
    return "".join(out)[:MAX_LABEL_CHARS]


def _label(element: ET.Element | None, work: _Work) -> str:
    """The ``sap:label`` of a V2 element."""
    return "" if element is None else work.label(element.get("label"))


def _safe_type(entity_type: str) -> str:
    """An entity type name for a skipped entry: only when it has the form
    of an EDM name, like every other text such an entry carries."""
    return entity_type if _NAME_RE.fullmatch(entity_type) else ""


def _name(element: ET.Element) -> str | None:
    """The element's ``Name`` when the definition models accept it, else ``None``.

    ``fullmatch``: with ``match`` the pattern's ``$`` also accepts a trailing
    newline.
    """
    value = element.get("Name")
    return value if value and _NAME_RE.fullmatch(value) else None


def _type_name(element: ET.Element, *, default: str = "Edm.String") -> str | None:
    """The ``Type``, or ``None`` when it is too long or missing without a default.

    ``Edm.String`` for a missing type is the V2 (CSDL 2.0) rule. V4 requires
    the attribute and passes ``default=""``: a guessed type would decide how
    a value is quoted in a URL.
    """
    raw = element.get("Type")
    if raw is None and _dropped(element, "Type"):
        return None  # there was a type, too long to keep: never the default
    value = (raw or "").strip() or default
    return value if value and len(value) <= _MAX_TYPE_CHARS else None


class _Schemas:
    """Qualified-name lookup over every ``Schema`` of the document.

    A reference may use the schema's namespace or its alias; both resolve to
    the canonical ``Namespace.Name``.
    """

    def __init__(self, schemas: list[ET.Element], work: _Work | None = None) -> None:
        self.elements = schemas
        self.work = work or _Work()
        self._chains: dict[int, list[ET.Element]] = {}
        self.entity_types: dict[str, tuple[str, ET.Element]] = {}
        self.associations: dict[str, tuple[str, ET.Element]] = {}
        self.containers: list[ET.Element] = []
        # id(element) -> canonical name, for entity types and containers. The
        # elements live as long as the tree, which outlives this object's use.
        self._names: dict[int, str] = {}
        self._namespaces: set[str] = set()
        # Alias -> namespace; of two schemas with one alias the first counts.
        self._aliases: dict[str, str] = {}
        # Up to the last dot of a reference -> how `canonical` rewrites it.
        self._prefixes: dict[str, tuple[int, str] | None] = {}
        for schema in schemas:
            namespace = (schema.get("Namespace") or "").strip()
            alias = (schema.get("Alias") or "").strip()
            prefixes = [p for p in (namespace, alias) if p]
            if namespace:
                self._namespaces.add(namespace)
                if alias:
                    self._aliases.setdefault(alias, namespace)
            # The schema's names are copied into every name below.
            shared = (len(namespace) + len(alias)) // _TEXT_UNIT
            for child in schema:
                if shared:
                    self.work.spend(shared)
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
        self._longest = max(map(len, (*self._namespaces, *self._aliases)), default=0)

    def name_of(self, element: ET.Element | None) -> str:
        """The canonical name of an entity type or container of the document."""
        return "" if element is None else self._names.get(id(element), "")

    def canonical(self, ref: str | None) -> str:
        """A qualified name with a schema alias replaced by its namespace.

        Purely textual, so it also serves names that have no lookup table
        here (actions, functions, containers). A name that starts with a
        schema's namespace is left alone; otherwise the longest alias it
        starts with is replaced. Both depend only on the name up to its
        last dot, so the answer is worked out once per such prefix and not
        by trying every schema for every reference.
        """
        ref = (ref or "").strip()
        cut = ref.rfind(".")
        if cut < 0:
            return ref
        prefix = ref[:cut]
        if prefix in self._prefixes:
            found = self._prefixes[prefix]
        else:
            found = self._prefixes[prefix] = self._resolve_prefix(prefix)
        if found is None:
            return ref
        length, namespace = found
        self.work.spend_text(namespace)
        return namespace + ref[length:]

    def _resolve_prefix(self, prefix: str) -> tuple[int, str] | None:
        """``(alias length, namespace)`` for a prefix that starts with an
        alias and with no namespace; else ``None``. Every head of the prefix
        that ends at a dot (or is all of it) is tried, shortest first, one
        unit each plus its length."""
        found: tuple[int, str] | None = None
        end = prefix.find(".")
        while True:
            stop = len(prefix) if end < 0 else end
            if stop > self._longest:
                break
            self.work.spend(1 + stop // _TEXT_UNIT)
            head = prefix[:stop]
            if head in self._namespaces:
                return None
            namespace = self._aliases.get(head)
            if namespace is not None:
                found = (stop, namespace)  # a longer alias replaces a shorter one
            if end < 0:
                break
            end = prefix.find(".", end + 1)
        return found

    def entity_type(self, ref: str | None) -> tuple[str, ET.Element] | None:
        return self.entity_types.get((ref or "").strip())

    def canonical_type(self, ref: str | None) -> str:
        found = self.entity_type(ref)
        return found[0] if found else (ref or "").strip()

    def chain(self, element: ET.Element) -> list[ET.Element]:
        """The type and its base types, base first. Built once per type and
        shared: callers only read it."""
        known = self._chains.get(id(element))
        if known is not None:
            return known
        chain = [element]
        seen = {id(element)}
        while True:
            self.work.spend()
            base = self.entity_type(chain[-1].get("BaseType"))
            if base is None or id(base[1]) in seen:
                break
            seen.add(id(base[1]))
            chain.append(base[1])
        chain.reverse()
        self._chains[id(element)] = chain
        return chain


# ---------------------------------------------------- entity sets, both versions


@dataclass
class _SetDraft:
    """An entity set that survived the first pass (name, type, fields, key)."""

    name: str
    element: ET.Element
    container: ET.Element
    type_element: ET.Element | None
    entity_type: str
    safe_type: str  # `entity_type` for a skipped entry, see `_safe_type`
    chain: list[ET.Element]
    fields: tuple[ParsedField, ...]
    keys: tuple[KeyDef, ...]


# (type chain, entity set name, container, EntitySet element) -> the fields,
# and (position, reason) for each property that was left out.
_FieldsOf = Callable[
    [list[ET.Element], str, ET.Element, ET.Element],
    tuple[tuple[ParsedField, ...], list[tuple[int, str]]],
]


def _keys(
    chain: list[ET.Element], fields: tuple[ParsedField, ...], work: _Work
) -> tuple[KeyDef, ...] | None:
    """The key, ``()`` for a type that declares none, ``None`` when it cannot be represented.

    A key part must be one of the parsed fields: a part that names no
    property (in V4 also a path into a complex property), or a property that
    was skipped, would leave an entity set whose rows cannot be addressed --
    and guessing a type for it would send wrongly quoted keys to SAP.
    """
    types = {f.name: f.type for f in fields}
    for entity_type in chain:  # the key is declared once, on the root of the hierarchy
        key = work.first(entity_type, "Key")
        if key is None:
            continue
        refs = work.kids(key, "PropertyRef")
        # A base type's key is read again for every type derived from it.
        work.spend(len(refs))
        names = list(dict.fromkeys(ref.get("Name") or "" for ref in refs))
        if any(name not in types for name in names):
            return None
        return tuple(KeyDef(name=name, type=types[name]) for name in names)
    return ()


_NO_KEY_MEMO = object()


def _entity_set_drafts(
    schemas: _Schemas, skipped: list[SkippedElement], fields_of: _FieldsOf
) -> tuple[dict[str, _SetDraft], int]:
    """The entity sets of the result, and how many the document declares.

    At most ``MAX_PARSED_ENTITY_SETS`` are built; the elements after that
    are counted only (``schemas.work.truncated``). The key is worked out
    once per entity type: every variant of a type's fields has the same
    names and types. A set of a type whose key is already known to be
    unusable is skipped without reading anything else of it -- such a set
    is never kept, so nothing stops a document from repeating it.
    """
    work = schemas.work
    drafts: dict[str, _SetDraft] = {}
    key_memo: dict[int, tuple[KeyDef, ...] | None] = {}
    declared = sum(len(work.kids(container, "EntitySet")) for container in schemas.containers)
    position = 0
    for container in schemas.containers:
        for element in work.kids(container, "EntitySet"):
            if len(drafts) >= MAX_PARSED_ENTITY_SETS:
                work.truncated = True
                return drafts, declared
            position += 1
            name = _name(element)
            if name is None:
                skipped.append(SkippedElement("entity_set", "", position, "invalid_name"))
                continue
            resolved = schemas.entity_type(element.get("EntityType"))
            entity_type = schemas.canonical_type(element.get("EntityType"))
            if name in drafts:
                skipped.append(
                    SkippedElement(
                        "entity_set", name, position, "duplicate_name", _safe_type(entity_type)
                    )
                )
                continue
            if len(entity_type) > _MAX_TYPE_CHARS or (
                not entity_type and _dropped(element, "EntityType")
            ):
                skipped.append(SkippedElement("entity_set", name, position, "invalid_type"))
                continue
            safe_type = _safe_type(entity_type)
            unusable = SkippedElement(
                "entity_set", name, position, "unrepresentable_key", safe_type
            )
            # A set whose type is not in the document is still listed (empty),
            # so the admin sees it exists instead of wondering where it went.
            chain = schemas.chain(resolved[1]) if resolved else []
            memo_key = id(chain[-1]) if chain else 0
            if key_memo.get(memo_key, _NO_KEY_MEMO) is None:
                skipped.append(unusable)
                continue
            fields, skipped_fields = fields_of(chain, name, container, element)
            keys = key_memo.get(memo_key, _NO_KEY_MEMO)  # type: ignore[assignment]
            if keys is _NO_KEY_MEMO:
                work.spend(len(fields))
                keys = key_memo[memo_key] = _keys(chain, fields, work)
            if keys is None:
                # One entry for the whole set; its properties are not listed.
                skipped.append(unusable)
                continue
            skipped.extend(
                SkippedElement("property", name, at, reason, safe_type)
                for at, reason in skipped_fields
            )
            drafts[name] = _SetDraft(
                name=name,
                element=element,
                container=container,
                type_element=resolved[1] if resolved else None,
                entity_type=entity_type,
                safe_type=safe_type,
                chain=chain,
                fields=fields,
                keys=keys,
            )
    return drafts, declared


def _sets_by_type(drafts: dict[str, _SetDraft]) -> dict[str, list[str]]:
    """Entity type -> the entity sets of the result that have it."""
    sets_by_type: dict[str, list[str]] = {}
    for draft in drafts.values():
        sets_by_type.setdefault(draft.entity_type, []).append(draft.name)
    return sets_by_type


_COLLECTION = "Collection("


@dataclass(frozen=True)
class _ReturnSets:
    """What a return type is resolved against: the kept entity sets, by name
    and by type. ``complete`` is false when entity sets of the document were
    left unread (the cap), so "the only set of this type" is not known."""

    drafts: dict[str, _SetDraft]
    by_type: dict[str, list[str]]
    complete: bool


def _parsed_return(
    type_ref: str | None, named: str | None, schemas: _Schemas, sets: _ReturnSets
) -> ParsedReturn | None:
    """A ``ReturnType`` and the entity set the document names for it.

    ``named`` is ``None`` when the document names no set, ``""`` when it
    names one that is not an entity set of the result, else that set's name.
    A named set counts only when it has exactly the declared entity type;
    without one, the only kept set of the type is taken -- never one of
    several, and never when sets were left unread. Dictionary lookups on one
    attribute value: nothing here grows with the document.
    """
    type_ref = (type_ref or "").strip()
    collection = type_ref.startswith(_COLLECTION)
    if collection != type_ref.endswith(")"):
        return None  # half a `Collection(...)`: one or many would be a guess
    inner = type_ref[len(_COLLECTION) : -1].strip() if collection else type_ref
    if not inner or len(inner) > _MAX_TYPE_CHARS:
        return None
    if inner.startswith("Edm."):
        return ParsedReturn(None, collection, inner) if _NAME_RE.fullmatch(inner) else None
    entity_type = schemas.canonical_type(inner)
    if named is None:
        candidates = sets.by_type.get(entity_type, [])
        named = candidates[0] if sets.complete and len(candidates) == 1 else ""
    elif named and sets.drafts[named].entity_type != entity_type:
        named = ""
    return ParsedReturn(named or None, collection, "")


# ------------------------------------------------------------------------- V2


def _v2_property(prop: ET.Element, work: _Work) -> ParsedField | str:
    """One ``Property`` as a field, or the reason code it is skipped for."""
    name, type_name = _name(prop), _type_name(prop)
    if name is None or type_name is None:
        return "invalid_name" if name is None else "invalid_type"
    return ParsedField(
        name=name,
        type=type_name,
        label=_label(prop, work),
        filterable=_flag(prop, "filterable"),
        creatable=_flag(prop, "creatable"),
        updatable=_flag(prop, "updatable"),
        nullable=(prop.get("Nullable") or "").strip().lower() != "false",
    )


def _v2_fields_of(work: _Work) -> _FieldsOf:
    """The fields of a type chain, once per entity type. In V2 a field
    depends on its type alone, so the sets of one type share one tuple; the
    type's skipped properties are handed out with the first of them only.
    Each ``Property`` element is read once, by the type that declares it;
    a derived type only walks over what its base types already gave."""
    memo: dict[int, tuple[ParsedField, ...]] = {}
    own: dict[int, list[ParsedField | str]] = {}

    def fields_of(
        chain: list[ET.Element], _name: str, _container: ET.Element, _element: ET.Element
    ) -> tuple[tuple[ParsedField, ...], list[tuple[int, str]]]:
        if not chain:
            return (), []
        known = memo.get(id(chain[-1]))
        if known is not None:
            return known, []
        fields: dict[str, ParsedField] = {}
        skipped: list[tuple[int, str]] = []
        position = 0
        for entity_type in chain:
            declared = own.get(id(entity_type))
            if declared is None:
                declared = own[id(entity_type)] = [
                    _v2_property(prop, work) for prop in work.kids(entity_type, "Property")
                ]
            for parsed in declared:
                work.spend()
                position += 1
                if isinstance(parsed, str):
                    skipped.append((position, parsed))
                else:
                    fields.setdefault(parsed.name, parsed)
        memo[id(chain[-1])] = tuple(fields.values())
        return memo[id(chain[-1])], skipped

    return fields_of


# One `NavigationProperty` as its type declares it: `None` for a name that
# is not usable, else (name, association, from role, to many) -- with
# association `None` when the relationship or its `ToRole` end is unknown.
_V2Nav = tuple[str, str | None, str, bool] | None


def _v2_own_navigations(schemas: _Schemas) -> Callable[[ET.Element], list[_V2Nav]]:
    """The navigation properties an entity type declares, read once per
    type; and each association's ends by role, read once per association
    (the first end of a role counts)."""
    work = schemas.work
    own: dict[int, list[_V2Nav]] = {}
    roles: dict[int, dict[str, ET.Element]] = {}

    def end_of(association: ET.Element, role: str) -> ET.Element | None:
        ends = roles.get(id(association))
        if ends is None:
            ends = roles[id(association)] = {}
            for end in work.kids(association, "End"):
                declared = end.get("Role")
                if declared is not None:
                    ends.setdefault(declared, end)
        return ends.get(role)

    def navigations(entity_type: ET.Element) -> list[_V2Nav]:
        known = own.get(id(entity_type))
        if known is not None:
            return known
        known = own[id(entity_type)] = []
        for nav in work.kids(entity_type, "NavigationProperty"):
            name = _name(nav)
            if name is None:
                known.append(None)
                continue
            association = schemas.associations.get((nav.get("Relationship") or "").strip())
            end = (
                end_of(association[1], nav.get("ToRole") or "")
                if association is not None
                else None
            )
            if association is None or end is None:
                known.append((name, None, "", False))
            else:
                known.append(
                    (
                        name,
                        association[0],
                        nav.get("FromRole") or "",
                        (end.get("Multiplicity") or "").strip() == "*",
                    )
                )
        return known

    return navigations


def _v2_navigations(
    draft: _SetDraft,
    work: _Work,
    own: Callable[[ET.Element], list[_V2Nav]],
    targets: dict[tuple[str, str, str], str],
) -> tuple[tuple[ParsedNavigation, ...], list[SkippedElement]]:
    navigations: dict[str, ParsedNavigation] = {}
    skipped: list[SkippedElement] = []
    position = 0
    for entity_type in draft.chain:
        for nav in own(entity_type):
            work.spend()
            position += 1
            if nav is None:
                skipped.append(
                    SkippedElement(
                        "navigation", draft.name, position, "invalid_name", draft.safe_type
                    )
                )
                continue
            name, association, from_role, collection = nav
            # Only an association set says which entity SET the other end is
            # (several sets can share one entity type); without one for this
            # set the navigation has no known target and is not offered.
            target = None
            if association is not None:
                work.spend_text(from_role)  # compared again for every set of the type
                target = targets.get((association, from_role, draft.name))
            if not target:
                skipped.append(
                    SkippedElement(
                        "navigation", draft.name, position, "unresolved_target", draft.safe_type
                    )
                )
                continue
            navigations.setdefault(
                name, ParsedNavigation(name=name, target=target, collection=collection)
            )
    return tuple(navigations.values()), skipped


def _v2_operation(
    element: ET.Element,
    schemas: _Schemas,
    sets_by_type: dict[str, list[str]],
    container: ET.Element,
    sets: _ReturnSets,
) -> ParsedOperation | str:
    """The function import, or the reason code it is skipped for."""
    work = schemas.work
    name = _name(element)
    if name is None:
        return "invalid_name"
    http_method = (element.get("HttpMethod") or "").strip().upper() or "GET"
    if http_method not in _HTTP_METHODS or _dropped(element, "HttpMethod"):
        # PUT / DELETE / MERGE exist in V2, but the tools call an operation
        # with GET or POST only, and `OperationDef` stores nothing else.
        return "unsupported_http_method"
    parameters: dict[str, ParamDef] = {}
    for param in work.kids(element, "Parameter"):
        work.spend()
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
    # The set of the RETURN value: a set of this import's own container.
    named = element.get("EntitySet")
    if named is not None:
        found = sets.drafts.get(named.strip())
        named = found.name if found is not None and found.container is container else ""
    elif _dropped(element, "EntitySet"):
        named = ""  # a set was named: the type alone does not decide
    return ParsedOperation(
        name=name,
        qualified_name="",  # a V2 function import is addressed by its plain name
        kind="function_import",
        http_method=http_method,
        bound_to=candidates[0] if element.get("action-for") and len(candidates) == 1 else None,
        parameters=tuple(parameters.values()),
        label=_label(element, work),
        returns=_parsed_return(element.get("ReturnType"), named, schemas, sets),
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
    work = schemas.work
    targets: dict[tuple[str, str, str], str] = {}
    for container in schemas.containers:
        for element in work.kids(container, "AssociationSet"):
            association = schemas.associations.get((element.get("Association") or "").strip())
            if association is None:
                continue
            ends = [
                (end.get("Role") or "", end.get("EntitySet") or "")
                for end in work.kids(element, "End")
            ]
            # Every pair of ends is looked at: two in a real association set.
            work.spend(len(ends) * len(ends))
            for from_role, from_set in ends:
                for to_role, to_set in ends:
                    if from_role == to_role or from_set not in drafts or to_set not in drafts:
                        continue
                    key = (association[0], from_role, from_set)
                    if targets.setdefault(key, to_set) != to_set:
                        targets[key] = ""  # stays ambiguous whatever follows
    return targets


def _parse_v2(schemas: _Schemas) -> ParsedMetadata:
    work = schemas.work
    skipped: list[SkippedElement] = _Skips(work)
    # Pass 1: which entity sets exist in the result. Navigations and bindings
    # are resolved afterwards, against these only.
    drafts, sets_declared = _entity_set_drafts(schemas, skipped, _v2_fields_of(work))
    sets_by_type = _sets_by_type(drafts)
    return_sets = _ReturnSets(drafts, sets_by_type, complete=not work.truncated)
    targets = _v2_navigation_targets(schemas, drafts)
    own_navigations = _v2_own_navigations(schemas)

    entity_sets: list[ParsedEntitySet] = []
    for draft in drafts.values():
        navigations, skipped_navigations = _v2_navigations(draft, work, own_navigations, targets)
        skipped.extend(skipped_navigations)
        entity_sets.append(
            ParsedEntitySet(
                name=draft.name,
                entity_type=draft.entity_type,
                label=_label(draft.element, work) or _label(draft.type_element, work),
                keys=draft.keys,
                fields=draft.fields,
                navigations=navigations,
                creatable=_flag(draft.element, "creatable"),
                updatable=_flag(draft.element, "updatable"),
                deletable=_flag(draft.element, "deletable"),
            )
        )

    operations: dict[str, ParsedOperation] = {}
    operations_declared = sum(
        len(work.kids(container, "FunctionImport")) for container in schemas.containers
    )
    position = 0
    for container in schemas.containers:
        for element in work.kids(container, "FunctionImport"):
            if len(operations) >= MAX_PARSED_OPERATIONS:
                work.truncated = True
                break
            position += 1
            operation = _v2_operation(element, schemas, sets_by_type, container, return_sets)
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
        truncated=work.truncated,
        entity_sets_declared=sets_declared,
        operations_declared=operations_declared,
        work=work.spent,
        attributes_dropped=work.attributes_dropped,
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

    Only the terms in ``_TERMS`` are ever asked for, so the annotations are
    sorted by term once -- per target over all its blocks, and per element
    when it is first asked about -- and an annotation of any other term is
    not looked at again. A lookup costs one unit per annotation it returns.
    """

    def __init__(self, root: ET.Element, schemas: _Schemas) -> None:
        self._schemas = schemas
        self._work = work = schemas.work
        self._aliases = dict(_DEFAULT_ALIASES)
        for include in root.iter("Include"):
            namespace = (include.get("Namespace") or "").strip()
            alias = (include.get("Alias") or "").strip()
            if namespace and alias:
                self._aliases[alias] = namespace
        # target -> term -> annotations, in document order over all blocks.
        self._blocks: dict[str, dict[str, list[ET.Element]]] = {}
        # id(element) -> term -> its own annotations.
        self._inline: dict[int, dict[str, list[ET.Element]]] = {}
        # id(Record) -> Property -> its PropertyValue children.
        self._values: dict[int, dict[str | None, list[ET.Element]]] = {}
        for schema in schemas.elements:
            for block in work.kids(schema, "Annotations"):
                if not block.get("Qualifier") and not _dropped(block, "Qualifier"):
                    target = self._target(block.get("Target"))
                    self._sort(block, self._blocks.setdefault(target, {}))

    def _target(self, raw: str | None) -> str:
        """``alias.Name(signature)/rest`` -> ``namespace.Name/rest``."""
        head, slash, rest = (raw or "").strip().partition("/")
        return self._schemas.canonical(head.partition("(")[0]) + slash + rest

    def target(self, owner: str, name: str = "") -> str:
        """The target of ``owner`` (a type, an operation) or of ``name``
        in it (a property, an entity set, an import), as blocks name it."""
        self._work.spend_text(owner)  # copied for every name in it
        return f"{owner}/{name}" if name else owner

    def _term(self, raw: str | None) -> str | None:
        """The full name of the term when it is one the parser reads."""
        prefix, _, name = (raw or "").strip().rpartition(".")
        known = _TERMS.get(name)
        if known is None or self._aliases.get(prefix, prefix) != known[0]:
            return None
        return known[1]

    def _sort(self, source: ET.Element, into: dict[str, list[ET.Element]]) -> None:
        for annotation in self._work.kids(source, "Annotation"):
            if annotation.get("Qualifier") or _dropped(annotation, "Qualifier"):
                continue
            term = self._term(annotation.get("Term"))
            if term is not None:
                into.setdefault(term, []).append(annotation)

    def find_all(self, term: str, element: ET.Element | None, target: str = "") -> list[ET.Element]:
        """Every annotation with that term: on the element, then in the blocks for ``target``.

        All of them, because a document may say one thing twice (inline and
        in a block, or in two blocks) and the readers below must not let the
        first one hide a restriction in a later one.
        """
        found: list[ET.Element] = []
        if element is not None and len(element):
            inline = self._inline.get(id(element))
            if inline is None:
                inline = self._inline[id(element)] = {}
                self._sort(element, inline)
            found.extend(inline.get(term, ()))
        if target:
            self._work.spend_text(target)  # compared with the block's own copy
            block = self._blocks.get(target)
            if block:
                found.extend(block.get(term, ()))
        if found:
            self._work.spend(len(found))
        return found

    def _expression(self, element: ET.Element, kind: str) -> str | None:
        """A constant, given as attribute (``String="x"``) or as child (``<String>x</String>``).

        ``""`` for a child element without usable text (empty, or longer than
        the text cap), which is no value to any reader here.
        """
        value = element.get(kind)
        if value is not None:
            return value
        child = self._work.first(element, kind)
        return None if child is None else child.text or ""

    def label(self, element: ET.Element | None, target: str = "") -> str:
        """The first ``Common.Label`` that says something."""
        for annotation in self.find_all(_TERM_LABEL, element, target):
            label = self._work.label(self._expression(annotation, "String"))
            if label:
                return label
        return ""

    def _record_values(
        self, term: str, prop: str, element: ET.Element | None, target: str
    ) -> list[ET.Element]:
        work = self._work
        values: list[ET.Element] = []
        for annotation in self.find_all(term, element, target):
            records = work.kids(annotation, "Record")
            work.spend(len(records))
            for record in records:
                by_property = self._values.get(id(record))
                if by_property is None:
                    by_property = self._values[id(record)] = {}
                    for value in work.kids(record, "PropertyValue"):
                        by_property.setdefault(value.get("Property"), []).append(value)
                matching = by_property.get(prop, ())
                work.spend(len(matching))
                values.extend(matching)
        return values

    def declared_false(
        self, term: str, prop: str, element: ET.Element | None, target: str = ""
    ) -> bool:
        """Whether a restriction record sets ``prop`` to a constant ``false``.

        Absent, or computed from a path, is not a declared "no": the flag
        stays ``True`` like a missing ``sap:`` annotation in V2. Among
        duplicates one constant ``false`` is enough, wherever it stands: a
        capability can only go down. What SAP declares is information for
        the admin, not a permission.
        """
        return any(
            (self._expression(value, "Bool") or "").strip().lower() == "false"
            for value in self._record_values(term, prop, element, target)
        )

    def property_paths(
        self, term: str, prop: str, element: ET.Element | None, target: str = ""
    ) -> set[str]:
        """The property names a restriction record lists under ``prop``, over all duplicates."""
        work = self._work
        names: set[str] = set()
        for value in self._record_values(term, prop, element, target):
            collections = work.kids(value, "Collection")
            work.spend(len(collections))
            for collection in collections:
                paths = work.kids(collection, "PropertyPath")
                work.spend(len(paths))
                names.update((path.text or "").strip() for path in paths)
        return names

    def declared_true(self, term: str, element: ET.Element | None, target: str = "") -> bool:
        """Whether a boolean tag term (``Core.Computed``) is set, by any of its duplicates.

        Such a term is true when it is merely present. ``Bool="false"`` and
        a value computed from a path are not a declared "yes".
        """
        for annotation in self.find_all(term, element, target):
            value = self._expression(annotation, "Bool")
            if value is None:
                # Present without any value expression: the default, true.
                # (The count first: the attributes are the document's.)
                if (
                    len(annotation) == 0
                    and len(annotation.attrib) <= 1
                    and set(annotation.keys()) <= {"Term"}
                ):
                    return True
            elif value.strip().lower() == "true":
                return True
        return False


# One property as its type declares it: a skip reason, or
# (name, type, label, computed, immutable, nullable).
_V4Own = tuple[str | None, tuple[str, str, str, bool, bool, bool] | None]
# The same as every set of a type chain sees it, with its position in the chain.
_V4Prop = tuple[int, str | None, tuple[str, str, str, bool, bool, bool] | None]


def _v4_own_properties(
    entity_type: ET.Element, schemas: _Schemas, annotations: _Annotations
) -> list[_V4Own]:
    """What a type says about the properties it declares itself."""
    declaring_type = schemas.name_of(entity_type)
    properties: list[_V4Own] = []
    for prop in schemas.work.kids(entity_type, "Property"):
        name, type_name = _name(prop), _type_name(prop, default="")
        if name is None or type_name is None:
            properties.append(("invalid_name" if name is None else "invalid_type", None))
            continue
        # A block annotates the property on the type that declares it.
        target = annotations.target(declaring_type, name)
        properties.append(
            (
                None,
                (
                    name,
                    type_name,
                    annotations.label(prop, target),
                    # Computed: the server sets it, a client never does.
                    # Immutable: a client may set it when creating, not
                    # afterwards.
                    annotations.declared_true(_TERM_COMPUTED, prop, target),
                    annotations.declared_true(_TERM_IMMUTABLE, prop, target),
                    (prop.get("Nullable") or "").strip().lower() != "false",
                ),
            )
        )
    return properties


def _v4_fields_of(schemas: _Schemas, annotations: _Annotations) -> _FieldsOf:
    """The fields of an entity set, built once per type and set-level variant.

    The restrictions below belong to the entity SET, not to the type: the
    same property can be filterable in one set and not in another. Sets of
    one type that declare the same restrictions (most declare none) share
    one tuple, and the type's skipped properties are handed out with the
    first set only. Each ``Property`` element is read once, by the type
    that declares it.
    """
    work = schemas.work
    own: dict[int, list[_V4Own]] = {}
    by_type: dict[int, list[_V4Prop]] = {}
    variants: dict[tuple, tuple[ParsedField, ...]] = {}

    def type_properties(chain: list[ET.Element]) -> list[_V4Prop]:
        """What the TYPE says about each property; the same for every set of it."""
        properties: list[_V4Prop] = []
        for entity_type in chain:
            declared = own.get(id(entity_type))
            if declared is None:
                declared = own[id(entity_type)] = _v4_own_properties(
                    entity_type, schemas, annotations
                )
            for reason, parsed in declared:
                work.spend()
                properties.append((len(properties) + 1, reason, parsed))
        return properties

    def fields_of(
        chain: list[ET.Element], set_name: str, container: ET.Element, set_element: ET.Element
    ) -> tuple[tuple[ParsedField, ...], list[tuple[int, str]]]:
        if not chain:
            return (), []
        set_target = annotations.target(schemas.name_of(container), set_name)

        def listed(term: str, prop: str) -> frozenset[str]:
            return frozenset(annotations.property_paths(term, prop, set_element, set_target))

        nothing_filterable = annotations.declared_false(
            _TERM_FILTER, "Filterable", set_element, set_target
        )
        non_filterable = listed(_TERM_FILTER, "NonFilterableProperties")
        non_insertable = listed(_TERM_INSERT, "NonInsertableProperties")
        non_updatable = listed(_TERM_UPDATE, "NonUpdatableProperties")

        type_id = id(chain[-1])
        skipped: list[tuple[int, str]] = []
        properties = by_type.get(type_id)
        if properties is None:
            properties = by_type[type_id] = type_properties(chain)
            skipped = [
                (position, reason) for position, reason, _ in properties if reason is not None
            ]
        variant = (type_id, nothing_filterable, non_filterable, non_insertable, non_updatable)
        known = variants.get(variant)
        if known is not None:
            return known, skipped
        work.spend(len(properties))
        fields: dict[str, ParsedField] = {}
        for _position, _reason, parsed in properties:
            if parsed is None:
                continue
            name, type_name, label, computed, immutable, nullable = parsed
            fields.setdefault(
                name,
                ParsedField(
                    name=name,
                    type=type_name,
                    label=label,
                    filterable=not nothing_filterable and name not in non_filterable,
                    creatable=not computed and name not in non_insertable,
                    updatable=not computed and not immutable and name not in non_updatable,
                    nullable=nullable,
                ),
            )
        variants[variant] = tuple(fields.values())
        return variants[variant], skipped

    return fields_of


def _v4_bindings(
    draft: _SetDraft, schemas: _Schemas, drafts: dict[str, _SetDraft]
) -> dict[str, str]:
    """Navigation property name -> target entity set (``""`` when there is none to trust).

    Only a binding whose path is the navigation property itself counts. Its
    target is an entity set of the result that lives where the binding says:
    ``Container/EntitySet`` in the container named, a bare ``EntitySet`` in
    the binding's own container. Anything else (an unknown or skipped set, a
    set of that name in another container, a longer path to a contained
    entity) is no target, and bindings of one path that do not agree on one
    valid target resolve nothing.
    """
    bindings: dict[str, str] = {}
    for binding in schemas.work.kids(draft.element, "NavigationPropertyBinding"):
        schemas.work.spend()
        path = (binding.get("Path") or "").strip()
        if not path:
            continue
        head, slash, rest = (binding.get("Target") or "").strip().partition("/")
        found = drafts.get(rest if slash else head)
        if found is None:
            target = ""
        elif slash:
            named = schemas.canonical(head)
            target = found.name if named and schemas.name_of(found.container) == named else ""
        else:
            target = found.name if found.container is draft.container else ""
        if bindings.setdefault(path, target) != target:
            bindings[path] = ""  # two different answers: not guessed
    return bindings


# One `NavigationProperty` as its type declares it: (name, the reason it
# cannot be offered whatever the set, to many).
_V4Nav = tuple[str | None, str | None, bool]


def _v4_own_navigations(work: _Work) -> Callable[[ET.Element], list[_V4Nav]]:
    """The navigation properties an entity type declares, read once per type."""
    own: dict[int, list[_V4Nav]] = {}

    def navigations(entity_type: ET.Element) -> list[_V4Nav]:
        known = own.get(id(entity_type))
        if known is not None:
            return known
        known = own[id(entity_type)] = []
        for nav in work.kids(entity_type, "NavigationProperty"):
            name = _name(nav)
            type_ref = (nav.get("Type") or "").strip()
            collection = type_ref.startswith("Collection(")
            reason = None
            if name is None:
                reason = "invalid_name"
            elif not type_ref or collection != type_ref.endswith(")"):
                # No Type, or half a `Collection(...)`: the multiplicity
                # would be a guess.
                reason = "invalid_type"
            known.append((name, reason, collection))
        return known

    return navigations


def _v4_navigations(
    draft: _SetDraft,
    bindings: dict[str, str],
    work: _Work,
    own: Callable[[ET.Element], list[_V4Nav]],
) -> tuple[tuple[ParsedNavigation, ...], list[SkippedElement]]:
    navigations: dict[str, ParsedNavigation] = {}
    skipped: list[SkippedElement] = []
    position = 0
    for entity_type in draft.chain:
        for name, reason, collection in own(entity_type):
            work.spend()
            position += 1
            target = bindings.get(name or "")
            if reason is None and not target:
                reason = "unresolved_target"
            if reason is None and name is not None and target:
                navigations.setdefault(
                    name, ParsedNavigation(name=name, target=target, collection=collection)
                )
                continue
            skipped.append(
                SkippedElement(
                    "navigation", draft.name, position, reason or "", draft.safe_type
                )
            )
    return tuple(navigations.values()), skipped


def _v4_parameters(
    element: ET.Element, kind: str, bound: bool, annotations: _Annotations, work: _Work
) -> tuple[ParamDef, ...] | None:
    """The parameters of the call, ``None`` when one cannot be represented.

    The first parameter of a bound operation is the entity it is called on
    (part of the URL), not an argument.
    """
    parameters: dict[str, ParamDef] = {}
    for param in work.kids(element, "Parameter")[1 if bound else 0 :]:
        work.spend()
        name, type_name = _name(param), _type_name(param, default="")
        if name is None or type_name is None:
            return None
        if kind == "action":
            # An action's nullable parameter may be left out of the body.
            required = (param.get("Nullable") or "").strip().lower() == "false"
        else:
            # A function's parameters are part of the URL: all of them, unless
            # the service marks one as optional.
            required = not annotations.find_all(_TERM_OPTIONAL, param)
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
    import_labels: dict[int, str],
    sets: _ReturnSets,
) -> list[ParsedOperation | str]:
    """What one ``Action`` / ``Function`` gives: operations, or reason codes.

    A bound one is one operation, addressed by its qualified name on an
    entity. An unbound one is callable only through an import in the
    container and is addressed by the import's name -- one operation per
    import, none without.

    Overloads share one qualified name. The caller keeps the first operation
    of a name (``duplicate_name`` for the rest), and a label given in an
    ``Annotations`` block attaches to every overload of that name, because
    the block's signature is not compared. An import's own label is read
    once (``import_labels``), however many overloads it is an import of.
    """
    work = schemas.work
    name = _name(element)
    work.spend_text(namespace)  # copied into every qualified name of the schema
    qualified = f"{namespace}.{name}" if namespace else name or ""
    # models.OperationDef holds the qualified name to the same EDM rule.
    if name is None or not _NAME_RE.fullmatch(qualified):
        return ["invalid_name"]
    bound = (element.get("IsBound") or "").strip().lower() == "true"
    parameters = _v4_parameters(element, kind, bound, annotations, work)
    if parameters is None:
        return ["invalid_parameter"]
    http_method = "POST" if kind == "action" else "GET"
    label = annotations.label(element, annotations.target(qualified))
    returned = work.first(element, "ReturnType")
    return_type = None if returned is None else returned.get("Type")

    if bound:
        binding = work.first(element, "Parameter")
        type_ref = "" if binding is None else (binding.get("Type") or "").strip()
        # `sets_by_type` holds kept sets only. Several sets of the type, or a
        # collection binding (called on the set, without a key), have no
        # place in `bound_to` + key; not guessed.
        candidates = sets_by_type.get(schemas.canonical_type(type_ref), [])
        if len(candidates) != 1:
            return ["unsupported_binding"]
        # `EntitySetPath` that is the binding parameter itself: the set the
        # operation is called on comes back. A longer path (through a
        # navigation) is not followed; without one the type decides.
        path = (element.get("EntitySetPath") or "").strip()
        own = binding is not None and path and path == (binding.get("Name") or "").strip()
        named = candidates[0] if own else None
        if not own and (path or _dropped(element, "EntitySetPath")):
            named = ""
        return [
            ParsedOperation(
                name=name,
                qualified_name=qualified,
                kind=kind,
                http_method=http_method,
                bound_to=candidates[0],
                parameters=parameters,
                label=label,
                returns=_parsed_return(return_type, named, schemas, sets),
            )
        ]

    outcomes: list[ParsedOperation | str] = []
    for container, imported in imports.get((kind, qualified), []):
        work.spend()
        import_name = _name(imported)
        if import_name is None:
            outcomes.append("invalid_name")
            continue
        own_label = import_labels.get(id(imported))
        if own_label is None:
            own_label = import_labels[id(imported)] = annotations.label(
                imported, annotations.target(schemas.name_of(container), import_name)
            )
        # The import's `EntitySet`: a set of its own container, or
        # `Container/EntitySet` in the container named (as a binding target).
        named = imported.get("EntitySet")
        if named is not None:
            head, slash, rest = named.strip().partition("/")
            found = sets.drafts.get(rest if slash else head)
            if found is None:
                named = ""
            elif slash:
                place = schemas.canonical(head)
                named = found.name if place and schemas.name_of(found.container) == place else ""
            else:
                named = found.name if found.container is container else ""
        elif _dropped(imported, "EntitySet"):
            named = ""
        outcomes.append(
            ParsedOperation(
                name=import_name,
                qualified_name=qualified,
                kind=kind,
                http_method=http_method,
                bound_to=None,
                parameters=parameters,
                label=own_label or label,
                returns=_parsed_return(return_type, named, schemas, sets),
            )
        )
    return outcomes or ["not_imported"]


def _parse_v4(root: ET.Element, schemas: _Schemas) -> ParsedMetadata:
    annotations = _Annotations(root, schemas)
    work = schemas.work
    skipped: list[SkippedElement] = _Skips(work)

    # Pass 1: which entity sets exist in the result. Navigations and bindings
    # are resolved afterwards, against these only.
    drafts, sets_declared = _entity_set_drafts(
        schemas, skipped, _v4_fields_of(schemas, annotations)
    )
    sets_by_type = _sets_by_type(drafts)
    return_sets = _ReturnSets(drafts, sets_by_type, complete=not work.truncated)
    own_navigations = _v4_own_navigations(work)
    # id(EntityType) -> its label: the fallback of every set of the type.
    type_labels: dict[int, str] = {}

    def type_label(draft: _SetDraft) -> str:
        if draft.type_element is None:
            return annotations.label(None, annotations.target(draft.entity_type))
        known = type_labels.get(id(draft.type_element))
        if known is None:
            known = type_labels[id(draft.type_element)] = annotations.label(
                draft.type_element, annotations.target(draft.entity_type)
            )
        return known

    entity_sets: list[ParsedEntitySet] = []
    for draft in drafts.values():
        target = annotations.target(schemas.name_of(draft.container), draft.name)
        navigations, skipped_navigations = _v4_navigations(
            draft, _v4_bindings(draft, schemas, drafts), work, own_navigations
        )
        skipped.extend(skipped_navigations)
        entity_sets.append(
            ParsedEntitySet(
                name=draft.name,
                entity_type=draft.entity_type,
                label=annotations.label(draft.element, target) or type_label(draft),
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
    import_labels: dict[int, str] = {}

    operations: dict[str, ParsedOperation] = {}
    operations_declared = sum(
        1
        for schema in schemas.elements
        for element in schema
        if element.tag in ("Action", "Function")
    )
    position = 0
    for schema in schemas.elements:
        namespace = (schema.get("Namespace") or "").strip()
        for element in schema:
            kind = {"Action": "action", "Function": "function"}.get(element.tag)
            if kind is None:
                continue
            if len(operations) >= MAX_PARSED_OPERATIONS:
                work.truncated = True
                break
            position += 1
            for outcome in _v4_operation(
                element,
                kind,
                namespace,
                schemas,
                annotations,
                sets_by_type,
                imports,
                import_labels,
                return_sets,
            ):
                if isinstance(outcome, str):
                    skipped.append(SkippedElement("operation", "", position, outcome))
                elif outcome.name in operations:
                    # `OperationDef` names are unique: of several overloads
                    # (or bound actions of one name on different entities)
                    # only the first is offered. Its label may come from any
                    # of them: an `Annotations` block targets an operation by
                    # qualified name and the signature is not compared.
                    skipped.append(SkippedElement("operation", "", position, "duplicate_name"))
                elif len(operations) >= MAX_PARSED_OPERATIONS:
                    # The cap is on operations BUILT: one unbound action can
                    # have any number of imports. The rest of them is not read.
                    work.truncated = True
                    break
                else:
                    operations[outcome.name] = outcome

    return ParsedMetadata(
        version="v4",
        entity_sets=tuple(entity_sets),
        operations=tuple(operations.values()),
        skipped=tuple(skipped),
        truncated=work.truncated,
        entity_sets_declared=sets_declared,
        operations_declared=operations_declared,
        work=work.spent,
        attributes_dropped=work.attributes_dropped,
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
    if isinstance(xml, memoryview):
        xml = bytes(xml)
    # bytes and bytearray are parsed as they are: no second copy of a
    # document that may be `MAX_METADATA_BYTES` long.
    if len(xml) > MAX_METADATA_BYTES:
        raise MetadataError(f"the $metadata document is larger than {MAX_METADATA_BYTES} bytes")
    if _DTD_RE.search(xml):
        _refuse_dtd()

    counts: dict[str, int] = {}
    root, namespace = _parse_tree(xml, counts)
    if root.tag != "Edmx":
        raise MetadataError(_NOT_EDMX)
    # EDMX 4.0 has its own (OASIS) namespace; every older one is Microsoft's.
    # The Version attribute decides nothing: it is free text in the document.
    is_v4 = namespace == _EDMX_V4_NAMESPACE
    if is_v4 and version == "v2":
        raise MetadataError("the document is OData V4, not V2")
    if not is_v4 and version == "v4":
        raise MetadataError("the document is OData V2, not V4")
    schemas = list(root.iter("Schema"))
    if not schemas:
        raise MetadataError(_NOT_EDMX)
    work = _Work()
    work.attributes_dropped = counts.get("attributes_dropped", 0)
    if is_v4:
        return _parse_v4(root, _Schemas(schemas, work))
    return _parse_v2(_Schemas(schemas, work))
