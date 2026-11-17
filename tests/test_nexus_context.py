"""The agent-name and run-id context variables of the agent-register logging."""
import asyncio

from agents.nexus.context import (
    ORCHESTRATOR,
    PERSONAL_AGENT,
    calling_agent,
    current_agent_name,
    current_nexus_run_id,
    nexus_run,
)


def test_defaults():
    assert current_agent_name.get() == ORCHESTRATOR == "orchestrator"
    assert PERSONAL_AGENT == "personal-agent"
    assert current_nexus_run_id.get() is None


def test_inner_nexus_run_keeps_the_outer_id():
    with nexus_run("job-1") as outer:
        with nexus_run("other") as inner:
            assert inner == outer == "job-1" == current_nexus_run_id.get()
    assert current_nexus_run_id.get() is None


def test_run_id_is_cut_to_64():
    with nexus_run("r" * 80) as rid:
        assert len(rid) == 64


async def test_tasks_inherit_and_names_reset():
    seen = []

    async def child():
        seen.append((current_agent_name.get(), current_nexus_run_id.get()))

    with nexus_run("run-1"), calling_agent("invoice-assistant"):
        await asyncio.create_task(child())
    assert seen == [("invoice-assistant", "run-1")]
    assert current_agent_name.get() == "orchestrator"
