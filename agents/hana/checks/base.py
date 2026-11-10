"""The data model and the registry of ``builtin:hana`` checks.

A check is DATA: a name (hana-cli's command name), a category, a one-line
purpose, the upstream file it was derived from, and one or more fixed SQL
statements whose only variable parts are ``?`` bind parameters. The model
picks a check by name and passes at most five arguments (``schema``,
``object``, ``top``, ``hours``, ``host``); it never supplies SQL. Every argument is
checked here, before anything is sent, and a refusal names the argument and
the rule, never the value: the value is model output and may hold anything.

Standard library and the package's own pure modules only (no ``hdbcli``, no
pydantic-ai, no ``agents.db``): the registry is imported by the admin gate and
by ``python -m agents.hana.checks --dump`` as well as by the toolset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from agents.hana.errors import HanaError
from agents.hana.guard import FORBIDDEN_WORDS
from agents.hana.masking import mask_statement
from agents.hana.sqltokens import SelectRefused, Token, tokenize

CATEGORIES: tuple[str, ...] = (
    "monitoring", "performance", "system", "backup", "security", "database", "schema")

CHECK_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,47}$")
"""hana-cli's command names (``longRunning``, ``expensiveStatements``); it fits
the probe's ``CHECK_NAME_RE`` also with a ``.<label>`` suffix (``--dump``)."""

LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
COLUMN_RE = re.compile(r"^[A-Z_][A-Z0-9_#$]{0,126}$")
SOURCE_RE = re.compile(r"^bin/[A-Za-z][A-Za-z0-9]{0,47}\.js$")

HANA_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#./]{0,126}$")
"""A schema or object name as an argument. HANA's undelimited identifiers are
a letter or ``_`` followed by letters, digits, ``_``, ``$`` and ``#``, at most
127 characters; ``.`` and ``/`` are added for repository and namespaced names
(``my.pkg/OBJ``). The value is only ever a bind parameter, compared with a
catalogue column as given (no upper-casing), so these two characters cannot
change the statement. Any other character of a delimited identifier (a space,
a quote, ``::``) is refused for now."""

HOST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
"""A host name as an argument (``traceContents``): a letter or digit, then
letters, digits, ``.``, ``_`` and ``-``, at most 255 characters, as
``M_TRACEFILES.HOST`` writes it. Only ever a bind parameter; no port, no
``:`` and no ``/``, so it cannot be read as an address or a path."""

MAX_PURPOSE = 120
MAX_ROWS = 500
_FORBIDDEN_MARKERS = ("{", "}", "%s", "%(")


@dataclass(frozen=True)
class Arg:
    """One argument a check may declare. ``kind`` is ``int``, ``ident`` or ``host``."""

    name: str
    kind: str
    default: Any
    low: int = 0
    high: int = 0
    sample: Any = None

    @property
    def rule(self) -> str:
        if self.kind == "int":
            return f"must be a whole number from {self.low} to {self.high}"
        if self.kind == "host":
            return ("must be a host name: a letter or digit then letters, digits, . _ -,"
                    " at most 255")
        return "must be an identifier: a letter or _ then letters, digits, _ $ # . /, at most 127"


ARGS: dict[str, Arg] = {
    "schema": Arg("schema", "ident", None, sample="SYS"),
    "object": Arg("object", "ident", None, sample="DUMMY"),
    "top": Arg("top", "int", 50, 1, MAX_ROWS, sample=1),
    "hours": Arg("hours", "int", 24, 1, 720, sample=24),
    # The sample is a host that does not exist: the probe's run of a check
    # that binds it fails harmlessly (SPS07 answers SQL code 8).
    "host": Arg("host", "host", None, sample="probe-no-such-host"),
}
"""The closed set of arguments. A statement binds only these names."""

TOP_PLUS_ONE = "top_plus_one"
"""A derived bind, not an argument: the effective ``top`` plus one."""
_DUMP_SAMPLES: dict[str, Any] = {
    **{name: arg.sample for name, arg in ARGS.items()}, TOP_PLUS_ONE: 2}

UNDECLARED_RULE = "not an argument of this check"
REQUIRED_RULE = "is required by this check"


class InvalidArgument(HanaError):
    """``invalid_argument`` that names the argument and the rule, never the value."""

    def __init__(self, argument: str, rule: str) -> None:
        super().__init__("invalid_argument")
        self.argument = argument
        self.rule = rule

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out["argument"] = self.argument
        out["rule"] = self.rule
        return out


@dataclass(frozen=True)
class Statement:
    label: str
    """The part's name in a composite check; ``""`` for a single statement."""
    sql: str
    binds: tuple[str, ...]
    """:data:`ARGS` names in ``?`` order, plus :data:`TOP_PLUS_ONE`.

    A row limit in the SQL (``TOP ?``, ``LIMIT ?``) binds ``top_plus_one``,
    never ``top``: the connection fetches ``top + 1`` rows to tell whether the
    answer was cut (``truncated``), so a limit of ``top`` would hide that. A
    statement that binds ``top_plus_one`` needs ``top`` among the check's
    ``args``; one without an SQL limit binds neither (the fetch cuts it).
    """


@dataclass(frozen=True)
class Check:
    name: str
    category: str
    purpose: str
    source: str
    statements: tuple[Statement, ...]
    args: frozenset[str] = frozenset({"top"})
    masked_columns: frozenset[str] = frozenset()
    """Columns whose text is a statement other sessions ran: masked with
    :func:`agents.hana.masking.mask_statement` before the model sees them."""
    requires_select: bool = False
    """Listed and runnable only for an entry with ``allow_select``."""
    max_rows_cap: int = MAX_ROWS
    """The highest ``top`` this check accepts (at most :data:`MAX_ROWS`)."""
    required: frozenset[str] = frozenset()
    """Arguments the caller must give (a subset of ``args``). Without them a
    ``WHERE X = ?`` would bind NULL and answer no rows, which reads like "no
    data"; a refusal naming the argument tells the model what is missing."""

    @property
    def composite(self) -> bool:
        return len(self.statements) > 1


def validate_check(check: Check) -> None:
    """Refuse a check that breaks an invariant; ``ValueError`` naming the rule.

    The SQL is fixed text of this repository, so naming the check is fine; the
    statement itself is not repeated.
    """

    def refuse(rule: str) -> ValueError:
        return ValueError(f"check {check.name!r}: {rule}")

    if not isinstance(check.name, str) or not CHECK_NAME_RE.fullmatch(check.name):
        raise ValueError("check: name is not a hana-cli command name")
    if check.category not in CATEGORIES:
        raise refuse("category is not one of CATEGORIES")
    purpose = check.purpose
    if (not isinstance(purpose, str) or not purpose or len(purpose) > MAX_PURPOSE
            or purpose != purpose.strip() or not purpose.isprintable()):
        raise refuse(f"purpose is not one printable line of 1..{MAX_PURPOSE} characters")
    if check.source != f"bin/{check.name}.js" or not SOURCE_RE.fullmatch(check.source):
        raise refuse("source is not bin/<name>.js")
    if not isinstance(check.args, frozenset) or not check.args <= set(ARGS):
        raise refuse("args are not a subset of ARGS")
    if not isinstance(check.required, frozenset) or not check.required <= check.args:
        raise refuse("required is not a subset of args")
    if not isinstance(check.masked_columns, frozenset) or not all(
            isinstance(c, str) and COLUMN_RE.fullmatch(c) for c in check.masked_columns):
        raise refuse("masked_columns are not column names")
    if (not isinstance(check.max_rows_cap, int) or isinstance(check.max_rows_cap, bool)
            or not 1 <= check.max_rows_cap <= MAX_ROWS):
        raise refuse(f"max_rows_cap is not 1..{MAX_ROWS}")
    if not isinstance(check.requires_select, bool):
        raise refuse("requires_select is not a boolean")
    statements = check.statements
    if not isinstance(statements, tuple) or not statements:
        raise refuse("has no statement")
    labels = [s.label for s in statements]
    if len(statements) == 1:
        if labels != [""]:
            raise refuse("a single statement has the label \"\"")
    elif not all(isinstance(lbl, str) and LABEL_RE.fullmatch(lbl) for lbl in labels) or len(
            set(labels)) != len(labels):
        raise refuse("the parts of a composite check need distinct labels")
    for index, statement in enumerate(statements):
        _validate_statement(check, index, statement, refuse)


def _validate_statement(check: Check, index: int, statement: Statement, refuse) -> None:
    where = f"statement {index}"
    sql = statement.sql
    if not isinstance(sql, str) or not sql.strip():
        raise refuse(f"{where} has no SQL")
    if any(marker in sql for marker in _FORBIDDEN_MARKERS):
        raise refuse(f"{where} holds a format placeholder: arguments are bound, never built")
    try:
        tokens = tokenize(sql)
    except SelectRefused:
        raise refuse(f"{where} is not readable SQL") from None
    _require_read(tokens, where, refuse)
    params = [t for t in tokens if t.kind == "param"]
    if any(t.text != "?" for t in params):
        raise refuse(f"{where} has a named parameter: only ? is bound")
    if not isinstance(statement.binds, tuple) or len(params) != len(statement.binds):
        raise refuse(f"{where} binds do not match its ? count")
    if not set(statement.binds) <= check.args | {TOP_PLUS_ONE}:
        raise refuse(f"{where} binds an argument the check does not declare")
    if TOP_PLUS_ONE in statement.binds and "top" not in check.args:
        raise refuse(f"{where} binds top_plus_one without declaring top")


def _require_read(tokens: list[Token], where: str, refuse) -> None:
    """A registered statement is one read: strict, at registration, on tokens.

    ``--dump`` hands every statement to the probe, which runs it on a landscape
    whose database user may hold wide privileges, so a check that writes must
    never get as far as the registry. Words inside a string literal or a quoted
    identifier are tokens of another kind and do not count. Which views a read
    may touch is the SELECT guard's job (``agents.hana.guard.check_select``),
    run over every registered statement by the generic test.
    """
    if not tokens or not tokens[0].is_word("SELECT", "WITH"):
        raise refuse(f"{where} does not start with SELECT or WITH")
    for token in tokens:
        if token.kind == "comment":
            raise refuse(f"{where} holds a comment")
        if token.is_symbol(";"):
            raise refuse(f"{where} holds a ;: one statement only")
        if token.kind == "word" and (token.value in FORBIDDEN_WORDS or token.value == "INTO"):
            raise refuse(f"{where} holds a keyword that is no part of a read")


# ---- the registry -------------------------------------------------------------------

_REGISTRY: dict[str, Check] = {}


def register(*checks: Check) -> None:
    """Add checks; all or none. Refuses an invalid check and a name taken."""
    seen = set(_REGISTRY)
    for check in checks:
        validate_check(check)
        if check.name in seen:
            raise ValueError(f"check {check.name!r}: name is registered twice")
        seen.add(check.name)
    for check in checks:
        _REGISTRY[check.name] = check


def all_checks() -> tuple[Check, ...]:
    """Every registered check, in registration order."""
    return tuple(_REGISTRY.values())


def get_check(name: str) -> Check | None:
    return _REGISTRY.get(name) if isinstance(name, str) else None


def visible_checks(*, allow_select: bool) -> tuple[Check, ...]:
    """What an entry may list: a ``requires_select`` check only with ``allow_select``."""
    return tuple(c for c in _REGISTRY.values() if allow_select or not c.requires_select)


def lookup(name: Any, *, allow_select: bool) -> Check:
    """The check a call names, else ``unknown_check`` (never naming what was sent).

    A ``requires_select`` check without ``allow_select`` does not exist for the
    caller, so it is ``unknown_check`` too.
    """
    check = get_check(name)
    if check is None or (check.requires_select and not allow_select):
        raise HanaError("unknown_check")
    return check


# ---- arguments ----------------------------------------------------------------------


def _check_value(arg: Arg, value: Any) -> Any:
    if arg.kind == "int":
        if not isinstance(value, int) or isinstance(value, bool) or not (
                arg.low <= value <= arg.high):
            raise InvalidArgument(arg.name, arg.rule)
        return value
    pattern = HOST_NAME_RE if arg.kind == "host" else HANA_IDENT_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise InvalidArgument(arg.name, arg.rule)
    return value


def resolve_args(
    check: Check,
    *,
    schema: Any = None,
    object: Any = None,  # noqa: A002 - the tool's argument name
    top: Any = None,
    hours: Any = None,
    host: Any = None,
) -> dict[str, Any]:
    """The values of the arguments ``check`` declares, defaults filled in.

    ``None`` means "not given". An argument the check does not declare is
    refused when given, a ``required`` one when not given; a given value is
    checked against its :class:`Arg`
    rule, and ``top`` also against the check's ``max_rows_cap``. Every refusal
    is :class:`InvalidArgument` naming the argument and the rule only.
    """
    given = {"schema": schema, "object": object, "top": top, "hours": hours,
             "host": host}
    resolved: dict[str, Any] = {}
    for name in ARGS:  # a fixed order: the first refused argument is deterministic
        value = given[name]
        if name not in check.args:
            if value is not None:
                raise InvalidArgument(name, UNDECLARED_RULE)
            continue
        arg = ARGS[name]
        if value is None and name in check.required:
            raise InvalidArgument(name, REQUIRED_RULE)
        if value is None:
            # Under a cap below the default, leaving top out means the cap.
            resolved[name] = (
                min(arg.default, check.max_rows_cap) if name == "top" else arg.default)
            continue
        resolved[name] = _check_value(arg, value)
    if "top" in resolved and resolved["top"] > check.max_rows_cap:
        raise InvalidArgument(
            "top", f"must be a whole number from 1 to {check.max_rows_cap} for this check")
    return resolved


def bind_values(statement: Statement, args: dict[str, Any]) -> tuple[Any, ...]:
    """The bind parameters of one statement, in ``?`` order.

    ``top_plus_one`` is derived from the resolved ``top`` (see :class:`Statement`).
    """
    return tuple(
        args["top"] + 1 if name == TOP_PLUS_ONE else args[name] for name in statement.binds)


# ---- the answer ---------------------------------------------------------------------


def _mask_rows(columns: Sequence[str], rows: Iterable[Sequence[Any]],
               masked: frozenset[str]) -> list[list[Any]]:
    positions = {i for i, column in enumerate(columns) if str(column).upper() in masked}
    out = []
    for row in rows:
        out.append([
            (None if value is None else mask_statement(str(value)))
            if i in positions else value
            for i, value in enumerate(row)
        ])
    return out


def build_answer(
    check: Check,
    results: Sequence[tuple[Sequence[str], Sequence[Sequence[Any]], bool]],
) -> dict[str, Any]:
    """The tool's answer from one ``(columns, rows, truncated)`` per statement.

    A single statement answers ``{columns, rows, row_count, truncated}``, a
    composite check ``{"parts": [{label, columns, rows, row_count,
    truncated}]}``. The masked columns are masked here, so no caller can skip it.
    """
    if len(results) != len(check.statements):
        raise ValueError("build_answer: one result per statement")
    parts = []
    for statement, (columns, rows, truncated) in zip(check.statements, results):
        masked_rows = _mask_rows(columns, rows, check.masked_columns)
        parts.append({
            "label": statement.label,
            "columns": list(columns),
            "rows": masked_rows,
            "row_count": len(masked_rows),
            "truncated": bool(truncated),
        })
    if not check.composite:
        single = parts[0]
        del single["label"]
        return single
    return {"parts": parts}


# ---- --dump -------------------------------------------------------------------------


def dump_entries() -> list[dict[str, Any]]:
    """``[{name, sql, binds}]`` for ``scripts/probe_hana_connectivity.py --checks-file``.

    One entry per statement (a composite check's as ``<name>.<label>``), with
    the sample values of :data:`ARGS` (``SYS``, ``DUMMY``, 1, 24,
    ``probe-no-such-host``; ``top_plus_one`` 2)
    as binds.
    """
    entries = []
    for check in _REGISTRY.values():
        for statement in check.statements:
            name = f"{check.name}.{statement.label}" if statement.label else check.name
            entries.append({
                "name": name,
                "sql": statement.sql,
                "binds": [_DUMP_SAMPLES[b] for b in statement.binds],
            })
    return entries
