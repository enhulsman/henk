"""Agent-core delta tests (task 2.5), from specs/agent-core. SDK fully mocked.

Typed turns: event turns share the serial lane, start a fresh session when idle,
carry the delimited untrusted block + triage framing (owner turns carry neither),
route output through the proactive send (suppressed when non-announceable), and
are discarded by ``/new`` like any other session.
"""

from __future__ import annotations

import asyncio

import pytest

from henk.agent.core import AgentCore
from henk.agent.triage import UNTRUSTED_BEGIN
from henk.agent.turns import EventTurn, EventTurnItem
from henk.events.identity import derive_identity
from henk.events.types import Event
from tests.conftest import EventSessionFactory, FakeChannel, make_clock


def _item(title: str, message: str = "", *, recurrence: bool = False,
          prior_ref: str | None = None, eid: str = "e1") -> EventTurnItem:
    event = Event(id=eid, title=title, message=message, arrival_time=0.0)
    return EventTurnItem(
        event=event,
        identity=derive_identity(event),
        recurrence=recurrence,
        prior_handoff_ref=prior_ref,
    )


def _turn(*items: EventTurnItem, announceable: bool = True, suppressed: int = 0) -> EventTurn:
    return EventTurn(items=tuple(items), announceable=announceable, suppressed_count=suppressed)


async def test_event_turn_runs_after_a_queued_owner_turn():
    channel = FakeChannel()
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(factory, channel, clock=make_clock([0]))
    await core.submit("owner question")
    await core.submit_event(_turn(_item("Gatus: svc/api", "triggered")))

    worker = asyncio.create_task(core.run())
    for _ in range(2000):
        if len(channel.sent) >= 2:
            break
        await asyncio.sleep(0)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert len(channel.sent) == 2  # both processed, neither dropped, in order


async def test_event_turn_when_idle_starts_fresh_session():
    channel = FakeChannel()
    factory = EventSessionFactory()
    core = AgentCore(factory, channel, clock=make_clock([0]))
    await core.process(_turn(_item("Gatus: svc/api", "triggered")))
    assert factory.create_count == 1
    assert UNTRUSTED_BEGIN in factory.created[0].contents[0]  # triage framing present


async def test_owner_turn_has_no_triage_framing_event_turn_does():
    channel = FakeChannel()
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1]))
    await core.process("what's my todo list?")           # owner turn
    await core.process(_turn(_item("Gatus: svc/api")))    # event turn, same session
    session = factory.created[0]
    assert factory.create_count == 1  # reused (not idle)
    assert UNTRUSTED_BEGIN not in session.contents[0]     # owner turn: no framing
    assert "Triage this incident" not in session.contents[0]
    assert UNTRUSTED_BEGIN in session.contents[1]          # event turn: framing
    assert "Triage this incident" in session.contents[1]


async def test_announceable_event_output_is_sent_proactively():
    channel = FakeChannel()
    factory = EventSessionFactory()
    core = AgentCore(factory, channel, clock=make_clock([0]))
    await core.process(_turn(_item("Gatus: svc/api"), announceable=True))
    assert len(channel.sent) == 1
    assert "Diagnosis" in channel.sent[0]


async def test_non_announceable_event_output_is_suppressed():
    channel = FakeChannel()
    factory = EventSessionFactory()
    core = AgentCore(factory, channel, clock=make_clock([0]))
    await core.process(_turn(_item("Gatus: svc/api"), announceable=False))
    assert channel.sent == []  # cap-overflow: no Signal send


def test_base_system_prompt_enumerates_handoff_without_triage_instructions():
    from henk.config import AgentConfig

    prompt = AgentConfig().system_prompt
    assert "publish_handoff" in prompt          # enumerated in the base toolset
    # Triage-mode instructions arrive with event turns, never in the base prompt.
    assert "triage arc" not in prompt.lower()
    assert "Diagnosis:" not in prompt


def test_production_registry_includes_publish_handoff():
    import httpx

    from henk.config import Config
    from henk.tools import build_production_registry

    cfg = Config.load(
        __import__("pathlib").Path(__file__).resolve().parent.parent / "config.yaml",
        env={},
    )
    registry = build_production_registry(cfg, httpx.AsyncClient())
    assert "publish_handoff" in registry.names()


async def test_new_after_triage_discards_incident_context():
    channel = FakeChannel()
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1, 2, 2]))
    await core.process(_turn(_item("Gatus: svc/api")))   # triage session A
    await core.process("/new")                            # reset
    await core.process("hello")                           # fresh session B
    assert factory.create_count == 2
    assert factory.created[1].contents == ["hello"]      # no incident context
