"""The ARC-1 batch reader: SQL text, result parsing, preflight, errors.

A stub stands in for the MCP server: ``for_run(None)`` returns a run object
that is entered with ``async with`` and records every ``direct_call_tool``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelRetry

from agents.destination_auth import DestinationUserRequired
from agents.oauth2 import OAuthAuthorizationRequired
from agents.reconcile import arc1_reader
from agents.reconcile.arc1_reader import Arc1Reader, Arc1ReadError, build_sql, parse_rows

QUERY_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}, "maxRows": {"type": "number"}},
    "required": ["sql"],
}


def _rows(columns: list[str], rows: list[list[Any]]) -> str:
    return json.dumps({"columns": columns, "rows": rows})


class StubRun:
    def __init__(self, server: StubServer):
        self.server = server

    async def __aenter__(self) -> StubRun:
        self.server.entered += 1
        if self.server.enter_error is not None:
            raise self.server.enter_error
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.server.exited += 1

    async def list_tools(self) -> list[Any]:
        return self.server.tools

    async def direct_call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.server.calls.append((name, dict(args)))
        answer = self.server.answers.pop(0) if self.server.answers else self.server.default
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer(name, args)
        return answer


class StubServer:
    def __init__(
        self,
        *,
        schema: dict[str, Any] | None = None,
        tools: list[Any] | None = None,
        answers: list[Any] | None = None,
        dd03l: list[str] | None = None,
    ):
        self.tools = (
            tools
            if tools is not None
            else [SimpleNamespace(name="SAPQuery", inputSchema=schema or QUERY_SCHEMA)]
        )
        self.answers = list(answers or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = 0
        self.exited = 0
        self.enter_error: BaseException | None = None
        fields = dd03l if dd03l is not None else ["MATNR", "MATKL", "MTART"]
        self.default = _rows(["FIELDNAME"], [[f] for f in fields])

    async def for_run(self, ctx: Any) -> StubRun:
        assert ctx is None
        return StubRun(self)


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _reader(server: StubServer, **kw: Any) -> Arc1Reader:
    kw.setdefault("sleep", Sleeps())
    return Arc1Reader(
        server, table="MARA", key_field="MATNR", fields=["MATNR", "MATKL"], **kw
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- build_sql ---------------------------------------------------------------


def test_build_sql_quotes_and_escapes_keys() -> None:
    sql = build_sql("MARA", ["MATNR", "MATKL"], "MATNR", ["000000000000000001", "O'X"])
    assert sql == (
        "SELECT MATNR, MATKL FROM MARA WHERE MATNR IN ('000000000000000001', 'O''X')"
    )


@pytest.mark.parametrize(
    ("table", "fields", "key_field"),
    [
        ("mara", ["MATNR"], "MATNR"),
        ("MARA;", ["MATNR"], "MATNR"),
        ("MARA\n", ["MATNR"], "MATNR"),
        ("MARA", ["MATNR, X"], "MATNR"),
        ("MARA", ["MATNR"], "MATNR OR 1=1"),
        ("/ABC/TAB", ["MATNR"], "MATNR"),
        ("MARA", [], "MATNR"),
    ],
)
def test_build_sql_refuses_bad_identifiers(table: str, fields: list[str], key_field: str) -> None:
    with pytest.raises(ValueError):
        build_sql(table, fields, key_field, ["1"])


@pytest.mark.parametrize("key", ["A\x00B", "A\nB", "AB\n", "A\tB", "", "x" * 41, "A;B", "A%B"])
def test_build_sql_refuses_bad_keys(key: str) -> None:
    with pytest.raises(ValueError):
        build_sql("MARA", ["MATNR"], "MATNR", [key])


def test_build_sql_refuses_no_keys_and_non_strings() -> None:
    with pytest.raises(ValueError):
        build_sql("MARA", ["MATNR"], "MATNR", [])
    with pytest.raises(ValueError):
        build_sql("MARA", ["MATNR"], "MATNR", [1])  # type: ignore[list-item]


# --- parse_rows --------------------------------------------------------------


def test_parse_rows_columns_and_lists() -> None:
    text = json.dumps({"columns": ["matnr", "MATKL"], "rows": [["1", None], ["2", 7]]})
    assert parse_rows(text) == [
        {"MATNR": "1", "MATKL": ""},
        {"MATNR": "2", "MATKL": "7"},
    ]


def test_parse_rows_column_objects_and_row_objects() -> None:
    text = json.dumps(
        {"columns": [{"name": "MATNR"}, {"name": "matkl"}], "rows": [{"MATNR": "1", "matkl": "A"}]}
    )
    assert parse_rows(text) == [{"MATNR": "1", "MATKL": "A"}]


def test_parse_rows_bare_list() -> None:
    text = json.dumps([{"matnr": "1", "MATKL": None}])
    assert parse_rows(text) == [{"MATNR": "1", "MATKL": ""}]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "42",
        json.dumps({"columns": ["A"], "rows": "x"}),
        json.dumps({"columns": ["A"], "rows": [["1", "2"]]}),
        json.dumps({"columns": ["A"], "rows": [[{"nested": 1}]]}),
        json.dumps([1, 2]),
        json.dumps({"something": "else"}),
        json.dumps([{"A": [1]}]),
    ],
)
def test_parse_rows_refuses_other_shapes(text: str) -> None:
    with pytest.raises(Arc1ReadError) as info:
        parse_rows(text)
    assert info.value.code == "unexpected_result"


# --- read --------------------------------------------------------------------


def test_read_sends_one_query_with_max_rows_and_maps_by_key() -> None:
    server = StubServer()

    async def go() -> dict[str, dict[str, str]]:
        async with _reader(server) as reader:
            await reader.preflight()
            server.calls.clear()
            server.answers.append(
                _rows(["MATNR", "MATKL"], [["000000000000000001", "A1"]])
            )
            return await reader.read(["000000000000000001", "000000000000000002"])

    out = _run(go())
    assert out == {"000000000000000001": {"MATNR": "000000000000000001", "MATKL": "A1"}}
    assert server.calls == [
        (
            "SAPQuery",
            {
                "sql": "SELECT MATNR, MATKL FROM MARA WHERE MATNR IN "
                "('000000000000000001', '000000000000000002')",
                # One more than the keys: a full answer (rows == keys) is then
                # provably not cut at maxRows, and a duplicate shows up.
                "maxRows": 3,
            },
        )
    ]
    assert server.entered == 1 and server.exited == 1


def test_read_full_result_is_fine() -> None:
    server = StubServer()

    async def go() -> dict[str, dict[str, str]]:
        async with _reader(server) as reader:
            await reader.preflight()
            server.answers.append(_rows(["MATNR", "MATKL"], [["1", "A"]]))
            return await reader.read(["1"])

    assert _run(go()) == {"1": {"MATNR": "1", "MATKL": "A"}}


def test_read_adds_target_from_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"sql": {}, "maxRows": {}, "target": {"type": "string"}},
        "required": ["sql"],
    }
    server = StubServer(schema=schema)

    async def go() -> None:
        async with _reader(server, target="SRC") as reader:
            await reader.preflight()
            server.answers.append(_rows(["MATNR", "MATKL"], []))
            await reader.read(["1"])

    _run(go())
    assert len(server.calls) == 2
    assert all(args["target"] == "SRC" for _, args in server.calls)
    assert server.calls[0][1] == {
        "sql": "SELECT FIELDNAME FROM DD03L WHERE TABNAME = 'MARA' AND AS4LOCAL = 'A'",
        "maxRows": 1000,
        "target": "SRC",
    }


def test_target_sid_slash_client_is_sent_verbatim() -> None:
    schema = {
        "type": "object",
        "properties": {"sql": {}, "maxRows": {}, "target": {"type": "string"}},
        "required": ["sql"],
    }
    server = StubServer(schema=schema)

    async def go() -> None:
        async with _reader(server, target="SBQ/200") as reader:
            await reader.preflight()
            server.answers.append(_rows(["MATNR", "MATKL"], []))
            await reader.read(["1"])

    _run(go())
    assert all(args["target"] == "SBQ/200" for _, args in server.calls)
    with pytest.raises(ValueError):
        Arc1Reader(server, table="MARA", key_field="MATNR", fields=["MATNR"], target="SBQ/200\nx")


def test_read_uses_schema_spelling_of_target_property() -> None:
    schema = {"type": "object", "properties": {"sql": {}, "SapSystem": {}}, "required": ["sql"]}
    server = StubServer(schema=schema)

    async def go() -> None:
        async with _reader(server, target="SRC") as reader:
            await reader.preflight()

    _run(go())
    assert server.calls[0][1]["SapSystem"] == "SRC"


def test_read_without_preflight_runs_it_first() -> None:
    server = StubServer()

    async def go() -> dict[str, dict[str, str]]:
        async with _reader(server) as reader:
            server.answers.extend(
                [_rows(["FIELDNAME"], [["MATNR"], ["MATKL"]]), _rows(["MATNR", "MATKL"], [])]
            )
            return await reader.read(["1"])

    assert _run(go()) == {}
    assert [args["sql"].split(" FROM ")[1].split()[0] for _, args in server.calls] == [
        "DD03L",
        "MARA",
    ]


def test_read_row_without_key_field_is_unexpected() -> None:
    server = StubServer()

    async def go() -> None:
        async with _reader(server) as reader:
            await reader.preflight()
            server.answers.append(_rows(["MATKL"], [["A"]]))
            await reader.read(["1"])

    with pytest.raises(Arc1ReadError) as info:
        _run(go())
    assert info.value.code == "unexpected_result"


def test_read_refuses_bad_key_before_any_call() -> None:
    server = StubServer()

    async def go() -> None:
        async with _reader(server) as reader:
            await reader.preflight()
            server.calls.clear()
            await reader.read(["1", "X'); DELETE\n"])

    with pytest.raises(ValueError):
        _run(go())
    assert server.calls == []


def test_constructor_refuses_bad_identifiers() -> None:
    with pytest.raises(ValueError):
        Arc1Reader(StubServer(), table="MARA X", key_field="MATNR", fields=["MATNR"])
    with pytest.raises(ValueError):
        Arc1Reader(StubServer(), table="MARA", key_field="MATNR", fields=["A B"])


# --- preflight ---------------------------------------------------------------


def _preflight(server: StubServer, **kw: Any) -> None:
    async def go() -> None:
        async with _reader(server, **kw) as reader:
            await reader.preflight()

    _run(go())


def test_preflight_no_target_adds_nothing() -> None:
    server = StubServer()
    _preflight(server)
    assert server.calls == [
        (
            "SAPQuery",
            {
                "sql": "SELECT FIELDNAME FROM DD03L WHERE TABNAME = 'MARA' AND AS4LOCAL = 'A'",
                "maxRows": 1000,
            },
        )
    ]


def test_preflight_target_not_supported_names_schema_properties() -> None:
    server = StubServer()
    with pytest.raises(Arc1ReadError) as info:
        _preflight(server, target="SRC")
    assert info.value.code == "target_not_supported"
    assert "sql" in str(info.value) and "maxRows" in str(info.value)
    assert "SRC" not in str(info.value)
    assert server.calls == []


def test_preflight_target_required() -> None:
    schema = {
        "type": "object",
        "properties": {"sql": {}, "system": {}},
        "required": ["sql", "system"],
    }
    server = StubServer(schema=schema)
    with pytest.raises(Arc1ReadError) as info:
        _preflight(server)
    assert info.value.code == "target_required"
    assert server.calls == []


def test_preflight_optional_target_without_config_adds_nothing() -> None:
    schema = {"type": "object", "properties": {"sql": {}, "targetId": {}}, "required": ["sql"]}
    server = StubServer(schema=schema)
    _preflight(server)
    assert "targetId" not in server.calls[0][1]


def test_preflight_query_tool_missing() -> None:
    server = StubServer(tools=[SimpleNamespace(name="SAPRead", inputSchema={})])
    with pytest.raises(Arc1ReadError) as info:
        _preflight(server)
    assert info.value.code == "query_tool_missing"


def test_preflight_field_not_in_table() -> None:
    server = StubServer(dd03l=["MATNR"])
    with pytest.raises(Arc1ReadError) as info:
        _preflight(server)
    assert info.value.code == "field_not_in_table"
    assert info.value.fields == ["MATKL"]


def test_preflight_accepts_dict_tools() -> None:
    server = StubServer(tools=[{"name": "SAPQuery", "inputSchema": QUERY_SCHEMA}])
    _preflight(server)
    assert len(server.calls) == 1


# --- errors ------------------------------------------------------------------


def _payload(**kw: Any) -> str:
    return json.dumps({"error": kw.pop("error", "SAP_FAILED"), **kw})


def _read_one(server: StubServer, sleeps: Sleeps) -> None:
    async def go() -> None:
        async with _reader(server, sleep=sleeps) as reader:
            await reader.preflight()
            server.calls.clear()
            await reader.read(["1"])

    _run(go())


def test_retryable_payload_is_retried_then_fails() -> None:
    server = StubServer()
    sleeps = Sleeps()
    server.answers = []
    # preflight answer first, then three retryable errors.
    server.answers.append(server.default)
    server.answers += [_payload(retryable=True, requestId="r-1")] * 3
    with pytest.raises(Arc1ReadError) as info:
        _read_one(server, sleeps)
    assert info.value.code == "arc1_failed"
    assert info.value.request_id == "r-1"
    assert len(server.calls) == 3
    assert len(sleeps.calls) == 2 and sleeps.calls[0] < sleeps.calls[1]


def test_retryable_payload_then_success() -> None:
    server = StubServer()
    server.answers = [
        server.default,
        _payload(retryable=True),
        _rows(["MATNR", "MATKL"], [["1", "A"]]),
    ]

    async def go() -> dict[str, dict[str, str]]:
        async with _reader(server) as reader:
            await reader.preflight()
            return await reader.read(["1"])

    assert _run(go()) == {"1": {"MATNR": "1", "MATKL": "A"}}


def test_retryable_payload_as_model_retry_is_retried() -> None:
    server = StubServer()
    sleeps = Sleeps()
    server.answers = [server.default] + [ModelRetry(_payload(retryable=True))] * 3
    with pytest.raises(Arc1ReadError) as info:
        _read_one(server, sleeps)
    assert info.value.code == "arc1_failed"
    assert len(server.calls) == 3


@pytest.mark.parametrize(
    "payload",
    [
        _payload(retryable=False, requestId="r-2"),
        _payload(error="POLICY_DENIED", retryable=True),
        _payload(error="Forbidden", retryable=True),
        _payload(error="OPERATION_NOT_ALLOWED", requestId="x"),
        _payload(error="table blocked", retryable=True),
    ],
)
def test_refused_payload_is_not_retried(payload: str) -> None:
    server = StubServer()
    sleeps = Sleeps()
    server.answers = [server.default, payload]
    with pytest.raises(Arc1ReadError) as info:
        _read_one(server, sleeps)
    assert info.value.code == "arc1_refused"
    assert len(server.calls) == 1
    assert sleeps.calls == []


def test_refused_payload_as_model_retry_is_not_retried() -> None:
    server = StubServer()
    sleeps = Sleeps()
    server.answers = [server.default, ModelRetry(_payload(retryable=False))]
    with pytest.raises(Arc1ReadError) as info:
        _read_one(server, sleeps)
    assert info.value.code == "arc1_refused"
    assert len(server.calls) == 1


def test_transport_error_is_retried_then_fails() -> None:
    server = StubServer()
    sleeps = Sleeps()
    server.answers = [server.default] + [ConnectionError("https://h/x?token=secret")] * 3
    with pytest.raises(Arc1ReadError) as info:
        _read_one(server, sleeps)
    assert info.value.code == "arc1_failed"
    assert "secret" not in str(info.value)
    assert len(server.calls) == 3
    assert len(sleeps.calls) == 2


@pytest.mark.parametrize(
    "exc",
    [
        OAuthAuthorizationRequired("https://arc1.example/mcp"),
        ExceptionGroup("tg", [OAuthAuthorizationRequired("https://arc1.example/mcp")]),
        DestinationUserRequired("srv", "dest"),
        ExceptionGroup("tg", [ExceptionGroup("inner", [DestinationUserRequired("srv", "dest")])]),
    ],
)
def test_sign_in_needed_is_not_retried(exc: BaseException) -> None:
    server = StubServer()
    sleeps = Sleeps()
    server.answers = [server.default, exc]
    with pytest.raises(Arc1ReadError) as info:
        _read_one(server, sleeps)
    assert info.value.code == "sign_in_needed"
    assert len(server.calls) == 1
    assert sleeps.calls == []


def test_sign_in_needed_on_enter() -> None:
    server = StubServer()
    server.enter_error = ExceptionGroup("tg", [OAuthAuthorizationRequired("k")])
    with pytest.raises(Arc1ReadError) as info:
        _preflight(server)
    assert info.value.code == "sign_in_needed"


def test_error_messages_are_fixed_texts() -> None:
    server = StubServer()
    server.answers = [
        server.default,
        _payload(error="X", message="row MATNR=SECRETVALUE missing", retryable=False),
    ]
    with pytest.raises(Arc1ReadError) as info:
        _read_one(server, Sleeps())
    assert "SECRETVALUE" not in str(info.value)
    assert info.value.message == str(info.value)


def test_logs_carry_code_and_table_only(caplog: pytest.LogCaptureFixture) -> None:
    server = StubServer()
    server.answers = [server.default, _payload(retryable=False)]
    with caplog.at_level("WARNING", logger=arc1_reader.logger.name):
        with pytest.raises(Arc1ReadError):
            _read_one(server, Sleeps())
    text = caplog.text
    assert "arc1_refused" in text and "MARA" in text
    assert "SELECT" not in text and "'1'" not in text


def test_only_sap_query_is_called() -> None:
    server = StubServer()

    async def go() -> None:
        async with _reader(server) as reader:
            await reader.preflight()
            server.answers.append(_rows(["MATNR", "MATKL"], []))
            await reader.read(["1"])

    _run(go())
    assert {name for name, _ in server.calls} == {"SAPQuery"}


# --- fix round 1 -------------------------------------------------------------


def _read_with(server: StubServer, answer: Any, keys: list[str], **kw: Any) -> Any:
    async def go() -> Any:
        async with _reader(server, **kw) as reader:
            await reader.preflight()
            server.calls.clear()
            server.answers.append(answer)
            return await reader.read(keys)

    return _run(go())


def test_read_duplicate_key_rows_are_ambiguous() -> None:
    answer = _rows(["MATNR", "MATKL"], [["1", "A"], ["1", "B"]])
    with pytest.raises(Arc1ReadError) as info:
        _read_with(StubServer(), answer, ["1", "2"])
    assert info.value.code == "ambiguous_key"


def test_read_more_rows_than_keys_fails() -> None:
    answer = _rows(["MATNR", "MATKL"], [["1", "A"], ["9", "B"]])
    with pytest.raises(Arc1ReadError) as info:
        _read_with(StubServer(), answer, ["1"])
    assert info.value.code == "unexpected_result"


def test_read_unrequested_key_fails() -> None:
    answer = _rows(["MATNR", "MATKL"], [["9", "B"]])
    with pytest.raises(Arc1ReadError) as info:
        _read_with(StubServer(), answer, ["1", "2"])
    assert info.value.code == "unexpected_result"


@pytest.mark.parametrize(
    "envelope",
    [
        {"truncated": True},
        {"hasMore": True},
        {"total": 5},
        {"totalRows": 5},
        {"rowCount": 5},
    ],
)
def test_read_truncation_marker_fails(envelope: dict[str, Any]) -> None:
    answer = json.dumps({"columns": ["MATNR", "MATKL"], "rows": [["1", "A"]], **envelope})
    with pytest.raises(Arc1ReadError) as info:
        _read_with(StubServer(), answer, ["1", "2", "3", "4", "5"])
    assert info.value.code == "truncated"


def test_read_envelope_counts_that_match_are_fine() -> None:
    answer = json.dumps(
        {"columns": ["MATNR", "MATKL"], "rows": [["1", "A"]], "rowCount": 1, "truncated": False}
    )
    assert _read_with(StubServer(), answer, ["1", "2"]) == {"1": {"MATNR": "1", "MATKL": "A"}}


def test_parse_answer_keeps_envelope() -> None:
    answer = arc1_reader.parse_answer(
        json.dumps({"columns": ["A"], "rows": [["1"]], "truncated": True, "rowCount": 7})
    )
    assert answer.rows == [{"A": "1"}]
    assert answer.truncated is True
    assert answer.total == 7


def test_read_padded_key_maps_to_requested_key() -> None:
    answer = _rows(["MATNR", "MATKL"], [["AB   ", "X"]])
    out = _read_with(StubServer(), answer, ["AB", "CD"])
    assert out == {"AB": {"MATNR": "AB   ", "MATKL": "X"}}


def test_read_requested_key_with_trailing_blank_maps_too() -> None:
    server = StubServer()
    answer = _rows(["MATNR", "MATKL"], [["AB", "X"]])
    out = _read_with(server, answer, ["AB ", "CD"])
    assert out == {"AB ": {"MATNR": "AB", "MATKL": "X"}}


def test_read_refuses_trailing_newline_key() -> None:
    server = StubServer()

    async def go() -> None:
        async with _reader(server) as reader:
            await reader.preflight()
            server.calls.clear()
            await reader.read(["AB\n"])

    with pytest.raises(ValueError):
        _run(go())
    assert server.calls == []
    assert arc1_reader.KEY_RE.pattern.endswith(r"\Z")


def test_preflight_dd03l_refused_has_own_code() -> None:
    server = StubServer()
    server.answers = [_payload(error="POLICY_DENIED", retryable=False, requestId="r-9")]
    with pytest.raises(Arc1ReadError) as info:
        _preflight(server)
    assert info.value.code == "preflight_refused"
    assert info.value.request_id == "r-9"


def test_check_fields_false_skips_dd03l() -> None:
    server = StubServer()

    async def go() -> dict[str, dict[str, str]]:
        async with _reader(server, check_fields=False) as reader:
            await reader.preflight()
            server.answers.append(_rows(["MATNR", "MATKL"], [["1", "A"]]))
            return await reader.read(["1"])

    assert _run(go()) == {"1": {"MATNR": "1", "MATKL": "A"}}
    assert len(server.calls) == 1
    assert " FROM MARA " in server.calls[0][1]["sql"]


def test_preflight_full_dd03l_answer_skips_verdict() -> None:
    fields = ["MATNR"] + [f"F{i:04d}" for i in range(999)]
    server = StubServer(dd03l=fields)  # exactly 1000 rows, MATKL not among them
    _preflight(server)


def test_preflight_dd03l_truncation_marker_skips_verdict() -> None:
    server = StubServer()
    server.answers = [
        json.dumps({"columns": ["FIELDNAME"], "rows": [["MATNR"]], "truncated": True})
    ]
    _preflight(server)


def test_aexit_error_is_mapped_when_nothing_propagates() -> None:
    server = StubServer()

    async def boom(*exc: Any) -> None:
        raise ExceptionGroup("tg", [ConnectionError("https://h/?token=secret")])

    async def go() -> None:
        async with _reader(server) as reader:
            reader._run.__aexit__ = boom

    with pytest.raises(Arc1ReadError) as info:
        _run(go())
    assert info.value.code == "arc1_failed"
    assert "secret" not in str(info.value)


def test_aexit_error_does_not_replace_propagating_error() -> None:
    server = StubServer()

    async def boom(*exc: Any) -> None:
        raise ConnectionError("closing")

    async def go() -> None:
        async with _reader(server) as reader:
            reader._run.__aexit__ = boom
            raise KeyError("body")

    with pytest.raises(KeyError):
        _run(go())


def test_target_with_unreadable_schema_says_so() -> None:
    server = StubServer(tools=[SimpleNamespace(name="SAPQuery", inputSchema=None)])
    with pytest.raises(Arc1ReadError) as info:
        _preflight(server, target="SRC")
    assert info.value.code == "target_not_supported"
    assert "could not be read" in info.value.message
    assert "It takes" not in info.value.message


def test_target_inside_any_of_is_found() -> None:
    schema = {
        "type": "object",
        "properties": {"sql": {}, "maxRows": {}},
        "anyOf": [{"properties": {"system": {"type": "string"}}}],
    }
    server = StubServer(schema=schema)
    _preflight(server, target="SRC")
    assert server.calls[0][1]["system"] == "SRC"


def test_target_required_inside_all_of() -> None:
    schema = {
        "type": "object",
        "allOf": [{"properties": {"sql": {}, "target": {}}, "required": ["sql", "target"]}],
    }
    server = StubServer(schema=schema)
    with pytest.raises(Arc1ReadError) as info:
        _preflight(server)
    assert info.value.code == "target_required"


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"columns": ["a", "A"], "rows": [["1", "2"]]}),
        json.dumps([{"a": "1", "A": "2"}]),
        "No rows found.",
    ],
)
def test_parse_rows_collisions_and_plain_text(text: str) -> None:
    with pytest.raises(Arc1ReadError) as info:
        parse_rows(text)
    assert info.value.code == "unexpected_result"


def test_read_refuses_more_keys_than_leave_room_for_the_extra_row() -> None:
    server = StubServer()
    keys = [str(i) for i in range(arc1_reader.MAX_BATCH_KEYS + 1)]

    async def go() -> None:
        async with _reader(server) as reader:
            await reader.preflight()
            await reader.read(keys)

    assert arc1_reader.MAX_BATCH_KEYS == arc1_reader.MAX_ROWS - 1 == 9_999
    with pytest.raises(ValueError):
        _run(go())


def test_a_duplicate_row_at_the_extra_row_is_an_ambiguous_key() -> None:
    server = StubServer()

    async def go() -> None:
        async with _reader(server) as reader:
            await reader.preflight()
            server.answers.append(_rows(["MATNR", "MATKL"], [["1", "A"], ["1", "B"]]))
            await reader.read(["1"])

    with pytest.raises(Arc1ReadError) as info:
        _run(go())
    assert info.value.code == "ambiguous_key"
    assert server.calls[-1][1]["maxRows"] == 2


# --- final review hygiene ----------------------------------------------------


def test_target_not_supported_names_only_plain_property_names() -> None:
    schema = {
        "type": "object",
        "properties": {
            "sql": {"type": "string"},
            "maxRows": {"type": "integer"},
            "bad\nname": {"type": "string"},
            "<b>x</b>": {"type": "string"},
            "y" * 41: {"type": "string"},
        },
        "required": ["sql"],
    }
    with pytest.raises(Arc1ReadError) as info:
        _preflight(StubServer(schema=schema), target="SRC")
    text = str(info.value)
    assert info.value.code == "target_not_supported"
    assert "sql" in text and "maxRows" in text
    assert "\n" not in text and "<b>" not in text and "y" * 41 not in text


def test_keys_equal_but_for_trailing_blanks_send_one_literal_and_get_both() -> None:
    server = StubServer()
    answer = _rows(["MATNR", "MATKL"], [["AB", "A"]])
    out = _read_with(server, answer, ["AB", "AB "])
    sql = server.calls[0][1]["sql"]
    assert sql.count("'AB'") == 1 and "'AB '" not in sql
    assert out["AB"] == out["AB "] == {"MATNR": "AB", "MATKL": "A"}


# --- wide field lists: several statements per batch ---------------------------
#
# ARC-1's IN-list chunker splits only the key list, so a statement whose fixed
# part (SELECT ... FROM ... WHERE key IN () plus one key) is wider than about
# 255 characters cannot be sent at all (SAP answers 400). The reader splits a
# wide field list over several statements, each with the key field first.

WIDE = [f"F{i:04d}" for i in range(1, 31)]  # 30 fields of 5 characters


def _wide_reader(server: StubServer, fields: list[str] = WIDE, **kw: Any) -> Arc1Reader:
    kw.setdefault("sleep", Sleeps())
    return Arc1Reader(
        server, table="MARA", key_field="MATNR", fields=fields, check_fields=False, **kw
    )


def _prefix(sql: str) -> str:
    """The statement without its key literals: what ARC-1 cannot split."""
    head, _, _ = sql.partition(" IN (")
    return head + " IN ()"


def _answer_by_select(rows_per_key: dict[str, dict[str, str]], **envelope: Any) -> Any:
    """A fake SAPQuery that answers the selected columns of the keys asked for."""

    def answer(name: str, args: dict[str, Any]) -> str:
        sql = args["sql"]
        cols = sql[len("SELECT "):sql.index(" FROM ")].split(", ")
        keys = [k.strip("'") for k in sql[sql.index(" IN (") + 5:-1].split(", ")]
        rows = [[rows_per_key[k][c] for c in cols] for k in keys if k in rows_per_key]
        return json.dumps({"columns": cols, "rows": rows, **envelope})

    return answer


def _full_row(key: str) -> dict[str, str]:
    return {"MATNR": key, **{f: f"{f}-{key}" for f in WIDE}}


def test_a_wide_field_list_is_split_over_statements_and_merged() -> None:
    server = StubServer()
    server.default = _answer_by_select({"1": _full_row("1"), "2": _full_row("2")})

    async def go() -> dict[str, dict[str, str]]:
        async with _wide_reader(server) as reader:
            await reader.preflight()
            server.calls.clear()
            return await reader.read(["1", "2", "3"])

    out = _run(go())
    assert out == {"1": _full_row("1"), "2": _full_row("2")}
    assert len(server.calls) == 2  # one statement per group, per batch
    sent_fields: list[str] = []
    for name, args in server.calls:
        assert name == "SAPQuery" and args["maxRows"] == 4
        sql = args["sql"]
        assert sql.startswith("SELECT MATNR, ")
        assert len(_prefix(sql)) + arc1_reader.KEY_LITERAL_MAX <= (
            arc1_reader.MAX_STATEMENT_PREFIX_CHARS
        )
        cols = sql[len("SELECT "):sql.index(" FROM ")].split(", ")
        assert cols.count("MATNR") == 1
        sent_fields += cols[1:]
    assert sent_fields == WIDE  # every field once, in the spec's order


def test_ten_fields_stay_one_statement_per_batch() -> None:
    fields = WIDE[:10]
    server = StubServer()
    server.default = _answer_by_select({"1": _full_row("1")})

    async def go() -> dict[str, dict[str, str]]:
        async with _wide_reader(server, fields) as reader:
            await reader.preflight()
            server.calls.clear()
            return await reader.read(["1"])

    out = _run(go())
    assert out == {"1": {"MATNR": "1", **{f: f"{f}-1" for f in fields}}}
    assert len(server.calls) == 1
    assert server.calls[0][1]["sql"].startswith("SELECT MATNR, F0001, ")


def test_a_key_missing_from_the_second_statement_is_unexpected() -> None:
    first = _answer_by_select({"1": _full_row("1"), "2": _full_row("2")})
    second = _answer_by_select({"1": _full_row("1")})  # "2" changed in between
    server = StubServer()

    async def go() -> Any:
        async with _wide_reader(server) as reader:
            await reader.preflight()
            server.answers[:] = [first, second]
            return await reader.read(["1", "2"])

    with pytest.raises(Arc1ReadError) as info:
        _run(go())
    assert info.value.code == "unexpected_result"


def test_a_field_that_does_not_fit_alone_is_refused_at_construction(monkeypatch) -> None:
    monkeypatch.setattr(arc1_reader, "MAX_STATEMENT_PREFIX_CHARS", 90)
    with pytest.raises(ValueError, match="fields too wide"):
        _wide_reader(StubServer(), ["A" * 30])


def test_the_grouping_is_logged_once_without_field_names(caplog) -> None:
    with caplog.at_level("INFO", logger="agents.reconcile.arc1_reader"):
        _wide_reader(StubServer())
    lines = [r.getMessage() for r in caplog.records if "statements per batch" in r.getMessage()]
    assert lines == ["[reconcile] MARA: 31 fields in 2 statements per batch"]
    assert not any("F0001" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("second", "code"),
    [
        ({"truncated": True}, "truncated"),
        ({"duplicate": True}, "ambiguous_key"),
    ],
)
def test_truncated_and_ambiguous_on_the_second_statement(second: dict[str, Any], code: str) -> None:
    rows = {"1": _full_row("1"), "2": _full_row("2")}
    first = _answer_by_select(rows)

    def second_answer(name: str, args: dict[str, Any]) -> str:
        data = json.loads(_answer_by_select(rows)(name, args))
        if second.get("duplicate"):
            data["rows"].append(list(data["rows"][0]))
        else:
            data.update(second)
        return json.dumps(data)

    server = StubServer()

    async def go() -> Any:
        async with _wide_reader(server) as reader:
            await reader.preflight()
            server.answers[:] = [first, second_answer]
            return await reader.read(["1", "2", "3"])

    with pytest.raises(Arc1ReadError) as info:
        _run(go())
    assert info.value.code == code
