"""Approval-gate tests (task 2.3), from specs/approval-gate."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from henk.config import Config
from henk.gate.approval import (
    ApprovalGate,
    ApprovalOutcome,
    Classification,
    GateBusyError,
    gated_invoke,
)
from henk.tools import build_production_registry
from henk.tools.base import Tool, ToolClass, ToolRegistry, ToolResult
from tests.conftest import FakeChannel
from tests.test_config import SAMPLE


class SpyMutatingTool(Tool):
    name = "spy_mutate"
    description = "test-only mutating tool"
    tool_class = ToolClass.MUTATING
    parameters = {"type": "object", "properties": {"x": {"type": "integer"}}}

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def _run(self, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult.success("mutated")


class ReadTool(Tool):
    name = "read_thing"
    tool_class = ToolClass.READ_ONLY
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0

    async def _run(self, **kwargs) -> ToolResult:
        self.calls += 1
        return ToolResult.success("read")


async def _until_pending(gate: ApprovalGate) -> None:
    for _ in range(1000):
        if gate.has_pending():
            return
        await asyncio.sleep(0)
    raise AssertionError("gate never became pending")


# --- Classification required at registration ------------------------------


def test_unclassified_tool_rejected_naming_it():
    class Unclassified(Tool):
        name = "no_class"

    registry = ToolRegistry()
    with pytest.raises(ValueError, match="no_class"):
        registry.register(Unclassified())


def test_production_registry_has_no_mutating_tools():
    async def handler(request):  # pragma: no cover - never called at registration
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = build_production_registry(Config.load(SAMPLE, env={}), client)
    assert registry.has_mutating() is False
    # taiga_read AND todo_read are both deferred (mixed personal/work data, Tier-W)
    # — not registered. publish_handoff (henk-events v1.2) is notify-class, so the
    # registry is still zero mutating.
    assert set(registry.names()) == {
        "homelab_health",
        "notify",
        "publish_handoff",
    }


# --- Read-only bypass -----------------------------------------------------


async def test_read_only_tool_bypasses_gate():
    channel = FakeChannel()
    gate = ApprovalGate(channel)
    tool = ReadTool()
    result = await gated_invoke(gate, tool, {})
    assert result.ok
    assert tool.calls == 1
    assert channel.sent == []  # no approval prompt


# --- Approve / deny / unrelated / timeout paths ---------------------------


async def test_owner_approves_executes_once_with_shown_args():
    channel = FakeChannel()
    gate = ApprovalGate(channel, timeout_seconds=5)
    tool = SpyMutatingTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"x": 1}))
    await _until_pending(gate)
    assert "spy_mutate" in channel.sent[0] and "1" in channel.sent[0]
    gate.deliver("yes")
    result = await task
    assert result.ok
    assert tool.calls == [{"x": 1}]


async def test_owner_denies_cancels_without_executing():
    channel = FakeChannel()
    gate = ApprovalGate(channel, timeout_seconds=5)
    tool = SpyMutatingTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"x": 2}))
    await _until_pending(gate)
    classification, requeue = gate.deliver("no")
    result = await task
    assert classification is Classification.DENY
    assert requeue is False
    assert result.ok is False
    assert "denied" in (result.error or "")
    assert tool.calls == []


async def test_unrelated_message_fails_closed_and_requeues():
    channel = FakeChannel()
    gate = ApprovalGate(channel, timeout_seconds=5)
    tool = SpyMutatingTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"x": 3}))
    await _until_pending(gate)
    classification, requeue = gate.deliver("what's on my board?")
    result = await task
    assert classification is Classification.UNRELATED
    assert requeue is True  # the message must still be processed as a new turn
    assert result.ok is False
    assert tool.calls == []


async def test_timeout_counts_as_denial():
    channel = FakeChannel()
    gate = ApprovalGate(channel, timeout_seconds=0.02)
    tool = SpyMutatingTool()
    result = await gated_invoke(gate, tool, {"x": 4})
    assert result.ok is False
    assert "timed out" in (result.error or "")
    assert tool.calls == []
    assert gate.has_pending() is False


# --- Single pending & single-use, argument-bound --------------------------


async def test_only_one_approval_pending_per_conversation():
    channel = FakeChannel()
    gate = ApprovalGate(channel, timeout_seconds=5)
    tool = SpyMutatingTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"x": 5}))
    await _until_pending(gate)
    with pytest.raises(GateBusyError):
        await gate.authorize(tool, {"x": 6})
    gate.deliver("no")
    await task


async def test_approval_is_single_use_new_invocation_needs_new_prompt():
    channel = FakeChannel()
    gate = ApprovalGate(channel, timeout_seconds=5)
    tool = SpyMutatingTool()

    task1 = asyncio.create_task(gated_invoke(gate, tool, {"x": 7}))
    await _until_pending(gate)
    gate.deliver("yes")
    assert (await task1).ok

    task2 = asyncio.create_task(gated_invoke(gate, tool, {"x": 7}))
    await _until_pending(gate)
    gate.deliver("yes")
    assert (await task2).ok

    assert tool.calls == [{"x": 7}, {"x": 7}]
    assert len(channel.sent) == 2  # each invocation prompted separately


def test_keyword_classification_case_insensitive():
    assert ApprovalGate.classify("YES") is Classification.APPROVE
    assert ApprovalGate.classify(" Approve ") is Classification.APPROVE
    assert ApprovalGate.classify("No") is Classification.DENY
    assert ApprovalGate.classify("deny") is Classification.DENY
    assert ApprovalGate.classify("maybe later") is Classification.UNRELATED
