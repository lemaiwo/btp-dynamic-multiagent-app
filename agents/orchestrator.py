"""SAP BTP Management orchestrator agent.

Coordinates between specialist agents (audit log, Cloud Foundry, BTP)
to handle user requests about SAP BTP platform management.
"""

import logging

from pydantic_ai import Agent, RunContext

from agents.shared import get_model

logger = logging.getLogger(__name__)


def _format_error(exc: BaseException) -> str:
    """Unwrap ExceptionGroup/TaskGroup to surface the real cause."""
    if isinstance(exc, BaseExceptionGroup):
        messages = [_format_error(e) for e in exc.exceptions]
        return "; ".join(messages)
    return f"{type(exc).__name__}: {exc}"


def _create_orchestrator() -> Agent:
    """Create the orchestrator agent with delegation tools.

    Uses a factory to avoid circular imports — sub-agents are imported
    inside the tool functions on first use.
    """
    orchestrator = Agent(
        get_model(),
        instructions=(
            "You are an SAP BTP platform management orchestrator. "
            "You coordinate between specialized agents to help users manage "
            "their SAP BTP landscape.\n\n"
            "Delegate tasks to the appropriate specialist:\n"
            # "- **Audit Log agent**: querying and analyzing audit logs, "
            # "security events, change tracking, compliance\n"
            "- **Cloud Foundry agent**: CF operations — apps, services, "
            "spaces, orgs, routes, buildpacks\n"
            "- **BTP agent**: platform operations — subaccounts, "
            "entitlements, directories, environments, service assignments\n\n"
            "Always delegate to the most appropriate specialist. "
            "You may combine results from multiple agents to give "
            "comprehensive answers. When a request spans multiple domains, "
            "call the relevant specialists one at a time and synthesize "
            "their responses."
        ),
    )

    # @orchestrator.tool
    # async def query_audit_logs(ctx: RunContext, query: str) -> str:
    #     """Delegate to the Audit Log specialist to query and analyze SAP BTP audit logs.
    #
    #     Use this for questions about: audit events, security logs, user activities,
    #     change tracking, compliance auditing, and log analysis.
    #     """
    #     try:
    #         from agents.auditlog import agent as auditlog_agent
    #
    #         result = await auditlog_agent.run(query, usage=ctx.usage)
    #         return str(result.output)
    #     except Exception as e:
    #         logger.exception("Audit log agent failed")
    #         return f"Error from audit log agent: {e}"

    @orchestrator.tool
    async def manage_cloud_foundry(ctx: RunContext, query: str) -> str:
        """Delegate to the Cloud Foundry specialist for CF operations.

        Use this for: managing applications, services, spaces, organizations,
        routes, buildpacks, service instances, service bindings, and other
        Cloud Foundry resources.
        """
        try:
            from agents.cloudfoundry import agent as cf_agent

            result = await cf_agent.run(query, usage=ctx.usage)
            return str(result.output)
        except BaseException as e:
            logger.exception("Cloud Foundry agent failed")
            return f"Error from Cloud Foundry agent: {_format_error(e)}"

    @orchestrator.tool
    async def manage_btp(ctx: RunContext, query: str) -> str:
        """Delegate to the BTP specialist for platform-level operations.

        Use this for: managing subaccounts, entitlements, service assignments,
        directories, environments, and other SAP BTP platform resources.
        """
        try:
            from agents.btp import agent as btp_agent

            result = await btp_agent.run(query, usage=ctx.usage)
            return str(result.output)
        except BaseException as e:
            logger.exception("BTP agent failed")
            return f"Error from BTP agent: {_format_error(e)}"

    return orchestrator


orchestrator = _create_orchestrator()
