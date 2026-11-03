"""Find the catalogue nodes that answer a question, inside one scope.

The scope (``allowed_sources``) is applied FIRST, in the statement and again
on every row read: a node of another source is never scored, never ranked
and never named, so nothing about an unattached source leaks through a
score, an order or a relation. Then:

* with a question vector and node vectors (``agents.data_catalog.embeddings``)
  the score is the cosine similarity;
* otherwise (no model, no vector for a node) the score is lexical: the
  question's words against the node's name, label and synonyms (weight 3),
  purpose and examples (2), its readable children's names, labels and
  synonyms -- an entity's fields, an InA query's dimensions and measures --
  (1 each, at most 5: the field-of-source boost) and its source's label,
  purpose and synonyms (1), normalised to 0..1.

Candidates are entities, functions and InA queries an agent can read or
change. A child that is not readable adds nothing and is never named; a
candidate's ``keys`` are its readable children marked ``is_key``; a relation
to a node outside the scope is dropped. Hints inherit: a candidate without a
purpose or "not for" shows its source's; a node without a label shows its
name.

An InA query is answered under its registration's slug (``source_name``),
as a candidate and as the end of a relation: ``run_query`` takes that slug,
and the model never gets the query's InA object name (``agents.ina.tools``),
which is the node's ``name``.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.access import BASIC, agent_allowed
from agents.data_catalog.embeddings import cosine, embed_question, resolve_model, unpack
from agents.data_catalog.importer import batched
from agents.data_catalog.models import (
    INA,
    DataCatalogEdge,
    DataCatalogNode,
    DataCatalogVector,
)
from agents.data_catalog.scope import node_sources
from agents.odata.search import _hits

Source = tuple[str, str]

CANDIDATE_KINDS = ("entity", "function", "query")
# The children whose names and labels boost their parent candidate.
CHILD_KINDS = ("field", "dimension", "measure")
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
    """Every node of the allowed sources, and only those (a catalogue
    service's under the kind it is stored as: `node_sources`)."""
    allowed = node_sources(allowed)
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


def _answered_name(node: DataCatalogNode) -> str:
    """The name a candidate or a relation answers for ``node``: the slug of
    an InA query's registration (what ``run_query`` takes; its InA object
    name never reaches the model), the node's name for every other node."""
    if node.source_kind == INA and node.kind == "query":
        return node.source_name
    return node.name


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


# How a search ranked (``find_sources_detail``): ``vector`` when the
# question was embedded and at least one candidate had a vector of the
# effective model (the others are still ranked by words), else ``lexical``.
VECTOR, LEXICAL = "vector", "lexical"


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
    candidates, _ = await find_sources_detail(
        db, question, allowed_sources=allowed_sources, tier=tier, k=k,
        scope_tier=scope_tier, use_vectors=use_vectors,
    )
    return candidates


async def find_sources_detail(
    db: AsyncSession,
    question: str,
    *,
    allowed_sources: set[Source],
    tier: str,
    k: int = 5,
    scope_tier: str = BASIC,
    use_vectors: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    """``(candidates, mode)``: what :func:`find_sources` answers, and
    whether vectors ranked it (:data:`VECTOR`, :data:`LEXICAL`), for the
    admin's test search. The agents' tool answers the candidates only."""
    if not agent_allowed(scope_tier, tier):
        return [], LEXICAL
    allowed = {(str(a), str(b)) for a, b in allowed_sources}
    if not allowed:
        return [], LEXICAL
    k = max(1, min(int(k), MAX_K))
    nodes = await scoped_nodes(db, allowed)
    by_id = {n.id: n for n in nodes}
    fields_of: dict[int, list[DataCatalogNode]] = {}
    for n in nodes:
        if n.kind in CHILD_KINDS and n.readable and n.parent_id is not None:
            fields_of.setdefault(n.parent_id, []).append(n)
    candidates = [n for n in nodes if n.kind in CANDIDATE_KINDS and _visible(n)]
    if not candidates:
        return [], LEXICAL

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
    mode = VECTOR if question_vector is not None and vectors else LEXICAL
    found = [_out(by_id[nid], by_id, fields_of, relations.get(nid, []), scores[nid]) for nid in top]
    return found, mode


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
                        "name": _answered_name(other),
                        "label": other.label or _answered_name(other),
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
        "name": _answered_name(node),
        "label": node.label or _answered_name(node),
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
