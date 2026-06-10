"""List all RUNNING/PENDING deployments in the AI Core foundation-models scenario."""

from __future__ import annotations

import os
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


def main() -> None:
    r = httpx.post(
        AUTH_URL,
        data={"grant_type": "client_credentials"},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=30,
    )
    r.raise_for_status()
    token = r.json()["access_token"]

    with httpx.Client(
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "AI-Resource-Group": RG,
        },
        timeout=60,
    ) as s:
        r = s.get("/lm/deployments?scenarioId=foundation-models")
        r.raise_for_status()
        for d in r.json().get("resources", []):
            details = d.get("details", {}) or {}
            resources = details.get("resources", {}) or {}
            backend = resources.get("backend_details", {}) or {}
            model = (
                backend.get("model", {}).get("name")
                or details.get("modelName")
                or "?"
            )
            print(f"{d.get('status'):<10} {model:<40} id={d.get('id')}")


if __name__ == "__main__":
    main()
