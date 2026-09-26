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
import json
import logging
import time

import pytest

from henk.agent.core import (
    DEFAULT_ERROR_REPLY,
    DEGRADED_DURABILITY_NOTICE,
    RESET_CONFIRMATION,
    TRIAGE_FAILURE_NOTICE,
    AgentCore,
)
from henk.agent.digest import read_digest
from henk.agent.ending import COMPLETED
from henk.agent.permission import decide_tool_permission
from henk.agent.recall import RecallBlock
from henk.agent.session import TurnEnding
from henk.agent.triage import compose_event_turn_content
from henk.agent.turns import OwnerTurn
from henk.audit import AuditLog
from henk.channel.acknowledge import PAUSE_POLL_SECONDS, OwnerAcknowledgement
from henk.channel.signal import TYPING_REFRESH_SECONDS, SignalAdapter
from henk.gate.approval import ApprovalGate, Classification
from henk.reminders.timeparse import TimeResolver
from henk.tools.base import ToolRegistry
from tests.conftest import (
    TRIAGE_REPLY,
    EventSessionFactory,
    FakeBridge,
    FakeChannel,
    FakeSessionFactory,
    handoff_stats,
)
from tests.test_acknowledge import FakeTime
from tests.test_agent_core_durability import FlakyAudit
from tests.test_agent_core_reminders import AMS, NOW, _header_for
from tests.test_agent_core_turn_scope import _turn as make_event_turn
from tests.test_gate_authorization import EventScopedTool, StandingTool

OWNER = "+31600000000"
ACCOUNT = "+31611111111"
#: A small real acknowledge bound, and the fail-fast bound for anything that could
#: hang. ``BOUND`` is for the test that measures a bound: large enough that
#: scheduling noise is small against it, so the gap is held to
#: [LOWER, UPPER) x BOUND and a bound applied 2x or more fails.
T = 0.05
BOUND = 0.2
LOWER, UPPER = 0.9, 1.5
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


def _session_records(path) -> list[dict]:
    lines = path.read_text().splitlines() if path.exists() else []
    records = [json.loads(line) for line in lines if line.strip()]
    return [r for r in records if r.get("record_type") == "session"]


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


async def test_a_cap_suppressed_triage_shows_no_indicator(tmp_path):
    # Scenario: A cap-suppressed triage shows no indicator (triage-working-indicator
    # task 1.1; it replaces the removed *Event turns are not bracketed*). The triage
    # still runs and its audit record is written, and nothing reaches the owner.
    channel = OrderedChannel()
    audit_path = tmp_path / "audit.jsonl"
    factory = EventSessionFactory()
    core = _core(factory, channel, audit=AuditLog(audit_path))
    await asyncio.wait_for(
        core.process(make_event_turn(announceable=False)), FAIL_FAST
    )
    assert channel.acks == []
    assert channel.order == []  # no typing request and no send
    assert channel.sent == []
    [session] = factory.created
    assert len(session.contents) == 1  # the triage ran
    [record] = _session_records(audit_path)
    assert record["trigger"] == "event"
    assert record["announceable"] is False
    assert record["outcome"] == COMPLETED
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
    # starts one bound after it: not before the bound was spent, not a multiple of it.
    bridge = _TimedBridge()
    bridge.ack_faults["stop"] = asyncio.Event()  # never set
    adapter = SignalAdapter(bridge, account=ACCOUNT, owner=OWNER, sleep=_nosleep)
    started: list[tuple[str, float]] = []

    class Factory:
        def create(self):
            return _TimedSession(started)

    core = AgentCore(
        Factory(), adapter, working_indicator=_ack(adapter, timeout=BOUND).working
    )
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
    assert gap < UPPER * BOUND, f"{gap:.3f}s is more than one {BOUND}s bound"
    assert gap >= LOWER * BOUND, "the close did not wait on the hung stop at all"
    assert ("stop", OWNER) not in bridge.typing  # never completed
    assert len(close_lines()) == 2  # one line per turn, nothing raised


# --- Sent event triages (triage-working-indicator 1.2) ----------------------
#
# From specs/agent-core, ADDED requirement *Sent event triages are bracketed by the
# working indicator*. The same bracket, the same shared ordered list: a triage's
# proactive send lands in ``OrderedChannel.order`` as ``("proactive", text)``.

INCOMPLETE_PREFIX = "[AI] Triage incomplete for "


def _kinds(order: list[tuple]) -> list[str]:
    return [entry[0] for entry in order]


class _TriageSession(OrderedSession):
    """An ``OrderedSession`` for an event turn: it can also report an ending
    (``TurnEnding``) and session stats, as the SDK session does."""

    def __init__(self, order, reply=TRIAGE_REPLY, *, ending=None, stats=None, **kwargs):
        super().__init__(order, reply, **kwargs)
        self._ending = ending
        self._stats = stats

    def ending(self):
        return self._ending

    def stats(self):
        return self._stats


class _TriageFactory:
    def __init__(self, order, **kwargs) -> None:
        self._order = order
        self._kwargs = kwargs
        self.created: list[_TriageSession] = []

    def create(self) -> _TriageSession:
        session = _TriageSession(self._order, **self._kwargs)
        self.created.append(session)
        return session


async def test_a_sent_triage_is_bracketed():
    # Scenario: A sent triage is bracketed. Started before the session received
    # the content; stopped after the triage message was sent.
    channel = OrderedChannel()
    factory = _TriageFactory(channel.order)
    core = _core(factory, channel)
    await asyncio.wait_for(core.process(make_event_turn()), FAIL_FAST)
    assert _kinds(channel.order) == ["start", "turn", "proactive", "stop"]
    [session] = factory.created
    [content] = session.turns
    assert channel.order == [START, ("turn", content), ("proactive", TRIAGE_REPLY), STOP]
    assert channel.acks == [("start", None), ("stop", None)]
    await core.aclose()


#: How the turn did not complete -> (session kwargs, the notice's phrase).
NOT_COMPLETED = {
    "raise": ({"fail": True}, "the triage failed with an error"),
    "refusal": ({"ending": TurnEnding(refusal=True)}, "the model declined the request"),
    "no-reply": ({"reply": ""}, "the model produced no reply"),
}


@pytest.mark.parametrize("ending", sorted(NOT_COMPLETED))
async def test_a_sent_triage_that_did_not_complete_clears_after_its_notice(ending):
    # Scenario: A sent triage that did not complete clears after its notice, for
    # each of the three endings (a raise, a refusal, no reply).
    session_kwargs, phrase = NOT_COMPLETED[ending]
    channel = OrderedChannel()
    core = _core(_TriageFactory(channel.order, **session_kwargs), channel)
    await asyncio.wait_for(core.process(make_event_turn()), FAIL_FAST)
    assert _kinds(channel.order) == ["start", "turn", "proactive", "stop"]
    notice = channel.order[2][1]
    assert notice.startswith(INCOMPLETE_PREFIX) and phrase in notice
    assert channel.acks == [("start", None), ("stop", None)]
    await core.aclose()


async def test_the_indicator_clears_when_starting_the_incident_session_fails():
    # Scenario: the "exception while starting the incident session" exit. The
    # bracket opened before `_start_event_session`, so its close still runs, and
    # the exception reaches the caller as before.
    channel = OrderedChannel()
    core = _core(RaisingFactory(), channel)
    with pytest.raises(RuntimeError, match="session setup failed"):
        await asyncio.wait_for(core.process(make_event_turn()), FAIL_FAST)
    assert channel.order == [START, STOP]
    assert asyncio.all_tasks() == {asyncio.current_task()}, "the refresh outlived the turn"


class _SinkFailed(RuntimeError):
    pass


def _raising_sink(_key: str, _ref: str) -> None:
    raise _SinkFailed("handoff sink failed")


@pytest.mark.parametrize("via", ["process", "run"])
async def test_an_exception_inside_the_bracket_still_stops_the_indicator(via, caplog):
    # Scenario: An exception inside the bracket still stops the indicator. The
    # recurrence `handoff_sink` raises inside the flush, between the agent turn and
    # the proactive send: stop sent, no send, no task left, and the exception
    # propagates out of `process` and to `AgentCore.run`'s logger as today.
    channel = OrderedChannel()
    factory = _TriageFactory(channel.order, stats=handoff_stats("hf-1"))
    core = _core(factory, channel, handoff_sink=_raising_sink)
    line = "unexpected error processing turn"
    if via == "process":
        with pytest.raises(_SinkFailed):
            await asyncio.wait_for(core.process(make_event_turn()), FAIL_FAST)
    else:
        await core.submit_event(make_event_turn())
        with caplog.at_level(logging.ERROR, logger="henk.agent"):
            worker = asyncio.create_task(core.run())
            try:
                await _until(
                    lambda: any(r.getMessage() == line for r in caplog.records),
                    "run's logger",
                )
            finally:
                worker.cancel()
                await asyncio.wait_for(asyncio.wait({worker}), FAIL_FAST)
        [record] = [r for r in caplog.records if r.getMessage() == line]
        assert isinstance(record.exc_info[1], _SinkFailed)
    assert _kinds(channel.order) == ["start", "turn", "stop"]
    assert channel.acks == [("start", None), ("stop", None)]
    assert channel.sent == []
    assert asyncio.all_tasks() == {asyncio.current_task()}, "a task outlived the turn"
    await core.aclose()


@pytest.mark.parametrize("via", ["process", "run"])
async def test_cancellation_during_a_sent_triage_sends_no_stop(via):
    # Scenario: Cancellation during a sent triage sends no stop. Out of `process`
    # and out of `AgentCore.run`; the autouse fixture proves the task finished.
    hold = asyncio.Event()  # never set: the triage is mid-flight when cancelled
    channel = OrderedChannel()
    core = _core(_TriageFactory(channel.order, hold=hold), channel)
    if via == "run":
        await core.submit_event(make_event_turn())
        task = asyncio.create_task(core.run())
    else:
        task = asyncio.create_task(core.process(make_event_turn()))
    await _until(lambda: "turn" in _kinds(channel.order), "the triage to be running")
    task.cancel()
    await asyncio.wait_for(asyncio.wait({task}), FAIL_FAST)
    assert task.cancelled(), "the cancellation was absorbed"
    assert channel.acks == [("start", None)]  # started, and no stop attempted
    assert channel.sent == []


async def test_a_cancel_at_the_brackets_entry_leaves_no_indicator_and_sends_no_stop():
    # Scenario: cancellation delivered while the bracket is being entered. One loop
    # step puts the turn at the bracket's entry yield, which comes before
    # `_start_event_session`: no session is created, no stop, no task left.
    channel = OrderedChannel()
    factory = _TriageFactory(channel.order)
    core = _core(factory, channel)
    task = asyncio.create_task(core.process(make_event_turn()))
    await asyncio.sleep(0)
    assert not task.done(), "the triage ran to its end without entering a bracket"
    task.cancel()
    await asyncio.wait_for(asyncio.wait({task}), FAIL_FAST)
    assert task.cancelled(), "the cancellation was absorbed"
    assert factory.created == [], "the cancel did not land at the bracket's entry"
    assert ("stop", None) not in channel.acks
    assert channel.sent == []


class _TriageThenOwnerSession:
    """The triage turn replies at once; the owner follow-up (same session) holds
    until ``release`` is set, so the test can heal the bridge first."""

    def __init__(self, started: list, release: asyncio.Event) -> None:
        self._started = started
        self._release = release

    async def run_turn(self, text: str) -> str:
        self._started.append((text, time.monotonic()))
        if len(self._started) == 1:
            return TRIAGE_REPLY
        await self._release.wait()
        return f"ok:{text}"

    async def close(self) -> None:
        pass


async def test_a_hung_stop_after_a_triage_does_not_hold_the_next_turn_beyond_the_bound(
    caplog,
):
    # Scenario: A hung stop after a triage does not hold the next turn beyond the
    # bound. The close begins as the triage message's send returns; the queued
    # owner turn starts one bound after it, not before, not a multiple of it.
    bridge = _TimedBridge()
    bridge.ack_faults["stop"] = asyncio.Event()  # never set
    adapter = SignalAdapter(bridge, account=ACCOUNT, owner=OWNER, sleep=_nosleep)
    started: list[tuple[str, float]] = []
    release = asyncio.Event()
    session = _TriageThenOwnerSession(started, release)

    class Factory:
        def create(self):
            return session

    core = AgentCore(
        Factory(), adapter, working_indicator=_ack(adapter, timeout=BOUND).working
    )
    await core.submit_event(make_event_turn())
    await core.submit("what happened?")
    worker = asyncio.create_task(core.run())

    def close_lines() -> list[str]:
        return [r.getMessage() for r in caplog.records if CLOSE_LINE in r.getMessage()]

    try:
        with caplog.at_level(logging.WARNING, logger=ACK_LOGGER):
            await _until(lambda: len(started) == 2, "the queued owner turn to start")
            # Heal the bridge so the owner turn's own stop completes, then let it end.
            bridge.ack_faults.pop("stop")
            release.set()
            await _until(lambda: ("stop", OWNER) in bridge.typing, "the owner turn's stop")
    finally:
        worker.cancel()
        await asyncio.wait_for(asyncio.wait({worker}), FAIL_FAST)

    # The triage's start and hung stop, then the owner turn's start and stop.
    assert [op for op, _ in bridge.ack_attempts] == ["start", "stop", "start", "stop"]
    assert bridge.sends[0] == (OWNER, TRIAGE_REPLY)  # sent before the close began
    gap = started[1][1] - bridge.sent_at[0]
    assert gap < UPPER * BOUND, f"{gap:.3f}s is more than one {BOUND}s bound"
    assert gap >= LOWER * BOUND, "the close did not wait on the hung stop at all"
    assert len(close_lines()) == 1  # the triage's close, logged once, nothing raised


@pytest.mark.parametrize("wiring", ["omitted", "explicit-none", "enabled"])
async def test_disabled_acknowledgement_leaves_event_turns_unchanged(wiring):
    # Scenario: Disabled acknowledgement leaves event turns unchanged. The session
    # content is the composition the core has always sent, and the proactive send
    # is the triage reply, byte for byte, in all three wirings; only enabled has acks.
    channel = FakeChannel()
    factory = EventSessionFactory()
    kwargs = {
        "omitted": {},
        "explicit-none": {"working_indicator": None},
        "enabled": {"working_indicator": _ack(channel).working},
    }[wiring]
    core = AgentCore(factory, channel, **kwargs)
    turn = make_event_turn()
    await asyncio.wait_for(core.process(turn), FAIL_FAST)
    expected = compose_event_turn_content(
        turn, recall=None, tool_names=None, digest=read_digest(None, turn)
    )
    [session] = factory.created
    assert session.contents == [expected]
    assert channel.calls == [("proactive", TRIAGE_REPLY, TRIAGE_FAILURE_NOTICE)]
    if wiring == "enabled":
        assert channel.acks == [("start", None), ("stop", None)]
    else:
        assert channel.acks == []
    await core.aclose()


async def test_a_degraded_durability_notice_inside_a_sent_triage_precedes_the_stop():
    # Scenario: A sent triage is bracketed, on the genuine-audit-failure path. The
    # degraded-durability notice is sent inside the bracket, then the triage
    # message, then the stop.
    channel = OrderedChannel()
    core = _core(_TriageFactory(channel.order), channel, audit=FlakyAudit([False]))
    await asyncio.wait_for(core.process(make_event_turn()), FAIL_FAST)
    assert _kinds(channel.order) == ["start", "turn", "proactive", "proactive", "stop"]
    assert channel.order[2] == ("proactive", DEGRADED_DURABILITY_NOTICE)
    assert channel.order[3] == ("proactive", TRIAGE_REPLY)
    await core.aclose()


class _StampedChannel(OrderedChannel):
    """An ``OrderedChannel`` that also stamps sends and typing requests with the
    fake clock's time, so "within one poll" and "refreshed at 7 s" are exact."""

    def __init__(self, fake: FakeTime) -> None:
        super().__init__()
        self._fake = fake
        self.stamps: list[tuple[str, float]] = []

    async def send(self, text):
        self.stamps.append(("send", self._fake.now))
        return await super().send(text)

    async def send_proactive(self, text, *, failure_notice=None):
        self.stamps.append(("proactive", self._fake.now))
        return await super().send_proactive(text, failure_notice=failure_notice)

    async def start_working(self) -> bool:
        self.stamps.append(("start", self._fake.now))
        return await super().start_working()

    async def stop_working(self) -> bool:
        self.stamps.append(("stop", self._fake.now))
        return await super().stop_working()

    def at(self, op: str) -> list[float]:
        return [now for kind, now in self.stamps if kind == op]


class _EventApprovalSession:
    """Invokes an EVENT-scoped per-instance tool through the REAL permission path.

    Inside an announceable event turn the gate prompts the owner and waits (the
    tool declares event scope, so taint does not deny it). After the tool runs the
    session holds until ``proceed`` is set, so the turn is still running when the
    indicator should resume.
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
            self._registry, self._gate, "mcp__henk__event_scoped_write", {"text": "x"}
        )
        if not decision.allow:
            return f"blocked: {decision.reason}"
        await self._tool.run(text="x")
        self.approved = True
        await self.proceed.wait()
        return TRIAGE_REPLY

    async def close(self) -> None:
        pass


async def test_the_indicator_pauses_during_an_approval_in_a_sent_triage():
    # Scenario: the approval-pause behaviour carried over to sent triages (the ADDED
    # requirement's "same ... approval-pause behaviour"; channel-adapter *The
    # indicator is suspended during an approval wait*). A test-only EVENT-scoped
    # per-instance tool, a real gate, `paused` that gate's `has_pending`, FakeTime.
    fake = FakeTime()
    channel = _StampedChannel(fake)
    gate = ApprovalGate(channel, timeout_seconds=60)
    tool = EventScopedTool()
    registry = ToolRegistry()
    registry.register(tool)
    session = _EventApprovalSession(channel.order, registry, gate, tool)

    class Factory:
        def create(self):
            return session

    ack = OwnerAcknowledgement(
        channel,
        timeout=T,
        refresh_seconds=TYPING_REFRESH_SECONDS,
        paused=gate.has_pending,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    core = AgentCore(Factory(), channel, gate=gate, working_indicator=ack.working)
    turn = asyncio.create_task(core.process(make_event_turn()))
    try:
        await _until(lambda: "turn" in _kinds(channel.order), "the triage to be running")
        assert channel.order[0] == START, "no indicator on the sent triage"
        await _until(gate.has_pending, "the approval prompt")
        prompt = next(
            i for i, entry in enumerate(channel.order)
            if entry[0] == "send" and "Approval needed" in entry[1]
        )
        [prompted_at] = channel.at("send")

        # Well past the refresh interval while the owner decides.
        await fake.run_until(10.0)
        await fake.sleeping()
        assert fake.now > TYPING_REFRESH_SECONDS
        assert channel.order[prompt + 1 :] == [STOP], "refreshed while pending"
        [paused_at] = channel.at("stop")
        assert prompted_at <= paused_at <= prompted_at + PAUSE_POLL_SECONDS

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
    assert tool.calls == [{"text": "x"}]
    assert channel.order[prompt + 1 :] == [
        STOP,
        START,
        ("proactive", TRIAGE_REPLY),
        STOP,
    ]


async def test_a_long_sent_triage_refreshes_the_indicator():
    # Scenario: the refresh carried over to sent triages (channel-adapter *A triage
    # the owner will receive shows the indicator*, "with the same refresh"): start
    # at 0, refreshes at 7 s and 14 s, stop after the proactive send.
    fake = FakeTime()
    channel = _StampedChannel(fake)
    hold = asyncio.Event()
    ack = OwnerAcknowledgement(
        channel,
        timeout=T,
        refresh_seconds=TYPING_REFRESH_SECONDS,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    core = AgentCore(
        _TriageFactory(channel.order, hold=hold), channel, working_indicator=ack.working
    )
    turn = asyncio.create_task(core.process(make_event_turn()))
    try:
        await _until(lambda: "turn" in _kinds(channel.order), "the triage to be running")
        assert channel.at("start") == [0.0], "no indicator on the sent triage"
        await fake.run_until(2 * TYPING_REFRESH_SECONDS)
        await fake.sleeping()
        assert channel.at("start") == [0.0, 7.0, 14.0]
        assert channel.at("stop") == []
        hold.set()
        await asyncio.wait_for(turn, FAIL_FAST)
    finally:
        if not turn.done():
            turn.cancel()
            await asyncio.wait({turn})
    assert _kinds(channel.order)[-2:] == ["proactive", "stop"]
    assert channel.order[-2] == ("proactive", TRIAGE_REPLY)
    await core.aclose()
