"""Integration tests: the security controls exercised through the composed path.

Unlike the unit tests, these wire allowlist → dispatcher → core → gate together
and prove the boundaries hold where they actually run (scrutiny C3/M6/C2).
"""

from __future__ import annotations

import asyncio

from henk.agent.core import AgentCore
from henk.agent.permission import decide_tool_permission
from henk.app import Dispatcher
from henk.channel.allowlist import AllowlistFilter
from henk.channel.base import InboundMessage
from henk.gate.approval import ApprovalGate
from henk.tools.base import ToolRegistry
from tests.conftest import FakeChannel
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
