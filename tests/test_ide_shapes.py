"""Result-shape recognition lives in ``agents.ide.shapes`` (plan B1).

Run:  python -m pytest tests/test_ide_shapes.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
(ROOT / "tests" / "_test_ide_shapes.db").unlink(missing_ok=True)
os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_shapes.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents.ide import shapes  # noqa: E402

T = "SAPDiagnose"

CASES = [
    ({"action": "dumps"}, {"dumps": []}, "dumps_list"),
    ({"action": "dumps", "id": "X1"}, {"chapters": []}, "dump_detail"),
    ({"action": "traces"}, {"traces": []}, "traces_list"),
    ({"action": "gateway_errors"}, {"errors": []}, "gateway_errors_list"),
    ({"action": "gateway_errors", "id": "A1"}, {"errorType": "x"}, "gateway_error_detail"),
    ({"action": "authorization_trace"}, {"rows": []}, "authorization_trace"),
    ({"action": "odata_perf"}, {"requests": []}, "odata_perf"),
]


@pytest.mark.parametrize("args,data,name", CASES)
def test_match_shape(args, data, name):
    assert shapes.match_shape(T, args, data) == name
    assert shapes.match_shape("arc1_" + T, args, data) == name


def test_other_tool_or_shape_is_none():
    assert shapes.match_shape("SAPRead", {}, {}) is None
    assert shapes.match_shape(T, {"action": "dumps"}, {"unknown": 1}) is None


def test_max_input():
    assert shapes.MAX_INPUT == 2_000_000


def test_findings_does_not_import_masking():
    import agents.ide.findings as f

    assert "masking" not in vars(f)
