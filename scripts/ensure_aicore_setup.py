"""Ensure the SAP AI Core resource group this app bills to actually exists.

AI Core consumption is measured per resource group, so a landscape that needs
its own line in the bill points AICORE_RESOURCE_GROUP at a dedicated id.
Nothing creates that group for you: the `aicore` service binding is
tenant-wide and names no group, and a group exists only once someone POSTs it.
This script does that POST, idempotently, so a fresh landscape deploys without
a manual click in the AI Core cockpit.

It creates the group and nothing else. Model deployments are deliberately out
of scope: they bill real money and take minutes to reach RUNNING, which is not
something to do implicitly from a deploy hook. Run scripts/deploy_claude.py for
those, after the group exists.

Credentials resolve the same way the app's own resolve: AICoreV2Client.from_env
reads AICORE_* environment variables and falls back to the bound service in
VCAP_SERVICES, so this runs unchanged from a local .env and from a CF task.

Run:  python scripts/ensure_aicore_setup.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# The tenant-wide group. It always exists and cannot be created, so asking for
# it is a no-op rather than an error -- that is what an unconfigured landscape
# inherits from mta.yaml's default.
DEFAULT_GROUP = "default"

POLL_SECONDS = 10


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _status_of(resource_group) -> str:
    """Read a resource group's status as a plain string.

    ResourceGroup.from_dict only converts the enum when the payload carries a
    `resource_group_status` key, but the API sends `status`, so the attribute
    is sometimes a ResourceGroupStatus and sometimes a bare string.
    """
    status = getattr(resource_group, "status", None)
    if status is None:
        return "UNKNOWN"
    return getattr(status, "value", None) or str(status)


def ensure_resource_group(
    client,
    group_id: str,
    timeout_s: float = 300.0,
    sleep=time.sleep,
    now=time.monotonic,
    log=print,
) -> bool:
    """Create `group_id` if absent, then wait until it leaves PROVISIONING.

    Returns True when the group is usable, False when it ended in ERROR or was
    still provisioning when the timeout expired. Raises whatever the SDK raises
    for anything else (401/403 on a key without admin scope, most importantly)
    so the caller decides whether that is fatal.
    """
    from ai_api_client_sdk.exception import AIAPINotFoundException

    try:
        group = client.resource_groups.get(group_id)
        log(f"Resource group '{group_id}' exists (status={_status_of(group)}).")
    except AIAPINotFoundException:
        log(f"Resource group '{group_id}' not found; creating it.")
        client.resource_groups.create(group_id)
        log(f"Created resource group '{group_id}'.")

    deadline = now() + timeout_s
    last_status = None
    while True:
        status = _status_of(client.resource_groups.get(group_id))
        if status != last_status:
            log(f"  status={status}")
            last_status = status
        if status == "PROVISIONED":
            return True
        if status == "ERROR":
            log(f"Resource group '{group_id}' is in ERROR; AI Core could not provision it.")
            return False
        if now() >= deadline:
            log(
                f"Timed out after {timeout_s:.0f}s waiting for '{group_id}' to reach "
                f"PROVISIONED (last status={status}). It may still finish on its own."
            )
            return False
        sleep(POLL_SECONDS)


def main() -> int:
    group_id = (os.environ.get("AICORE_RESOURCE_GROUP") or DEFAULT_GROUP).strip()
    strict = _env_bool("AICORE_ENSURE_STRICT", True)
    timeout_s = float(os.environ.get("AICORE_ENSURE_TIMEOUT", "300"))

    if group_id == DEFAULT_GROUP:
        print(
            f"AICORE_RESOURCE_GROUP is '{DEFAULT_GROUP}'; the tenant default group "
            "always exists. Nothing to do."
        )
        return 0

    # Documented in ResourceGroupsClient.create as 3-10 characters. Warn rather
    # than refuse: the server is the authority and the limit has moved before.
    if not 3 <= len(group_id) <= 10:
        print(
            f"Warning: resource group id '{group_id}' is {len(group_id)} characters; "
            "the AI Core SDK documents a 3-10 character limit. Creation may be rejected."
        )

    try:
        from ai_core_sdk.ai_core_v2_client import AICoreV2Client

        # The group to act on is the path parameter, not the header. Pin the
        # header to the tenant default -- which always exists -- so the admin
        # calls are not made under a group that is precisely what we are about
        # to create. Keyword arguments win over AICORE_RESOURCE_GROUP here.
        client = AICoreV2Client.from_env(resource_group=DEFAULT_GROUP)
    except Exception as exc:  # noqa: BLE001 - the message is the whole point
        print(f"Could not build an AI Core client: {exc}")
        print(
            "Expected AICORE_* environment variables or a bound 'aicore' service "
            "in VCAP_SERVICES."
        )
        return 1 if strict else 0

    print(f"AI Core base: {client.base_url}  resource-group: {group_id}")

    try:
        ok = ensure_resource_group(client, group_id, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - reported, then policy decides
        print(f"Could not ensure resource group '{group_id}': {exc}")
        print(
            "Creating a resource group needs an aicore service key with admin "
            "scope. If this key is restricted, create the group once in the AI "
            "Core cockpit and set AICORE_ENSURE_STRICT=false to stop the deploy "
            "hook from failing on it."
        )
        return 1 if strict else 0

    if not ok:
        return 1 if strict else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
