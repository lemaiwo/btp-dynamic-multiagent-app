"""The deterministic compare engine: one CSV row against its source row, per column.

The model's work ends with the :class:`~agents.reconcile.spec.ReconcileSpec`;
from here on code alone decides whether a migrated cell is what its rule says,
so a run of a million rows costs no tokens and gives the same answer twice.
The engine does no I/O: the caller reads the CSV (:func:`iter_csv`), builds the
code-table lookups from the mapping workbook and reads the source rows through
ARC-1, then feeds each row to :meth:`Comparer.compare`.

What "the same" means lives in :mod:`agents.reconcile.values` (blank vs. space,
leading zeros, numbers); this module only decides which value is *expected*.

Memory stays bounded for any input size: counters are exact, but the lists of
mismatches and of keys missing in the source stop at ``mismatch_cap`` and count
what they dropped, so a CSV that is wrong in every cell cannot exhaust the
instance. The only per-row state is the set of keys seen (for duplicates).

Keys are compared case-sensitively, as SAP stores them: a lower-case CSV key
for an upper-case source key is reported as missing in the source.

Error texts are fixed (:class:`CsvError` carries a ``code``): a CSV holds
customer data, and an exception that quoted a cell would carry it into logs.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from agents.reconcile.spec import (
    MAX_COLUMNS,
    ConstantRule,
    DirectRule,
    ExpressionRule,
    KeySpec,
    NotNeededRule,
    ReconcileSpec,
    UnresolvedRule,
    Value,
    ValueMapRule,
)
from agents.reconcile.values import alpha, clean, pad_key, same

# Used with fullmatch; also what may be pasted into an ABAP SQL literal.
KEY_RE = re.compile(r"[A-Za-z0-9 _\-./]+")

CSV_ERRORS = {
    "not_utf8": "The CSV file is not UTF-8 text.",
    "no_header": "The CSV file has no header row.",
    "duplicate_header": "The CSV header names a column twice.",
    "too_many_columns": f"The CSV file has more than {MAX_COLUMNS} columns.",
    "malformed_csv": "The CSV file cannot be parsed.",
    "column_missing": "A column of the spec is not in the CSV header.",
}

# Mismatch reasons (stable codes; the report and the model show them).
VALUE_DIFFERS = "value_differs"
NO_XREF_ENTRY = "no_xref_entry"
MUST_BE_BLANK = "must_be_blank"


class CsvError(ValueError):
    """A CSV file that cannot be read; ``code`` is one of :data:`CSV_ERRORS`."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(CSV_ERRORS[code])


class LookupTable(Protocol):
    """A code table: cleaned source value(s) → target value (``xref_reader.Lookup``).

    ``entries`` (in workbook order) is read only for rules with ``alpha``, to
    build the leading-zero-free index once per lookup; keys that collapse to
    the same zero-free form keep the FIRST workbook entry (as a duplicate key
    does in the table itself). Read-only: the engine never writes it, so a
    frozen dataclass field satisfies it.
    """

    @property
    def entries(self) -> Mapping[tuple[str, ...], str]: ...

    def get(self, key: tuple[str, ...]) -> str | None: ...


# --- CSV ------------------------------------------------------------------------


class CsvRows(Iterator[tuple[int, dict[str, str]]]):
    """Data rows as ``(row_no, {header: value})``; ``malformed`` counts skipped rows.

    ``row_no`` counts CSV records with the header as row 1, so it matches the
    row a spreadsheet shows when it opens the file (a quoted newline does not
    start a new record). Blank lines are skipped without counting as malformed.
    """

    def __init__(self, reader: Iterator[list[str]], header: list[str]) -> None:
        # ``reader`` decodes lazily: a bad byte late in the file surfaces here.
        self._reader = reader
        self._header = header
        self._row_no = 1
        self.malformed = 0

    def __iter__(self) -> CsvRows:
        return self

    def __next__(self) -> tuple[int, dict[str, str]]:
        width = len(self._header)
        while True:
            try:
                fields = next(self._reader)
            except csv.Error:
                raise CsvError("malformed_csv") from None
            except UnicodeDecodeError:
                raise CsvError("not_utf8") from None
            self._row_no += 1
            if not fields:
                continue
            if len(fields) != width:
                self.malformed += 1
                continue
            return self._row_no, dict(zip(self._header, fields, strict=True))


def iter_csv(data: bytes) -> tuple[list[str], CsvRows]:
    """The header and a lazy iterator over the data rows of a UTF-8 comma CSV.

    A byte-order mark is dropped; header names are stripped. The header is
    checked at once (so a caller can refuse the file before starting a run);
    the rows are decoded and parsed while iterated, so the file is not copied
    into one large string: a non-UTF-8 byte in the first chunk fails here,
    one further on fails during iteration, both as ``CsvError("not_utf8")``.
    """
    stream = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", newline="")
    reader = csv.reader(stream)
    try:
        first = next(reader, None)
    except csv.Error:
        raise CsvError("malformed_csv") from None
    except UnicodeDecodeError:
        raise CsvError("not_utf8") from None
    header = [name.strip() for name in first or []]
    if not any(header):
        raise CsvError("no_header")
    if len(header) > MAX_COLUMNS:
        raise CsvError("too_many_columns")
    if len(set(header)) != len(header):
        raise CsvError("duplicate_header")
    return header, CsvRows(reader, header)


def normalise_key(raw: str | None, key: KeySpec) -> str | None:
    """The source key for a CSV key cell, or ``None`` when it cannot be one.

    All-digit keys are zero-padded to ``key.length`` (as SAP stores them) when
    ``pad_numeric``, after their leading zeros are dropped (ALPHA), so a key
    written with more zeros than the field holds is still that key. A key
    outside :data:`KEY_RE` or longer than the field is refused: it could not
    exist in the source and must never reach a query.
    """
    text = clean(raw)
    if not text or not KEY_RE.fullmatch(text):
        return None
    if key.pad_numeric:
        text = pad_key(alpha(text), key.length)
    return text if len(text) <= key.length else None


# --- results --------------------------------------------------------------------


@dataclass
class ColumnStats:
    """Exact per-column counters. ``first_reason`` is the reason code of the
    column's first mismatch, kept here because the mismatch list stops at the
    cap and may not hold one of the column's mismatches at all."""

    kind: str
    checked: int = 0
    matched: int = 0
    mismatched: int = 0
    not_checked: int = 0
    first_reason: str | None = None


@dataclass(frozen=True)
class Mismatch:
    """One cell that differs. ``source`` is the source value(s) the rule read."""

    key: str
    column: str
    kind: str
    source: str
    expected: str
    actual: str
    reason: str


@dataclass(frozen=True)
class NotChecked:
    """A column the engine cannot compare (one entry per column, not per row)."""

    column: str
    kind: str
    reason: str
    needs_table: str | None


@dataclass
class EngineResult:
    rows_total: int
    rows_compared: int
    malformed_rows: int
    invalid_keys: int
    duplicate_keys: int
    missing_in_ecc: list[tuple[str, int]]
    stats: dict[str, ColumnStats]
    mismatches: list[Mismatch]
    mismatches_dropped: int
    not_checked: list[NotChecked]
    missing_in_ecc_dropped: int = 0
    invalid_key_rows: list[int] = field(default_factory=list)  # capped like the other lists

    @property
    def mismatch_count(self) -> int:
        return len(self.mismatches) + self.mismatches_dropped

    @property
    def missing_count(self) -> int:
        return len(self.missing_in_ecc) + self.missing_in_ecc_dropped


@dataclass
class _Expected:
    value: str
    source: str
    reason: str | None = None  # set when the rule itself already decides "mismatch"


# --- the comparer ----------------------------------------------------------------


@dataclass
class _Counters:
    rows_total: int = 0
    rows_compared: int = 0
    invalid_keys: int = 0
    duplicate_keys: int = 0
    seen: set[str] = field(default_factory=set)
    invalid_rows: list[int] = field(default_factory=list)


class Comparer:
    """Compares CSV rows with source rows under one spec; collects counters and lists.

    Source rows are dicts keyed by the upper-case field name, as the ARC-1 reader
    returns them; ``lookups`` is keyed by ``(csv column, lookup index)``.
    """

    def __init__(
        self,
        spec: ReconcileSpec,
        lookups: Mapping[tuple[str, int], LookupTable],
        *,
        mismatch_cap: int = 50_000,
    ) -> None:
        for column, rule in spec.columns.items():
            if isinstance(rule, ValueMapRule):
                for index in range(len(rule.lookups)):
                    if (column, index) not in lookups:
                        raise ValueError("A code-table lookup of a value_map rule is missing.")
        self._spec = spec
        self._lookups = lookups
        self._cap = max(0, mismatch_cap)
        self._trim = spec.trim
        self._counters = _Counters()
        self._stats = {column: ColumnStats(kind=rule.kind) for column, rule in spec.columns.items()}
        self._mismatches: list[Mismatch] = []
        self._dropped = 0
        self._missing: list[tuple[str, int]] = []
        self._missing_dropped = 0
        self._alpha_index: dict[tuple[str, int], dict[tuple[str, ...], str]] = {}
        self._not_checked = [
            NotChecked(
                column=column,
                kind=rule.kind,
                reason="needs_other_table" if rule.needs_table else "unresolved",
                needs_table=rule.needs_table,
            )
            for column, rule in spec.columns.items()
            if isinstance(rule, UnresolvedRule)
        ]

    def needed_fields(self) -> list[str]:
        """The source fields to read: the key first, then every field a rule reads."""
        return self._spec.ecc_fields()

    def missing_columns(self, header: list[str]) -> list[str]:
        """Spec columns (and the key column) that the CSV header does not have."""
        present = set(header)
        wanted = [self._spec.key.csv_column, *self._spec.columns]
        return [name for name in dict.fromkeys(wanted) if name not in present]

    def key_of(self, csv_row: Mapping[str, str], row_no: int | None = None) -> str | None:
        """The normalised key of a CSV row; counts the row, invalid and duplicate keys.

        Contract: call it exactly once per data row, because every call counts
        a row (calling it twice for one row counts a duplicate). A duplicate is
        still returned (and compared): both rows were migrated, so both are
        checked. Remembering the keys seen costs memory per distinct key; that
        is the one per-row state the engine keeps. ``row_no`` of an invalid key
        is listed in ``invalid_key_rows`` up to the cap.
        """
        counters = self._counters
        counters.rows_total += 1
        key = normalise_key(csv_row.get(self._spec.key.csv_column), self._spec.key)
        if key is None:
            counters.invalid_keys += 1
            if row_no is not None and len(counters.invalid_rows) < self._cap:
                counters.invalid_rows.append(row_no)
        elif key in counters.seen:
            counters.duplicate_keys += 1
        else:
            counters.seen.add(key)
        return key

    def compare(
        self,
        key: str,
        row_no: int,
        csv_row: Mapping[str, str],
        ecc_row: Mapping[str, object] | None,
    ) -> None:
        """Compare every spec column of one CSV row; ``ecc_row=None`` = not in the source.

        A spec column absent from ``csv_row`` raises ``CsvError("column_missing")``
        before anything is counted: read as blank it would pass every
        ``not_needed`` rule and fail every other one. Callers check the header
        with :meth:`missing_columns` first; this is the guard if they did not.
        """
        for column in self._spec.columns:
            if column not in csv_row:
                raise CsvError("column_missing")
        if ecc_row is None:
            if len(self._missing) < self._cap:
                self._missing.append((key, row_no))
            else:
                self._missing_dropped += 1
            return
        self._counters.rows_compared += 1
        for column, rule in self._spec.columns.items():
            stats = self._stats[column]
            if isinstance(rule, UnresolvedRule):
                stats.not_checked += 1
                continue
            actual = clean(csv_row.get(column), trim=self._trim)
            expected = self._expected(column, rule, ecc_row)
            stats.checked += 1
            reason = expected.reason
            if reason is None and not same(
                expected.value, actual, trim=self._trim, alpha=rule.alpha, numeric=rule.numeric
            ):
                reason = MUST_BE_BLANK if isinstance(rule, NotNeededRule) else VALUE_DIFFERS
            if reason is None:
                stats.matched += 1
                continue
            stats.mismatched += 1
            if stats.first_reason is None:
                stats.first_reason = reason
            if len(self._mismatches) < self._cap:
                self._mismatches.append(
                    Mismatch(
                        key=key,
                        column=column,
                        kind=rule.kind,
                        source=expected.source,
                        expected=expected.value,
                        actual=actual,
                        reason=reason,
                    )
                )
            else:
                self._dropped += 1

    def result(self, *, malformed_rows: int = 0) -> EngineResult:
        """A snapshot of the counters and lists (``malformed_rows`` from :class:`CsvRows`)."""
        counters = self._counters
        return EngineResult(
            rows_total=counters.rows_total,
            rows_compared=counters.rows_compared,
            malformed_rows=malformed_rows,
            invalid_keys=counters.invalid_keys,
            duplicate_keys=counters.duplicate_keys,
            missing_in_ecc=list(self._missing),
            stats={column: ColumnStats(**vars(s)) for column, s in self._stats.items()},
            mismatches=list(self._mismatches),
            mismatches_dropped=self._dropped,
            not_checked=list(self._not_checked),
            missing_in_ecc_dropped=self._missing_dropped,
            invalid_key_rows=list(counters.invalid_rows),
        )

    # --- expected values per rule kind ---

    def _field(self, ecc_row: Mapping[str, object], name: str) -> str:
        return clean(ecc_row.get(name), trim=self._trim)

    def _expected(self, column: str, rule: object, ecc_row: Mapping[str, object]) -> _Expected:
        if isinstance(rule, DirectRule):
            value = self._field(ecc_row, rule.source)
            return _Expected(value, value)
        if isinstance(rule, ConstantRule):
            return _Expected(rule.value, "")
        if isinstance(rule, ValueMapRule):
            return self._value_map(column, rule, ecc_row)
        if isinstance(rule, ExpressionRule):
            for case in rule.cases:
                if all(self._holds(rule, c.field, c.op, c.values, ecc_row) for c in case.when):
                    return self._value(case.then, ecc_row)
            return self._value(rule.otherwise, ecc_row)
        return _Expected("", "")  # NotNeededRule: the CSV value must be blank

    def _value(self, value: Value, ecc_row: Mapping[str, object]) -> _Expected:
        if value.field is not None:
            text = self._field(ecc_row, value.field)
            return _Expected(text, text)
        return _Expected(value.const or "", "")

    def _holds(
        self,
        rule: ExpressionRule,
        name: str,
        op: str,
        values: list[str],
        ecc_row: Mapping[str, object],
    ) -> bool:
        """One condition; ``equals``/``in`` honour the rule's ``alpha`` and ``numeric``.

        Conditions always ignore surrounding blanks (a source ``' '`` is blank),
        whatever ``spec.trim`` says about the output comparison.
        """
        text = clean(ecc_row.get(name))
        if op == "blank":
            return not text
        if op == "not_blank":
            return bool(text)
        return any(  # equals (one value) or in
            same(text, v, trim=True, alpha=rule.alpha, numeric=rule.numeric) for v in values
        )

    def _value_map(
        self, column: str, rule: ValueMapRule, ecc_row: Mapping[str, object]
    ) -> _Expected:
        """Translate the source value(s) through the lookups; the first that matches wins.

        Lookup keys are cleaned like the workbook cells (trimmed). An exact pass
        over all lookups comes first, so an exact hit in a later lookup beats a
        leading-zero match in an earlier one. Only with ``rule.alpha`` does a
        second pass compare both sides without leading zeros (a source NUMC
        ``0001`` finds ``1`` in a cell Excel stored as a number, and the other
        way round); without it ``0010`` and ``10`` stay different codes.

        Each lookup is keyed by its own source fields (``XrefLookup.sources``,
        else the rule's), so a chain can switch fields between lookups; a
        lookup whose fields are all blank is skipped, and the expected value is
        blank only when every field the rule reads is blank. ``on_miss
        "source"`` takes the RULE's own fields, never an override's.
        """
        count = len(rule.lookups)
        keyed = [rule.sources_of(index) for index in range(count)]
        names = list(dict.fromkeys([*rule.sources, *(n for f in keyed for n in f)]))
        read = {name: clean(ecc_row.get(name)) for name in names}
        source = " | ".join(read[name] for name in names)
        if not any(read.values()):
            return _Expected("", source)
        keys = [tuple(read[name] for name in fields) for fields in keyed]
        for index in range(count):
            if any(keys[index]):
                hit = self._lookups[(column, index)].get(keys[index])
                if hit is not None:
                    return _Expected(hit, source)
        if rule.alpha:
            for index in range(count):
                if any(keys[index]):
                    stripped = tuple(alpha(p) for p in keys[index])
                    hit = self._alpha_lookup(column, index).get(stripped)
                    if hit is not None:
                        return _Expected(hit, source)
        if rule.on_miss == "default":
            return _Expected(rule.default or "", source)
        if rule.on_miss == "source":
            # several sources: the first non-blank one of the rule's own fields
            return _Expected(next((read[n] for n in rule.sources if read[n]), ""), source)
        return _Expected("", source, NO_XREF_ENTRY)

    def _alpha_lookup(self, column: str, index: int) -> dict[tuple[str, ...], str]:
        """The lookup keyed without leading zeros, built once; the first entry wins."""
        slot = (column, index)
        table = self._alpha_index.get(slot)
        if table is None:
            table = {}
            for key, value in self._lookups[slot].entries.items():
                table.setdefault(tuple(alpha(p) for p in key), value)
            self._alpha_index[slot] = table
        return table
