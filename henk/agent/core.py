"""Agent core: turns owner messages AND triageable events into SDK turns.

Responsibilities (v1.2):
- one serial per-owner queue carrying **typed turns** — owner turns (reply path)
  and event turns (proactive triage path) never run concurrently (design D5);
- one session per conversation, reused across follow-ups for context continuity;
- event turns arrive with delimited-untrusted-data + triage framing composed by
  the app layer; owner turns get neither (agent-core delta);
- event-turn output routes to the proactive owner-directed send, suppressed for
  non-announceable (cap-overflow) incidents;
- reset on ``/new`` and after an idle window;
- one append-only audit record per session, flushed on session close.

Only the agent's final text reply is sent; intermediate tool activity is not.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from henk.agent.session import AgentSession, SessionFactory, SessionStats
from henk.agent.triage import (
    check_triage_arc,
    compose_event_turn_content,
    extract_diagnosis,
)
from henk.agent.turns import EventTurn, OwnerTurn, Turn

logger = logging.getLogger("henk.agent")

RESET_COMMAND = "/new"
RESET_CONFIRMATION = "Session reset."
DEFAULT_ERROR_REPLY = (
    "Sorry — I hit an error handling that and couldn't complete it. "
    "Try again in a moment."
)


class _Sender:
    async def send(self, text: str) -> None: ...


@dataclass
class _SessionAudit:
    """Accumulates one session's audit data; flushed as one record on close."""

    trigger: str
    events: list[dict] = field(default_factory=list)
    had_event_turn: bool = False
    triage_arc_complete: bool | None = None
    diagnosis: str | None = None
    confidence: str | None = None
    announceable: bool | None = None
    turn_count: int = 0
    outcome: str = "completed"


class AgentCore:
    def __init__(
        self,
        factory: SessionFactory,
        channel: _Sender,
        *,
        idle_timeout_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
        error_reply: str = DEFAULT_ERROR_REPLY,
        audit: Any | None = None,
        model: str | None = None,
    ) -> None:
        self._factory = factory
        self._channel = channel
        self._idle_timeout = idle_timeout_seconds
        self._clock = clock
        self._error_reply = error_reply
        self._audit = audit
        self._model = model
        self._session: AgentSession | None = None
        self._last_activity: float | None = None
        self._acc: _SessionAudit | None = None
        self._queue: "asyncio.Queue[Turn]" = asyncio.Queue()

    async def submit(self, text: str) -> None:
        """Enqueue an inbound owner message for serial processing."""
        await self._queue.put(OwnerTurn(text))

    async def submit_event(self, turn: EventTurn) -> None:
        """Enqueue a debounced, triageable event turn for serial processing."""
        await self._queue.put(turn)

    async def run(self) -> None:
        """Process the queue forever, one turn at a time, in arrival order."""
        while True:
            turn = await self._queue.get()
            try:
                await self.process(turn)
            except Exception:  # pragma: no cover - defensive; process handles its own
                logger.exception("unexpected error processing turn")
            finally:
                self._queue.task_done()

    async def process(self, turn: str | Turn) -> None:
        """Handle a single turn end to end (used directly by tests)."""
        if isinstance(turn, str):
            turn = OwnerTurn(turn)
        if isinstance(turn, EventTurn):
            await self._process_event(turn)
        else:
            await self._process_owner(turn.text)

    # --- Owner turns (reply path, no triage framing) ----------------------

    async def _process_owner(self, text: str) -> None:
        if text.strip() == RESET_COMMAND:
            await self._close_session()
            self._last_activity = None
            await self._channel.send(RESET_CONFIRMATION)
            return

        await self._ensure_session("owner-message")
        try:
            reply = await self._session.run_turn(text)  # type: ignore[union-attr]
        except Exception:
            logger.exception("agent turn failed")
            if self._acc is not None:
                self._acc.outcome = "error"
            await self._channel.send(self._error_reply)
            self._last_activity = self._clock()
            return
        if self._acc is not None:
            self._acc.turn_count += 1
        self._last_activity = self._clock()
        if reply:
            await self._channel.send(reply)

    # --- Event turns (proactive triage path) ------------------------------

    async def _process_event(self, turn: EventTurn) -> None:
        await self._ensure_session("event")
        content = compose_event_turn_content(turn)
        try:
            reply = await self._session.run_turn(content)  # type: ignore[union-attr]
        except Exception:
            logger.exception("triage turn failed")
            if self._acc is not None:
                self._acc.outcome = "error"
            self._last_activity = self._clock()
            return

        arc = check_triage_arc(reply)
        if self._acc is not None:
            self._acc.had_event_turn = True
            self._acc.turn_count += 1
            self._acc.triage_arc_complete = arc.complete
            self._acc.confidence = arc.confidence
            self._acc.diagnosis = extract_diagnosis(reply)
            self._acc.announceable = turn.announceable
            self._acc.events.extend(
                {
                    "identity_key": it.identity.key,
                    "source": it.identity.source,
                    "name": it.identity.name,
                    "state": it.identity.state.value,
                    "recurrence": it.recurrence,
                    "event_id": it.event.id,
                }
                for it in turn.items
            )
        self._last_activity = self._clock()

        # Proactive send only for announceable incidents; cap-overflow triage
        # still ran (and will still publish its handoff + audit record).
        if turn.announceable and reply:
            await self._channel.send(self._with_suppressed_note(reply, turn))

    @staticmethod
    def _with_suppressed_note(reply: str, turn: EventTurn) -> str:
        if turn.suppressed_count <= 0:
            return reply
        n = turn.suppressed_count
        return (
            f"{reply}\n\n(Note: {n} earlier incident"
            f"{'s' if n != 1 else ''} were suppressed to stay under the alert "
            "cap — retrieve them with henk-pickup.)"
        )

    # --- Session lifecycle + audit ----------------------------------------

    async def _ensure_session(self, trigger: str) -> None:
        now = self._clock()
        expired = (
            self._last_activity is not None
            and (now - self._last_activity) > self._idle_timeout
        )
        if self._session is None or expired:
            await self._close_session()
            self._session = self._factory.create()
            self._last_activity = now
            self._acc = _SessionAudit(trigger=trigger)

    async def _close_session(self) -> None:
        if self._session is None:
            return
        session = self._session
        acc = self._acc
        self._session = None
        self._acc = None
        self._flush_audit(session, acc)
        try:
            await session.close()
        except Exception:  # pragma: no cover - best effort
            logger.warning("error closing session", exc_info=True)

    async def aclose(self) -> None:
        """Flush and close the current session (shutdown / test boundary)."""
        await self._close_session()

    def _flush_audit(self, session: AgentSession, acc: _SessionAudit | None) -> None:
        if self._audit is None or acc is None:
            return
        from henk.audit import session_record

        stats = self._session_stats(session)
        handoff_id = None
        tool_calls = []
        for call in stats.tool_calls if stats else ():
            tool_calls.append(
                {"name": call.name, "tool_class": call.tool_class,
                 "result_id": call.result_id}
            )
            if call.name == "publish_handoff" and call.result_id:
                handoff_id = call.result_id
        record = session_record(
            trigger=acc.trigger,
            event=acc.events if acc.had_event_turn else None,
            tool_calls=tool_calls,
            diagnosis=acc.diagnosis,
            confidence=acc.confidence,
            handoff_message_id=handoff_id,
            triage_arc_complete=(
                acc.triage_arc_complete if acc.had_event_turn else None
            ),
            outcome=acc.outcome,
            announceable=acc.announceable,
            turn_count=acc.turn_count,
            model=(stats.model if stats and stats.model else self._model),
            usage=(
                {"input_tokens": stats.input_tokens,
                 "output_tokens": stats.output_tokens}
                if stats
                else None
            ),
        )
        self._audit.write(record)

    @staticmethod
    def _session_stats(session: AgentSession) -> SessionStats | None:
        getter = getattr(session, "stats", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:  # pragma: no cover - stats are best-effort
            return None
