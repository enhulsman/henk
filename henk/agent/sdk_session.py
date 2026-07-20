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

    def _build_can_use_tool(self):  # pragma: no cover - exercised at deploy
        """Return the SDK ``can_use_tool`` callback bound to registry + gate."""
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        registry, gate = self._registry, self._gate

        async def can_use_tool(tool_name, input_data, context):
            decision = await decide_tool_permission(
                registry, gate, tool_name, input_data
            )
            if decision.allow:
                return PermissionResultAllow()
            return PermissionResultDeny(message=decision.reason)

        return can_use_tool

    def _adapt_tool(self, henk_tool):  # pragma: no cover - deploy path
        """Wrap a Henk Tool as an in-process SDK MCP tool."""
        from claude_agent_sdk import tool as sdk_tool

        # VERIFY AT DEPLOY (task 1.4): confirm @tool accepts a JSON-schema dict for
        # input_schema in 0.2.123 (the docs also show a {name: type} shorthand).
        @sdk_tool(henk_tool.name, henk_tool.description, henk_tool.parameters)
        async def _handler(args):
            result = await henk_tool.run(**(args or {}))
            text = result.content if result.ok else f"ERROR: {result.error}"
            return {"content": [{"type": "text", "text": text}]}

        return _handler

    def _build_mcp_server(self):  # pragma: no cover - deploy path
        from claude_agent_sdk import create_sdk_mcp_server

        tools = [self._adapt_tool(t) for t in self._registry.tools()]
        return create_sdk_mcp_server(name=MCP_SERVER_NAME, tools=tools)

    def create(self):  # pragma: no cover - requires the SDK + live credentials
        """Build a real Claude Agent SDK session with the closed toolset + gate.

        VERIFY AT DEPLOY (task 1.4/5.3): confirm the ClaudeSDKClient method names
        and ClaudeAgentOptions field names against installed 0.2.123, and smoke-test
        that a built-in (e.g. Bash) is genuinely uncallable.
        """
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        options = ClaudeAgentOptions(
            model=self._config.model,
            system_prompt=self._config.system_prompt,
            mcp_servers={MCP_SERVER_NAME: self._build_mcp_server()},
            allowed_tools=list(self._config.allowed_tools),  # empty by design
            disallowed_tools=list(self._config.disallowed_tools),
            permission_mode=self._config.permission_mode,
            can_use_tool=self._build_can_use_tool(),
        )
        return _SdkAgentSession(ClaudeSDKClient(options=options))


class _SdkAgentSession:  # pragma: no cover - requires the SDK + live credentials
    """Adapts a stateful claude_agent_sdk client to the AgentSession protocol."""

    def __init__(self, client) -> None:
        self._client = client
        self._connected = False

    async def run_turn(self, text: str) -> str:
        if not self._connected:
            await self._client.connect()
            self._connected = True
        await self._client.query(text)
        parts: list[str] = []
        async for message in self._client.receive_response():
            for block in getattr(message, "content", None) or []:
                chunk = getattr(block, "text", None)
                if chunk:
                    parts.append(chunk)
        return "".join(parts).strip()

    async def close(self) -> None:
        if self._connected:
            await self._client.disconnect()
            self._connected = False
