"""Masking of ``SAPDiagnose`` results before the model or the UI sees them.

The fixtures under ``tests/fixtures/ide_diagnose`` are synthetic, shaped after
the ARC-1 ``SAPDiagnose`` tool description (live payloads could not be
captured yet; task L5 re-validates against real ones). Every fixture carries
the sentinels below wherever its shape has room for them; none may survive.

Run:  python -m pytest tests/test_ide_masking.py -q
"""

from __future__ import annotations

import copy
import json
import logging
import pickle
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ide.masking import (  # noqa: E402
    KEEP_DIGITS,
    SHAPES,
    USER_KEYS,
    Masker,
    mask_result,
    mask_text,
)

FIXTURES = ROOT / "tests" / "fixtures" / "ide_diagnose"
RAW = "RAWSENTINEL-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789"
SENTINELS = [
    "DEVUSER01",
    "jane.doe@example.com",
    "BE71 0961 2345 6769",
    "123456789012",
    "RAWSENTINEL-0123456789-ABCDEF",
]

KNOWN = [
    ("dumps_list.json", {"action": "dumps"}),
    ("dump_detail.json", {"action": "dumps", "id": "x"}),
    ("traces_list.json", {"action": "traces"}),
    ("trace_hitlist.json", {"action": "traces", "id": "t", "analysis": "hitlist"}),
    ("trace_statements.json", {"action": "traces", "id": "t", "analysis": "statements"}),
    ("trace_dbaccesses.json", {"action": "traces", "id": "t", "analysis": "dbAccesses"}),
    ("authorization_trace.json", {"action": "authorization_trace"}),
    ("gateway_errors_list.json", {"action": "gateway_errors"}),
    (
        "gateway_error_detail.json",
        {"action": "gateway_errors", "id": "g", "errorType": "Frontend Error"},
    ),
    ("odata_perf.json", {"action": "odata_perf", "url": "/x"}),
    ("trace_requests.json", {"action": "trace_requests"}),
]


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def masked(name: str, args: dict, masker: Masker | None = None) -> str:
    return mask_result("SAPDiagnose", args, load(name), masker or Masker())


def mask_dumps(payload: dict) -> dict:
    """Mask an inline dumps-list payload and parse the result."""
    return json.loads(
        mask_result("SAPDiagnose", {"action": "dumps"}, json.dumps(payload), Masker())
    )


def test_every_fixture_carries_the_sentinels():
    """Guards the fixtures themselves: a sentinel missing from the input
    would make the leak test below pass vacuously."""
    for name in [f for f, _ in KNOWN] + ["unknown_shape.json", "not_json.txt"]:
        text = load(name)
        for s in SENTINELS:
            assert s in text, f"{name} lacks {s}"


@pytest.mark.parametrize("fixture,args", KNOWN)
def test_no_sentinel_survives(fixture, args):
    out = masked(fixture, args)
    for s in SENTINELS:
        assert s.lower() not in out.lower(), f"{s} survived in {fixture}"
    # Known shapes stay JSON and are not the metadata fallback.
    data = json.loads(out)
    assert data.get("shape") != "unknown"


@pytest.mark.parametrize("fixture,args", KNOWN)
def test_known_fixture_matches_its_shape(fixture, args):
    """Each fixture must hit a known shape; otherwise the leak test above
    only proves the fail-safe works."""
    data = json.loads(masked(fixture, args))
    assert not (data.get("masked") is True and data.get("shape") == "unknown")


def test_pseudonyms_are_stable_within_a_run():
    data = json.loads(masked("dumps_list.json", {"action": "dumps"}))
    users = [d["user"] for d in data["dumps"]]
    # DEVUSER01 twice (second time lower case) -> same pseudonym; DEVUSER02 next.
    assert users == ["USER_1", "USER_1", "USER_2"]


def test_pseudonyms_carry_over_between_results_of_one_masker():
    m = Masker()
    a = json.loads(masked("dumps_list.json", {"action": "dumps"}, m))
    b = json.loads(masked("authorization_trace.json", {"action": "authorization_trace"}, m))
    assert a["dumps"][0]["user"] == "USER_1"
    assert {r["user"] for r in b["rows"]} == {"USER_1"}


def test_pseudonyms_restart_per_masker():
    first = json.loads(
        mask_result(
            "SAPDiagnose",
            {"action": "dumps"},
            json.dumps({"dumps": [{"user": "OTHERUSER"}]}),
            Masker(),
        )
    )
    second = json.loads(masked("dumps_list.json", {"action": "dumps"}, Masker()))
    assert first["dumps"][0]["user"] == "USER_1"
    assert second["dumps"][0]["user"] == "USER_1"


def test_user_name_replaced_in_free_text():
    data = json.loads(masked("dump_detail.json", {"action": "dumps", "id": "x"}))
    kap3 = next(c for c in data["chapters"] if c["id"] == "kap3")
    assert "User USER_1 triggered" in kap3["text"]
    assert "USER_1" in data["formattedText"]


def test_free_text_user_pattern_without_user_key():
    """A name only seen after ``User:`` / ``SY-UNAME =`` is pseudonymised
    and then replaced everywhere in the result."""
    payload = {
        "id": "d",
        "runtimeError": "X",
        "chapters": [{"id": "kap3", "title": "Error analysis", "text": "User: ALICE77 failed"}],
        "formattedText": "SY-UNAME = BOBUSER\nlater ALICE77 and BOBUSER again",
    }
    out = mask_result("SAPDiagnose", {"action": "dumps", "id": "d"}, json.dumps(payload), Masker())
    assert "ALICE77" not in out and "BOBUSER" not in out
    data = json.loads(out)
    assert "USER_1" in data["chapters"][0]["text"]
    assert "USER_2" in data["formattedText"]


def test_unknown_shape_is_metadata_only():
    raw = load("unknown_shape.json")
    out = mask_result("SAPDiagnose", {"action": "dumps"}, raw, Masker())
    data = json.loads(out)
    assert data == {"masked": True, "shape": "unknown", "chars": len(raw), "keys": 1}
    for s in SENTINELS:
        assert s.lower() not in out.lower()


def test_not_json_is_metadata_only():
    raw = load("not_json.txt")
    out = mask_result("SAPDiagnose", {"action": "dumps"}, raw, Masker())
    assert json.loads(out) == {"masked": True, "shape": "unknown", "chars": len(raw), "keys": 0}


@pytest.mark.parametrize(
    "tool,args,payload",
    [
        # Right payload, wrong action: no shape is keyed by (traces, list) for dumps.
        ("SAPDiagnose", {"action": "traces"}, {"dumps": [{"user": "DEVUSER01"}]}),
        # Unknown action.
        ("SAPDiagnose", {"action": "brand_new"}, {"dumps": [{"user": "DEVUSER01"}]}),
        # Not SAPDiagnose at all.
        ("SAPRead", {"action": "dumps"}, {"dumps": [{"user": "DEVUSER01"}]}),
        # Top-level list instead of an object.
        ("SAPDiagnose", {"action": "dumps"}, [{"user": "DEVUSER01"}]),
        # Detail call whose payload lacks the detail keys.
        ("SAPDiagnose", {"action": "dumps", "id": "x"}, {"user": "DEVUSER01"}),
        # Non-string action.
        ("SAPDiagnose", {"action": ["dumps"]}, {"dumps": [{"user": "DEVUSER01"}]}),
    ],
)
def test_unmatched_inputs_fail_safe(tool, args, payload):
    out = mask_result(tool, args, json.dumps(payload), Masker())
    data = json.loads(out)
    assert data["masked"] is True and data["shape"] == "unknown"
    assert "DEVUSER01" not in out


def test_unknown_shape_never_returns_key_names():
    """Review #8: a lower-case key can be a value (``{"devuser01": 1}``)."""
    payload = {"devuser01": 1, "jane.doe@example.com": 2, "ok_key": 3}
    out = mask_result("SAPDiagnose", {"action": "x"}, json.dumps(payload), Masker())
    assert json.loads(out)["keys"] == 3
    assert "devuser01" not in out and "ok_key" not in out


def test_known_fields_survive():
    d = json.loads(masked("dumps_list.json", {"action": "dumps"}))["dumps"][0]
    assert d["id"] == "20261003101530DEVHOST_DEMO_00"
    assert d["runtimeError"] == "COMPUTE_INT_ZERODIVIDE"
    assert d["exception"] == "CX_SY_ZERODIVIDE"
    assert d["program"] == "ZCL_DEMO_CALC=================CP"
    assert d["include"] == "ZCL_DEMO_CALC=================CM001"
    assert d["line"] == 12
    assert d["timestamp"] == "2026-10-03T10:15:30Z"
    assert d["client"] == "100"

    db = json.loads(
        masked("trace_dbaccesses.json", {"action": "traces", "id": "t", "analysis": "dbAccesses"})
    )
    assert [a["table"] for a in db["dbAccesses"]] == ["ZDEMO_T", "ZDEMO_ACC"]
    assert db["dbAccesses"][0]["executions"] == 3
    assert db["dbAccesses"][0]["duration"] == 8900

    hit = json.loads(
        masked("trace_hitlist.json", {"action": "traces", "id": "t", "analysis": "hitlist"})
    )
    # grossTime is in KEEP_DIGITS: a 9-digit runtime is a measurement, not an id.
    assert hit["hitlist"][0]["grossTime"] == 123456789
    assert hit["hitlist"][0]["hits"] == 3

    auth = json.loads(masked("authorization_trace.json", {"action": "authorization_trace"}))
    row = auth["rows"][0]
    assert (row["authObject"], row["field1"], row["value1"], row["rc"]) == (
        "S_TCODE",
        "TCD",
        "ZDEMO",
        4,
    )
    assert row["program"] == "ZDEMO_REPORT"
    assert row["timestamp"] == "2026-10-03T10:16:00Z"

    gw = json.loads(
        masked("gateway_error_detail.json", {"action": "gateway_errors", "id": "g"})
    )
    assert gw["errorType"] == "Frontend Error"
    assert gw["service"] == "ZDEMO_SRV"
    assert gw["callStack"][0]["line"] == 31


def test_numeric_values_with_many_digits_are_redacted():
    payload = {"dumps": [{"client": 123456789012, "line": 12, "duration": 123456789012}]}
    row = mask_dumps(payload)["dumps"][0]
    assert row["client"] == "[NUMBER]"
    assert row["line"] == 12
    assert row["duration"] == 123456789012


def test_redaction_markers_present():
    out = masked("gateway_errors_list.json", {"action": "gateway_errors"})
    assert "[EMAIL]" in out and "[IBAN]" in out and "[NUMBER]" in out


def test_variable_values_are_replaced():
    """Review #3: a 40-char cut keeps names and short numbers. Only the
    variable NAME and harmless scalars survive."""
    data = json.loads(masked("dump_detail.json", {"action": "dumps", "id": "x"}))
    kap8 = next(c for c in data["chapters"] if c["id"] == "kap8")
    lines = dict(line.split(" = ", 1) for line in kap8["text"].splitlines())
    assert lines["LV_DIVISOR"] == "0"
    assert lines["LV_RAW"] == f"[VALUE len={len(RAW)}]"
    assert lines["LV_IBAN"] == "[VALUE len=19]"
    assert lines["SY-UNAME"] == "USER_1"
    ft = dict(
        line.split(" = ", 1) for line in data["formattedText"].splitlines() if " = " in line
    )
    assert ft["LV_RAW"] == f"[VALUE len={len(RAW)}]"
    assert ft["LV_PARTNER"] == "[VALUE len=12]"
    assert ft["SY-UNAME"] == "USER_1"


def test_variable_chapter_detected_by_title():
    long_value = "A" * 100
    payload = {
        "id": "d",
        "chapters": [{"id": "kapX", "title": "Chosen Variables", "text": f"LV_X = {long_value}"}],
    }
    data = json.loads(
        mask_result("SAPDiagnose", {"action": "dumps", "id": "d"}, json.dumps(payload), Masker())
    )
    assert data["chapters"][0]["text"] == "LV_X = [VALUE len=100]"


def test_sections_keyed_by_kap_are_handled():
    payload = {
        "id": "d",
        "sections": {"kap0": "Short", "kap8": "LV_X = " + "B" * 100 + "\nDEVUSER01"},
        "user": "DEVUSER01",
    }
    out = mask_result("SAPDiagnose", {"action": "dumps", "id": "d"}, json.dumps(payload), Masker())
    data = json.loads(out)
    assert "DEVUSER01" not in out and "BBBB" not in out
    assert data["sections"]["kap0"] == "Short"
    assert data["sections"]["kap8"].splitlines()[0] == "LV_X = [VALUE len=100]"


def test_long_values_are_truncated():
    payload = {"dumps": [{"shortText": "x" * 2500}]}
    data = mask_dumps(payload)
    text = data["dumps"][0]["shortText"]
    assert text == "x" * 2000 + "[… truncated 500 chars]"


def test_user_name_in_long_value_replaced_before_cut():
    payload = {"dumps": [{"user": "DEVUSER01", "shortText": "y" * 1990 + " DEVUSER01 tail"}]}
    out = mask_result("SAPDiagnose", {"action": "dumps"}, json.dumps(payload), Masker())
    assert "DEVUSER" not in out
    assert "USER_" in json.loads(out)["dumps"][0]["shortText"]


def test_user_keys_case_insensitive_and_non_string():
    payload = {
        "rows": [
            {"UserName": "ALPHA1", "SY-UNAME": "BETA22", "BNAME": "ALPHA1", "owner": 4711,
             "authObject": "S_TCODE"},
        ]
    }
    out = mask_result(
        "SAPDiagnose", {"action": "authorization_trace"}, json.dumps(payload), Masker()
    )
    row = json.loads(out)["rows"][0]
    assert row["UserName"] == "USER_1" and row["BNAME"] == "USER_1"
    assert row["SY-UNAME"] == "USER_2"
    assert row["owner"] == "USER_3"
    assert "ALPHA1" not in out and "BETA22" not in out


def test_user_name_word_boundary():
    """Replacement must not eat a longer identifier that merely contains the name."""
    payload = {"dumps": [{"user": "DEMO", "program": "ZDEMO_REPORT", "shortText": "by DEMO."}]}
    data = mask_dumps(payload)
    row = data["dumps"][0]
    assert row["program"] == "ZDEMO_REPORT"
    assert row["shortText"] == "by USER_1."


def test_input_is_not_mutated_and_args_unchanged():
    args = {"action": "dumps"}
    before = copy.deepcopy(args)
    masked("dumps_list.json", args)
    assert args == before


def test_mask_text_redacts_email_iban_digits():
    text = (
        "Please check dump of User: DEVUSER01, mail jane.doe@example.com, "
        "IBAN BE71 0961 2345 6769, partner 123456789012 and 1234 5678 9012. "
        "DEVUSER01 again. Program ZDEMO_REPORT line 12."
    )
    out = mask_text(text)
    for s in SENTINELS[:4]:
        assert s.lower() not in out.lower()
    assert "1234 5678 9012" not in out
    assert "[EMAIL]" in out and "[IBAN]" in out and "[NUMBER]" in out
    assert "User: USER_1" in out and out.count("USER_1") == 2
    assert "ZDEMO_REPORT line 12" in out


def test_mask_text_does_not_cut():
    text = "a" * 3000
    assert mask_text(text) == text


def test_mask_text_handles_benutzer_and_sy_uname():
    out = mask_text("Benutzer KLAUS99 und SY-UNAME = MARIA_X1.")
    assert "KLAUS99" not in out and "MARIA_X1" not in out


def test_masker_never_logs_values(caplog):
    caplog.set_level(logging.DEBUG)
    m = Masker()
    for fixture, args in KNOWN:
        masked(fixture, args, m)
    mask_result("SAPDiagnose", {"action": "dumps"}, load("unknown_shape.json"), m)
    mask_result("SAPDiagnose", {"action": "dumps"}, load("not_json.txt"), m)
    mask_text(load("not_json.txt"))
    for s in SENTINELS + ["USER_1"]:
        assert s.lower() not in caplog.text.lower()


def test_masker_map_is_never_serialised():
    m = Masker()
    masked("dumps_list.json", {"action": "dumps"}, m)
    assert "DEVUSER01" not in repr(m) and "DEVUSER01" not in str(m)
    with pytest.raises(TypeError):
        pickle.dumps(m)
    with pytest.raises(TypeError):
        json.dumps(m)
    with pytest.raises(TypeError):
        copy.deepcopy(m)
    assert not hasattr(m, "__dict__")


def test_contract_constants():
    expected_users = {
        "user", "uname", "username", "sapuser", "traceuser", "createdby", "changedby",
        "lastchangedby", "requestuser", "owner", "sy-uname", "syuname", "bname",
    }
    assert expected_users <= {k.lower() for k in USER_KEYS}
    assert {"timestamp", "line", "id", "duration", "grosstime", "nettime", "hits"} <= {
        k.lower() for k in KEEP_DIGITS
    }
    for name in [
        "dumps_list", "dump_detail", "traces_list", "trace_hitlist", "trace_statements",
        "trace_dbaccesses", "authorization_trace", "gateway_errors_list",
        "gateway_error_detail", "odata_perf", "trace_requests", "sql_trace_state",
        "sql_trace_directory",
    ]:
        assert name in SHAPES


def test_module_is_pure():
    """No I/O imports: the masker must not reach files, network or a DB."""
    src = (ROOT / "agents" / "ide" / "masking.py").read_text(encoding="utf-8")
    for banned in ("import os", "import httpx", "import sqlalchemy", "open(", "print("):
        assert banned not in src


# ---------------------------------------------------------------------------
# Fix round 1: one test per review finding (.sdd/abap-ide-1c/task-5-review.md)
# ---------------------------------------------------------------------------


def detail(payload: dict, masker: Masker | None = None) -> tuple[dict, str]:
    """Mask an inline dump-detail payload; returns (parsed, raw output)."""
    out = mask_result(
        "SAPDiagnose", {"action": "dumps", "id": "d"}, json.dumps(payload), masker or Masker()
    )
    return json.loads(out), out


def chapter_text(text: str, title: str = "Error analysis", cid: str = "kap3") -> str:
    data, _ = detail({"id": "d", "chapters": [{"id": cid, "title": title, "text": text}]})
    return data["chapters"][0]["text"]


# -- #1 Blocker: free-text user forms, no user key anywhere -------------------


@pytest.mark.parametrize(
    "text",
    [
        "User................ DEVUSER01",
        'User  "DEVUSER01"',
        "GET /sap/opu/odata/x?sap-user=DEVUSER01&x=1",
        "GET /sap/opu/odata/x?sap-user=devuser01&x=1",
        '{"user":"DEVUSER01","createdBy":"DEVUSER02"}',
        'payload was {"user":"DEVUSER01","createdBy":"DEVUSER02"} at that time',
        "sy-uname DEVUSER01",
        "uname: DEVUSER01",
        "Benutzer.......: DEVUSER01",
        "User​: DEV​USER01",
    ],
)
def test_free_text_user_forms(text):
    out = chapter_text(text + "\nlater DEVUSER01 and devuser01 again")
    assert "devuser01" not in out.lower()
    assert "devuser02" not in out.lower()
    assert "USER_1" in out


def test_embedded_json_string_is_masked_recursively():
    inner = {"user": "DEVUSER01", "message": "mail jane.doe@example.com", "secretField": "Jane Doe"}
    chapter = {"id": "kap3", "title": "T", "text": json.dumps(inner)}
    data, out = detail({"id": "d", "chapters": [chapter]})
    emb = json.loads(data["chapters"][0]["text"])
    assert emb == {"user": "USER_1", "message": "mail [EMAIL]"}
    assert "Jane Doe" not in out
    assert data["dropped_fields"] == 1


def test_names_learned_late_are_replaced_in_earlier_fields():
    payload = {
        "id": "d",
        "chapters": [
            {"id": "kap1", "title": "A", "text": "locked by DEVUSER01 again"},
            {"id": "kap2", "title": "B", "text": "User................ DEVUSER01"},
        ],
    }
    _, out = detail(payload)
    assert "DEVUSER01" not in out


# -- #2 Major: real ST22 name-line / value-line layout ------------------------


def test_formatted_text_two_line_variables():
    ft = (
        "Short text\n    Division by zero\n\nChosen variables\n"
        "LV_NAME\n    Jane Doe\n" + "LV_LONG\n    " + "Q" * 300 + "\n"
        "SY-SUBRC\n    4\nLV_FLAG\n    X\n"
    )
    data, out = detail({"id": "d", "formattedText": ft})
    assert "Jane Doe" not in out and "QQQQ" not in out
    lines = data["formattedText"].splitlines()
    assert "Chosen variables" in lines
    assert lines[lines.index("LV_NAME") + 1] == "    [VALUE len=8]"
    assert lines[lines.index("LV_LONG") + 1] == "    [VALUE len=300]"
    assert lines[lines.index("SY-SUBRC") + 1] == "    4"
    assert lines[lines.index("LV_FLAG") + 1] == "    X"
    assert "Division by zero" in data["formattedText"]


def test_st22_fixture_variables():
    data = json.loads(masked("dump_detail_st22.json", {"action": "dumps", "id": "x"}))
    for text in (data["chapters"][1]["text"], data["formattedText"]):
        assert "Jane Doe" not in text and "1234567" not in text and "4A616E" not in text
        lines = text.splitlines()
        assert lines[lines.index("LV_NAME") + 1] == "    [VALUE len=8]"
        assert lines[lines.index("LV_NAME") + 2] == "    [VALUE len=16]"
        assert lines[lines.index("LV_BP") + 1] == "    [VALUE len=7]"
        assert lines[lines.index("SY-SUBRC") + 1] == "    4"
        assert lines[lines.index("LV_FLAG") + 1] == "    X"
        assert lines[lines.index("LV_COUNT") + 1] == "    42"
    assert "User................ USER_1" in data["formattedText"]
    assert "Runtime Errors         COMPUTE_INT_ZERODIVIDE" in data["formattedText"]


def test_unindented_value_after_name_line_is_still_a_value():
    """Fail safe: the line after a name is a value even if it looks like a name."""
    out = chapter_text("LV_NAME\nJANE_DOE\nLV_B\n    1", "Chosen variables", "kap8")
    assert "JANE_DOE" not in out
    assert out.splitlines()[0] == "LV_NAME"


# -- #3 Major: allowlist instead of denylist ----------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("LV_NAME = Jane Doe", "LV_NAME = [VALUE len=8]"),
        ("LV_BP = 1234567", "LV_BP = [VALUE len=7]"),
        ("LV_N = 123456", "LV_N = 123456"),
        ("SY-SUBRC = 4", "SY-SUBRC = 4"),
        ("LV_FLAG = X", "LV_FLAG = X"),
        ("LV_OK = abap_true", "LV_OK = abap_true"),
        ("LV_EMPTY = ", "LV_EMPTY = "),
        ("LV_Q = 'Jane'", "LV_Q = [VALUE len=6]"),
        ("ME->MV_NAME = Jane", "ME->MV_NAME = [VALUE len=4]"),
        ("<LS_ROW>-NAME1 = Jane", "<LS_ROW>-NAME1 = [VALUE len=4]"),
        ("ZCL_X=>GV_NAME = Jane", "ZCL_X=>GV_NAME = [VALUE len=4]"),
    ],
)
def test_variable_value_rule(line, expected):
    assert chapter_text(line, "Chosen variables", "kap8") == expected


def test_sql_literals_replaced():
    payload = {
        "dbAccesses": [
            {
                "table": "KNA1",
                "statement": "SELECT * FROM kna1 WHERE name1 = 'Jane Doe' AND kunnr = '1234567' "
                'AND city = "Brussels" AND flag = \'X\'',
            }
        ]
    }
    out = mask_result(
        "SAPDiagnose",
        {"action": "traces", "id": "t", "analysis": "dbAccesses"},
        json.dumps(payload),
        Masker(),
    )
    stmt = json.loads(out)["dbAccesses"][0]["statement"]
    assert "Jane Doe" not in out and "1234567" not in out and "Brussels" not in out
    assert stmt == (
        "SELECT * FROM kna1 WHERE name1 = '[LITERAL]' AND kunnr = '[LITERAL]' "
        "AND city = '[LITERAL]' AND flag = 'X'"
    )


def test_message_literals_replaced_but_apostrophes_survive():
    payload = {"errors": [{"message": "Customer 'Jane Doe' can't be read, it doesn't exist"}]}
    out = mask_result("SAPDiagnose", {"action": "gateway_errors"}, json.dumps(payload), Masker())
    assert json.loads(out)["errors"][0]["message"] == (
        "Customer '[LITERAL]' can't be read, it doesn't exist"
    )


def test_unlisted_fields_dropped_and_counted():
    payload = {
        "dumps": [
            {"runtimeError": "X", "customerName": "Jane Doe", "nested": {"a": "Jane Doe"}},
            {"runtimeError": "Y", "requestBody": '{"Name":"Jane Doe"}'},
        ]
    }
    out = mask_result("SAPDiagnose", {"action": "dumps"}, json.dumps(payload), Masker())
    data = json.loads(out)
    assert "Jane Doe" not in out
    assert data["dumps"] == [{"runtimeError": "X"}, {"runtimeError": "Y"}]
    assert data["dropped_fields"] == 3


def test_fixture_payload_fields_are_dropped():
    gw = json.loads(masked("gateway_error_detail.json", {"action": "gateway_errors", "id": "g"}))
    assert "requestBody" not in gw and gw["dropped_fields"] == 1
    od = json.loads(masked("odata_perf.json", {"action": "odata_perf", "url": "/x"}))
    assert "responseSample" not in od["requests"][0]
    assert "dropped_fields" not in json.loads(masked("dumps_list.json", {"action": "dumps"}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("value1", "Jane 'Doe' <jane.doe@example.com>"),
        ("program", "Jane Doe"),
        ("line", "12; Jane Doe"),
        ("timestamp", "0475 12 34 56 78"),
        ("timestamp", "born 1980-01-01 Jane"),
        ("id", "Jane Doe"),
        ("hits", "Jane"),
    ],
)
def test_typed_field_with_unexpected_content_is_not_shown(field, value):
    """An identifier/number/time field is allowlisted for that type only."""
    payload = {"rows": [{"authObject": "S_TCODE", field: value}]}
    out = mask_result(
        "SAPDiagnose", {"action": "authorization_trace"}, json.dumps(payload), Masker()
    )
    row = json.loads(out)["rows"][0]
    assert row == {"authObject": "S_TCODE", field: f"[VALUE len={len(value)}]"}


# -- #4 Major: names embedded in identifiers ----------------------------------


@pytest.mark.parametrize(
    "text", ["object ZTEST_DEVUSER01 failed", "Z_DEVUSER01_MAIN", "path x_devuser01/tmp"]
)
def test_name_embedded_in_identifier_in_free_text(text):
    data, out = detail(
        {"id": "d", "user": "DEVUSER01", "chapters": [{"id": "kap3", "title": "T", "text": text}]}
    )
    assert "devuser01" not in data["chapters"][0]["text"].lower()
    assert "USER_1" in data["chapters"][0]["text"]


def test_identifier_fields_keep_object_names():
    """Documented exception: program/include keep ``_`` as a word character,
    because a finding has to open that object by its real name."""
    data, _ = detail(
        {"id": "d", "user": "DEMO", "program": "ZDEMO_REPORT", "include": "Z_DEMO_TOP",
         "formattedText": "x"}
    )
    assert data["program"] == "ZDEMO_REPORT" and data["include"] == "Z_DEMO_TOP"


# -- #5 Major: user keys by pattern -------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["userId", "modifiedBy", "creator", "executingUser", "author", "executedBy", "USER_NAME",
     "lastChangedBy", "objectOwner", "sapUser"],
)
def test_user_keys_by_pattern(key):
    payload = {"dumps": [{key: "DEVUSER01", "shortText": "by DEVUSER01"}]}
    out = mask_result("SAPDiagnose", {"action": "dumps"}, json.dumps(payload), Masker())
    row = json.loads(out)["dumps"][0]
    assert row[key] == "USER_1" and row["shortText"] == "by USER_1"
    assert "DEVUSER01" not in out


def test_user_names_as_dict_keys():
    payload = {"dumps": [{"byUser": {"DEVUSER01": 3, "DEVUSER02": 1}, "shortText": "DEVUSER02"}]}
    out = mask_result("SAPDiagnose", {"action": "dumps"}, json.dumps(payload), Masker())
    row = json.loads(out)["dumps"][0]
    assert row["byUser"] == {"USER_1": 3, "USER_2": 1}
    assert row["shortText"] == "USER_2"
    assert "DEVUSER" not in out


def test_authorization_key_is_not_a_user_key():
    payload = {"rows": [{"authObject": "S_TCODE", "user": "DEVUSER01"}]}
    out = mask_result(
        "SAPDiagnose", {"action": "authorization_trace"}, json.dumps(payload), Masker()
    )
    assert json.loads(out)["rows"][0]["authObject"] == "S_TCODE"


# -- #6 Major: ReDoS / performance --------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "a" * 300_000,
        "sy-uname" + " " * 300_000 + "x",
        "user" + "." * 300_000,
        "1" * 300_000,
        "BE71" + "A1" * 150_000,
        "'" * 300_000,
        "a@" * 150_000,
        "User: " * 50_000,
        "{" * 300_000,
        "LV_A" + "=" * 300_000,
    ],
)
def test_single_long_token_is_fast(token):
    import time

    payload = {
        "id": "d",
        "chapters": [
            {"id": "kap3", "title": "T", "text": token},
            {"id": "kap8", "title": "Chosen variables", "text": token},
        ],
        "formattedText": token,
        "user": "DEVUSER01",
    }
    raw = json.dumps(payload)
    start = time.perf_counter()
    out = mask_result("SAPDiagnose", {"action": "dumps", "id": "d"}, raw, Masker())
    mask_text(token)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"{elapsed:.2f}s"
    assert len(out) < 80_000  # every string is capped


def test_oversized_input_is_metadata_only():
    raw = json.dumps({"dumps": [{"shortText": "x" * 2_100_000, "user": "DEVUSER01"}]})
    out = mask_result("SAPDiagnose", {"action": "dumps"}, raw, Masker())
    assert json.loads(out) == {"masked": True, "shape": "unknown", "chars": len(raw), "keys": 0}


def test_name_replaced_before_the_per_string_cap():
    text = "y" * 19_995 + " DEVUSER01 tail"
    data, out = detail(
        {"id": "d", "user": "DEVUSER01", "chapters": [{"id": "kap3", "title": "T", "text": text}]}
    )
    assert "DEVUS" not in out
    assert "truncated" in data["chapters"][0]["text"]


# -- #7 Major: number / IBAN / e-mail variants --------------------------------


@pytest.mark.parametrize(
    "value,leftover",
    [
        ("BE71-0961-2345-6769", ["0961", "BE71", "6769"]),
        ("BE71.0961.2345.6769", ["0961", "BE"]),
        ("be71 0961 2345 6769", ["0961", "be7", "be["]),
        ("BE71096123456769", ["0961", "BE"]),
        ("0475-12-34-56-78", ["0475", "56"]),
        ("0475/12/34/56/78", ["0475", "56"]),
        ("+32 475 12 34 56", ["475", "34"]),
        ("josé@example.com", ["jos", "example"]),
        ("jane@münchen.example", ["jane", "nchen"]),
        ("jane.doe%40example.com", ["jane", "example"]),
        ("jane.doe%2540example.com", ["jane", "example"]),
        ("jane​.doe@example.com", ["jane", "example"]),
        ("jane.doe@exam​ple.com", ["jane", "ple.com"]),
        ("x" * 100 + "@example.com", ["xxx", "example"]),
    ],
)
def test_redaction_variants(value, leftover):
    for out in (mask_text(f"see {value} now"), chapter_text(f"see {value} now")):
        assert out.startswith("see [") and out.endswith("] now"), out
        for piece in leftover:
            assert piece not in out, out


# -- #8/#9/#10 Minors ----------------------------------------------------------


@pytest.mark.parametrize("value", ["-", "1", "AB", "*", "", "N/A", 1, "SYSTEM"])
def test_placeholder_user_values_are_not_replaced_everywhere(value):
    payload = {"dumps": [{"user": value, "shortText": "count 1 of 1 - AB * N/A SYSTEM_FAILURE"}]}
    row = mask_dumps(payload)["dumps"][0]
    assert row["shortText"] == "count 1 of 1 - AB * N/A SYSTEM_FAILURE"


def test_numeric_and_short_user_values_are_still_hidden():
    row = mask_dumps({"dumps": [{"user": "AB", "owner": 4711, "creator": "-"}]})["dumps"][0]
    assert row == {"user": "USER_1", "owner": "USER_2", "creator": "-"}


@pytest.mark.parametrize(
    "value,kept",
    [
        ("20261003101530DEVHOST_DEMO_00", True),
        ("20261003101530", True),
        ("0A1B2C3D4E5F", True),
        ("6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f", True),
        ("42", True),
        (42, True),
        (20261003101530, True),
        ("123456789012", False),
        (123456789012, False),
        ("1234567", False),
        ("0475 12 34 56 78", False),
    ],
)
def test_id_keeps_digits_only_when_system_generated(value, kept):
    row = mask_dumps({"dumps": [{"id": value, "requestId": value}]})["dumps"][0]
    assert (row["id"] == value) is kept
    assert (row["requestId"] == value) is kept


def test_keep_rule_does_not_leak_into_nested_text():
    """Review #10: the digit exemption belonged to the key and carried
    through lists; now each field has its own kind."""
    payload = {"dumps": [{"count": ["call 123456789012"], "date": "tel 123456789012"}]}
    out = mask_result("SAPDiagnose", {"action": "dumps"}, json.dumps(payload), Masker())
    assert "123456789012" not in out


# -- H: prefixed MCP tool names -----------------------------------------------


@pytest.mark.parametrize(
    "tool,known",
    [
        ("SAPDiagnose", True),
        ("arc1_SAPDiagnose", True),
        ("mcp__arc1__SAPDiagnose", True),
        ("NotSAPDiagnose", False),
        ("SAPDiagnose_x", False),
        ("SAPRead", False),
        (None, False),
    ],
)
def test_prefixed_tool_names(tool, known):
    out = mask_result(tool, {"action": "dumps"}, load("dumps_list.json"), Masker())
    assert (json.loads(out).get("shape") != "unknown") is known
    assert "DEVUSER01" not in out
