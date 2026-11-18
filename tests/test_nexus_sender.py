"""The agent-register sender: bounded queue, one worker, pause, rate-limited warning."""
import asyncio
import logging

import pytest

from agents.nexus import sender
from agents.nexus.client import RegisterClient
from tests.nexus_helpers import FakeResolver, Recorder


@pytest.fixture
async def running():
    sender.reset_for_tests()
    rec = Recorder()
    sender.start(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()), queue_max=3)
    yield rec
    await sender.stop(flush_seconds=0.1)
    sender.reset_for_tests()


async def test_sends_in_order(running):
    for i in range(3):
        sender.enqueue("logRequest", {"correlationId": str(i)})
    await sender.drain()
    assert [r["body"]["correlationId"] for r in running.requests] == ["0", "1", "2"]
    assert sender.counters()["sent"] == 3


async def test_enqueue_never_awaits_and_full_drops_oldest():
    sender.reset_for_tests()
    sender.configure(queue_max=2)                     # no worker
    for i in range(4):
        assert sender.enqueue("logRequest", {"correlationId": str(i)}) is None
    assert [e.body["correlationId"] for e in sender.pending()] == ["2", "3"]
    assert sender.counters()["dropped"] == 2
    sender.reset_for_tests()


def test_entry_repr_hides_the_identity():
    sender.reset_for_tests()
    from agents.nexus.context import Identity
    sender.enqueue("logRequest", {"correlationId": "c"}, identity=Identity(jwt="secret-jwt"))
    entry = sender.pending()[0]
    assert entry.correlation_id == "c"
    assert "secret-jwt" not in repr(entry)
    sender.reset_for_tests()


async def test_five_failures_pause_sending(monkeypatch):
    sender.reset_for_tests()
    rec = Recorder(statuses={"logRequest": 500})
    clock = [1000.0]
    monkeypatch.setattr(sender, "_now", lambda: clock[0])
    sender.start(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()), queue_max=50)
    for i in range(8):
        sender.enqueue("logRequest", {"correlationId": str(i)})
    await sender.wait_paused()
    assert len(rec.requests) == 5
    assert len(sender.pending()) == 3                 # entries keep queueing
    clock[0] += 61
    rec.statuses.pop("logRequest")                    # the register is back
    sender.wake()
    await sender.drain()
    assert len(rec.requests) == 8
    await sender.stop(flush_seconds=0)
    sender.reset_for_tests()


async def test_after_a_pause_one_probe_then_pause_again(monkeypatch):
    sender.reset_for_tests()
    rec = Recorder(statuses={"logRequest": 500})
    clock = [1000.0]
    monkeypatch.setattr(sender, "_now", lambda: clock[0])
    sender.start(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()), queue_max=50)
    for i in range(8):
        sender.enqueue("logRequest", {"correlationId": str(i)})
    await sender.wait_paused()
    assert len(rec.requests) == 5
    clock[0] += 61                                    # still down: ONE probe, then pause
    sender.wake()
    await sender.wait_paused()
    assert len(rec.requests) == 6
    assert len(sender.pending()) == 2
    await sender.stop(flush_seconds=0)
    sender.reset_for_tests()


def test_worker_of_a_closed_loop_is_not_running():
    sender.reset_for_tests()
    first = asyncio.new_event_loop()

    async def start_first():
        sender.start(RegisterClient(FakeResolver(), timeout=5, transport=Recorder().transport()),
                     queue_max=5)

    first.run_until_complete(start_first())
    assert sender.running()
    first.close()                                     # e.g. a test's loop, or a restarted app
    assert not sender.running()

    rec = Recorder()

    async def start_second():
        client = RegisterClient(FakeResolver(), timeout=5, transport=rec.transport())
        sender.start(client, queue_max=5)
        sender.enqueue("logRequest", {"correlationId": "a"})
        await sender.drain()
        await sender.stop(flush_seconds=0.1)
        await client.aclose()

    asyncio.run(start_second())
    assert [r["body"]["correlationId"] for r in rec.requests] == ["a"]
    sender.reset_for_tests()


async def test_failure_warning_is_rate_limited(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="agents.nexus.sender")
    sender.reset_for_tests()
    rec = Recorder(statuses={"logRequest": 500})
    clock = [1000.0]
    monkeypatch.setattr(sender, "_now", lambda: clock[0])
    sender.start(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()), queue_max=50)
    sender.enqueue("logRequest", {"correlationId": "a", "prompt": "secret prompt text"})
    sender.enqueue("logRequest", {"correlationId": "b"})
    await sender.drain()
    clock[0] += 100
    sender.enqueue("logResponse", {"correlationId": "c"})   # a success breaks the run
    sender.enqueue("logRequest", {"correlationId": "d"})
    sender.enqueue("logRequest", {"correlationId": "e"})
    await sender.drain()
    lines = [r for r in caplog.records if r.name == "agents.nexus.sender"]
    assert len(lines) == 1
    assert "failed=1" in lines[0].getMessage() and "last=http_500" in lines[0].getMessage()
    clock[0] += 301
    sender.enqueue("logRequest", {"correlationId": "f"})
    await sender.drain()
    lines = [r for r in caplog.records if r.name == "agents.nexus.sender"]
    assert len(lines) == 2
    second = lines[1].getMessage()
    assert "sent=1" in second and "failed=4" in second and "dropped=0" in second
    for text in ("register.example.test", "Bearer", "tech-token", "secret prompt"):
        assert text not in caplog.text
    await sender.stop(flush_seconds=0)
    sender.reset_for_tests()


async def test_shutdown_flushes_at_most_flush_seconds(caplog):
    caplog.set_level(logging.WARNING, logger="agents.nexus.sender")
    sender.reset_for_tests()
    rec = Recorder(hang=True)
    sender.start(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()), queue_max=10)
    for i in range(4):
        sender.enqueue("logRequest", {"correlationId": str(i)})
    started = asyncio.get_running_loop().time()
    left = await sender.stop(flush_seconds=0.2)
    assert asyncio.get_running_loop().time() - started < 1.0
    assert left == 4 and "dropped at shutdown" in caplog.text
    assert not sender.running()
    sender.reset_for_tests()


async def test_shutdown_flush_sends_what_fits(running):
    for i in range(2):
        sender.enqueue("logRequest", {"correlationId": str(i)})
    left = await sender.stop(flush_seconds=1.0)
    assert left == 0
    assert len(running.requests) == 2


async def test_worker_survives_a_raising_client():
    class Boom:
        async def post(self, action, body, *, identity=None):
            raise RuntimeError("https://register.example.test/x?token=abc")

    sender.reset_for_tests()
    sender.start(Boom(), queue_max=5)
    sender.enqueue("logRequest", {"correlationId": "a"})
    sender.enqueue("logRequest", {"correlationId": "b"})
    await sender.drain()
    assert sender.counters()["failed"] == 2
    assert sender.running()
    await sender.stop(flush_seconds=0)
    sender.reset_for_tests()


async def test_user_fallback_alone_reaches_the_warning(caplog, monkeypatch):
    """Every send succeeds but goes technical instead of as the user: still warned."""
    from types import SimpleNamespace
    caplog.set_level(logging.WARNING, logger="agents.nexus.sender")
    clock = [1000.0]
    monkeypatch.setattr(sender, "_now", lambda: clock[0])

    class Fallback:
        async def post(self, action, body, *, identity=None):
            return SimpleNamespace(ok=True, status=200, failure=None, user_fallback=True,
                                   sent_as="technical")

    sender.reset_for_tests()
    sender.start(Fallback(), queue_max=50)
    for cid in ("a", "b"):
        sender.enqueue("logRequest", {"correlationId": cid})
    await sender.drain()
    lines = [r.getMessage() for r in caplog.records if r.name == "agents.nexus.sender"]
    assert len(lines) == 1                           # rate limited: one line, not two
    assert "failed=0" in lines[0] and "user_fallback=1" in lines[0]
    clock[0] += 301
    sender.enqueue("logRequest", {"correlationId": "c"})
    await sender.drain()
    lines = [r.getMessage() for r in caplog.records if r.name == "agents.nexus.sender"]
    assert len(lines) == 2 and "user_fallback=2" in lines[1]
    await sender.stop(flush_seconds=0)
    sender.reset_for_tests()


async def test_a_missing_destination_pauses_and_asks_once(monkeypatch):
    from agents.destination import DestinationNotFound
    sender.reset_for_tests()
    clock = [1000.0]
    monkeypatch.setattr(sender, "_now", lambda: clock[0])
    tech = FakeResolver(fail=DestinationNotFound("404"))
    asked: list[int] = []
    sender.start(RegisterClient(tech, timeout=5, transport=Recorder().transport()),
                 queue_max=50, on_destination_missing=lambda: asked.append(1))
    for i in range(4):
        sender.enqueue("logRequest", {"correlationId": str(i)})
    await sender.wait_paused()
    assert asked == [1]                              # once, not once per entry
    assert len(tech.calls) == 1                      # no destination-service call per entry
    assert len(sender.pending()) == 3
    await sender.stop(flush_seconds=0)
    sender.reset_for_tests()


async def test_stop_does_not_hide_a_cancellation_of_its_caller():
    class SlowToCancel:
        async def post(self, action, body, *, identity=None):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)             # the worker takes a while to end
                raise

    sender.reset_for_tests()
    sender.start(SlowToCancel(), queue_max=5)
    sender.enqueue("logRequest", {"correlationId": "a"})
    await asyncio.sleep(0.01)

    stopping = asyncio.ensure_future(sender.stop(flush_seconds=0))
    await asyncio.sleep(0.01)                        # stop() awaits the cancelled worker
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    sender.reset_for_tests()
