"""The core supplies the gate's per-turn context (task 2.3/2.4), design D10.

The gate can only enforce turn scope if the core tells it, per turn, what kind of
turn is running, whether it is announceable, and whether the session is tainted.
These tests drive ``AgentCore`` with a recording gate double, so the contract is
pinned at the seam rather than inside the gate.
"""

from __future__ import annotations

from henk.agent.core import AgentCore
from henk.agent.turns import EventTurn, EventTurnItem
from henk.events.identity import derive_identity
from henk.events.types import Event
from henk.tools.base import TurnType
from tests.conftest import (
    EventSessionFactory,
    FakeChannel,
    FakeSessionFactory,
    make_clock,
)


class RecordingGate:
    """Gate double capturing every turn context the core frames."""

    def __init__(self) -> None:
        self.contexts: list[object] = []
        self.depth = 0
        self.current = None

    def enter_turn(self, context) -> None:
        self.contexts.append(context)
        self.current = context
        self.depth += 1

    def exit_turn(self) -> None:
        self.current = None
        self.depth -= 1

    def has_pending(self) -> bool:  # pragma: no cover - not exercised here
        return False


def _item(title: str = "Gatus: svc/api", eid: str = "e1") -> EventTurnItem:
    event = Event(id=eid, title=title, message="", arrival_time=0.0)
    return EventTurnItem(event=event, identity=derive_identity(event))


def _turn(*, announceable: bool = True) -> EventTurn:
    return EventTurn(items=(_item(),), announceable=announceable)


# --- Owner turns ----------------------------------------------------------


async def test_owner_turn_is_framed_as_owner_untainted():
    gate = RecordingGate()
    core = AgentCore(
        FakeSessionFactory(), FakeChannel(), clock=make_clock([0, 0]), gate=gate
    )
    await core.process("what's up?")
    assert len(gate.contexts) == 1
    ctx = gate.contexts[0]
    assert ctx.turn_type is TurnType.OWNER
    assert ctx.tainted is False
    assert ctx.announceable is True
    assert gate.current is None  # cleared on the way out


async def test_owner_commands_never_frame_a_turn():
    # Commands run app-side, outside any turn or session — the gate governs
    # model-initiated calls only (design D8).
    gate = RecordingGate()
    core = AgentCore(
        FakeSessionFactory(), FakeChannel(), clock=make_clock([0, 0]), gate=gate
    )
    await core.process("/new")
    assert gate.contexts == []


# --- Event turns and taint ------------------------------------------------


async def test_event_turn_is_framed_as_event_with_its_announceability():
    gate = RecordingGate()
    core = AgentCore(
        EventSessionFactory(), FakeChannel(), clock=make_clock([0]), gate=gate
    )
    await core.process(_turn(announceable=False))
    ctx = gate.contexts[0]
    assert ctx.turn_type is TurnType.EVENT
    assert ctx.announceable is False
    assert ctx.tainted is True  # the session is tainted from its first event turn
    assert gate.current is None


async def test_owner_followup_in_an_event_started_session_is_tainted():
    gate = RecordingGate()
    core = AgentCore(
        EventSessionFactory(), FakeChannel(), clock=make_clock([0]), gate=gate
    )
    await core.process(_turn())
    await core.process("what did you find?")
    owner_ctx = gate.contexts[-1]
    assert owner_ctx.turn_type is TurnType.OWNER
    assert owner_ctx.tainted is True  # taint outlives the event turn itself


async def test_taint_is_never_cleared_for_the_sessions_life():
    gate = RecordingGate()
    core = AgentCore(
        EventSessionFactory(), FakeChannel(), clock=make_clock([0]), gate=gate
    )
    await core.process(_turn())
    for _ in range(3):
        await core.process("still asking")
    assert all(ctx.tainted for ctx in gate.contexts)


async def test_reset_clears_taint_for_the_next_session():
    gate = RecordingGate()
    core = AgentCore(
        EventSessionFactory(), FakeChannel(), clock=make_clock([0]), gate=gate
    )
    await core.process(_turn())
    await core.process("/new")
    await core.process("fresh start")
    assert gate.contexts[-1].tainted is False  # a new session, no incident in it


async def test_idle_expiry_clears_taint_for_the_new_session():
    gate = RecordingGate()
    core = AgentCore(
        EventSessionFactory(),
        FakeChannel(),
        idle_timeout_seconds=60,
        clock=make_clock([0, 0, 500, 500]),
        gate=gate,
    )
    await core.process(_turn())
    await core.process("much later")
    assert gate.contexts[-1].tainted is False


async def test_a_new_incident_taints_its_own_fresh_session():
    gate = RecordingGate()
    core = AgentCore(
        EventSessionFactory(), FakeChannel(), clock=make_clock([0]), gate=gate
    )
    await core.process("owner first")
    assert gate.contexts[0].tainted is False
    await core.process(_turn())
    assert gate.contexts[1].tainted is True


# --- The context never outlives the turn, error paths included ------------


async def test_context_cleared_when_an_owner_turn_raises():
    gate = RecordingGate()
    core = AgentCore(
        FakeSessionFactory(fail=True),
        FakeChannel(),
        clock=make_clock([0, 0, 1, 1]),
        gate=gate,
    )
    await core.process("boom")
    assert gate.current is None
    assert gate.depth == 0


async def test_context_cleared_when_an_event_turn_raises():
    class FailingFactory(EventSessionFactory):
        def create(self):
            session = super().create()

            async def boom(text):
                raise RuntimeError("simulated SDK failure")

            session.run_turn = boom  # type: ignore[method-assign]
            return session

    gate = RecordingGate()
    core = AgentCore(
        FailingFactory(), FakeChannel(), clock=make_clock([0]), gate=gate
    )
    await core.process(_turn())
    assert gate.current is None
    assert gate.depth == 0


async def test_core_without_a_gate_still_processes_turns():
    # The gate is optional wiring (unit tests, reactive-only deployments); its
    # absence must not change turn handling.
    channel = FakeChannel()
    core = AgentCore(FakeSessionFactory(reply="ok"), channel, clock=make_clock([0, 0]))
    await core.process("hello")
    assert channel.sent == ["ok:hello"]
