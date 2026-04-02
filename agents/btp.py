"""SAP BTP platform specialist agent.

Connects to the BTP MCP server to manage platform-level resources
such as subaccounts, entitlements, directories, and environments.
"""

from pydantic_ai import Agent

from agents.shared import create_mcp_server, get_model

MCP_BASE_URL = (
    "https://infrabel-app-dev-cf-ai-btp-agent-btp-mcp.cfapps.eu20-001.hana.ondemand.com"
)

mcp_server = create_mcp_server("btp", MCP_BASE_URL)

agent = Agent(
    get_model(),
    instructions=(
        "You are an SAP BTP platform management specialist. "
        "Use the available tools to manage BTP platform resources including "
        "subaccounts, directories, entitlements, service assignments, "
        "environments, and platform-level configurations. Provide clear "
        "status updates and confirm before performing destructive operations."
    ),
    toolsets=[mcp_server],
)
