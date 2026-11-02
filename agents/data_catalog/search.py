"""Find the catalogue nodes that answer a question, inside one scope.

The scope (``allowed_sources``) is applied FIRST, in the statement and again
on every row read: a node of another source is never scored, never ranked
and never named, so nothing about an unattached source leaks through a
score, an order or a relation. Then:

* with a question vector and node vectors (``agents.data_catalog.embeddings``)
  the score is the cosine similarity;
* otherwise (no model, no vector for a node) the score is lexical: the
  question's words against the node's name, label and synonyms (weight 3),
  purpose and examples (2), its readable fields' names, labels and synonyms
  (1 each, at most 5: the field-of-source boost) and its source's label,
  purpose and synonyms (1), normalised to 0..1.

Candidates are entities and functions an agent can read or change. A field
that is not readable adds nothing and is never named; a relation to a node
outside the scope is dropped. Hints inherit: an entity or function without
a purpose or "not for" shows its source's; a node without a label shows its
name.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.access import BASIC, agent_allowed
from agents.data_catalog.embeddings import cosine, embed_question, resolve_model, unpack
from agents.data_catalog.importer import batched
from agents.data_catalog.models import DataCatalogEdge, DataCatalogNode, DataCatalogVector
from agents.odata.search import _hits

Source = tuple[str, str]

CANDIDATE_KINDS = ("entity", "function", "query")
QUESTION_MAX = 500
MAX_K = 25
MIN_COSINE = 0.2
_FIELD_BOOST_CAP = 5
_PAIRS_PER_STATEMENT = 100
_WORD_RE = re.compile(r"\w+")
# Words that carry no meaning for picking a source; without this list "the"
# would hit every name that contains "the" (substring matching).
_STOP_WORDS = frozenset(
    """a an and any are as at be by can could do does for from get give has have how i in
    is it list me my of on or our please show tell than that the their them there these
    this to us was we were what when where which who why will with would you your all
    find see much many""".split()
)


def question_tokens(question: str) -> list[str]:
    """The question's meaningful words, case-folded, without repeats (at
    most 20). A plural ``s`` is dropped from words over four letters, so
    "orders" finds ``Order`` (matching is by substring)."""
    words = []
    for word in _WORD_RE.findall((question or "")[:QUESTION_MAX].casefold()):
        if len(word) < 2 or word in _STOP_WORDS:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return list(dict.fromkeys(words))[:20]


async def scoped_nodes(db: AsyncSession, allowed: set[Source]) -> list[DataCatalogNode]:
    """Every node of the allowed sources, and only those."""
    pairs = sorted(allowed)
    nodes: list[DataCatalogNode] = []
    for chunk in batched(pairs, _PAIRS_PER_STATEMENT):
        condition = or_(
            *(
                and_(DataCatalogNode.source_kind == k, DataCatalogNode.source_name == n)
                for k, n in chunk
            )
        )
        rows = await db.execute(select(DataCatalogNode).where(condition))
        nodes.extend(rows.scalars())
    # Again in Python: the scope is the one rule this module exists for.
    return [n for n in nodes if (n.source_kind, n.source_name) in allowed]


def _lexical(
    tokens: list[str],
    node: DataCatalogNode,
    fields: list[DataCatalogNode],
    source: DataCatalogNode | None,
) -> float:
    if not tokens:
        return 0.0
    raw = 3 * _hits(tokens, node.name, node.label, *node.synonyms)
    raw += 2 * _hits(tokens, node.purpose, *node.examples)
    boost = sum(_hits(tokens, f.name, f.label, *f.synonyms) for f in fields)
    raw += min(boost, _FIELD_BOOST_CAP)
    if source is not None:
        raw += _hits(tokens, source.label, source.purpose, *source.synonyms)
    # A question whose every word hits name and purpose scores about 1.
    return min(1.0, raw / (5 * len(tokens)))


def _visible(node: DataCatalogNode) -> bool:
    return bool(node.readable) or bool(node.writable)


async def _vectors(
    db: AsyncSession, ids: list[int], model: str
) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {}
    for chunk in batched(ids):
        rows = await db.execute(
            select(DataCatalogVector.node_id, DataCatalogVector.vector).where(
                DataCatalogVector.node_id.in_(chunk), DataCatalogVector.model == model
            )
        )
        out.update({nid: unpack(raw) for nid, raw in rows.all()})
    return out


async def find_sources(
    db: AsyncSession,
    question: str,
    *,
    allowed_sources: set[Source],
    tier: str,
    k: int = 5,
    scope_tier: str = BASIC,
    use_vectors: bool = True,
) -> list[dict[str, Any]]:
    """The ``k`` best candidates of the allowed sources for ``question``.

    ``tier`` is the turn's tier and ``scope_tier`` the tier of the agent
    whose scope this is: an advanced agent's scope is empty in a basic turn.
    """
    if not agent_allowed(scope_tier, tier):
        return []
    allowed = {(str(a), str(b)) for a, b in allowed_sources}
    if not allowed:
        return []
    k = max(1, min(int(k), MAX_K))
    nodes = await scoped_nodes(db, allowed)
    by_id = {n.id: n for n in nodes}
    fields_of: dict[int, list[DataCatalogNode]] = {}
    for n in nodes:
        if n.kind == "field" and n.readable and n.parent_id is not None:
            fields_of.setdefault(n.parent_id, []).append(n)
    candidates = [n for n in nodes if n.kind in CANDIDATE_KINDS and _visible(n)]
    if not candidates:
        return []

    tokens = question_tokens(question)
    lexical = {
        n.id: _lexical(tokens, n, fields_of.get(n.id, []), by_id.get(n.parent_id or -1))
        for n in candidates
    }
    scores: dict[int, float] = {}
    vectors: dict[int, list[float]] = {}
    question_vector = None
    if use_vectors:
        model = await resolve_model()
        if model:
            vectors = await _vectors(db, [n.id for n in candidates], model)
            if vectors:
                question_vector = await embed_question(question[:QUESTION_MAX], model)
    for n in candidates:
        if question_vector is not None and n.id in vectors:
            score = cosine(question_vector, vectors[n.id])
            if score >= MIN_COSINE:
                scores[n.id] = score
        elif lexical[n.id] > 0:
            scores[n.id] = lexical[n.id]
    ranked = sorted(scores, key=lambda nid: (-scores[nid], by_id[nid].source_name, by_id[nid].name))
    top = ranked[:k]
    relations = await _relations(db, top, by_id)
    return [_out(by_id[nid], by_id, fields_of, relations.get(nid, []), scores[nid]) for nid in top]


async def _relations(
    db: AsyncSession, ids: list[int], by_id: dict[int, DataCatalogNode]
) -> dict[int, list[dict[str, Any]]]:
    """One hop from each node, both directions, scoped: an edge whose other
    end is not a node of the scope (``by_id``) is dropped."""
    if not ids:
        return {}
    rows = await db.execute(
        select(DataCatalogEdge).where(
            or_(DataCatalogEdge.from_id.in_(ids), DataCatalogEdge.to_id.in_(ids))
        )
    )
    out: dict[int, list[dict[str, Any]]] = {}
    seen: set[tuple[int, int, str]] = set()
    for edge in rows.scalars():
        for here, there in ((edge.from_id, edge.to_id), (edge.to_id, edge.from_id)):
            if here not in ids or there not in by_id or (here, there, edge.kind) in seen:
                continue
            other = by_id[there]
            if not _visible(other):
                continue
            seen.add((here, there, edge.kind))
            out.setdefault(here, []).append(
                {
                    "kind": edge.kind,
                    "to": {
                        "source_name": other.source_name,
                        "name": other.name,
                        "label": other.label or other.name,
                    },
                }
            )
    return out


def _out(
    node: DataCatalogNode,
    by_id: dict[int, DataCatalogNode],
    fields_of: dict[int, list[DataCatalogNode]],
    relations: list[dict[str, Any]],
    score: float,
) -> dict[str, Any]:
    source = by_id.get(node.parent_id or -1)
    keys = [f.name for f in fields_of.get(node.id, []) if f.is_key]
    return {
        "source_kind": node.source_kind,
        "source_name": node.source_name,
        "kind": node.kind,
        "name": node.name,
        "label": node.label or node.name,
        "purpose": node.purpose or (source.purpose if source is not None else ""),
        "not_for": node.not_for or (source.not_for if source is not None else ""),
        "keys": keys,
        "relations": relations[:10],
        "score": round(score, 3),
    }


async def list_sources(
    db: AsyncSession, *, allowed_sources: set[Source], tier: str, scope_tier: str = BASIC
) -> list[dict[str, Any]]:
    """The source nodes of the scope, as names and purposes."""
    if not agent_allowed(scope_tier, tier) or not allowed_sources:
        return []
    allowed = {(str(a), str(b)) for a, b in allowed_sources}
    nodes = [n for n in await scoped_nodes(db, allowed) if n.kind == "source"]
    nodes.sort(key=lambda n: (n.source_kind, n.source_name))
    return [
        {
            "source_kind": n.source_kind,
            "name": n.source_name,
            "label": n.label or n.source_name,
            "purpose": n.purpose or "",
            "not_for": n.not_for or "",
        }
        for n in nodes
    ]
