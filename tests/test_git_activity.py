"""The fixed-form activity lines of ``builtin:git``: what a run keeps of a
result is counts, fixed words and a refusal's code; no path, title, message,
content, branch, commit or URL."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402

from agents.git_tools import TOOL_NAMES, activity_summary  # noqa: E402

LEAK = "Zx9-leak"
CHECKS = [{"name": LEAK, "status": "success"}, {"name": LEAK, "status": "failure"},
          {"name": LEAK, "status": "pending"}, {"name": LEAK, "status": "weird"}]


@pytest.mark.parametrize("tool, result, line", [
    ("list_repositories", {"repositories": [{"repository": LEAK, "default_branch": LEAK},
                                            {"repository": LEAK, "error": "not_found"}]},
     "list_repositories: 2 repositories, 1 failed"),
    ("list_files", {"entries": [{"path": LEAK}] * 3, "truncated": True, "path": LEAK},
     "list_files: 3 entries, truncated yes"),
    ("read_repository_file", {"content": LEAK, "path": LEAK, "truncated": False},
     f"read_repository_file: {len(LEAK)} chars, truncated no"),
    ("list_branches", {"branches": [{"name": LEAK, "agent": True},
                                    {"name": LEAK, "agent": False}], "truncated": False},
     "list_branches: 2 branches, 1 agent branches, truncated no"),
    ("create_branch", {"branch": LEAK, "commit": LEAK}, "create_branch: created"),
    ("commit_files", {"files": [LEAK, LEAK], "deletes": [LEAK], "commit": LEAK, "url": LEAK},
     "commit_files: committed 2 files, 1 deletes"),
    ("open_pull_request", {"number": 3, "url": LEAK, "already_open": False},
     "open_pull_request: opened"),
    ("open_pull_request", {"number": 3, "url": LEAK, "already_open": True},
     "open_pull_request: already open"),
    ("get_pull_request", {"state": "open", "mergeable": True, "title": LEAK, "checks": CHECKS},
     "get_pull_request: state open, mergeable yes, 4 checks (1 success, 1 failure, "
     "1 pending, 1 unknown)"),
    ("get_pull_request", {"state": LEAK, "mergeable": None, "checks": []},
     "get_pull_request: state ?, mergeable unknown, 0 checks (0 success, 0 failure, "
     "0 pending, 0 unknown)"),
    ("get_pipeline_status", {"commit": LEAK, "ref": LEAK, "checks": CHECKS[:1]},
     "get_pipeline_status: 1 checks (1 success, 0 failure, 0 pending, 0 unknown)"),
])
def test_every_tool_has_a_fixed_line(tool, result, line):
    for name in (tool, f"git_{tool}", f"git_0_{tool}"):
        said = activity_summary(name, result)
        assert said == line and LEAK not in said


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_a_refusal_is_said_by_its_code_only(tool):
    assert activity_summary(tool, {"error": {"code": "outcome_unknown", "message": LEAK,
                                             "hint": LEAK}}) == "error: outcome_unknown"
    assert activity_summary(tool, {"error": {"code": f"bad {LEAK}"}}) == "error"
    assert activity_summary(tool, "a retry prompt " + LEAK) == f"{tool}: no summary"


@pytest.mark.parametrize("name", ["read_file", "ls", "github_list_files", "git_x_list_files",
                                  "list_pull_requests", "get_file", None, 3])
def test_other_tools_keep_their_normal_preview(name):
    assert activity_summary(name, {"content": LEAK}) is None


def test_the_registry_preview_uses_the_fixed_line():
    from agents.registry import _short_tool_output

    result = SimpleNamespace(tool_name="git_read_repository_file",
                             content={"content": LEAK, "truncated": False})
    assert _short_tool_output(result) == f"read_repository_file: {len(LEAK)} chars, truncated no"
    result = SimpleNamespace(tool_name="commit_files",
                             content={"files": [LEAK], "deletes": [], "url": LEAK})
    assert _short_tool_output(result) == "commit_files: committed 1 files, 0 deletes"
