"""The per-model usage collector (``agents.model_usage``) and its hook in the
model wrapper: every ANSWERED request of a run tree is summed per model, with
the register off and on; a request that raises adds nothing.
"""
import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from agents import model_usage
from agents.nexus import sender, state
from agents.nexus.logged_model import NexusLoggedModel

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")


def test_nothing_is_recorded_without_a_collector():
    model_usage.add("m", RequestUsage(input_tokens=5, output_tokens=1))  # no raise, no-op
    assert model_usage.current() is None


def test_a_collector_sums_per_model_and_counts_requests():
    with model_usage.collecting() as c:
        model_usage.add("a", RequestUsage(input_tokens=10, output_tokens=2, cache_read_tokens=4))
        model_usage.add("a", RequestUsage(input_tokens=1, output_tokens=1))
        model_usage.add("b", RequestUsage(input_tokens=7, output_tokens=3))
    assert c.rows() == [
        {"model": "a", "input_tokens": 11, "output_tokens": 3,
         "cache_read_tokens": 4, "requests": 2},
        {"model": "b", "input_tokens": 7, "output_tokens": 3,
         "cache_read_tokens": 0, "requests": 1},
    ]
    assert model_usage.current() is None  # reset on exit


def test_an_unreadable_usage_is_dropped_and_never_raises():
    with model_usage.collecting() as c:
        model_usage.add("a", object())
        model_usage.add("", RequestUsage(input_tokens=1))
        model_usage.add(None, RequestUsage(input_tokens=1))
        model_usage.add("a", RequestUsage(input_tokens=-1))
    assert c.rows() == []


def test_a_name_over_128_characters_is_cut():
    with model_usage.collecting() as c:
        model_usage.add("x" * 200, RequestUsage(input_tokens=1))
    assert c.rows()[0]["model"] == "x" * 128


async def test_the_wrapper_records_a_plain_and_a_streamed_run_with_the_register_off():
    assert not state.enabled()
    spec = Agent(NexusLoggedModel(TestModel(), "model-a"))
    with model_usage.collecting() as c:
        r1 = await spec.run("hi")
        async with spec.run_stream("hi") as s:
            await s.get_output()
    rows = c.rows()
    assert [r["model"] for r in rows] == ["model-a"]
    total = r1.usage()
    assert rows[0]["requests"] >= 2
    assert rows[0]["input_tokens"] >= total.input_tokens


async def test_the_split_covers_a_delegated_agent_on_another_model():
    peer = Agent(NexusLoggedModel(TestModel(custom_output_text="peer"), "model-b"))
    top = Agent(NexusLoggedModel(TestModel(call_tools=["ask_peer"]), "model-a"))

    @top.tool_plain
    async def ask_peer(question: str) -> str:
        return (await peer.run(question)).output

    with model_usage.collecting() as c:
        result = await top.run("go")
    by_model = {r["model"]: r for r in c.rows()}
    assert set(by_model) == {"model-a", "model-b"}
    # D4: the ledger equals what RunUsage counts for the top run
    assert by_model["model-a"]["requests"] == result.usage().requests
    assert by_model["model-a"]["input_tokens"] == result.usage().input_tokens
    assert by_model["model-a"]["output_tokens"] == result.usage().output_tokens


async def test_the_register_on_branch_records_too(monkeypatch):
    queued = []
    monkeypatch.setattr(state, "enabled", lambda: True)
    monkeypatch.setattr(sender, "enqueue", lambda *a, **k: queued.append(a))
    spec = Agent(NexusLoggedModel(TestModel(), "model-on"))
    with model_usage.collecting() as c:
        r1 = await spec.run("hi")
        async with spec.run_stream("hi") as s:
            await s.get_output()
        r2 = s.usage()
    rows = c.rows()
    assert [r["model"] for r in rows] == ["model-on"]
    assert rows[0]["requests"] == r1.usage().requests + r2.requests
    assert rows[0]["input_tokens"] == r1.usage().input_tokens + r2.input_tokens
    assert queued  # the register path really ran


@pytest.mark.parametrize("register_on", [False, True])
async def test_a_request_that_raises_records_nothing(monkeypatch, register_on):
    if register_on:
        monkeypatch.setattr(state, "enabled", lambda: True)
        monkeypatch.setattr(sender, "enqueue", lambda *a, **k: None)

    def broken(messages, info):
        raise RuntimeError("model down")

    spec = Agent(NexusLoggedModel(FunctionModel(broken), "model-x"))
    with model_usage.collecting() as c:
        with pytest.raises(RuntimeError):
            await spec.run("hi")
    assert c.rows() == []
