"""The single tool-permission choke point — SDK-independent, and unit-tested.

Every tool call the agent attempts is decided here:
- a tool not in the registry (any built-in, any unknown name) is **denied** —
  this is the closed toolset, enforced as default-deny rather than an
  enumerate-and-hope denylist;
- a read-only / notify-only tool is allowed without a prompt;
- a mutating tool is routed through the approval gate, and allowed only if the
  owner approves.

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

from henk.gate.approval import ApprovalGate, ApprovalOutcome
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
    outcome = await gate.authorize(tool, dict(arguments or {}))
    if outcome is ApprovalOutcome.APPROVED:
        return PermissionDecision(True)
    if outcome is ApprovalOutcome.DENIED:
        return PermissionDecision(False, "denied by owner; not executed")
    return PermissionDecision(False, "approval timed out; not executed")
