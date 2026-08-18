"""The single tool-permission choke point — SDK-independent, and unit-tested.

Every tool call the agent attempts is decided here:
- a tool not in the registry (any built-in, any unknown name) is **denied** —
  this is the closed toolset, enforced as default-deny rather than an
  enumerate-and-hope denylist;
- a read-only / notify-only tool is allowed without a prompt;
- a mutating tool is routed through the authorization gate, and allowed only if
  its tier and turn scope permit it (standing) or the owner approves it
  (per-instance). Every non-executing decision comes back with the gate's own
  honest reason, which is what the model is told.

Because the decision is keyed on the registry's classification, *registering* a
mutating tool is what forces it through the gate — there is no way to add a write
tool that silently skips approval. The Claude Agent SDK wrapper
(``sdk_session``) binds this as its ``can_use_tool`` callback with nothing in
``allowed_tools`` (auto-approved tools would bypass the callback).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from henk.gate.approval import ApprovalGate
from henk.tools.base import ToolRegistry

logger = logging.getLogger("henk.agent.permission")

#: MCP namespace Henk's in-process tools are exposed under (mcp__<server>__<tool>).
HENK_MCP_PREFIX = "mcp__henk__"


@dataclass(frozen=True)
class PermissionDecision:
    allow: bool
    reason: str = ""


def base_tool_name(sdk_tool_name: str) -> str:
    """Strip the ``mcp__henk__`` namespace to get the registered tool name."""
    if sdk_tool_name.startswith(HENK_MCP_PREFIX):
        return sdk_tool_name[len(HENK_MCP_PREFIX) :]
    return sdk_tool_name


def pretooluse_block_decision(sdk_tool_name: str) -> dict | None:
    """Closed-toolset hard gate for the SDK ``PreToolUse`` hook.

    Returns a ``deny`` hook output for any tool that is not one of Henk's
    in-process MCP tools (``mcp__henk__*``), or ``None`` to let a Henk tool fall
    through to the normal permission flow (``can_use_tool`` →
    :func:`decide_tool_permission`, where read/mutate classification and the
    approval gate apply).

    Why this exists (deploy 2026-07-20): ``can_use_tool`` is NOT a universal
    gate — the SDK skips it for tools auto-approved earlier in its permission
    chain, and bundled-CLI built-ins (observed: ``ToolSearch``, ``TaskCreate``)
    executed without ``can_use_tool`` ever being consulted. A ``PreToolUse`` hook
    runs before that chain and cannot be bypassed by settings-file allow rules or
    auto-approved built-ins, so the closed-toolset guarantee lives HERE. Keyed on
    the ``mcp__henk__`` prefix as a strict allowlist (default-deny).
    """
    if sdk_tool_name.startswith(HENK_MCP_PREFIX):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"{sdk_tool_name} is not a Henk tool; blocked by the "
                "closed-toolset hook"
            ),
        }
    }


async def decide_tool_permission(
    registry: ToolRegistry,
    gate: ApprovalGate,
    sdk_tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> PermissionDecision:
    """Decide whether a single tool call may proceed. Default-deny."""
    name = base_tool_name(sdk_tool_name)
    if name not in registry.names():
        logger.warning("denied non-registered tool call: %s", sdk_tool_name)
        return PermissionDecision(
            False, f"{sdk_tool_name} is not a registered Henk tool"
        )

    tool = registry.get(name)
    # The gate resolves every ambiguous case itself (busy, out of scope, suppressed
    # turn) rather than raising, so there is no error path to translate here: a
    # decision either permits execution or carries the reason it did not.
    decision = await gate.authorize(tool, dict(arguments or {}))
    if decision.permits:
        return PermissionDecision(True)
    logger.info(
        "tool call not executed: %s (%s)", sdk_tool_name, decision.outcome.value
    )
    return PermissionDecision(False, decision.reason)
