"""The working-indicator bracket around owner agent turns (owner-acknowledgement 6.1).

From specs/agent-core, requirement *Owner agent turns are bracketed by the working
indicator*. The bracket is a real ``OwnerAcknowledgement`` throughout: over the
cooperative ``FakeChannel`` where ordering is the point, and over a real
``SignalAdapter`` + ``FakeBridge`` where a failure is (standing rule 1).

``OrderedChannel`` puts every send and every typing request into one list, and
the sessions below add the content they receive to the same list, so "started
before the session saw the content" and "stopped after the reply was sent" are
plain list-order assertions. Every await that could hang is bounded (standing
rule 7), and the autouse fixture fails any test that leaves a task running.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from henk.agent.core import DEFAULT_ERROR_REPLY, RESET_CONFIRMATION, AgentCore
from henk.agent.permission import decide_tool_permission
from henk.agent.recall import RecallBlock
from henk.agent.turns import OwnerTurn
from henk.channel.acknowledge import OwnerAcknowledgement
from henk.channel.signal import TYPING_REFRESH_SECONDS, SignalAdapter
from henk.gate.approval import ApprovalGate, Classification
from henk.reminders.timeparse import TimeResolver
from henk.tools.base import ToolRegistry
from tests.conftest import EventSessionFactory, FakeBridge, FakeChannel, FakeSessionFactory
from tests.test_acknowledge import FakeTime
from tests.test_agent_core_reminders import AMS, NOW, _header_for
from tests.test_agent_core_turn_scope import _turn as make_event_turn
from tests.test_gate_authorization import StandingTool

OWNER = "+31600000000"
ACCOUNT = "+31611111111"
#: A small real acknowledge bound, the slack allowed on top, and the fail-fast
#: bound for anything that could hang.
T = 0.05
MARGIN = 0.5
FAIL_FAST = 2.0
ACK_LOGGER = "henk.channel.acknowledge"
CLOSE_LINE = "working indicator close"

START, STOP = ("start",), ("stop",)


@pytest.fixture(autouse=True)
async def no_leftover_tasks():
    """No indicator task may outlive its turn."""
    yield
    await asyncio.sleep(0)
    leftovers = asyncio.all_tasks() - {asyncio.current_task()}
    for task in leftovers:  # clean up, so one failure does not cascade
        task.cancel()
    if leftovers:
        await asyncio.wait(leftovers, timeout=FAIL_FAST)
    assert not leftovers, f"tasks left running: {leftovers}"


async def _until(condition, what: str) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.001)

    try:
        await asyncio.wait_for(poll(), FAIL_FAST)
    except TimeoutError:
        pytest.fail(f"timed out waiting for {what}")


async def _nosleep(_delay: float) -> None:
    return None


class OrderedChannel(FakeChannel):
    """A FakeChannel whose sends and typing requests also land in ``order``."""

    def __init__(self) -> None:
        super().__init__()
        self.order: list[tuple] = []

    async def send(self, text):
        self.order.append(("send", text))
        return await super().send(text)

    async def send_proactive(self, text, *, failure_notice=None):
        self.order.append(("proactive", text))
        return await super().send_proactive(text, failure_notice=failure_notice)

    async def start_working(self) -> bool:
        self.order.append(START)
        return await super().start_working()

    async def stop_working(self) -> bool:
        self.order.append(STOP)
        return await super().stop_working()


class OrderedSession:
    """Adds the content it receives to ``order``, then replies as scripted.

    ``hold``, when given, is awaited before replying, so a test can catch the
    turn mid-flight.
    """

    def __init__(self, order, reply, *, fail=False, hold=None) -> None:
        self._order = order
        self._reply = reply
        self._fail = fail
        self._hold = hold
        self.turns: list[str] = []

    async def run_turn(self, text: str) -> str:
        self._order.append(("turn", text))
        self.turns.append(text)
        if self._hold is not None:
            await self._hold.wait()
        if self._fail:
            raise RuntimeError("simulated SDK failure")
        return self._reply.format(text=text)

    async def close(self) -> None:
        pass


class OrderedFactory:
    def __init__(self, order, reply="ok:{text}", *, fail=False, hold=None) -> None:
        self._args = (order, reply)
        self._kwargs = {"fail": fail, "hold": hold}
        self.created: list[OrderedSession] = []

    def create(self) -> OrderedSession:
        session = OrderedSession(*self._args, **self._kwargs)
        self.created.append(session)
        return session


class RaisingFactory:
    """Session setup fails: ``_ensure_session`` raises before any turn runs."""

    def create(self):
        raise RuntimeError("session setup failed")


def _ack(channel, **kwargs) -> OwnerAcknowledgement:
    kwargs.setdefault("timeout", T)
    kwargs.setdefault("refresh_seconds", TYPING_REFRESH_SECONDS)
    return OwnerAcknowledgement(channel, **kwargs)


def _core(factory, channel, **kwargs) -> AgentCore:
    return AgentCore(factory, channel, working_indicator=_ack(channel).working, **kwargs)


# --- The bracket's edges ---------------------------------------------------


async def test_the_indicator_brackets_a_normal_owner_turn():
    # Scenario: Indicator brackets a normal owner turn. Started before the session
    # received the content; stopped after the reply was sent.
    channel = OrderedChannel()
    core = _core(OrderedFactory(channel.order), channel)
    await asyncio.wait_for(core.process("hello"), FAIL_FAST)
    assert channel.order == [START, ("turn", "hello"), ("send", "ok:hello"), STOP]
    assert channel.acks == [("start", None), ("stop", None)]


async def test_the_indicator_clears_after_the_error_reply():
    # Scenario: Indicator clears on an errored turn.
    channel = OrderedChannel()
    core = _core(OrderedFactory(channel.order, fail=True), channel)
    await asyncio.wait_for(core.process("boom"), FAIL_FAST)
    assert channel.order == [START, ("turn", "boom"), ("send", DEFAULT_ERROR_REPLY), STOP]


async def test_the_indicator_clears_when_session_setup_fails():
    # Scenario: Indicator clears when session setup fails. The exception still
    # reaches AgentCore.run's logger exactly as before; the bracket's `finally`
    # is on its way.
    channel = OrderedChannel()
    core = _core(RaisingFactory(), channel)
    with pytest.raises(RuntimeError, match="session setup failed"):
        await asyncio.wait_for(core.process("hello"), FAIL_FAST)
    assert channel.order == [START, STOP]
    assert asyncio.all_tasks() == {asyncio.current_task()}, "the refresh outlived the turn"


async def test_the_indicator_clears_on_an_empty_reply():
    # Scenario: Indicator clears on an empty reply.
    channel = OrderedChannel()
    core = _core(OrderedFactory(channel.order, reply=""), channel)
    await asyncio.wait_for(core.process("hello"), FAIL_FAST)
    assert channel.order == [START, ("turn", "hello"), STOP]
    assert channel.sent == []


# --- What is not bracketed -------------------------------------------------


class _Commands:
    def handle(self, text):
        return "your memories: none" if text.strip() == "/memories" else None


@pytest.mark.parametrize(
    ("command", "reply"),
    [("/memories", "your memories: none"), ("/new", RESET_CONFIRMATION)],
)
async def test_commands_and_reset_are_not_bracketed(command, reply):
    # Scenarios: Commands are not bracketed; Reset is not bracketed.
    channel = OrderedChannel()
    factory = OrderedFactory(channel.order)
    core = _core(factory, channel, commands=_Commands())
    await asyncio.wait_for(core.process(command), FAIL_FAST)
    assert channel.acks == []
    assert channel.order == [("send", reply)]
    assert factory.created == []


async def test_event_turns_are_not_bracketed():
    # Scenario: Event turns are not bracketed; the triage output still goes out
    # through the proactive send.
    channel = OrderedChannel()
    core = _core(EventSessionFactory(), channel)
    await asyncio.wait_for(core.process(make_event_turn()), FAIL_FAST)
    assert channel.acks == []
    assert [kind for kind, _text, _notice in channel.calls] == ["proactive"]
    await core.aclose()


# --- Cancellation ------------------------------------------------------------


@pytest.mark.parametrize("via", ["process", "run"])
async def test_a_cancelled_worker_propagates_and_sends_no_stop(via):
    # Scenario: Cancellation leaves no refresh running and sends no stop. Out of
    # `process`, and out of `AgentCore.run`, so a shutdown cannot leave the worker
    # looping on `queue.get()`.
    hold = asyncio.Event()  # never set: the turn is mid-flight when cancelled
    channel = OrderedChannel()
    core = _core(OrderedFactory(channel.order, hold=hold), channel)
    if via == "run":
        await core.submit("hello")
        task = asyncio.create_task(core.run())
    else:
        task = asyncio.create_task(core.process("hello"))
    await _until(lambda: ("turn", "hello") in channel.order, "the turn to be running")
    task.cancel()
    await asyncio.wait_for(asyncio.wait({task}), FAIL_FAST)
    assert task.cancelled(), "the cancellation was absorbed"
    assert channel.acks == [("start", None)]  # no stop attempted
    assert channel.sent == []


# --- Disabled is unchanged -------------------------------------------------


class _Recall:
    TEXT = "===== BEGIN REMEMBERED FACTS =====\n- the owner likes tea"

    def block(self):
        return RecallBlock(text=self.TEXT, content_hash="h")


@pytest.mark.parametrize("wiring", ["omitted", "explicit-none", "enabled"])
async def test_the_content_and_the_reply_are_unchanged_by_the_indicator(wiring):
    # Scenario: Disabled acknowledgement leaves owner turns unchanged. The
    # composition the existing recall/time-header tests pin (time, then memory,
    # then the owner's text; recall on the first turn only), byte for byte, and
    # the same replies. Enabled is held to the same bytes: an acknowledgement is
    # not content.
    channel = FakeChannel()
    factory = FakeSessionFactory()
    resolver = TimeResolver(AMS, clock=lambda: NOW)
    kwargs = {
        "omitted": {},
        "explicit-none": {"working_indicator": None},
        "enabled": {"working_indicator": _ack(channel).working},
    }[wiring]
    core = AgentCore(
        factory,
        channel,
        recall=_Recall(),
        time_header=lambda: resolver.time_header(resolver.current_instant()),
        **kwargs,
    )
    await asyncio.wait_for(core.process(OwnerTurn("hello")), FAIL_FAST)
    await asyncio.wait_for(core.process(OwnerTurn("and again")), FAIL_FAST)
    header = _header_for(NOW)
    expected = [f"{header}\n\n{_Recall.TEXT}\n\nhello", f"{header}\n\nand again"]
    assert factory.created[0].turns == expected
    assert channel.sent == [f"ok:{content}" for content in expected]
    if wiring == "enabled":
        assert channel.acks == [("start", None), ("stop", None)] * 2
    else:
        assert channel.acks == []
    await core.aclose()


# --- Suspension during an approval wait ------------------------------------


class _ApprovalSession:
    """Invokes a standing tool through the REAL permission path, then waits.

    With ``demote_standing`` the gate turns that into a per-instance prompt and
    waits on the owner inside this turn. After the tool runs, the session holds
    until ``proceed`` is set, so the turn is still running when the indicator
    should resume.
    """

    def __init__(self, order, registry, gate, tool) -> None:
        self._order = order
        self._registry = registry
        self._gate = gate
        self._tool = tool
        self.proceed = asyncio.Event()
        self.approved = False

    async def run_turn(self, text: str) -> str:
        self._order.append(("turn", text))
        decision = await decide_tool_permission(
            self._registry, self._gate, "mcp__henk__standing_write", {"text": "tea"}
        )
        if not decision.allow:
            return f"blocked: {decision.reason}"
        await self._tool.run(text="tea")
        self.approved = True
        await self.proceed.wait()
        return "remembered"

    async def close(self) -> None:
        pass


async def test_the_indicator_is_suspended_while_an_approval_is_pending():
    # Scenario (channel-adapter): The indicator is suspended during an approval
    # wait — through a real gate. `paused` is that gate's `has_pending`, as the
    # runtime wires it. The cadence runs on FakeTime, so "a refresh was due at
    # t=7 and none was sent" is exact.
    channel = OrderedChannel()
    gate = ApprovalGate(channel, timeout_seconds=60, demote_standing=True)
    tool = StandingTool()
    registry = ToolRegistry()
    registry.register(tool)
    session = _ApprovalSession(channel.order, registry, gate, tool)

    class Factory:
        def create(self):
            return session

    fake = FakeTime()
    ack = OwnerAcknowledgement(
        channel,
        timeout=T,
        refresh_seconds=TYPING_REFRESH_SECONDS,
        paused=gate.has_pending,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    core = AgentCore(Factory(), channel, gate=gate, working_indicator=ack.working)
    turn = asyncio.create_task(core.process("remember that I like tea"))
    try:
        await _until(gate.has_pending, "the approval prompt")
        prompt = next(
            i for i, entry in enumerate(channel.order)
            if entry[0] == "send" and "Approval needed" in entry[1]
        )
        assert channel.order[:2] == [START, ("turn", "remember that I like tea")]

        # Well past the refresh interval while the owner decides.
        await fake.run_until(10.0)
        await fake.sleeping()
        assert fake.now > TYPING_REFRESH_SECONDS
        assert channel.order[prompt + 1 :] == [STOP], "refreshed while pending"

        assert gate.deliver("yes") == (Classification.APPROVE, False)
        await _until(lambda: session.approved, "the approved tool to run")
        assert not gate.has_pending()
        await fake.step()  # the next poll
        await fake.sleeping()
        assert channel.order[-1] == START, "did not resume after the approval"
        assert not turn.done(), "the resume start must come before the turn ends"

        session.proceed.set()
        await asyncio.wait_for(turn, FAIL_FAST)
    finally:
        if not turn.done():
            turn.cancel()
            await asyncio.wait({turn})
    assert tool.calls == [{"text": "tea"}]
    assert channel.order[prompt + 1 :] == [STOP, START, ("send", "remembered"), STOP]


# --- A hung stop at the core level -----------------------------------------


class _TimedBridge(FakeBridge):
    """Stamps each completed send with the real time it completed."""

    def __init__(self) -> None:
        super().__init__()
        self.sent_at: list[float] = []

    async def send(self, recipient: str, text: str) -> None:
        await super().send(recipient, text)
        self.sent_at.append(time.monotonic())


class _TimedSession:
    def __init__(self, started: list) -> None:
        self._started = started

    async def run_turn(self, text: str) -> str:
        self._started.append((text, time.monotonic()))
        return f"ok:{text}"

    async def close(self) -> None:
        pass


async def test_a_hung_stop_does_not_hold_the_next_turn_beyond_the_bound(caplog):
    # Scenario: A hung stop does not hold the next turn beyond the bound. The
    # close begins as the first reply's send returns; the queued second turn
    # starts within T (plus slack) of it, and not before the bound was spent.
    bridge = _TimedBridge()
    bridge.ack_faults["stop"] = asyncio.Event()  # never set
    adapter = SignalAdapter(bridge, account=ACCOUNT, owner=OWNER, sleep=_nosleep)
    started: list[tuple[str, float]] = []

    class Factory:
        def create(self):
            return _TimedSession(started)

    core = AgentCore(Factory(), adapter, working_indicator=_ack(adapter).working)
    await core.submit("one")
    await core.submit("two")
    worker = asyncio.create_task(core.run())

    def close_lines() -> list[str]:
        return [r.getMessage() for r in caplog.records if CLOSE_LINE in r.getMessage()]

    try:
        with caplog.at_level(logging.WARNING, logger=ACK_LOGGER):
            await _until(lambda: len(close_lines()) == 2, "both turns' closes")
    finally:
        worker.cancel()
        await asyncio.wait_for(asyncio.wait({worker}), FAIL_FAST)

    assert bridge.sends == [(OWNER, "ok:one"), (OWNER, "ok:two")]
    first_reply_at = bridge.sent_at[0]
    second_turn_at = started[1][1]
    gap = second_turn_at - first_reply_at
    assert gap < T + MARGIN, gap
    assert gap >= T * 0.9, "the close did not wait on the hung stop at all"
    assert ("stop", OWNER) not in bridge.typing  # never completed
    assert len(close_lines()) == 2  # one line per turn, nothing raised
