"""Integration tests: the security controls exercised through the composed path.

Unlike the unit tests, these wire allowlist → dispatcher → core → gate together
and prove the boundaries hold where they actually run (scrutiny C3/M6/C2).
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from henk.agent.core import AgentCore
from henk.agent.permission import decide_tool_permission
from henk.app import Dispatcher
from henk.channel.acknowledge import OwnerAcknowledgement
from henk.channel.allowlist import AllowlistFilter
from henk.channel.base import InboundMessage
from henk.channel.signal import TYPING_REFRESH_SECONDS, SignalAdapter
from henk.gate.approval import ApprovalGate
from henk.tools.base import ToolRegistry
from tests.conftest import FakeBridge, FakeChannel
from tests.test_approval_gate import ReadTool, SpyMutatingTool

OWNER = "+31600000000"


class ScriptedSession:
    """Fake session: 'mutate' attempts the spy tool via the REAL permission path."""

    def __init__(self, registry, gate, tool):
        self._registry = registry
        self._gate = gate
        self._tool = tool

    async def run_turn(self, text: str) -> str:
        if text == "mutate":
            decision = await decide_tool_permission(
                self._registry, self._gate, "mcp__henk__spy_mutate", {"x": 1}
            )
            if decision.allow:
                await self._tool.run(x=1)
                return "did mutate"
            return f"blocked: {decision.reason}"
        return f"echo:{text}"

    async def close(self):
        pass


class ScriptedFactory:
    def __init__(self, registry, gate, tool):
        self._registry = registry
        self._gate = gate
        self._tool = tool
        self.created = 0

    def create(self):
        self.created += 1
        return ScriptedSession(self._registry, self._gate, self._tool)


def _wire():
    channel = FakeChannel()
    gate = ApprovalGate(channel, timeout_seconds=5)
    registry = ToolRegistry()
    spy = SpyMutatingTool()
    registry.register(spy)
    registry.register(ReadTool())
    factory = ScriptedFactory(registry, gate, spy)
    core = AgentCore(factory, channel)
    dispatcher = Dispatcher(AllowlistFilter(OWNER), gate, core)
    return channel, gate, factory, core, dispatcher, spy


def _msg(text, sender=OWNER, is_group=False):
    return InboundMessage(sender=sender, text=text, timestamp=0.0, is_group=is_group)


async def _until(pred, tries=5000):
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not met in time")


async def _cancel(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_stranger_dropped_before_reaching_core():
    channel, gate, factory, core, dispatcher, spy = _wire()
    await dispatcher.on_inbound(_msg("hello", sender="+31699999999"))
    await dispatcher.on_inbound(_msg("hi", is_group=True))  # group, even from owner
    assert factory.created == 0  # no session ever created
    assert channel.sent == []  # no reply of any kind


async def test_owner_message_answered_through_full_path():
    channel, gate, factory, core, dispatcher, spy = _wire()
    worker = asyncio.create_task(core.run())
    await dispatcher.on_inbound(_msg("hello"))
    await _until(lambda: channel.sent)
    await _cancel(worker)
    assert channel.sent == ["echo:hello"]


async def test_pending_approval_unrelated_fails_closed_then_requeues():
    channel, gate, factory, core, dispatcher, spy = _wire()
    worker = asyncio.create_task(core.run())

    # Owner asks for the mutating action → turn suspends awaiting approval.
    await dispatcher.on_inbound(_msg("mutate"))
    await _until(gate.has_pending)
    assert any("spy_mutate" in s for s in channel.sent)  # approval prompt sent

    # An unrelated message arrives while the approval is pending.
    await dispatcher.on_inbound(_msg("what's up?"))
    await _until(lambda: "echo:what's up?" in channel.sent)
    await _cancel(worker)

    # The mutation never executed (fail-closed)...
    assert spy.calls == []
    # ...and the turn was told it was CANCELLED, not denied: an unrelated message
    # is not an owner "no", and the receipt vocabulary keeps them distinct (D5).
    blocked = next(s for s in channel.sent if s.startswith("blocked:"))
    assert "cancelled" in blocked and "not executed" in blocked
    # ...and the unrelated message was NOT swallowed — it ran as a later turn.
    assert channel.sent.index(blocked) < channel.sent.index("echo:what's up?")


async def test_command_during_pending_approval_fails_the_action_closed_then_runs():
    # agent-core delta: a command arriving while an approval is pending is
    # classified by the GATE first — as an unrelated message it fails the pending
    # action closed, and is only then handled as a command. It must not be
    # swallowed, and it must not be mistaken for an approval keyword.
    from henk.agent.commands import OwnerCommands
    from tests.test_store_seam import FakeInboxStore

    channel, gate, factory, core, dispatcher, spy = _wire()
    inbox = FakeInboxStore()
    inbox.append("something to drain")
    core._commands = OwnerCommands(inbox=inbox)
    worker = asyncio.create_task(core.run())

    await dispatcher.on_inbound(_msg("mutate"))
    await _until(gate.has_pending)

    await dispatcher.on_inbound(_msg("/inbox"))
    await _until(lambda: any("something to drain" in s for s in channel.sent))
    await _cancel(worker)

    assert spy.calls == []  # the pending mutation failed closed
    blocked = next(s for s in channel.sent if s.startswith("blocked:"))
    assert "cancelled" in blocked
    listing = next(s for s in channel.sent if "something to drain" in s)
    assert channel.sent.index(blocked) < channel.sent.index(listing)
    assert factory.created == 1  # the command itself started no session


# --- Owner acknowledgement: the dispatcher's read receipt ------------------
#
# owner-acknowledgement group 5, from specs/channel-adapter (*Owner-only
# acknowledgement of inbound messages*, *Owner-only allowlist*). Every test that
# needs a failure or a stranger runs a real SignalAdapter over a FakeBridge
# (standing rule 1), and every await that could hang is bounded (rule 7).

#: Henk's own account, and the stranger placeholder the pre-commit hook accepts
#: (standing rule 3).
ACCOUNT = "+31611111111"
STRANGER = "00000000-0000-4000-8000-000000000000"
#: A small real acknowledge bound, and the fail-fast bound for anything that could
#: hang. ``ACK_BOUND`` is for the test that measures a bound: large enough that
#: scheduling noise is small against it, so the elapsed time is held to
#: [LOWER, UPPER) x ACK_BOUND and a bound applied 2x or more fails.
ACK_T = 0.05
ACK_BOUND = 0.2
LOWER, UPPER = 0.9, 1.5
FAIL_FAST = 2.0
REF_1, REF_2 = "1700000000001", "1700000000002"
ALLOWLIST_LOGGER = "henk.channel.allowlist"


class _OrderedBridge(FakeBridge):
    """A FakeBridge that notes in a shared list when each receipt is ISSUED.

    ``receipts`` records completions only, so it cannot say whether the message
    was queued before the receipt began; this can.
    """

    def __init__(self, order: list, script=None, **kwargs) -> None:
        super().__init__(script, **kwargs)
        self._order = order

    async def send_receipt(self, recipient: str, timestamp: int) -> None:
        self._order.append(("receipt", timestamp))
        await super().send_receipt(recipient, timestamp)


async def _nosleep(_delay: float) -> None:
    return None


def _wire_ack(*, script=None, faults=None, timeout=ACK_T):
    """``_wire``, over a real SignalAdapter, with an acknowledgement on the dispatcher.

    ``order`` interleaves ``("submit", text)`` (the core queue took the message)
    with ``("receipt", timestamp)`` (the receipt was issued), in the order they
    happened.
    """
    from types import SimpleNamespace

    order: list = []
    bridge = _OrderedBridge(order, script, hold_open=True)
    bridge.ack_faults.update(faults or {})
    adapter = SignalAdapter(bridge, account=ACCOUNT, owner=OWNER, sleep=_nosleep)
    gate = ApprovalGate(adapter, timeout_seconds=5)
    registry = ToolRegistry()
    spy = SpyMutatingTool()
    registry.register(spy)
    registry.register(ReadTool())
    factory = ScriptedFactory(registry, gate, spy)
    core = AgentCore(factory, adapter)
    real_submit = core.submit

    async def submit(text: str) -> None:
        order.append(("submit", text))
        await real_submit(text)

    core.submit = submit
    acknowledgement = OwnerAcknowledgement(
        adapter,
        timeout=timeout,
        refresh_seconds=TYPING_REFRESH_SECONDS,
        paused=gate.has_pending,
    )
    dispatcher = Dispatcher(
        AllowlistFilter(OWNER), gate, core, acknowledgement=acknowledgement
    )
    return SimpleNamespace(
        order=order, bridge=bridge, adapter=adapter, gate=gate, spy=spy,
        factory=factory, core=core, dispatcher=dispatcher,
    )


def _ref_msg(text, ref, sender=OWNER, is_group=False):
    return InboundMessage(
        sender=sender, text=text, timestamp=0.0, is_group=is_group, channel_ref=ref
    )


async def _received(adapter: SignalAdapter, n: int) -> list[InboundMessage]:
    """The first ``n`` messages the real adapter converts from its bridge."""
    stream = adapter.messages()
    try:
        return [await anext(stream) for _ in range(n)]
    finally:
        await stream.aclose()


async def test_owner_message_is_queued_then_receives_a_read_receipt():
    # Scenario: Owner message receives a read receipt. Routing first, then the
    # receipt (design D1), addressed to the configured owner.
    w = _wire_ack()
    await asyncio.wait_for(w.dispatcher.on_inbound(_ref_msg("hello", REF_1)), FAIL_FAST)
    assert w.order == [("submit", "hello"), ("receipt", int(REF_1))]
    assert w.bridge.receipts == [(OWNER, int(REF_1))]
    assert w.bridge.typing == []  # the dispatcher never touches the indicator
    assert w.bridge.sends == []


async def test_an_approval_reply_is_routed_to_the_gate_and_acknowledged():
    # Scenario: An approval reply is acknowledged.
    w = _wire_ack()
    worker = asyncio.create_task(w.core.run())
    try:
        await asyncio.wait_for(
            w.dispatcher.on_inbound(_ref_msg("mutate", REF_1)), FAIL_FAST
        )
        await _until(w.gate.has_pending)
        await asyncio.wait_for(w.dispatcher.on_inbound(_ref_msg("yes", REF_2)), FAIL_FAST)
        await _until(lambda: (OWNER, "did mutate") in w.bridge.sends)
    finally:
        await _cancel(worker)
    assert w.spy.calls == [{"x": 1}]  # the gate took the "yes"
    assert ("submit", "yes") not in w.order  # routed to the gate, never queued
    assert w.bridge.receipts == [(OWNER, int(REF_1)), (OWNER, int(REF_2))]


async def test_an_unrelated_message_during_a_pending_approval_is_acknowledged_once():
    # The message fails the approval closed, is re-queued as a normal turn, and is
    # acknowledged once: it was received once, whichever path it took.
    w = _wire_ack()
    worker = asyncio.create_task(w.core.run())
    try:
        await asyncio.wait_for(
            w.dispatcher.on_inbound(_ref_msg("mutate", REF_1)), FAIL_FAST
        )
        await _until(w.gate.has_pending)
        await asyncio.wait_for(
            w.dispatcher.on_inbound(_ref_msg("what's up?", REF_2)), FAIL_FAST
        )
        await _until(lambda: (OWNER, "echo:what's up?") in w.bridge.sends)
    finally:
        await _cancel(worker)
    assert w.spy.calls == []  # failed closed
    assert w.bridge.receipts == [(OWNER, int(REF_1)), (OWNER, int(REF_2))]
    index = w.order.index(("submit", "what's up?"))
    assert w.order[index + 1 :] == [("receipt", int(REF_2))]


async def test_a_hung_receipt_does_not_hold_the_message_it_acknowledges(caplog):
    # Scenario: A hung receipt does not hold the message it acknowledges.
    w = _wire_ack(faults={"receipt": asyncio.Event()}, timeout=ACK_BOUND)  # never set
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="henk.channel.acknowledge"):
        await asyncio.wait_for(
            w.dispatcher.on_inbound(_ref_msg("hello", REF_1)), FAIL_FAST
        )
    elapsed = time.monotonic() - started
    # Already on the core queue when the receipt was issued.
    assert w.order == [("submit", "hello"), ("receipt", int(REF_1))]
    assert w.core._queue.qsize() == 1
    assert elapsed < UPPER * ACK_BOUND, f"{elapsed:.3f}s is more than one bound"
    assert elapsed >= LOWER * ACK_BOUND, "the hung receipt was not waited out"
    assert w.bridge.receipts == []  # it never completed
    assert len([r for r in caplog.records if "owner read receipt" in r.getMessage()]) == 1
    # The next inbound message is handled once the bound has cut the hang off.
    await asyncio.wait_for(w.dispatcher.on_inbound(_ref_msg("again", REF_2)), FAIL_FAST)
    assert ("submit", "again") in w.order


async def test_a_message_without_a_channel_reference_is_queued_and_not_acknowledged(
    caplog,
):
    # Scenario: A message without a channel reference is not acknowledged and logs
    # nothing — the dispatcher skips the call entirely.
    w = _wire_ack()
    with caplog.at_level(logging.DEBUG, logger="henk.channel.acknowledge"):
        await asyncio.wait_for(w.dispatcher.on_inbound(_ref_msg("hello", None)), FAIL_FAST)
    assert w.order == [("submit", "hello")]
    assert w.bridge.ack_attempts == []
    assert [r for r in caplog.records if r.name == "henk.channel.acknowledge"] == []


@pytest.mark.parametrize("wiring", ["omitted", "explicit-none"])
async def test_disabled_acknowledgement_sends_nothing_and_changes_nothing(wiring):
    # Scenario: Disabled means nothing is sent. The message carries a reference,
    # so only the dispatcher's own choice can keep the receipt from going out.
    channel, gate, factory, core, _dispatcher, spy = _wire()
    kwargs = {} if wiring == "omitted" else {"acknowledgement": None}
    dispatcher = Dispatcher(AllowlistFilter(OWNER), gate, core, **kwargs)
    worker = asyncio.create_task(core.run())
    try:
        await dispatcher.on_inbound(_ref_msg("hello", REF_1))
        await _until(lambda: channel.sent)
    finally:
        await _cancel(worker)
    assert channel.acks == []
    assert channel.sent == ["echo:hello"]  # as test_owner_message_answered_…


async def test_stranger_gets_nothing_with_acknowledgement_enabled(caplog):
    # Scenario: Stranger gets nothing with acknowledgement enabled — the one that
    # matters. Converted by the real adapter, so each message carries the
    # reference a misplaced receipt would use.
    script = [
        {"envelope": {"source": STRANGER, "sourceUuid": STRANGER,
                      "dataMessage": {"message": "hello", "timestamp": 1700000000101}}},
        {"envelope": {"source": OWNER,
                      "dataMessage": {"message": "hi", "timestamp": 1700000000102,
                                      "groupInfo": {"groupId": "g1"}}}},
    ]
    w = _wire_ack(script=script)
    messages = await asyncio.wait_for(_received(w.adapter, 2), FAIL_FAST)
    assert [m.channel_ref for m in messages] == ["1700000000101", "1700000000102"]
    assert messages[0].sender == STRANGER and not messages[0].is_group
    assert messages[1].sender == OWNER and messages[1].is_group

    worker = asyncio.create_task(w.core.run())
    try:
        with caplog.at_level(logging.WARNING, logger=ALLOWLIST_LOGGER):
            for message in messages:
                await asyncio.wait_for(w.dispatcher.on_inbound(message), FAIL_FAST)
            for _ in range(50):  # a queued turn would have started by now
                await asyncio.sleep(0)
    finally:
        await _cancel(worker)
    enabled_lines = [r.getMessage() for r in caplog.records if r.name == ALLOWLIST_LOGGER]

    assert w.bridge.receipts == []
    assert w.bridge.typing == []
    assert w.bridge.sends == []
    assert w.bridge.ack_attempts == []  # not even a refused or hung attempt
    assert w.factory.created == 0
    assert w.order == []  # nothing reached the core queue
    assert enabled_lines == [
        f"dropped message from non-owner sender={STRANGER}",
        f"dropped group message from sender={OWNER}",
    ]

    # Logged exactly as with acknowledgement disabled.
    caplog.clear()
    _channel, _gate, _factory, _core, disabled, _spy = _wire()
    with caplog.at_level(logging.WARNING, logger=ALLOWLIST_LOGGER):
        for message in messages:
            await disabled.on_inbound(message)
    assert [
        r.getMessage() for r in caplog.records if r.name == ALLOWLIST_LOGGER
    ] == enabled_lines
