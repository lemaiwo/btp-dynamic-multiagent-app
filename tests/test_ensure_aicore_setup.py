"""Tests for scripts/ensure_aicore_setup.py.

The script runs as an MTA deploy hook, where a wrong decision is expensive:
creating a group that already exists fails the deploy, and silently declaring
success on a group stuck in ERROR gives you an app with no usable models. So
the decision logic is exercised here against a fake resource_groups client
rather than a live AI Core tenant.

Run:  python tests/test_ensure_aicore_setup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from ai_api_client_sdk.exception import AIAPINotFoundException  # noqa: E402

import ensure_aicore_setup as mod  # noqa: E402

FAILED = 0
PASSED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if ok:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")


def not_found() -> AIAPINotFoundException:
    return AIAPINotFoundException(
        description="not found", error_code="404", error_message="nope", request_id="r1"
    )


class FakeGroup:
    def __init__(self, status):
        self.status = status


class FakeGroups:
    """Returns each queued status in turn; `None` means 404."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.created: list[str] = []
        self.gets = 0

    def get(self, group_id):
        self.gets += 1
        status = self._statuses[min(self.gets - 1, len(self._statuses) - 1)]
        if status is None:
            raise not_found()
        return FakeGroup(status)

    def create(self, group_id, labels=None):
        self.created.append(group_id)
        return FakeGroup("PROVISIONING")


class FakeClient:
    def __init__(self, statuses):
        self.resource_groups = FakeGroups(statuses)


class FakeClock:
    """Advances only when the code under test sleeps, so no test really waits."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def run(statuses, timeout_s=300.0):
    client = FakeClient(statuses)
    clock = FakeClock()
    ok = mod.ensure_resource_group(
        client,
        "elia-rg",
        timeout_s=timeout_s,
        sleep=clock.sleep,
        now=clock.now,
        log=lambda *a, **k: None,
    )
    return ok, client.resource_groups


def main() -> None:
    print("\nensure_resource_group")

    ok, groups = run(["PROVISIONED"])
    check("existing PROVISIONED group succeeds", ok is True)
    check("existing group is not re-created", groups.created == [], f"created={groups.created}")

    ok, groups = run([None, "PROVISIONING", "PROVISIONED"])
    check("missing group is created", groups.created == ["elia-rg"], f"created={groups.created}")
    check("waits through PROVISIONING then succeeds", ok is True)

    ok, groups = run(["ERROR"])
    check("group in ERROR fails", ok is False)

    ok, groups = run([None, "ERROR"])
    check("group that fails to provision fails", ok is False)

    ok, groups = run(["PROVISIONING"], timeout_s=30.0)
    check("stuck in PROVISIONING times out as failure", ok is False)
    check("timeout actually polled more than once", groups.gets > 1, f"gets={groups.gets}")

    print("\n_status_of")
    check("reads a plain string status", mod._status_of(FakeGroup("PROVISIONED")) == "PROVISIONED")

    from ai_api_client_sdk.models.resource_group_status import ResourceGroupStatus

    check(
        "unwraps a ResourceGroupStatus enum",
        mod._status_of(FakeGroup(ResourceGroupStatus.PROVISIONED)) == "PROVISIONED",
    )
    check("missing status is UNKNOWN, not a crash", mod._status_of(FakeGroup(None)) == "UNKNOWN")

    print("\n_env_bool")
    import os

    os.environ["_T_BOOL"] = "false"
    check("explicit false wins over a true default", mod._env_bool("_T_BOOL", True) is False)
    os.environ["_T_BOOL"] = "  "
    check("blank falls back to the default", mod._env_bool("_T_BOOL", True) is True)
    del os.environ["_T_BOOL"]
    check("unset falls back to the default", mod._env_bool("_T_BOOL", False) is False)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
