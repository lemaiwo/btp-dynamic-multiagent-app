"""SAP BTP Management — Multi-Agent Web Chat

Orchestrates specialized agents for audit logs, Cloud Foundry,
and BTP platform management through a web chat interface.

Usage:
    pip install -r requirements.txt
    cp .env.example .env  # fill in SAP AI Core credentials
    python app.py
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.DEBUG)

# Import after load_dotenv so SAP AI Core credentials are available
from agents.orchestrator import orchestrator  # noqa: E402

app = orchestrator.to_web()

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 7932))
    print(f"Starting SAP BTP Management Chat on http://127.0.0.1:{port}")
    print("OAuth2 callback listening on http://localhost:3000/callback")
    uvicorn.run(app, host="0.0.0.0", port=port)
