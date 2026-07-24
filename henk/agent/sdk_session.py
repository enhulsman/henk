"""Claude Agent SDK wrapper — the only module that imports ``claude_agent_sdk``.

The security-critical decision logic lives in ``henk.agent.permission`` and is
unit-tested there without the SDK. This module translates that decision into the
SDK's ``can_use_tool`` callback and assembles ``ClaudeAgentOptions`` so that:

- a ``PreToolUse`` hook is the real closed-toolset boundary: it default-denies
  every tool that is not ``mcp__henk__*`` and runs *before* the SDK's permission
  chain, so it cannot be bypassed (deploy 2026-07-20 proved ``can_use_tool`` is
  NOT universal — ``ToolSearch``/``TaskCreate`` built-ins executed without it);
- built-ins are also stripped from context (``disallowed_tools``) as hygiene;
- ``setting_sources=[]`` + ``strict_mcp_config`` stop settings files / stray MCP
  config from auto-approving tools behind our back;
- ``can_use_tool`` (``allowed_tools`` empty) remains as the read/mutate + approval
  decision for the Henk tools that pass the hook;
- Henk's tools are exposed as an in-process MCP server.

``build_closed_toolset_config`` and the permission wiring are pure and testable;
``SdkSessionFactory.create`` needs the SDK + live credentials and runs at deploy
(verified by a deploy smoke test that a built-in is genuinely uncallable — the
one part that cannot be proven without a real session; see task 1.4/5.3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from typing import Any, Mapping

from henk.agent.permission import decide_tool_permission, pretooluse_block_decision
from henk.agent.session import SessionStats, ToolCallRecord
from henk.gate.approval import ApprovalGate
from henk.tools.base import ToolRegistry

logger = logging.getLogger("henk.agent.sdk_session")

#: In-process MCP server name Henk tools are exposed under.
MCP_SERVER_NAME = "henk"

#: Built-ins the SDK/bundled-CLI ships. Listed in ``disallowed_tools`` to strip
#: them from the model's context (hygiene: the model won't see or attempt them).
#: This is NOT the security boundary — deploy 2026-07-20 proved ``can_use_tool``
#: is bypassable and that built-ins NOT on this list (``ToolSearch``,
#: ``TaskCreate``) executed ungated. The real boundary is the ``PreToolUse`` hook
#: (:func:`~henk.agent.permission.pretooluse_block_decision`), which default-denies
#: everything outside ``mcp__henk__*`` and cannot be bypassed. This list is kept
#: broad so the model is not even tempted by tools the hook would block anyway.
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
    # Deploy 2026-07-20: these reached the model ungated and must be stripped too.
    "ToolSearch",
    "TaskCreate",
    "TaskUpdate",
    "TaskList",
    "TaskGet",
    "TaskOutput",
    "TaskStop",
    "TodoWrite",
    "Skill",
    "SlashCommand",
    "ExitPlanMode",
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

    def _build_pretooluse_hook(self):  # pragma: no cover - exercised at deploy
        """Return the ``PreToolUse`` hook enforcing the closed toolset.

        This is the actual, unbypassable security boundary. It runs before the
        SDK's permission chain, so — unlike ``can_use_tool`` — it cannot be
        skipped by auto-approved built-ins or settings-file allow rules. Non-Henk
        tools are denied here; Henk tools return an empty output to defer to the
        normal flow (``can_use_tool`` → the approval gate).
        """

        async def pre_tool_use(input_data, tool_use_id, context):
            name = (input_data or {}).get("tool_name", "")
            decision = pretooluse_block_decision(name)
            if decision is not None:
                logger.warning("closed-toolset hook blocked non-Henk tool: %s", name)
                return decision
            return {}

        return pre_tool_use

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
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

        options = ClaudeAgentOptions(
            model=self._config.model,
            system_prompt=self._config.system_prompt,
            mcp_servers={MCP_SERVER_NAME: self._build_mcp_server()},
            allowed_tools=list(self._config.allowed_tools),  # empty by design
            disallowed_tools=list(self._config.disallowed_tools),
            permission_mode=self._config.permission_mode,
            can_use_tool=self._build_can_use_tool(),
            # The actual closed-toolset boundary (see _build_pretooluse_hook):
            # unbypassable, unlike can_use_tool.
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="*", hooks=[self._build_pretooluse_hook()])
                ]
            },
            # Ignore user/project/local settings files so no settings.json allow
            # rule can auto-approve a tool and skip our controls.
            setting_sources=[],
            # Only the explicitly-configured in-process MCP server.
            strict_mcp_config=True,
        )
        # name → tool_class so the audit record's tool_calls carry the class the
        # SDK stream does not report (the model only sees the mcp__henk__ name).
        tool_classes = {
            t.name: t.tool_class.value
            for t in self._registry.tools()
            if t.tool_class is not None
        }
        return _SdkAgentSession(
            ClaudeSDKClient(options=options), tool_classes=tool_classes
        )


def _strip_mcp_prefix(name: str) -> str:
    """``mcp__henk__publish_handoff`` → ``publish_handoff`` so audit tool names
    match the registry (and ``core`` can spot ``publish_handoff``)."""
    prefix = f"mcp__{MCP_SERVER_NAME}__"
    return name[len(prefix):] if name.startswith(prefix) else name


def _tool_result_text(content: Any) -> str:
    """Flatten a ToolResultBlock's content (``str | list[dict] | None``) to text.
    This carries the handoff message id through to ``handoff_message_id``."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if isinstance(item, Mapping):
            text = item.get("text")
            if text:
                parts.append(str(text))
    return "".join(parts)


class _StatsAccumulator:
    """Folds the SDK message stream into a :class:`SessionStats`.

    Kept free of any ``claude_agent_sdk`` import and duck-typed against the block
    shapes so it is unit-testable without the live SDK — the fields it fills
    (``tool_calls`` / ``model`` / token ``usage``) were exactly the ones missing
    from every production audit record before ``stats()`` existed.
    """

    def __init__(self, tool_classes: Mapping[str, str] | None = None) -> None:
        self._tool_classes = dict(tool_classes or {})
        self._tool_uses: list[tuple[str, str]] = []  # (tool_use_id, bare name)
        self._results: dict[str, str] = {}  # tool_use_id → result text
        self._model: str | None = None
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._cache_read_input_tokens: int | None = None

    def observe(self, message: Any) -> None:
        for block in getattr(message, "content", None) or []:
            tool_use_id = getattr(block, "tool_use_id", None)
            if tool_use_id is not None:  # ToolResultBlock
                self._results[tool_use_id] = _tool_result_text(
                    getattr(block, "content", None)
                )
                continue
            name = getattr(block, "name", None)
            block_id = getattr(block, "id", None)
            if name is not None and block_id is not None:  # ToolUseBlock
                self._tool_uses.append((block_id, _strip_mcp_prefix(name)))
        model = getattr(message, "model", None)
        if model:
            self._model = model
        # Only ResultMessage carries the query-total usage; total_cost_usd is its
        # distinctive field (AssistantMessage.usage would otherwise double-count).
        if hasattr(message, "total_cost_usd"):
            self._add_tokens(getattr(message, "usage", None) or {})

    def _add_tokens(self, usage: Mapping[str, Any]) -> None:
        # input_tokens keeps its meaning (uncached only); cache_read is additive
        # so a record written before this change (no cache field) stays a valid
        # reader — an absent key leaves the total None, never 0 (design D7).
        for key, attr in (("input_tokens", "_input_tokens"),
                          ("output_tokens", "_output_tokens"),
                          ("cache_read_input_tokens", "_cache_read_input_tokens")):
            value = usage.get(key)
            if value is not None:
                setattr(self, attr, (getattr(self, attr) or 0) + value)

    def snapshot(self) -> SessionStats:
        calls = tuple(
            ToolCallRecord(
                name=name,
                tool_class=self._tool_classes.get(name),
                result_id=self._results.get(tool_use_id) or None,
            )
            for tool_use_id, name in self._tool_uses
        )
        return SessionStats(
            tool_calls=calls,
            model=self._model,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cache_read_input_tokens=self._cache_read_input_tokens,
        )


class _SdkAgentSession:
    """Adapts a stateful claude_agent_sdk client to the AgentSession protocol.

    ``run_turn``/``close`` need the live client (exercised at deploy), but they
    contain no SDK import, so the fake-client stats tests cover the wiring that
    the earlier missing-``stats()`` defect had left untested.
    """

    def __init__(self, client, *, tool_classes: Mapping[str, str] | None = None) -> None:
        self._client = client
        self._connected = False
        self._stats = _StatsAccumulator(tool_classes)

    async def run_turn(self, text: str) -> str:
        if not self._connected:
            await self._client.connect()
            self._connected = True
        await self._client.query(text)
        parts: list[str] = []
        async for message in self._client.receive_response():
            self._stats.observe(message)
            for block in getattr(message, "content", None) or []:
                chunk = getattr(block, "text", None)
                if chunk:
                    parts.append(chunk)
        return "".join(parts).strip()

    def stats(self) -> SessionStats:
        return self._stats.snapshot()

    async def close(self) -> None:  # pragma: no cover - requires the live client
        if self._connected:
            await self._client.disconnect()
            self._connected = False
