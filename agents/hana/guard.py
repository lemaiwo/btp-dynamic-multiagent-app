"""The SELECT guard of ``builtin:hana``: the code gate of model-written SQL.

Why it is this strict: on the first landscape the database user may read and
write business schemas, so this guard, not the database's privileges, is what
keeps ``run_select`` on the monitoring views. It is closed by default: what
it does not positively recognise is refused. A refusal is a
:class:`SelectRefused` whose text names the rule, never any part of the SQL.

The rules, in the order they are checked:

``too_long``
    more than :data:`MAX_SQL_CHARS` characters (or not a string: ``unreadable``).
``unreadable``
    the tokenizer (``agents/hana/sqltokens.py``) could not read the text, or
    the parentheses do not balance.
``no_comments``
    a ``--``, ``//`` or ``/* */`` comment outside a literal.
``one_statement``
    a ``;`` outside a literal, trailing ones included.
``select_only``
    the first token is not ``SELECT`` or ``WITH``; ``WITH`` anywhere else
    (``WITH HINT``, a nested CTE); a data-changing, session or transaction
    keyword anywhere (:data:`FORBIDDEN_WORDS`); a sequence's ``NEXTVAL`` /
    ``CURRVAL``; a ``FOR`` clause other than a locking one.
``no_into``
    ``INTO`` anywhere.
``no_for_update``
    ``FOR UPDATE``, ``FOR SHARE LOCK`` and every other ``FOR`` + lock word.
``table_not_allowed``
    a table reference that is not ``SYS.<name>``, ``_SYS_STATISTICS.<name>``,
    an unqualified ``M_*`` name or a CTE declared before it; or anything after
    ``FROM`` / ``JOIN`` / ``APPLY`` / a ``,`` of a FROM list that is neither
    such a name nor ``(SELECT ...)``: a table function, ``LATERAL``,
    ``JSON_TABLE``, ``AS OF``, ``TABLESAMPLE``, a parenthesised join; and a
    schema-qualified function call outside those two schemas, or a quoted one.

How table references are found: the walker reads the tokens once, keeping a
frame per parenthesis level. In a query frame ``FROM`` opens the FROM list;
only ``WHERE`` / ``HAVING`` (followed by a condition), ``GROUP BY`` /
``ORDER BY``, ``LIMIT <n>`` and a set operator (followed by ``ALL``,
``DISTINCT``, ``SELECT`` or ``(``) close it: all HANA reserved words, and
only in a keyword position (never right after ``.`` or ``AS``, never right
before ``.``). A non-reserved word (``OFFSET``, ``OUTER``, ...) never closes
it, since it could be a column or alias and the next ``,`` would go
unchecked. Inside the list every ``,`` at that level, and ``JOIN`` (and
``APPLY`` in a keyword position) anywhere, introduce a reference that must
pass. Subqueries are
frames of their own, so a reference in a select list, a ``WHERE``, a CTE
body or the second branch of a ``UNION`` is checked like the first one.
``FROM`` inside ``EXTRACT(...)``, ``TRIM(...)``, ``SUBSTRING(...)`` and the
``*_REGEXPR`` functions is an expression keyword, not a table reference; a
subquery inside them is again a frame of its own.

The closing words were checked against the reserved-word list of the SAP
HANA Cloud SQL Reference only. The first target is SAP HANA 2.0 SPS07
on-premise, where that list is unverified, so the follow-token rules of
:meth:`_Walker._closes_from` (a clause must follow, a set operator's "(" must
open a query) are the safety net, not the list.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.hana.sqltokens import SelectRefused, Token, tokenize

__all__ = ["MAX_SQL_CHARS", "SelectRefused", "check_select"]

MAX_SQL_CHARS = 4000

ALLOWED_SCHEMAS = frozenset({"SYS", "_SYS_STATISTICS"})

#: Keywords that have no place in a read: data and schema changes, sessions,
#: transactions, procedures. Refused wherever they stand outside a literal or
#: a quoted identifier.
FORBIDDEN_WORDS = frozenset(
    {
        "INSERT", "UPDATE", "UPSERT", "DELETE", "MERGE", "TRUNCATE",
        "CREATE", "ALTER", "DROP", "RENAME", "COMMENT", "GRANT", "REVOKE",
        "CALL", "DO", "EXEC", "EXECUTE", "BEGIN", "DECLARE",
        "COMMIT", "ROLLBACK", "SAVEPOINT", "LOCK", "UNLOCK", "SET", "UNSET",
        "CONNECT", "DISCONNECT", "EXPLAIN", "LOAD", "UNLOAD", "IMPORT", "EXPORT",
        "BACKUP", "RECOVER", "REFRESH", "NEXTVAL", "CURRVAL",
    }
)
_LOCK_WORDS = frozenset({"UPDATE", "SHARE", "LOCK", "NOWAIT", "WAIT", "IGNORE"})

#: Words that end a FROM list at the same parenthesis level. Every one of
#: them is a HANA reserved word (SAP HANA SQL Reference, "Reserved Words"), so
#: it can never be a column, alias or CTE name written unquoted; and a word
#: only counts as one where it stands as a keyword (:meth:`_Walker._keyword_at`)
#: and is followed by what its clause demands (:meth:`_Walker._closes_from`).
#: A non-reserved word (``OFFSET``, ``OUTER``, ``APPLY``, ...) never switches
#: the FROM list off: a word that is not reserved could be an identifier, and
#: switching off would let the next ``, schema.table`` go unchecked.
_FROM_END = frozenset(
    {"WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "UNION", "INTERSECT",
     "EXCEPT", "MINUS"}
)
_SET_OPERATORS = frozenset({"UNION", "INTERSECT", "EXCEPT", "MINUS"})
#: What may follow a table reference (after its alias).
#: ``FOR`` is not in :data:`_FROM_END`: outside EXTRACT and friends it is refused
#: (``no_for_update`` / ``select_only``) wherever it stands.
_AFTER_REF = _FROM_END | {
    "FOR", "SELECT", "ON", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER"
}
#: Words never read as an alias of a table reference.
_NOT_ALIAS = (
    _AFTER_REF
    | FORBIDDEN_WORDS
    | {"AS", "OF", "INTO", "WITH", "FROM", "APPLY", "LATERAL", "TABLESAMPLE",
       "PARTITION", "USING", "NATURAL", "AT", "SYSTEM_TIME", "APPLICATION_TIME"}
)
#: Functions whose argument syntax uses FROM / FOR as expression keywords.
_EXPRESSION_FROM_FUNCTIONS = frozenset(
    {"EXTRACT", "TRIM", "SUBSTRING", "SUBSTR_REGEXPR", "SUBSTRING_REGEXPR",
     "OCCURRENCES_REGEXPR", "LOCATE_REGEXPR", "REPLACE_REGEXPR", "LIKE_REGEXPR"}
)


def check_select(sql: str) -> None:
    """Return when ``sql`` is one read of the monitoring views; else raise."""
    if not isinstance(sql, str):
        raise SelectRefused("unreadable")
    if len(sql) > MAX_SQL_CHARS:
        raise SelectRefused("too_long")
    tokens = tokenize(sql)
    if any(token.kind == "comment" for token in tokens):
        raise SelectRefused("no_comments")
    if any(token.is_symbol(";") for token in tokens):
        raise SelectRefused("one_statement")
    if not tokens or not tokens[0].is_word("SELECT", "WITH"):
        raise SelectRefused("select_only")
    _Walker(tokens).run()


@dataclass
class _Frame:
    in_from: bool = False
    expression: bool = False  # inside EXTRACT(...) and friends
    from_subquery: bool = False  # a "(SELECT ...)" standing as a table reference


class _Walker:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.size = len(tokens)
        # CTE name -> index of the ")" that closes its body: visible after it.
        self.ctes: dict[str, int] = {}
        self.cte_header: set[int] = set()

    # -- helpers -----------------------------------------------------------
    def _at(self, index: int) -> Token | None:
        return self.tokens[index] if 0 <= index < self.size else None

    def _closing(self, open_index: int) -> int:
        depth = 0
        for index in range(open_index, self.size):
            token = self.tokens[index]
            if token.is_symbol("("):
                depth += 1
            elif token.is_symbol(")"):
                depth -= 1
                if depth == 0:
                    return index
        raise SelectRefused("unreadable")

    # -- WITH ----------------------------------------------------------------
    def _declare_ctes(self) -> None:
        index = 1
        while True:
            name = self._at(index)
            if name is None or not name.is_ident or name.value in _NOT_ALIAS:
                raise SelectRefused("select_only")
            self.cte_header.add(index)
            index += 1
            token = self._at(index)
            if token is not None and token.is_symbol("("):
                index += 1
                while True:
                    column = self._at(index)
                    separator = self._at(index + 1)
                    if column is None or not column.is_ident or separator is None:
                        raise SelectRefused("select_only")
                    index += 2
                    if separator.is_symbol(")"):
                        break
                    if not separator.is_symbol(","):
                        raise SelectRefused("select_only")
            token = self._at(index)
            if token is None or not token.is_word("AS"):
                raise SelectRefused("select_only")
            index += 1
            opening = self._at(index)
            first = self._at(index + 1)
            if (
                opening is None
                or not opening.is_symbol("(")
                or first is None
                or not first.is_word("SELECT")
            ):
                raise SelectRefused("select_only")
            close = self._closing(index)
            if name.value in self.ctes:
                raise SelectRefused("select_only")
            self.ctes[name.value] = close
            index = close + 1
            token = self._at(index)
            if token is not None and token.is_symbol(","):
                index += 1
                continue
            if token is not None and token.is_word("SELECT"):
                return
            raise SelectRefused("select_only")

    # -- words ---------------------------------------------------------------
    def _check_word(self, index: int, frame: _Frame | None) -> None:
        token = self.tokens[index]
        word = token.value
        if word == "INTO":
            raise SelectRefused("no_into")
        if word == "FOR" and not (frame is not None and frame.expression):
            following = self._at(index + 1)
            if following is not None and following.is_word(*_LOCK_WORDS):
                raise SelectRefused("no_for_update")
            raise SelectRefused("select_only")
        if word == "WITH" and index != 0:
            raise SelectRefused("select_only")
        if word in FORBIDDEN_WORDS:
            raise SelectRefused("select_only")

    # -- table references ----------------------------------------------------
    def _reference(self, index: int, stack: list[_Frame]) -> int:
        """Check the reference starting at ``index``; return where to go on."""
        token = self._at(index)
        if token is None:
            raise SelectRefused("table_not_allowed")
        if token.is_symbol("("):
            first = self._at(index + 1)
            if first is None or not first.is_word("SELECT"):
                raise SelectRefused("table_not_allowed")
            stack.append(_Frame(from_subquery=True))
            return index + 1
        parts: list[Token] = []
        position = index
        while True:
            part = self._at(position)
            if part is None or not part.is_ident:
                raise SelectRefused("table_not_allowed")
            parts.append(part)
            position += 1
            dot = self._at(position)
            if dot is not None and dot.is_symbol("."):
                position += 1
                continue
            break
        following = self._at(position)
        if following is not None and following.is_symbol("("):
            raise SelectRefused("table_not_allowed")
        self._check_name(parts, index)
        return self._after_reference(position)

    def _check_name(self, parts: list[Token], index: int) -> None:
        if len(parts) == 1:
            name = parts[0].value
            visible_after = self.ctes.get(name)
            if visible_after is not None and index > visible_after:
                return
            if name.startswith("M_"):
                return
        elif len(parts) == 2 and parts[0].value in ALLOWED_SCHEMAS and parts[1].value:
            return
        raise SelectRefused("table_not_allowed")

    def _after_reference(self, index: int) -> int:
        token = self._at(index)
        if token is not None and token.is_word("AS"):
            alias = self._at(index + 1)
            if alias is None or not self._aliasable(alias):
                raise SelectRefused("table_not_allowed")
            index += 2
        elif token is not None and self._aliasable(token):
            index += 1
        token = self._at(index)
        if token is None or token.is_symbol(",", ")"):
            return index
        if token.kind == "word":
            if token.value in _AFTER_REF:
                return index
            self._check_word(index, None)
        raise SelectRefused("table_not_allowed")

    @staticmethod
    def _aliasable(token: Token) -> bool:
        return token.kind == "quoted" or (token.kind == "word" and token.value not in _NOT_ALIAS)

    # -- parentheses -----------------------------------------------------------
    def _open(self, index: int) -> _Frame:
        before = self._at(index - 1)
        inner = self._at(index + 1)
        if before is not None and before.is_ident:
            dot = self._at(index - 2)
            if dot is not None and dot.is_symbol("."):
                self._check_qualified_call(index - 1)
            elif before.kind == "quoted" and (index - 1) not in self.cte_header:
                raise SelectRefused("table_not_allowed")
        expression = (
            before is not None
            and before.kind == "word"
            and before.value in _EXPRESSION_FROM_FUNCTIONS
            and self._keyword_at(index - 1)
            and not (inner is not None and inner.is_word("SELECT"))
        )
        return _Frame(expression=expression)

    def _keyword_at(self, index: int) -> bool:
        """False where a word can only be an identifier: after ``.`` or ``AS``, before ``.``."""
        before = self._at(index - 1)
        after = self._at(index + 1)
        if before is not None and (before.is_symbol(".") or before.is_word("AS")):
            return False
        return not (after is not None and after.is_symbol("."))

    def _closes_from(self, index: int) -> bool:
        """Does the word at ``index`` really open the clause that ends a FROM list?

        Only reserved words are in :data:`_FROM_END`; on top of that the word
        must stand in a keyword position and be followed by what its clause
        demands. Anything else leaves the FROM list open, so the next ``,`` at
        this level is still read as a table separator (the safe direction).
        """
        word = self.tokens[index].value
        if word not in _FROM_END or not self._keyword_at(index):
            return False
        following = self._at(index + 1)
        if word in ("GROUP", "ORDER"):
            return following is not None and following.is_word("BY")
        if word == "LIMIT":
            return following is not None and following.kind in ("number", "param")
        if word in _SET_OPERATORS:
            if following is not None and following.is_symbol("("):
                # "(" is also the shape of a call ("MINUS(1)"): only a query in
                # parentheses opens the next branch.
                inner = self._at(index + 2)
                return inner is not None and (inner.is_word("SELECT") or inner.is_symbol("("))
            return following is not None and following.is_word("ALL", "DISTINCT", "SELECT")
        # WHERE, HAVING: a condition must follow
        return following is not None and not following.is_symbol(",", ")")

    def _check_qualified_call(self, last: int) -> None:
        parts = [self.tokens[last]]
        position = last
        while True:
            dot = self._at(position - 1)
            part = self._at(position - 2)
            if dot is None or not dot.is_symbol(".") or part is None or not part.is_ident:
                break
            parts.insert(0, part)
            position -= 2
        if len(parts) != 2 or parts[0].value not in ALLOWED_SCHEMAS:
            raise SelectRefused("table_not_allowed")

    # -- the walk ------------------------------------------------------------
    def run(self) -> None:
        if self.tokens[0].is_word("WITH"):
            self._declare_ctes()
        stack = [_Frame()]
        index = 0
        while index < self.size:
            token = self.tokens[index]
            frame = stack[-1]
            if token.kind == "symbol":
                if token.text == "(":
                    stack.append(self._open(index))
                elif token.text == ")":
                    closed = stack.pop()
                    if not stack:
                        raise SelectRefused("unreadable")
                    if closed.from_subquery:
                        index = self._after_reference(index + 1)
                        continue
                elif token.text == "," and frame.in_from:
                    index = self._reference(index + 1, stack)
                    continue
            elif token.kind == "word":
                word = token.value
                if index != 0:
                    self._check_word(index, frame)
                if frame.expression and (word == "SELECT" or word in _SET_OPERATORS):
                    # EXTRACT(...) and friends ignore FROM at their own level, so
                    # no query may start there: "TRIM((SELECT ..) UNION SELECT ..
                    # FROM t)" would read t unchecked.
                    raise SelectRefused("table_not_allowed")
                if word == "FROM" and not frame.expression:
                    frame.in_from = True
                    index = self._reference(index + 1, stack)
                    continue
                # FROM and JOIN are reserved and always start a reference (a
                # false refusal at worst); APPLY is not reserved, so it starts
                # one only in a keyword position.
                if word == "JOIN" or (word == "APPLY" and self._keyword_at(index)):
                    frame.in_from = True
                    index = self._reference(index + 1, stack)
                    continue
                if self._closes_from(index):
                    frame.in_from = False
            index += 1
        if len(stack) != 1:
            raise SelectRefused("unreadable")
