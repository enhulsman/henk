"""Agent-core tests (task 2.4), from specs/agent-core. SDK fully mocked."""

from __future__ import annotations

import asyncio

import pytest

from henk.agent.core import AgentCore, RESET_CONFIRMATION
from henk.agent.sdk_session import (
    BUILTIN_HOST_TOOLS,
    build_closed_toolset_config,
    mcp_tool_name,
)
from henk.tools.base import Tool, ToolClass, ToolRegistry
from tests.conftest import FakeChannel, FakeSessionFactory, make_clock


class _ReadTool(Tool):
    name = "read_thing"
    tool_class = ToolClass.READ_ONLY
    parameters = {"type": "object", "properties": {}}

    async def _run(self, **kwargs):  # pragma: no cover - not exercised here
        from henk.tools.base import ToolResult

        return ToolResult.success("ok")


# --- Inbound message becomes an agent turn --------------------------------


async def test_simple_question_gets_single_reply():
    channel = FakeChannel()
    factory = FakeSessionFactory(reply="up")
    core = AgentCore(factory, channel, clock=make_clock([0, 0]))
    await core.process("is everything up?")
    assert channel.sent == ["up:is everything up?"]


async def test_turn_failure_replies_honestly_and_stays_alive():
    channel = FakeChannel()
    factory = FakeSessionFactory(fail=True)
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1]))
    await core.process("boom")
    assert len(channel.sent) == 1
    assert "error" in channel.sent[0].lower()
    # Process another message: must not have crashed.
    await core.process("again")
    assert len(channel.sent) == 2


# --- Closed, explicit toolset ---------------------------------------------


def test_closed_toolset_exposes_only_registered_tools():
    registry = ToolRegistry()
    registry.register(_ReadTool())
    cfg = build_closed_toolset_config(registry, model="m", system_prompt="p")
    assert cfg.allowed_tools == (mcp_tool_name("read_thing"),)
    assert cfg.exposes_builtin() is False


def test_closed_toolset_disables_all_host_builtins():
    registry = ToolRegistry()
    registry.register(_ReadTool())
    cfg = build_closed_toolset_config(registry, model="m", system_prompt="p")
    for builtin in BUILTIN_HOST_TOOLS:
        assert builtin in cfg.disallowed_tools
        assert builtin not in cfg.allowed_tools


# --- Conversation continuity and reset ------------------------------------


async def test_followup_reuses_same_session():
    channel = FakeChannel()
    factory = FakeSessionFactory(reply="r")
    core = AgentCore(factory, channel, idle_timeout_seconds=3600, clock=make_clock([0, 0, 1, 1]))
    await core.process("what's on my board?")
    await core.process("and which are overdue?")
    assert factory.create_count == 1
    assert factory.created[0].turns == ["what's on my board?", "and which are overdue?"]


async def test_reset_command_confirms_and_starts_fresh_session():
    channel = FakeChannel()
    factory = FakeSessionFactory()
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1, 2, 2]))
    await core.process("hi")
    await core.process("/new")
    await core.process("hello")
    assert RESET_CONFIRMATION in channel.sent
    assert factory.create_count == 2  # one before reset, one after


async def test_idle_expiry_starts_fresh_session():
    channel = FakeChannel()
    factory = FakeSessionFactory()
    core = AgentCore(
        factory, channel, idle_timeout_seconds=60, clock=make_clock([0, 0, 100, 100])
    )
    await core.process("first")
    await core.process("second after idle")
    assert factory.create_count == 2


# --- Serial processing per conversation -----------------------------------


async def test_messages_processed_serially_in_order():
    channel = FakeChannel()
    factory = FakeSessionFactory(reply="r")
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1]))
    await core.submit("a")
    await core.submit("b")

    worker = asyncio.create_task(core.run())
    for _ in range(1000):
        if len(channel.sent) >= 2:
            break
        await asyncio.sleep(0)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert channel.sent == ["r:a", "r:b"]  # order preserved
    assert factory.create_count == 1  # same conversation, one session
