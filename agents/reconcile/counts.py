"""Per-column counters of a reconciliation run, flattened for ``runs_store``.

``runs_store`` keeps counters only (a flat map of names to whole numbers), so
the per-column statistics of the engine are written as ``col.<column>.<metric>``
plus ``col.<column>.kind.<kind>: 1``, and read back by :func:`split_counts`.
Split out of ``run.py`` (which re-exports both functions).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, get_args

from agents.reconcile.engine import ColumnStats, EngineResult
from agents.reconcile.spec import Rule
from agents.reconcile.xref_reader import Lookup

COLUMN_PREFIX = "col."
COLUMN_METRICS = ("checked", "matched", "mismatched", "not_checked")
_KIND_MARK = ".kind."
# The rule kinds of the spec, read from its models (one Literal per rule class),
# so a new kind is known here without a second list to keep in step.
_RULE_KINDS = frozenset(
    kind
    for rule in get_args(get_args(Rule)[0])
    for kind in get_args(rule.model_fields["kind"].annotation)
)


def column_counts(stats: Mapping[str, ColumnStats]) -> dict[str, int]:
    """Per-column counters flattened for ``runs_store`` (counters only):
    ``col.<column>.<metric>`` per metric, plus ``col.<column>.kind.<kind>: 1``
    so the status can say a column's rule kind without reading the spec file.
    One entry per spec column, so at most ``5 * MAX_COLUMNS`` keys.
    """
    out: dict[str, int] = {}
    for column, stat in stats.items():
        for metric in COLUMN_METRICS:
            out[f"{COLUMN_PREFIX}{column}.{metric}"] = int(getattr(stat, metric))
        out[f"{COLUMN_PREFIX}{column}{_KIND_MARK}{stat.kind}"] = 1
    return out


def split_counts(counts: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A stored ``counts`` back into ``(totals, columns)``, the inverse of
    :func:`column_counts`; columns sorted by mismatches, most first, then name.

    A column name may hold dots, so the metric (or ``kind.<kind>``) is read
    from the end; a ``col.`` key of any other form, or a value that is no
    whole number, is dropped rather than guessed.
    """
    totals: dict[str, Any] = {}
    columns: dict[str, dict[str, Any]] = {}
    for key, value in counts.items():
        if not key.startswith(COLUMN_PREFIX):
            totals[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        head, _, last = key[len(COLUMN_PREFIX):].rpartition(".")
        if head and last in COLUMN_METRICS:
            columns.setdefault(head, {})[last] = value
        elif head.endswith(_KIND_MARK[:-1]) and last in _RULE_KINDS:
            name = head[: -len(_KIND_MARK) + 1]
            if name:
                columns.setdefault(name, {})["kind"] = last
    listed = [
        {"column": name, "kind": seen.get("kind"),
         **{metric: seen.get(metric, 0) for metric in COLUMN_METRICS}}
        for name, seen in columns.items()
    ]
    listed.sort(key=lambda c: (-c["mismatched"], c["column"]))
    return totals, listed


def run_counts(result: EngineResult, lookups: dict[tuple[str, int], Lookup]) -> dict[str, int]:
    """Counters only (``runs_store`` refuses anything else)."""
    tables = {id(t): t for t in lookups.values()}.values()
    return {
        "rows_total": result.rows_total,
        "rows_compared": result.rows_compared,
        "malformed_rows": result.malformed_rows,
        "invalid_keys": result.invalid_keys,
        "duplicate_keys": result.duplicate_keys,
        "missing_in_ecc": result.missing_count,
        "missing_in_ecc_dropped": result.missing_in_ecc_dropped,
        "mismatches": result.mismatch_count,
        "mismatches_dropped": result.mismatches_dropped,
        "not_checked": len(result.not_checked),
        "columns": len(result.stats),
        "lookups": len(tables),
        "lookups_truncated": sum(1 for t in tables if t.truncated),
        "lookup_duplicates": sum(t.duplicates for t in tables),
        **column_counts(result.stats),
    }
