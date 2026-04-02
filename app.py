"""SAP BTP Management — Multi-Agent Web Chat

Orchestrates specialized agents for audit logs, Cloud Foundry,
and BTP platform management through a web chat interface.

Usage:
    pip install -r requirements.txt
    cp .env.example .env  # fill in SAP AI Core credentials
    python app.py
"""

import logging

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.DEBUG)

# Import after load_dotenv so SAP AI Core credentials are available
from agents.orchestrator import orchestrator  # noqa: E402

app = orchestrator.to_web()

if __name__ == "__main__":
    import uvicorn

    print("Starting SAP BTP Management Chat on http://127.0.0.1:7932")
    print("OAuth2 callback listening on http://localhost:3000/callback")
    uvicorn.run(app, host="127.0.0.1", port=7932)
