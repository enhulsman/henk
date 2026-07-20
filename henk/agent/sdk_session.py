"""Claude Agent SDK wrapper — the only module that imports ``claude_agent_sdk``.

The security-critical decision logic lives in ``henk.agent.permission`` and is
unit-tested there without the SDK. This module translates that decision into the
SDK's ``can_use_tool`` callback and assembles ``ClaudeAgentOptions`` so that:

- all host-touching built-ins are stripped from context (``disallowed_tools``);
- **nothing is auto-approved** (``allowed_tools`` empty) — auto-approved tools
  would skip ``can_use_tool`` and bypass the gate, so we never use it;
- ``permission_mode="default"`` keeps the callback in the loop for every call;
- Henk's tools are exposed as an in-process MCP server.

``build_closed_toolset_config`` and the permission wiring are pure and testable;
``SdkSessionFactory.create`` needs the SDK + live credentials and runs at deploy
(verified by a deploy smoke test that a built-in is genuinely uncallable — the
one part that cannot be proven without a real session; see task 1.4/5.3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from henk.agent.permission import decide_tool_permission
from henk.gate.approval import ApprovalGate
from henk.tools.base import ToolRegistry

logger = logging.getLogger("henk.agent.sdk_session")

#: In-process MCP server name Henk tools are exposed under.
MCP_SERVER_NAME = "henk"

#: Host-touching / agent-spawning built-ins the SDK ships. Listed in
#: ``disallowed_tools`` to strip them from context. This is context hygiene, NOT
#: the security boundary — the boundary is the default-deny permission callback,
#: which denies anything not in the registry even if the SDK adds a new built-in
#: not on this list.
BUILTIN_HOST_TOOLS = (
    "Bash",
    "BashOutput",
    "KillShell",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
)


def mcp_tool_name(tool_name: str) -> str:
    return f"mcp__{MCP_SERVER_NAME}__{tool_name}"


@dataclass(frozen=True)
class ClosedToolsetConfig:
    """SDK-agnostic description of the session's closed toolset."""

    model: str
    system_prompt: str
    disallowed_tools: tuple[str, ...]
    permission_mode: str = "default"
    #: Deliberately empty: auto-approving a tool skips ``can_use_tool`` and would
    #: bypass the gate. Every call must go through the callback.
    allowed_tools: tuple[str, ...] = ()

    def auto_approves_any(self) -> bool:
        return len(self.allowed_tools) > 0


def build_closed_toolset_config(
    registry: ToolRegistry, *, model: str, system_prompt: str
) -> ClosedToolsetConfig:
    # registry is accepted for symmetry/future use; the closed-toolset guarantee
    # comes from the empty allow-list + default-deny callback, not from naming
    # the tools here.
    return ClosedToolsetConfig(
        model=model,
        system_prompt=system_prompt,
        disallowed_tools=tuple(BUILTIN_HOST_TOOLS),
        permission_mode="default",
        allowed_tools=(),
    )


class SdkSessionFactory:
    """Deploy-time factory building real Claude Agent SDK sessions.

    Holds the registry + gate so the ``can_use_tool`` callback is derived from
    them: any registered mutating tool is forced through the gate automatically.
    ``claude_agent_sdk`` is imported lazily so importing this module never
    requires it.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        gate: ApprovalGate,
        *,
        model: str,
        system_prompt: str,
    ) -> None:
        self._registry = registry
        self._gate = gate
        self._config = build_closed_toolset_config(
            registry, model=model, system_prompt=system_prompt
        )

    @property
    def config(self) -> ClosedToolsetConfig:
        return self._config

    def _build_can_use_tool(self):
        """Return the SDK ``can_use_tool`` callback bound to registry + gate."""
        from claude_agent_sdk import (  # pragma: no cover - deploy path
            PermissionResultAllow,
            PermissionResultDeny,
        )

        registry, gate = self._registry, self._gate

        async def can_use_tool(tool_name, input_data, context):  # pragma: no cover
            decision = await decide_tool_permission(
                registry, gate, tool_name, input_data
            )
            if decision.allow:
                return PermissionResultAllow()
            return PermissionResultDeny(message=decision.reason)

        return can_use_tool

    def create(self):  # pragma: no cover - requires the SDK + live credentials
        raise NotImplementedError(
            "Wire against claude_agent_sdk at deploy time: build the in-process "
            "MCP server from the registry, set disallowed_tools + permission_mode "
            "from self.config, and pass self._build_can_use_tool(). A deploy smoke "
            "test must confirm a built-in (e.g. Bash) is uncallable. See task 1.4."
        )
