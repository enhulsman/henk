"""Agent core: turns owner messages AND triageable events into SDK turns.

Responsibilities (v1.2):
- one serial per-owner queue carrying **typed turns** — owner turns (reply path)
  and event turns (proactive triage path) never run concurrently (design D5);
- one session per conversation, reused across follow-ups for context continuity;
- event turns arrive with delimited-untrusted-data + triage framing composed by
  the app layer; owner turns get neither (agent-core delta);
- event-turn output routes to the proactive owner-directed send, suppressed for
  non-announceable (cap-overflow) incidents;
- every agent turn is framed for the gate with its turn type, announceability and
  the session's taint (design D10), cleared on every exit path including errors:
  the gate can only enforce turn scope if the core tells it what turn is running;
- reset on ``/new`` and after an idle window;
- one append-only audit record per session, flushed on session close, carrying
  every mutating authorization decision made while it was live (design D5).

Only the agent's final text reply is sent; intermediate tool activity is not.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from henk.agent.session import AgentSession, SessionFactory, SessionStats
from henk.agent.triage import (
    check_triage_arc,
    compose_event_turn_content,
    extract_diagnosis,
)
from henk.agent.turns import CheckpointMarker, EventTurn, OwnerTurn, Turn
from henk.gate.approval import EXECUTING_OUTCOMES, TurnContext
from henk.tools.base import ToolClass, TurnType

logger = logging.getLogger("henk.agent")

#: Receipt outcomes under which the invocation was permitted to proceed. Read as
#: the string values the records carry, not the enum, since that is what an audit
#: reader sees.
_EXECUTED_OUTCOMES = frozenset(o.value for o in EXECUTING_OUTCOMES)

RESET_COMMAND = "/new"
RESET_CONFIRMATION = "Session reset."
DEFAULT_ERROR_REPLY = (
    "Sorry — I hit an error handling that and couldn't complete it. "
    "Try again in a moment."
)
#: One-shot operator alert when the durability latch engages (design D1). Plain
#: text, Signal-suited. Henk runs unattended, so a frozen checkpoint that only a
#: restart can clear must not be silent.
DEGRADED_DURABILITY_NOTICE = (
    "⚠️ Henk: an audit write failed, so the event checkpoint is frozen. Events "
    "will replay on the next restart (some may re-notify). A restart is advised "
    "to clear this."
)


class _Sender:
    async def send(self, text: str) -> None: ...


@dataclass
class _SessionAudit:
    """Accumulates one session's audit data.

    Owner sessions flush one record on close; an event-triage session flushes its
    record at triage completion (design D3) and sets ``flushed`` so ``close`` does
    not write a second, conflated record.
    """

    trigger: str
    events: list[dict] = field(default_factory=list)
    had_event_turn: bool = False
    triage_arc_complete: bool | None = None
    diagnosis: str | None = None
    confidence: str | None = None
    announceable: bool | None = None
    turn_count: int = 0
    outcome: str = "completed"
    flushed: bool = False
    #: Model-initiated authorization receipts recorded while THIS acc was live.
    #: Scoped by construction: a continuation acc starts empty, so a triage's
    #: approvals can never reappear in the interrogation's record.
    approvals: list[dict] = field(default_factory=list)
    #: Hash of the recall block this session received, as injected (null if none).
    memory_hash: str | None = None
    #: Cumulative session stats at this acc's start; when set, the acc's record
    #: reports only stats accrued SINCE it (delta), so an owner interrogation
    #: continuing an event session is audited without double-counting the triage.
    stats_baseline: "SessionStats | None" = None


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
        checkpoint: Any | None = None,
        handoff_sink: Callable[[str, str], None] | None = None,
        gate: Any | None = None,
        receipts: Any | None = None,
    ) -> None:
        self._factory = factory
        self._channel = channel
        self._idle_timeout = idle_timeout_seconds
        self._clock = clock
        self._error_reply = error_reply
        self._audit = audit
        self._model = model
        # Durable intake-offset store: advanced only after a triage record is
        # durable (design D1). None outside the event path (reactive owner-only).
        self._checkpoint = checkpoint
        # Called with (identity_key, handoff_id) after a triage that published a
        # handoff, so the pipeline's recurrence framing can reference it next time.
        self._handoff_sink = handoff_sink
        # Durability barrier (design D1): once a genuine audit write fails, the
        # checkpoint freezes for the process lifetime — the cursor must never
        # advance past a non-durable event, and opaque ntfy ids can't be compared
        # for a per-offset high-water-mark, so "a gap appeared" latches globally.
        self._checkpoint_blocked = False
        # The authorization gate, framed per turn with the turn's context (D10).
        # Optional: unit tests and a reactive-only deployment run without one, and
        # its absence must not change turn handling.
        self._gate = gate
        # Session taint (D10): set the moment a session processes an event turn,
        # never cleared while that session lives. Out-of-scope mutations are denied
        # in EVERY turn of a tainted session, including the owner follow-up that
        # incident-triage mandates continues the same session.
        self._session_tainted = False
        # Mutation receipts: durable at decision time in the audit log, and fanned
        # back here so the session record's approvals[] is never empty when a
        # mutating tool was invoked (the verified defect this change fixes).
        self._receipts = receipts
        if receipts is not None:
            receipts.sink = self._note_receipt
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

    async def submit_marker(self, marker: CheckpointMarker) -> None:
        """Enqueue a suppression-only batch's checkpoint advance (design D1).

        It rides the same serial queue so it advances the intake offset only
        after any triage ahead of it has flushed — FIFO durability ordering.
        """
        await self._queue.put(marker)

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
        if isinstance(turn, CheckpointMarker):
            # Suppression-only batch: its suppression records were written by the
            # coordinator and any prior triage has been processed. Advancing the
            # cursor here is gated by the durability latch inside
            # _advance_checkpoint, so a prior FAILED flush blocks this too (the
            # marker can never leapfrog a non-durable event).
            self._advance_checkpoint(turn.offset)
        elif isinstance(turn, EventTurn):
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
            with self._framed_turn(TurnType.OWNER):
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
        # D5: a new incident always starts its own isolated session, displacing
        # any open session (owner conversation or a prior incident) so no context
        # bleeds across incidents. The displaced session's record is already
        # durable (event triages flush per-triage; owner sessions flush on close).
        await self._start_event_session()
        # Record which incidents this turn is triaging up front, so even an
        # errored triage's record names them (the audit is the transferable
        # artifact — an error must not be an anonymous blank).
        if self._acc is not None:
            self._acc.had_event_turn = True
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
        content = compose_event_turn_content(turn)
        try:
            with self._framed_turn(TurnType.EVENT, announceable=turn.announceable):
                reply = await self._session.run_turn(content)  # type: ignore[union-attr]
        except Exception:
            logger.exception("triage turn failed")
            if self._acc is not None:
                self._acc.outcome = "error"
            self._last_activity = self._clock()
            # D1/D3: record the errored triage and advance the cursor anyway, so
            # a poison event is not reprocessed forever.
            await self._flush_event_triage(turn)
            return

        arc = check_triage_arc(reply)
        if self._acc is not None:
            self._acc.turn_count += 1
            self._acc.triage_arc_complete = arc.complete
            self._acc.confidence = arc.confidence
            self._acc.diagnosis = extract_diagnosis(reply)
        self._last_activity = self._clock()

        # D3: make this triage durable now (session stays open for owner
        # interrogation), then advance the checkpoint gated on that write.
        await self._flush_event_triage(turn)

        # Proactive send only for announceable incidents; cap-overflow triage
        # still ran (and its handoff + audit record are already durable).
        if turn.announceable and reply:
            await self._channel.send(self._with_suppressed_note(reply, turn))

    async def _flush_event_triage(self, turn: EventTurn) -> None:
        """Write the event triage's record, wire recurrence, advance the cursor.

        The checkpoint advance is gated on the audit write succeeding; a GENUINE
        write failure (audit configured but the write returned False) latches the
        durability barrier and sends a one-shot operator notice (design D1). A
        no-audit flush returns False too (M4) but is a designed no-op — it must
        not latch or notify, hence the ``self._audit is not None`` gate."""
        if self._acc is None or self._acc.flushed:
            return
        ok, handoff_id = self._write_audit_record(self._session, self._acc)
        self._acc.flushed = True
        if self._handoff_sink is not None and handoff_id:
            for item in turn.items:
                self._handoff_sink(item.identity.key, handoff_id)
        genuine_failure = self._audit is not None and not ok
        if genuine_failure and not self._checkpoint_blocked:
            # Latch BEFORE the await so a send failure can't leave it unset.
            self._checkpoint_blocked = True
            try:
                await self._channel.send(DEGRADED_DURABILITY_NOTICE)
            except Exception:  # pragma: no cover - best effort; must not crash triage
                logger.warning("failed to send degraded-durability notice", exc_info=True)
        if ok and turn.offset:
            self._advance_checkpoint(turn.offset)

    def _advance_checkpoint(self, offset: str | None) -> None:
        # Frozen once any genuine flush failed: the cursor must never advance past
        # a non-durable event (via this triage's offset OR a later marker/triage).
        if self._checkpoint_blocked or self._checkpoint is None or not offset:
            return
        self._checkpoint.write(offset)

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

    # --- Receipts (design D5) ---------------------------------------------

    def _note_receipt(self, record: dict) -> None:
        """Collect a model-initiated authorization decision for the live acc.

        Owner-command receipts are deliberately skipped: commands run outside any
        turn or session (design D8), so they exist only as standalone authorization
        records and must not be attributed to whatever session happened to be open.
        """
        if record.get("initiated_by") != "model" or self._acc is None:
            return
        from henk.audit import approval_entry

        self._acc.approvals.append(approval_entry(record))

    # --- Gate framing (design D10) ----------------------------------------

    @contextmanager
    def _framed_turn(self, turn_type: TurnType, *, announceable: bool = True):
        """Frame one agent turn for the gate, clearing it on every exit path.

        try/finally rather than best-effort cleanup: a gate context that outlived
        an errored event turn would carry ``announceable=False`` into the owner's
        next conversation and silently suppress a legitimate approval prompt.
        """
        gate = self._gate
        if gate is None:
            yield
            return
        gate.enter_turn(
            TurnContext(
                turn_type=turn_type,
                announceable=announceable,
                tainted=self._session_tainted,
            )
        )
        try:
            yield
        finally:
            gate.exit_turn()

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
            self._session_tainted = False  # a brand-new session; no incident in it
            self._acc = _SessionAudit(trigger=trigger)
        elif self._acc is not None and self._acc.flushed:
            # Reusing a session whose event-triage record already flushed (D3): the
            # owner is now interrogating that incident. Start a fresh continuation
            # acc, baselined at the current cumulative session stats, so the
            # interrogation is audited as its own record with delta stats — not
            # lost, and not conflated with (or double-counting) the triage record.
            self._acc = _SessionAudit(
                trigger=trigger,
                stats_baseline=self._session_stats(self._session),
            )

    async def _start_event_session(self) -> None:
        """Always open a fresh isolated session for a new incident (D5 displace)."""
        await self._close_session()
        self._session = self._factory.create()
        self._last_activity = self._clock()
        # The ONLY way an event turn enters a session, so taint cannot be missed.
        self._session_tainted = True
        self._acc = _SessionAudit(trigger="event")

    async def _close_session(self) -> None:
        if self._session is None:
            return
        session = self._session
        acc = self._acc
        self._session = None
        self._acc = None
        self._session_tainted = False
        # An event-triage record was already flushed at triage completion (D3);
        # only owner sessions (and any un-flushed acc) write their record here.
        if acc is not None and not acc.flushed:
            self._write_audit_record(session, acc)
        try:
            await session.close()
        except Exception:  # pragma: no cover - best effort
            logger.warning("error closing session", exc_info=True)

    async def aclose(self) -> None:
        """Flush and close the current session (shutdown / test boundary)."""
        await self._close_session()

    def _write_audit_record(
        self, session: AgentSession, acc: _SessionAudit | None
    ) -> tuple[bool, str | None]:
        """Build and append one audit record. Returns ``(write_ok, handoff_id)``.

        ``write_ok`` gates the checkpoint advance (design D1); ``handoff_id`` feeds
        the recurrence sink. With no audit configured this returns
        ``(False, handoff_id)`` — no durable record means the checkpoint must NOT
        advance (M4). The whole record body (including the handoff scan) runs off
        the EFFECTIVE stats: for a continuation acc that is the delta since its
        baseline, so an interrogation record never inherits the triage's tool
        calls, tokens, or handoff id."""
        stats = self._session_stats(session)
        if acc is not None and acc.stats_baseline is not None:
            stats = self._stats_since(stats, acc.stats_baseline)
        handoff_id = None
        tool_calls = []
        pending_outcomes = self._outcomes_by_tool(acc)
        for call in stats.tool_calls if stats else ():
            tool_calls.append(
                {"name": call.name, "tool_class": call.tool_class,
                 "result_id": call.result_id,
                 "executed": self._was_executed(call, pending_outcomes)}
            )
            if call.name == "publish_handoff" and call.result_id:
                handoff_id = call.result_id
        if acc is None:
            return True, handoff_id  # defensive; callers always pass a live acc
        if self._audit is None:
            return False, handoff_id  # no durable record ⇒ no advance (M4)
        from henk.audit import session_record

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
            approvals=acc.approvals,
            memory_hash=acc.memory_hash,
            outcome=acc.outcome,
            announceable=acc.announceable,
            turn_count=acc.turn_count,
            model=(stats.model if stats and stats.model else self._model),
            usage=(
                {"input_tokens": stats.input_tokens,
                 "output_tokens": stats.output_tokens,
                 "cache_read_input_tokens": stats.cache_read_input_tokens}
                if stats
                else None
            ),
        )
        ok = self._audit.write(record)
        return ok, handoff_id

    @staticmethod
    def _outcomes_by_tool(acc: "_SessionAudit | None") -> dict[str, list[str]]:
        """This acc's authorization outcomes, per tool, in decision order."""
        outcomes: dict[str, list[str]] = {}
        for entry in acc.approvals if acc is not None else ():
            outcomes.setdefault(entry["tool"], []).append(entry["outcome"])
        return outcomes

    @staticmethod
    def _was_executed(call, pending_outcomes: dict[str, list[str]]) -> bool:
        """Whether this invocation was permitted to proceed.

        Derived by correlating with the gate's receipts — never from tool-result
        text, which the model can influence. Read-only and notify-only calls are
        true by construction: they bypass the gate by classification. A mutating
        call with no receipt left is not evidence of execution (structurally
        impossible, so it is reported false AND logged rather than assumed benign).
        """
        if call.tool_class != ToolClass.MUTATING.value:
            return True
        outcomes = pending_outcomes.get(call.name)
        if not outcomes:
            logger.warning(
                "mutating tool call %s appeared without an authorization receipt; "
                "recording it as not executed",
                call.name,
            )
            return False
        return outcomes.pop(0) in _EXECUTED_OUTCOMES

    @staticmethod
    def _stats_since(
        current: SessionStats | None, baseline: SessionStats
    ) -> SessionStats | None:
        """Return stats accrued since ``baseline`` (for a continuation record).

        ``tool_calls`` grows by appending (accumulator invariant), so the delta is
        the suffix past the baseline length; token counts subtract with a strict
        None-guard (never ``(cur or 0) - ...``, which could go negative)."""
        if current is None:
            return None

        def sub(cur: int | None, base: int | None) -> int | None:
            return None if cur is None else cur - (base or 0)

        return SessionStats(
            tool_calls=current.tool_calls[len(baseline.tool_calls):],
            model=current.model,
            input_tokens=sub(current.input_tokens, baseline.input_tokens),
            output_tokens=sub(current.output_tokens, baseline.output_tokens),
            cache_read_input_tokens=sub(
                current.cache_read_input_tokens, baseline.cache_read_input_tokens
            ),
        )

    @staticmethod
    def _session_stats(session: AgentSession) -> SessionStats | None:
        getter = getattr(session, "stats", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:  # pragma: no cover - stats are best-effort
            return None
