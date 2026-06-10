"""Discover and deploy the most recent Claude model in SAP AI Core.

Reads AICORE_* from .env, authenticates, lists Claude models in the
foundation-models scenario, picks the latest (highest version), creates a
configuration + deployment, and waits until it's RUNNING. Skips creation
if a RUNNING deployment for the same model+resource-group already exists.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

AUTH_URL = os.environ["AICORE_AUTH_URL"]
CLIENT_ID = os.environ["AICORE_CLIENT_ID"]
CLIENT_SECRET = os.environ["AICORE_CLIENT_SECRET"]
BASE_URL = os.environ["AICORE_BASE_URL"].rstrip("/")
if not BASE_URL.endswith("/v2"):
    BASE_URL = f"{BASE_URL}/v2"
RG = os.environ.get("AICORE_RESOURCE_GROUP", "default")

SCENARIO_ID = "foundation-models"


def get_token() -> str:
    r = httpx.post(
        AUTH_URL,
        data={"grant_type": "client_credentials"},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def session(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "AI-Resource-Group": RG,
            "Content-Type": "application/json",
        },
        timeout=60,
    )


def claude_version_key(model_name: str) -> tuple:
    """Sort key: extract major/minor numbers from names like
    'anthropic--claude-opus-4-7' or 'claude-3-5-sonnet'."""
    nums = [int(n) for n in re.findall(r"\d+", model_name)]
    # opus > sonnet > haiku as a tiebreaker for same version
    tier = 2 if "opus" in model_name else 1 if "sonnet" in model_name else 0
    return (*nums, tier)


def list_claude_models(s: httpx.Client) -> list[dict]:
    r = s.get(f"/lm/scenarios/{SCENARIO_ID}/models")
    r.raise_for_status()
    models = r.json().get("resources", [])
    out = []
    for m in models:
        name = m.get("model") or m.get("name") or ""
        if "claude" in name.lower() or name.startswith("anthropic"):
            out.append(m)
    return out


def list_executables(s: httpx.Client) -> list[dict]:
    r = s.get(f"/lm/scenarios/{SCENARIO_ID}/executables")
    r.raise_for_status()
    return r.json().get("resources", [])


def find_executable_for_model(executables: list[dict], model_name: str) -> dict | None:
    """Pick the executable for a model. For Anthropic Claude models in
    SAP AI Core, the executable is `aws-bedrock`."""
    if model_name.startswith("anthropic") or "claude" in model_name.lower():
        for ex in executables:
            if ex.get("id") == "aws-bedrock":
                return ex
    for ex in executables:
        for p in ex.get("parameters", []):
            if p.get("name") != "modelName":
                continue
            allowed = p.get("constraints", {}).get("enum") or []
            if model_name in allowed:
                return ex
    return None


def existing_deployment(s: httpx.Client, model_name: str) -> dict | None:
    r = s.get(f"/lm/deployments?scenarioId={SCENARIO_ID}")
    r.raise_for_status()
    for d in r.json().get("resources", []):
        details = d.get("details", {}) or {}
        resources = details.get("resources", {}) or {}
        backend = resources.get("backend_details", {}) or {}
        deployed_model = (
            backend.get("model", {}).get("name")
            or details.get("modelName")
        )
        if deployed_model == model_name and d.get("status") in {"RUNNING", "PENDING", "UNKNOWN"}:
            return d
    return None


def create_configuration(s: httpx.Client, executable_id: str, model_name: str) -> str:
    payload = {
        "name": f"claude-{model_name.replace('--', '-').replace('_', '-')[:60]}",
        "executableId": executable_id,
        "scenarioId": SCENARIO_ID,
        "parameterBindings": [{"key": "modelName", "value": model_name}],
        "inputArtifactBindings": [],
    }
    r = s.post("/lm/configurations", json=payload)
    if r.status_code >= 400:
        print("Configuration create failed:", r.status_code, r.text)
        r.raise_for_status()
    return r.json()["id"]


def create_deployment(s: httpx.Client, configuration_id: str) -> str:
    r = s.post("/lm/deployments", json={"configurationId": configuration_id})
    if r.status_code >= 400:
        print("Deployment create failed:", r.status_code, r.text)
        r.raise_for_status()
    return r.json()["id"]


def wait_running(s: httpx.Client, deployment_id: str, timeout_s: int = 600) -> dict:
    start = time.time()
    last_status = None
    while time.time() - start < timeout_s:
        r = s.get(f"/lm/deployments/{deployment_id}")
        r.raise_for_status()
        d = r.json()
        status = d.get("status")
        if status != last_status:
            print(f"  deployment {deployment_id} status={status}")
            last_status = status
        if status == "RUNNING":
            return d
        if status in {"DEAD", "STOPPED", "UNKNOWN_FAILED"}:
            print("  deployment failed; full payload:")
            print(d)
            sys.exit(2)
        time.sleep(10)
    print("  timed out waiting for RUNNING")
    return d


def main() -> None:
    print(f"AI Core base: {BASE_URL}  resource-group: {RG}")
    token = get_token()
    with session(token) as s:
        models = list_claude_models(s)
        if not models:
            print("No Claude models found in scenario foundation-models.")
            print("Tip: confirm your subaccount has Bedrock/Anthropic enabled in AI Core.")
            sys.exit(1)

        names = sorted(
            {m.get("model") or m.get("name") for m in models if (m.get("model") or m.get("name"))},
            key=claude_version_key,
            reverse=True,
        )
        print("Claude models available:")
        for n in names:
            print(f"  {n}")

        target = names[0]
        print(f"\nPicking most recent: {target}")

        existing = existing_deployment(s, target)
        if existing:
            print(f"Already deployed: id={existing['id']} status={existing.get('status')}")
            url = existing.get("deploymentUrl") or existing.get("url")
            if url:
                print(f"  url: {url}")
            return

        executables = list_executables(s)
        ex = find_executable_for_model(executables, target)
        if not ex:
            print("No executable accepts this modelName. Executables:")
            for e in executables:
                print(" ", e.get("id"))
            sys.exit(1)
        print(f"Using executable: {ex.get('id')}")

        config_id = create_configuration(s, ex["id"], target)
        print(f"Created configuration {config_id}")

        deployment_id = create_deployment(s, config_id)
        print(f"Created deployment {deployment_id}; waiting for RUNNING...")

        d = wait_running(s, deployment_id)
        url = d.get("deploymentUrl") or d.get("url")
        print(f"\nDeployment RUNNING")
        print(f"  id:    {deployment_id}")
        print(f"  model: {target}")
        if url:
            print(f"  url:   {url}")
        print(f"\nNext: add '{target}' to AICORE_AVAILABLE_MODELS and select it in /admin.")


if __name__ == "__main__":
    main()
