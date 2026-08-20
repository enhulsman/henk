"""The clock that delivers: a polling scheduler for due reminders.

`reminders-core` shipped the store, the time model, the tools and the commands, all
inert behind ``reminders.enabled: false``, because a build that confirms reminders it
cannot deliver has spent the owner's trust on a promise it structurally cannot keep.
This module is the second half. It is also the first path where Henk speaks because a
clock said so, and the first real second sender on the channel — which is why outbound
sends acquired a lock in the same change.

The whole design in one paragraph: an async task ticks every ``poll_interval_seconds``.
Each tick captures the current instant **once**, opens one pre-work transaction (grace
transitions, then selection against the post-grace state, then attempt increments and
both pre-work bounds), closes it, then sends — each due reminder as its own message
oldest-due first, then at most one catch-up summary — and records each outcome in its
own post-send transaction. There is no startup catch-up pass, because catch-up is a
property of the selection query rather than a boot ritual.

Four rules here are load-bearing and none of them is obvious:

- **No transaction scope spans a suspension point.** ``Store.transaction()`` is
  reentrant by *instance depth on one shared connection*, not per task. This module is
  the second concurrent writer, so a scope held across a send could join whatever the
  other task does next and be rolled back with it. The pre-work scope closes before the
  first send; every post-send write opens its own. An AST guard in the suite fails on
  any violation anywhere in ``henk/``.
- **Every exit writes state the selector predicates on.** The selector is a query, so an
  exit held only in this process's memory reverts on restart.
- **The two bounds live on opposite sides of the send, for symmetric reasons.** A crash
  is what prevents the post-send write, so the crash-attempt bound must be evaluated
  pre-work. A channel outcome does not exist until the send returns, so the report
  horizon can only be evaluated post-send — and must be, or a row that arrived already
  stale would be retired before any summary ever named it.
- **Instants only.** Every comparison is on epoch seconds. This module holds no zone of
  its own and formats nothing itself: human-facing times go through the shared renderer,
  so the time the owner is told is the same string everywhere else in Henk.

Duplicate delivery is the accepted failure mode; silent loss is not.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Protocol, Sequence

from henk.channel.base import SendOutcome
from henk.config import RemindersConfig
from henk.reminders.timeparse import TimeResolver
from henk.store.reminders import (
    ABANDONED,
    DELIVERED,
    DELIVERED_LATE,
    MISSED,
    PENDING,
    Reminder,
    ReminderStore,
)

logger = logging.getLogger("henk.reminders.scheduler")

#: Prefix on every delivered reminder. Its only specified job is to be
#: distinguishable from a triage message, so the owner never has to work out whether
#: Henk is reporting an incident or keeping a promise they made.
REMINDER_MARKER = "⏰ Reminder"

#: Heading on the catch-up summary. Deliberately covers both of the things the summary
#: can name — reminders whose window passed, and reminders Henk tried to send and gave
#: up on — because one message carries both.
SUMMARY_HEADING = (
    "⏰ Catch-up: reminders that came due but were not delivered on time."
)

#: Suffix marking a row Henk actually tried to deliver and abandoned, as opposed to one
#: whose grace window simply expired. The distinction matters to the owner: one is "you
#: were away", the other is "I could not reach you".
ABANDONED_SUFFIX = "(I tried to send this one and gave up.)"


class _Sender(Protocol):
    async def send_proactive(
        self, text: str, *, failure_notice: str | None = None
    ) -> SendOutcome: ...


@dataclass(frozen=True)
class _Tick:
    """What one tick's pre-work transaction decided, read after it commits.

    ``receipts`` is carried out of the transaction rather than written inside it: the
    JSONL log is not in the SQLite transaction, and a record must never claim a
    transition the store did not commit. A crash between the two loses one receipt,
    which is the preferable direction.
    """

    deliveries: tuple[Reminder, ...] = ()
    report: tuple[Reminder, ...] = ()
    receipts: tuple[tuple[int, float, str], ...] = ()


class ReminderScheduler:
    """Polls for due reminders and delivers them. One instance per process."""

    def __init__(
        self,
        reminders: ReminderStore,
        channel: _Sender,
        *,
        config: RemindersConfig,
        resolver: TimeResolver,
        receipts=None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._reminders = reminders
        # The SAME adapter instance the agent core holds. The send lock is instance
        # state, so a second adapter over the same bridge would serialize nothing
        # while satisfying every serialization test.
        self._channel = channel
        self._config = config
        self._resolver = resolver
        self._receipts = receipts
        self._clock = clock
        self._sleep = sleep

    # --- the loop ---------------------------------------------------------

    async def run(self) -> None:
        """Tick forever. Only cancellation stops this.

        A failure inside one tick is logged and dropped: the store transaction has
        already rolled back, the next tick re-selects from committed state, and a
        scheduler that died on a transient store or channel error would turn one bad
        minute into indefinite silence.
        """
        logger.info(
            "reminder scheduler started; polling every %.0fs",
            self._config.poll_interval_seconds,
        )
        try:
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error(
                        "a reminder scheduler tick failed; the next tick will retry",
                        exc_info=True,
                    )
                await self._sleep(self._config.poll_interval_seconds)
        except asyncio.CancelledError:
            logger.info("reminder scheduler stopped")
            raise

    async def tick(self) -> None:
        """One poll: select, deliver, report. Reads the clock exactly once."""
        instant = self._clock()
        plan = self._pre_work(instant)
        # After the commit, never inside it.
        for reminder_id, due_at, transition in plan.receipts:
            self._receipt(reminder_id, due_at, transition)
        # Individual reminders first, then the stale news: the timely message should
        # not queue behind a catch-up summary.
        for reminder in plan.deliveries:
            await self._deliver(reminder, instant)
        if plan.report:
            await self._report(plan.report, instant)

    # --- the pre-work transaction ----------------------------------------

    def _pre_work(self, instant: float) -> _Tick:
        """Grace, then selection, then increments and both pre-work bounds.

        The order is stated in the requirement and matters: selection runs against the
        **post-grace** state, so a row whose window expired this tick is report work
        rather than delivery work, and the same tick's summary names it.

        Contains no suspension point, so it cannot join another task's transaction.
        """
        config = self._config
        repo = self._reminders
        deliveries: list[Reminder] = []
        report: list[Reminder] = []
        receipts: list[tuple[int, float, str]] = []
        gave_up_delivering: list[int] = []
        gave_up_reporting: list[int] = []

        with repo.transaction():
            # 1. Grace applies to EVERY past-grace pending row, selected or not. A
            #    grace pass bounded by the delivery cap would leave the eleventh-oldest
            #    row pending past its window indefinitely.
            for row in repo.select_past_grace(
                now=instant, grace=config.late_grace_seconds
            ):
                repo.mark_missed(row.id, now=instant)
                receipts.append((row.id, row.due_at, MISSED))

            # 2. Selection, against the post-grace state.
            due = repo.select_due(now=instant, limit=config.tick_delivery_limit)
            reportable = repo.select_reportable(now=instant)

            # 3. Increments for exactly the selected rows, then the crash bound. Rows
            #    beyond the delivery cap are untouched — not charged, not written — so
            #    pacing costs nothing in bookkeeping.
            for row in due:
                attempts = repo.charge_attempt(row.id)
                if attempts >= config.crash_attempt_limit:
                    # Evaluated HERE, beside the increment. A crash is what prevents
                    # the post-send write, so a bound evaluated there would never be
                    # evaluated on the path it exists to bound.
                    repo.mark_abandoned(row.id, now=instant)
                    receipts.append((row.id, row.due_at, ABANDONED))
                    gave_up_delivering.append(row.id)
                    # Joins this tick's summary without a second increment: charged
                    # rows are exactly the selected rows.
                    report.append(replace(row, status=ABANDONED))
                else:
                    deliveries.append(row)

            for row in reportable:
                attempts = repo.charge_attempt(row.id)
                if attempts >= config.crash_attempt_limit:
                    # The report path's crash give-up. Writes `reported_at` to
                    # terminate reporting, which is NOT an assertion that the owner
                    # was told — hence the error log, and hence no audit transition.
                    repo.mark_reported([row.id], now=instant)
                    gave_up_reporting.append(row.id)
                else:
                    report.append(row)

        for reminder_id in gave_up_delivering:
            logger.error(
                "gave up delivering reminder %s after %d counted attempts; it will "
                "be named in this tick's catch-up summary",
                reminder_id,
                config.crash_attempt_limit,
            )
        for reminder_id in gave_up_reporting:
            logger.error(
                "gave up reporting reminder %s after %d counted attempts, none of "
                "which survived to send a summary; reported_at was written to stop "
                "retrying and is not a claim the owner was told",
                reminder_id,
                self._config.crash_attempt_limit,
            )

        return _Tick(
            deliveries=tuple(deliveries),
            report=tuple(report),
            receipts=tuple(receipts),
        )

    # --- delivery ---------------------------------------------------------

    async def _deliver(self, reminder: Reminder, instant: float) -> None:
        """Send one due reminder and record its outcome."""
        # The pre-send re-read. Synchronous, and nothing suspends between it and the
        # dispatch below: the pre-work selection is stale by however long the earlier
        # sends in this tick held the send lock, which serialization makes tens of
        # seconds rather than microseconds.
        if self._reminders.status_of(reminder.id) != PENDING:
            logger.info(
                "reminder %s is no longer pending; not dispatching it", reminder.id
            )
            return

        late = instant > reminder.due_at + self._config.late_delivery_threshold_seconds
        outcome = await self._channel.send_proactive(
            self._compose_delivery(reminder, late=late),
            failure_notice=self._notice_for(reminder),
        )

        if outcome is SendOutcome.FAILED:
            with self._reminders.transaction():
                self._reminders.schedule_retry(
                    reminder.id,
                    next_attempt_at=instant + self._config.retry_floor_seconds,
                )
            logger.error(
                "reminder %s was not delivered; retrying after the %.0fs floor",
                reminder.id,
                self._config.retry_floor_seconds,
            )
            return

        # `partial` maps to delivered too, and the asymmetry is deliberate: a reminder
        # is one text whose delivered head IS the reminder, the notice already told the
        # owner it was cut, and a retry would re-send the whole thing as a duplicate.
        status = DELIVERED_LATE if late else DELIVERED
        with self._reminders.transaction():
            self._reminders.mark_delivered(reminder.id, now=instant, late=late)
        detail = None
        if outcome is SendOutcome.PARTIAL:
            detail = "partial"
            logger.error(
                "reminder %s was only partially delivered; recorded as %s and not "
                "re-sent",
                reminder.id,
                status,
            )
        self._receipt(reminder.id, reminder.due_at, status, detail=detail)

    def _notice_for(self, reminder: Reminder) -> str | None:
        """The caller-supplied failure notice, or None on a floor retry.

        A first-or-crash attempt is recognizable without new state: its
        ``next_attempt_at`` still equals ``due_at``, because nothing but a floor retry
        moves it. Floor retries pass no notice, so a persistently failing reminder
        produces at most ``crash_attempt_limit`` notices over its grace window rather
        than one every fifteen minutes — the notice is a separate short send, and it
        can succeed while the reminder's own content keeps failing.
        """
        if reminder.next_attempt_at > reminder.due_at:
            return None
        return (
            f"[⚠ the reminder due {self._resolver.render(reminder.due_at)} could not "
            "be fully delivered]"
        )

    def _compose_delivery(self, reminder: Reminder, *, late: bool) -> str:
        """The delivered message: marker, the original due time if late, then the text.

        The stored text is reproduced **unchanged**. It is the owner's own wording, and
        rephrasing what someone asked to be told is the one thing this path must never
        do.
        """
        if late:
            due = self._resolver.render(reminder.due_at)
            return f"{REMINDER_MARKER} (was due {due}): {reminder.text}"
        return f"{REMINDER_MARKER}: {reminder.text}"

    # --- the catch-up summary --------------------------------------------

    async def _report(self, rows: Sequence[Reminder], instant: float) -> None:
        """One summary naming exactly this tick's report set, and its outcome write."""
        ordered = sorted(rows, key=lambda row: (row.due_at, row.id))
        # No failure notice: the summary IS the last-resort report, and a banner
        # saying it was cut adds nothing a retry does not — while a second
        # owner-visible message per failed summary is a real cost.
        outcome = await self._channel.send_proactive(self._compose_summary(ordered))

        if outcome is SendOutcome.DELIVERED:
            with self._reminders.transaction():
                self._reminders.mark_reported(
                    [row.id for row in ordered], now=instant
                )
            logger.info(
                "catch-up summary delivered, naming %d reminder(s)", len(ordered)
            )
            return

        # Not delivered. `reported_at` stays NULL for every named row: the summary
        # carries one promise per row, a lost tail is other reminders entirely, and a
        # duplicated head beats a row that silently vanishes.
        #
        # The horizon is evaluated HERE, in the post-send write of an ATTEMPTED
        # summary, and only on a partial outcome. Post-send placement is what
        # guarantees every horizon give-up follows at least one naming; the
        # partial-only condition is what keeps a channel outage of any length from
        # forfeiting the report.
        edge = self._config.late_grace_seconds + self._config.report_horizon_seconds
        expired = (
            [row for row in ordered if instant > row.due_at + edge]
            if outcome is SendOutcome.PARTIAL
            else []
        )
        expired_ids = {row.id for row in expired}
        retrying = [row for row in ordered if row.id not in expired_ids]

        with self._reminders.transaction():
            if expired:
                self._reminders.mark_reported(list(expired_ids), now=instant)
            for row in retrying:
                self._reminders.schedule_retry(
                    row.id,
                    next_attempt_at=instant + self._config.retry_floor_seconds,
                )

        logger.error(
            "catch-up summary naming %d reminder(s) was not delivered (outcome=%s); "
            "%d will be retried after the %.0fs floor",
            len(ordered),
            getattr(outcome, "value", outcome),
            len(retrying),
            self._config.retry_floor_seconds,
        )
        if expired:
            logger.error(
                "gave up reporting %d reminder(s) past the report horizon: %s. Each "
                "was named in at least one attempted summary, whose head chunks were "
                "delivered; reported_at was written to stop re-delivering that head "
                "and is not a claim the owner read it",
                len(expired),
                sorted(expired_ids),
            )

    def _compose_summary(self, rows: Sequence[Reminder]) -> str:
        """One message naming every row, in selection order, with no item bound.

        No bound and no pagination, deliberately: a long summary splits into sequential
        chunks like any long message, and composition that could omit a selected row
        would leave it attempt-charged with no recording write — the row would be
        incremented again next tick and eventually retired without ever being named.

        Oldest-due first is not merely tidy. It places any horizon-eligible rows in the
        head chunks, which is exactly the part a partial send did deliver.
        """
        lines = [SUMMARY_HEADING, ""]
        for row in rows:
            due = self._resolver.render(row.due_at)
            suffix = f" {ABANDONED_SUFFIX}" if row.status == ABANDONED else ""
            lines.append(f"• {due} — {row.text}{suffix}")
        return "\n".join(lines)

    # --- receipts ---------------------------------------------------------

    def _receipt(
        self,
        reminder_id: int,
        due_at: float,
        transition: str,
        *,
        detail: str | None = None,
    ) -> None:
        """Append one audit record for a transition that actually happened.

        Never blocks and never raises: a receipt that could take the delivery path down
        would make the audit log a liability rather than evidence.
        """
        if self._receipts is None:
            return
        try:
            self._receipts.record(
                reminder_id=reminder_id,
                due_at=due_at,
                transition=transition,
                initiated_by="scheduler",
                detail=detail,
            )
        except Exception:  # pragma: no cover - receipts are best-effort
            logger.warning(
                "could not record the %s receipt for reminder %s",
                transition,
                reminder_id,
                exc_info=True,
            )


__all__ = [
    "ABANDONED_SUFFIX",
    "REMINDER_MARKER",
    "SUMMARY_HEADING",
    "ReminderScheduler",
]
