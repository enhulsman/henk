"""The seam between agent-core and the Claude Agent SDK.

Agent-core depends only on these Protocols, so its behaviour (queueing, reset,
idle, error handling) is tested with the SDK fully mocked. The real
implementation lives in ``henk.agent.sdk_session`` and is the only code that
imports ``claude_agent_sdk``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


#: The one tool whose RESULT the application reads rather than merely recording:
#: its return value becomes the audit record's ``handoff_message_id``. Defined here,
#: in the neutral seam both sides depend on, so the SDK wrapper (which decides whose
#: result to retain) and the agent core (which reads it back out) cannot drift apart.
HANDOFF_TOOL_NAME = "publish_handoff"

#: The two session profiles (triage-quality D11). A factory carries one, and every
#: session record names the profile of the factory that CREATED its session, never
#: one inferred from the record's trigger. A factory that carries none is ``chat``.
CHAT_PROFILE = "chat"
EVENT_PROFILE = "event"
PROFILES = frozenset({CHAT_PROFILE, EVENT_PROFILE})


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
    #: Cache-read input tokens (prompt caching). ``input_tokens`` counts only
    #: UNCACHED input, so this is additive for true cost accounting (design D7).
    cache_read_input_tokens: int | None = None


@dataclass(frozen=True)
class TurnEnding:
    """The structured signals of how a session's LAST turn ended (D12).

    Observed from the SDK stream as it arrives, so a result that reported an
    error is recorded even when the stream raises right after it (evidence-probe
    1.2). Values are as the SDK reported them; the classifier
    (``henk.agent.ending``) decides what they mean and what may be rendered. No
    field carries message text.
    """

    #: The first ``AssistantMessage.error`` of the turn (a closed SDK enum).
    assistant_error: str | None = None
    #: A ``stop_reason`` of ``"refusal"`` on an assistant or the result message.
    refusal: bool = False
    #: ``ResultMessage.is_error``. Read on its own: a failed turn has been seen
    #: with ``subtype="success"`` and ``is_error=True`` (evidence-probe 1.3).
    result_is_error: bool = False
    #: ``ResultMessage.api_error_status``, the failing API call's HTTP status.
    api_error_status: int | None = None
    #: ``ResultMessage.terminal_reason`` (SDK 0.2.157), e.g. ``completed``.
    terminal_reason: str | None = None


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

    # Optional: sessions MAY report how their last turn ended (D12). A session
    # without it, like one whose ``ending()`` raises or returns None, is treated
    # as reporting no signal, so every existing fake keeps working.
    # def ending(self) -> TurnEnding: ...


@runtime_checkable
class SessionFactory(Protocol):
    """Creates fresh sessions. Encapsulates tools, model, and closed-toolset config.

    A factory MAY also carry ``profile`` (``chat`` or ``event``), ``effort`` and
    ``model``, which the core stamps on the records of the sessions it creates
    (D11). A factory without ``profile`` is ``chat``.
    """

    def create(self) -> AgentSession:
        ...
