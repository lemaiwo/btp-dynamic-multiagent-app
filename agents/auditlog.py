"""SAP BTP Audit Log specialist agent.

Connects to the audit log MCP server to query and analyze
SAP BTP audit log entries.
"""

from pydantic_ai import Agent

from agents.shared import create_mcp_server, get_model

MCP_BASE_URL = (
    "https://infrabel-app-dev-cf-ai-btp-agent-auditlog-mcp.cfapps.eu20-001.hana.ondemand.com"
)

mcp_server = create_mcp_server("auditlog", MCP_BASE_URL)

agent = Agent(
    get_model(),
    instructions=(
        "You are an SAP BTP audit log specialist. "
        "Use the available tools to query, filter, and analyze audit log entries. "
        "Provide clear summaries of audit events and highlight any anomalies or "
        "security-relevant findings."
    ),
    toolsets=[mcp_server],
)
