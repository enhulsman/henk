"""The seam between agent-core and the Claude Agent SDK.

Agent-core depends only on these Protocols, so its behaviour (queueing, reset,
idle, error handling) is tested with the SDK fully mocked. The real
implementation lives in ``henk.agent.sdk_session`` and is the only code that
imports ``claude_agent_sdk``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolCallRecord:
    """One tool invocation observed during a session, for the audit record."""

    name: str
    tool_class: str | None = None
    result_id: str | None = None


@dataclass(frozen=True)
class SessionStats:
    """Session-level metadata the app layer folds into the audit record.

    Sourced from the SDK's result stream by the real session; fakes stub it.
    A session that does not implement ``stats()`` simply contributes empty
    tool-call/usage fields — audit is best-effort and never blocking.
    """

    tool_calls: tuple[ToolCallRecord, ...] = field(default_factory=tuple)
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class AgentSession(Protocol):
    """A single Claude Agent SDK conversation."""

    async def run_turn(self, text: str) -> str:
        """Run one turn and return the agent's final text reply."""
        ...

    async def close(self) -> None:
        """Release any resources held by the session."""
        ...

    # Optional: sessions MAY expose accumulated metadata for the audit log.
    # def stats(self) -> SessionStats: ...


@runtime_checkable
class SessionFactory(Protocol):
    """Creates fresh sessions. Encapsulates tools, model, and closed-toolset config."""

    def create(self) -> AgentSession:
        ...
