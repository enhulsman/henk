"""The seam between agent-core and the Claude Agent SDK.

Agent-core depends only on these Protocols, so its behaviour (queueing, reset,
idle, error handling) is tested with the SDK fully mocked. The real
implementation lives in ``henk.agent.sdk_session`` and is the only code that
imports ``claude_agent_sdk``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AgentSession(Protocol):
    """A single Claude Agent SDK conversation."""

    async def run_turn(self, text: str) -> str:
        """Run one turn and return the agent's final text reply."""
        ...

    async def close(self) -> None:
        """Release any resources held by the session."""
        ...


@runtime_checkable
class SessionFactory(Protocol):
    """Creates fresh sessions. Encapsulates tools, model, and closed-toolset config."""

    def create(self) -> AgentSession:
        ...
