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
    # D5 (event-pipeline-durability): a new incident displaces the owner
    # conversation into its own isolated session rather than reusing it.
    channel = FakeChannel()
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1]))
    await core.process("what's my todo list?")           # owner turn → session A
    await core.process(_turn(_item("Gatus: svc/api")))    # event turn → fresh session B
    assert factory.create_count == 2  # incident does NOT inherit the owner session
    owner_session, event_session = factory.created[0], factory.created[1]
    assert UNTRUSTED_BEGIN not in owner_session.contents[0]   # owner turn: no framing
    assert "Triage this incident" not in owner_session.contents[0]
    assert UNTRUSTED_BEGIN in event_session.contents[0]        # event turn: framing
    assert "Triage this incident" in event_session.contents[0]


async def test_new_incident_does_not_inherit_prior_incident_context():
    # agent-core delta: a new incident starts fresh even while another incident's
    # session is still open — no cross-incident context bleed.
    channel = FakeChannel()
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1]))
    await core.process(_turn(_item("Gatus: svc/a", eid="a")))   # incident A → session 1
    await core.process(_turn(_item("Gatus: svc/b", eid="b")))   # incident B → session 2
    assert factory.create_count == 2
    # B's session saw only B's payload — nothing from A.
    b_content = factory.created[1].contents[0]
    assert "svc/b" in b_content
    assert "svc/a" not in b_content


async def test_owner_reply_after_triage_continues_incident_session():
    channel = FakeChannel()
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1]))
    await core.process(_turn(_item("Gatus: svc/api")))    # incident → session 1
    await core.process("what does the log say?")          # owner follow-up
    assert factory.create_count == 1                       # same session (interrogation)
    assert len(factory.created[0].contents) == 2


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
