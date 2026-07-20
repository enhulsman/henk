"""Permission-decision tests — the actual closed-toolset + gate enforcement.

These drive the same function the SDK's can_use_tool callback calls, so they test
the control that runs, not a config object.
"""

from __future__ import annotations

import asyncio

from henk.agent.permission import (
    base_tool_name,
    decide_tool_permission,
    pretooluse_block_decision,
)
from henk.gate.approval import ApprovalGate
from henk.tools.base import ToolRegistry
from tests.conftest import FakeChannel
from tests.test_approval_gate import ReadTool, SpyMutatingTool, _until_pending


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadTool())
    registry.register(SpyMutatingTool())
    return registry


def test_base_tool_name_strips_prefix():
    assert base_tool_name("mcp__henk__todo_read") == "todo_read"
    assert base_tool_name("Bash") == "Bash"


async def test_builtin_and_unknown_tools_denied():
    gate = ApprovalGate(FakeChannel())
    reg = _registry()
    # A host built-in that somehow reached the callback: denied (default-deny).
    assert (await decide_tool_permission(reg, gate, "Bash", {"cmd": "ls"})).allow is False
    # An MCP-namespaced but unregistered tool: denied.
    assert (
        await decide_tool_permission(reg, gate, "mcp__henk__nope", {})
    ).allow is False


async def test_read_tool_allowed_without_prompt():
    channel = FakeChannel()
    gate = ApprovalGate(channel)
    decision = await decide_tool_permission(
        _registry(), gate, "mcp__henk__read_thing", {}
    )
    assert decision.allow is True
    assert channel.sent == []  # read-only never prompts


async def test_mutating_tool_allowed_only_after_approval():
    channel = FakeChannel()
    gate = ApprovalGate(channel, timeout_seconds=5)
    reg = _registry()
    task = asyncio.create_task(
        decide_tool_permission(reg, gate, "mcp__henk__spy_mutate", {"x": 1})
    )
    await _until_pending(gate)
    assert any("spy_mutate" in s for s in channel.sent)
    gate.deliver("yes")
    assert (await task).allow is True


async def test_mutating_tool_denied_on_no():
    gate = ApprovalGate(FakeChannel(), timeout_seconds=5)
    reg = _registry()
    task = asyncio.create_task(
        decide_tool_permission(reg, gate, "mcp__henk__spy_mutate", {"x": 1})
    )
    await _until_pending(gate)
    gate.deliver("no")
    decision = await task
    assert decision.allow is False
    assert "denied" in decision.reason


async def test_mutating_tool_denied_on_timeout():
    gate = ApprovalGate(FakeChannel(), timeout_seconds=0.02)
    decision = await decide_tool_permission(
        _registry(), gate, "mcp__henk__spy_mutate", {"x": 1}
    )
    assert decision.allow is False
    assert "timed out" in decision.reason


# --- PreToolUse hook (the unbypassable closed-toolset boundary) ---
# Deploy 2026-07-20: can_use_tool was bypassed for bundled-CLI built-ins
# (ToolSearch loaded TaskCreate's schema, TaskCreate then executed ungated). The
# hook must hard-deny every tool outside mcp__henk__* BEFORE the permission chain.


def test_hook_defers_henk_tools():
    # Henk tools return None → fall through to can_use_tool / approval gate.
    assert pretooluse_block_decision("mcp__henk__homelab_health") is None
    assert pretooluse_block_decision("mcp__henk__notify") is None


def test_hook_blocks_the_exact_tools_that_leaked():
    for name in ("ToolSearch", "TaskCreate", "TaskUpdate", "TodoWrite"):
        out = pretooluse_block_decision(name)
        assert out is not None, f"{name} must be blocked"
        spec = out["hookSpecificOutput"]
        assert spec["hookEventName"] == "PreToolUse"
        assert spec["permissionDecision"] == "deny"


def test_hook_blocks_host_builtins_and_unknown_tools():
    for name in ("Bash", "Read", "Write", "WebFetch", "Task", "mcp__other__x", ""):
        out = pretooluse_block_decision(name)
        assert out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"
