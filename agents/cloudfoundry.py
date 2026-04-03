"""SAP BTP Cloud Foundry specialist agent.

Connects to the Cloud Foundry MCP server to manage CF resources
such as applications, services, spaces, and organizations.
"""

import os

from pydantic_ai import Agent

from agents.shared import create_mcp_server, get_model

MCP_BASE_URL = os.environ.get(
    "MCP_CLOUDFOUNDRY_URL",
    "https://cloudfoundry-mcp.example.com",
)

mcp_server = create_mcp_server("cloudfoundry", MCP_BASE_URL)

agent = Agent(
    get_model(),
    instructions=(
        "You are an SAP BTP Cloud Foundry specialist. "
        "Use the available tools to manage Cloud Foundry resources including "
        "applications, service instances, service bindings, spaces, organizations, "
        "routes, and buildpacks. Provide clear status updates and confirm "
        "before performing destructive operations."
    ),
    toolsets=[mcp_server],
)
