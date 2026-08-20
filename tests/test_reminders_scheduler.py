"""The delivery scheduler (reminder-delivery group 5), from the reminders delta.

Real `sqlite3` files and the real repository throughout. Two rules from the task list
shape the doubles, and both are load-bearing:

- **The three delivery-TIMING scenarios run against `SignalAdapter`'s real lock with a
  slow fake bridge, never `conftest.FakeChannel`.** Timing claims — "does not wait on a
  turn", "waits on an in-flight send but is never skipped", "delivered within a tick
  when nothing is in flight" — are claims about a lock. A cooperative double has no
  lock and never suspends mid-send, so it satisfies all three while proving nothing.
  `OutcomeChannel` below is for the many scenarios that are about *outcome mapping*
  rather than concurrency, and it is deliberately not used for any timing property.
- **Process death is simulated by dropping the connection and reopening the file**,
  so a claim about what survives a crash is a claim about what SQLite committed.

`now` is injected everywhere. The scheduler reads instants only — no wall clock, no
zone reads — and the suite's process-timezone guard covers `henk/reminders/`, so a
leak there fails the guard rather than these tests.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from henk.channel.base import SendOutcome
from henk.channel.signal import SignalAdapter, SignalBridgeError
from henk.config import RemindersConfig
from henk.reminders.scheduler import ReminderScheduler
from henk.reminders.timeparse import TimeResolver, render_instant
from henk.store import Store
from henk.store.reminders import (
    ABANDONED,
    DELIVERED,
    DELIVERED_LATE,
    MISSED,
    PENDING,
    ReminderStore,
)

TZ = "Europe/Amsterdam"
ZONE = ZoneInfo(TZ)

#: A fixed instant clear of any DST transition (2026-08-20, mid-morning CEST).
NOW = 1_787_000_000.0

GRACE = 86400.0
FLOOR = 900.0
HORIZON = 86400.0
THRESHOLD = 300.0
CRASH_LIMIT = 3
TICK_LIMIT = 10


class Clock:
    """A mutable injected clock. Advanced explicitly; never reads the host."""

    def __init__(self, at: float = NOW) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += seconds


class SteppingClock:
    """Advances on EVERY read, so a tick that reads twice disagrees with itself.

    This is the discriminating double for "each tick captures the current instant
    exactly once": under it, a scheduler that re-read the clock mid-tick would compare
    a row's due instant against one value and record its delivery against another.
    """

    def __init__(self, at: float = NOW, step: float = 3600.0) -> None:
        self.at = at
        self.step = step
        self.reads: list[float] = []

    def __call__(self) -> float:
        self.reads.append(self.at)
        value = self.at
        self.at += self.step
        return value


class OutcomeChannel:
    """Records proactive sends and returns scripted outcomes.

    NOT for any concurrency or timing property — it has no lock and never suspends.
    Its job is outcome mapping, composition and bookkeeping, where the send's timing
    is irrelevant and its outcome is the whole point.
    """

    def __init__(self, outcomes=None) -> None:
        #: (text, failure_notice) in send order.
        self.calls: list[tuple[str, str | None]] = []
        self._outcomes = list(outcomes or [])
        self.default = SendOutcome.DELIVERED
        #: Set to raise instead of returning, to prove a tick survives it.
        self.explode: Exception | None = None

    @property
    def sent(self) -> list[str]:
        return [text for text, _ in self.calls]

    async def send_proactive(self, text, *, failure_notice=None) -> SendOutcome:
        self.calls.append((text, failure_notice))
        if self.explode is not None:
            raise self.explode
        if self._outcomes:
            return self._outcomes.pop(0)
        return self.default

    async def send(self, text) -> SendOutcome:  # pragma: no cover - reply path unused
        return await self.send_proactive(text)


class SlowBridge:
    """A bridge whose send suspends, so the real adapter lock is observable."""

    def __init__(self, fail_marker: str | None = None) -> None:
        self.sends: list[str] = []
        self.fail_marker = fail_marker
        #: Held while a send is in flight, so a test can gate on it.
        self.in_flight = asyncio.Event()
        self.release = asyncio.Event()
        self.gate = False

    async def receive(self):  # pragma: no cover
        if False:
            yield {}

    async def send(self, recipient, text):
        if self.fail_marker is not None and self.fail_marker in text:
            await asyncio.sleep(0)
            raise SignalBridgeError("refused")
        if self.gate:
            self.in_flight.set()
            await self.release.wait()
        self.sends.append(text)
        await asyncio.sleep(0)


async def _nosleep(_):
    # Must actually YIELD, not merely return: an async function that awaits nothing
    # never hands control back, so `run()` would spin without ever letting the test
    # body advance. This is the difference between "instant" and "hangs".
    await asyncio.sleep(0)


class Receipts:
    """Collects reminder receipts as (id, transition, initiated_by, detail)."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, *, reminder_id, due_at, transition, initiated_by="model",
               detail=None):
        entry = {
            "reminder_id": reminder_id,
            "due_at": due_at,
            "transition": transition,
            "initiated_by": initiated_by,
            "detail": detail,
        }
        self.records.append(entry)
        return entry

    def transitions(self, transition: str) -> list[int]:
        return [r["reminder_id"] for r in self.records if r["transition"] == transition]


def _config(**overrides) -> RemindersConfig:
    base = dict(
        enabled=True,
        late_grace_seconds=GRACE,
        retry_floor_seconds=FLOOR,
        report_horizon_seconds=HORIZON,
        late_delivery_threshold_seconds=THRESHOLD,
        crash_attempt_limit=CRASH_LIMIT,
        tick_delivery_limit=TICK_LIMIT,
        poll_interval_seconds=30,
    )
    base.update(overrides)
    return RemindersConfig(**base)


def _build(tmp_path: Path, *, channel=None, clock=None, receipts=None, **overrides):
    """A scheduler over a real store. Returns (store, repo, scheduler, channel)."""
    clock = clock or Clock()
    channel = channel if channel is not None else OutcomeChannel()
    store = Store(tmp_path / "store" / "henk.db", clock=clock)
    repo = ReminderStore(store)
    scheduler = ReminderScheduler(
        repo,
        channel,
        config=_config(**overrides),
        resolver=TimeResolver(ZONE, clock=clock),
        receipts=receipts,
        clock=clock,
        sleep=_nosleep,
    )
    return store, repo, scheduler, channel


def _seed(repo: ReminderStore, *, due_at: float, text="call the plumber"):
    return repo.schedule(text, due_at=due_at, due_tz=TZ, input_spec="+1h")


def _reopen(tmp_path: Path):
    store = Store(tmp_path / "store" / "henk.db")
    return store, ReminderStore(store)


# --- 5.1 Delivery ---------------------------------------------------------


async def test_a_due_reminder_is_delivered_within_a_tick_verbatim_and_marked(
    tmp_path: Path,
):
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - 60, text="take the bins out")
    await scheduler.tick()
    assert len(channel.sent) == 1
    assert "take the bins out" in channel.sent[0]
    after = repo.get(row.id)
    assert after.status == DELIVERED
    assert after.delivered_at == NOW
    assert after.send_attempts == 0
    store.close()


async def test_the_delivered_message_carries_a_marker_distinguishing_it_from_triage(
    tmp_path: Path,
):
    store, repo, scheduler, channel = _build(tmp_path)
    _seed(repo, due_at=NOW - 60)
    await scheduler.tick()
    # The requirement asks only for distinguishability, not for specific wording.
    assert channel.sent[0] != "call the plumber"
    assert "reminder" in channel.sent[0].lower()
    store.close()


async def test_the_stored_text_is_never_rewritten(tmp_path: Path):
    awkward = "don't say 'ok' — ask Marieke about the RENT, 50% split?!"
    store, repo, scheduler, channel = _build(tmp_path)
    _seed(repo, due_at=NOW - 60, text=awkward)
    await scheduler.tick()
    assert awkward in channel.sent[0]
    store.close()


async def test_an_on_time_delivery_does_not_state_a_due_time(tmp_path: Path):
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - THRESHOLD + 1)  # inside the threshold
    await scheduler.tick()
    assert render_instant(row.due_at, ZONE) not in channel.sent[0]
    assert repo.get(row.id).status == DELIVERED
    store.close()


async def test_a_late_delivery_states_its_original_due_time_and_records_late(
    tmp_path: Path,
):
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - THRESHOLD - 1)  # just past the threshold
    await scheduler.tick()
    assert render_instant(row.due_at, ZONE) in channel.sent[0]
    assert repo.get(row.id).status == DELIVERED_LATE
    store.close()


async def test_the_lateness_boundary_is_the_configured_threshold(tmp_path: Path):
    # Exactly at the threshold is NOT late ("more than the threshold after").
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - THRESHOLD)
    await scheduler.tick()
    assert repo.get(row.id).status == DELIVERED
    store.close()


async def test_a_cancelled_reminder_is_never_delivered(tmp_path: Path):
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    repo.cancel(row.id)
    await scheduler.tick()
    assert channel.sent == []
    store.close()


async def test_a_future_reminder_is_never_delivered_however_many_ticks_run(
    tmp_path: Path,
):
    clock = Clock()
    store, repo, scheduler, channel = _build(tmp_path, clock=clock)
    row = _seed(repo, due_at=NOW + 7 * 86400)
    # Force the schema default, which means "eligible now" — the exact shape the
    # selector's due_at conjunct exists to make harmless.
    with store.transaction() as conn:
        conn.execute("UPDATE reminders SET next_attempt_at = 0 WHERE id = ?", (row.id,))
    for _ in range(50):
        await scheduler.tick()
        clock.advance(60)
    assert channel.sent == []
    after = repo.get(row.id)
    assert after.status == PENDING
    assert after.send_attempts == 0
    store.close()


async def test_reminders_are_delivered_oldest_due_first_one_message_each(
    tmp_path: Path,
):
    store, repo, scheduler, channel = _build(tmp_path)
    # Inserted newest-due first so insertion order and due order disagree.
    for offset in (60, 300, 900, 1800):
        _seed(repo, due_at=NOW - offset, text=f"due {offset}s ago")
    await scheduler.tick()
    assert len(channel.sent) == 4  # one message per reminder, never batched
    order = [text for text in channel.sent]
    assert "due 1800s ago" in order[0]
    assert "due 900s ago" in order[1]
    assert "due 300s ago" in order[2]
    assert "due 60s ago" in order[3]
    store.close()


async def test_individual_deliveries_are_sent_before_the_summary(tmp_path: Path):
    """The timely message before the stale news."""
    store, repo, scheduler, channel = _build(tmp_path)
    _seed(repo, due_at=NOW - 60, text="deliver me now")
    stale = _seed(repo, due_at=NOW - GRACE - 100, text="too late for me")
    await scheduler.tick()
    assert len(channel.sent) == 2
    assert "deliver me now" in channel.sent[0]
    assert "too late for me" in channel.sent[1]
    assert repo.get(stale.id).status == MISSED
    store.close()


async def test_a_failed_send_leaves_the_row_pending_on_the_floor(tmp_path: Path):
    clock = Clock()
    channel = OutcomeChannel()
    channel.default = SendOutcome.FAILED
    store, repo, scheduler, _ = _build(tmp_path, channel=channel, clock=clock)
    row = _seed(repo, due_at=NOW - 60)
    await scheduler.tick()
    after = repo.get(row.id)
    assert after.status == PENDING
    assert after.next_attempt_at == NOW + FLOOR
    assert after.send_attempts == 0
    # No attempt before the floor elapses, however many ticks run.
    sends = len(channel.sent)
    for _ in range(10):
        clock.advance(60)
        await scheduler.tick()
    assert len(channel.sent) == sends
    # And it is attempted again on the first eligible tick after the floor.
    clock.at = NOW + FLOOR
    await scheduler.tick()
    assert len(channel.sent) == sends + 1
    store.close()


async def test_a_transient_failure_delivers_on_the_first_tick_after_the_floor(
    tmp_path: Path,
):
    clock = Clock()
    channel = OutcomeChannel([SendOutcome.FAILED])
    store, repo, scheduler, _ = _build(tmp_path, channel=channel, clock=clock)
    row = _seed(repo, due_at=NOW - 60)
    await scheduler.tick()
    assert repo.get(row.id).status == PENDING
    clock.at = NOW + FLOOR
    await scheduler.tick()
    # Past the lateness threshold by then, so `delivered-late` is the honest status.
    assert repo.get(row.id).status == DELIVERED_LATE
    store.close()


async def test_a_partial_reminder_is_recorded_delivered_and_never_re_sent(
    tmp_path: Path, caplog
):
    clock = Clock()
    channel = OutcomeChannel()
    channel.default = SendOutcome.PARTIAL
    store, repo, scheduler, _ = _build(tmp_path, channel=channel, clock=clock)
    row = _seed(repo, due_at=NOW - 60)
    with caplog.at_level(logging.ERROR, logger="henk.reminders.scheduler"):
        await scheduler.tick()
    after = repo.get(row.id)
    assert after.status == DELIVERED
    assert after.delivered_at == NOW
    assert any("partial" in r.message.lower() for r in caplog.records)
    # A reminder is one text: the head that landed IS the reminder, and a retry
    # would re-send the whole thing as a duplicate.
    sends = len(channel.sent)
    for _ in range(5):
        clock.advance(FLOOR)
        await scheduler.tick()
    assert len(channel.sent) == sends
    store.close()


# --- 5.1 The failure notice ----------------------------------------------


async def test_the_failure_notice_names_the_due_time_and_says_not_fully_delivered(
    tmp_path: Path,
):
    channel = OutcomeChannel()
    channel.default = SendOutcome.FAILED
    store, repo, scheduler, _ = _build(tmp_path, channel=channel)
    row = _seed(repo, due_at=NOW - 60)
    await scheduler.tick()
    notice = channel.calls[0][1]
    assert notice is not None
    # Truthful for BOTH shapes: a single-chunk reminder fails whole, so "part of
    # this reminder" would claim a delivery that never happened.
    assert "could not be fully delivered" in notice
    assert render_instant(row.due_at, ZONE) in notice
    store.close()


async def test_a_persistent_failure_carries_no_notice_on_floor_retries(
    tmp_path: Path,
):
    """At most `crash_attempt_limit` notices per grace window, not one per retry.

    A notice is a separate short send that can succeed while the reminder's content
    fails, so an every-retry notice is an every-15-minutes notice.
    """
    clock = Clock()
    channel = OutcomeChannel()
    channel.default = SendOutcome.FAILED
    store, repo, scheduler, _ = _build(tmp_path, channel=channel, clock=clock)
    _seed(repo, due_at=NOW - 60)
    for _ in range(40):
        await scheduler.tick()
        clock.advance(FLOOR)
    notices = [n for _, n in channel.calls if n is not None]
    assert len(channel.calls) > 10, "the retries should have happened"
    assert len(notices) == 1, notices
    store.close()


async def test_a_crash_retried_attempt_still_carries_the_notice(tmp_path: Path):
    """A first-or-crash attempt is recognizable without new state.

    Its `next_attempt_at` still equals `due_at`; a floor retry's is later. A crash
    leaves `next_attempt_at` untouched, so a redelivery after death carries the
    notice — which is right, because the owner may never have seen the first one.
    """
    channel = OutcomeChannel()
    channel.default = SendOutcome.FAILED
    store, repo, scheduler, _ = _build(tmp_path, channel=channel)
    row = _seed(repo, due_at=NOW - 60)
    # Simulate a crash-charged row: the counter advanced, the floor did not move.
    repo.charge_attempt(row.id)
    await scheduler.tick()
    assert channel.calls[0][1] is not None
    store.close()


# --- 5.1 Cancellation races (real lock, slow bridge) ---------------------


def _adapter(bridge, **kwargs):
    # Roomy by default so a composed reminder is ONE chunk and assertions can look
    # for its text in a single send. Tests that are about multi-chunk behaviour pass
    # a small safe_length explicitly.
    kwargs.setdefault("safe_length", 400)
    kwargs.setdefault("max_send_attempts", 1)
    return SignalAdapter(
        bridge, account="+31611111111", owner="+31600000000",
        sleep=_nosleep, **kwargs
    )


async def test_a_cancellation_between_selection_and_dispatch_is_skipped(
    tmp_path: Path,
):
    """Driven with the real lock held by a slow bridge, per the task list.

    The pre-work selection is stale by however long the earlier sends in the tick
    held the lock — which serialization makes tens of seconds, not microseconds. So
    this race is real, and the pre-send status re-read is what closes it.
    """
    bridge = SlowBridge()
    bridge.gate = True
    adapter = _adapter(bridge)
    store, repo, scheduler, _ = _build(tmp_path, channel=adapter)
    first = _seed(repo, due_at=NOW - 600, text="first, holds the lock")
    second = _seed(repo, due_at=NOW - 60, text="second, gets cancelled")

    task = asyncio.create_task(scheduler.tick())
    await bridge.in_flight.wait()          # the first send is in flight
    repo.cancel(second.id)                 # commits between selection and dispatch
    bridge.release.set()
    await task

    assert any("first, holds the lock" in t for t in bridge.sends)
    assert not any("second, gets cancelled" in t for t in bridge.sends)
    assert repo.get(second.id).status == "cancelled"
    assert repo.get(first.id).status == DELIVERED_LATE
    store.close()


async def test_a_cancellation_after_dispatch_records_delivered(tmp_path: Path):
    """The residual window, recorded honestly rather than closed.

    The message factually reached the owner, so recording `cancelled` would be false.
    The audit trail carries both transitions, which is what makes it reconstructible.
    """
    bridge = SlowBridge()
    bridge.gate = True
    adapter = _adapter(bridge)
    receipts = Receipts()
    store, repo, scheduler, _ = _build(
        tmp_path, channel=adapter, receipts=receipts
    )
    row = _seed(repo, due_at=NOW - 600, text="in flight when cancelled")

    task = asyncio.create_task(scheduler.tick())
    await bridge.in_flight.wait()   # dispatched; the send is mid-flight
    cancelled = repo.cancel(row.id)
    assert cancelled is not None    # the cancellation really did commit
    bridge.release.set()
    await task

    assert repo.get(row.id).status == DELIVERED_LATE
    assert receipts.transitions(DELIVERED_LATE) == [row.id]
    store.close()


# --- 5.1 The three delivery-timing scenarios (real lock, slow bridge) ----


async def test_delivery_does_not_wait_on_a_turn(tmp_path: Path):
    """No session, no model turn, no queue: a turn cannot delay a delivery.

    The scheduler never touches the core's queue, so this is asserted structurally as
    well as behaviourally — a long-running "turn" coroutine runs concurrently and the
    delivery completes without it finishing.
    """
    bridge = SlowBridge()
    adapter = _adapter(bridge)
    store, repo, scheduler, _ = _build(tmp_path, channel=adapter)
    _seed(repo, due_at=NOW - 60, text="delivered during a turn")

    turn_finished = asyncio.Event()

    async def long_turn():
        for _ in range(50):
            await asyncio.sleep(0)
        turn_finished.set()

    turn = asyncio.create_task(long_turn())
    await scheduler.tick()
    assert not turn_finished.is_set(), "the delivery waited for the turn"
    assert any("delivered during a turn" in t for t in bridge.sends)
    await turn
    store.close()


async def test_delivery_waits_on_an_in_flight_send_but_is_never_skipped(
    tmp_path: Path,
):
    """The consequence design D6 carried rather than left implicit.

    Once sends serialize, a reminder due mid-reply waits — and the requirement is that
    it waits and then DELIVERS, with its chunks contiguous, never that it skips.
    """
    bridge = SlowBridge()
    adapter = _adapter(bridge, safe_length=30)  # multi-chunk is the point here
    store, repo, scheduler, _ = _build(tmp_path, channel=adapter)
    _seed(repo, due_at=NOW - 60, text="R" * 70)  # several chunks at safe_length 30

    reply = "\n\n".join("Q" * 25 for _ in range(3))
    both = await asyncio.gather(
        adapter.send(reply), scheduler.tick()
    )
    assert both[0] is SendOutcome.DELIVERED
    # The reply's chunks occupy a contiguous run, so the reminder did not cut into it.
    reply_at = [i for i, t in enumerate(bridge.sends) if t.strip().startswith("Q")]
    assert reply_at == list(range(reply_at[0], reply_at[0] + len(reply_at))), (
        f"the reply's chunks were split: {bridge.sends}"
    )
    assert "".join(bridge.sends[i] for i in reply_at) == reply
    # And the reminder was delivered — waited on, never skipped.
    rest = "".join(t for i, t in enumerate(bridge.sends) if i not in set(reply_at))
    assert "R" * 70 in rest
    store.close()


async def test_a_due_reminder_is_delivered_within_a_tick_when_nothing_is_in_flight(
    tmp_path: Path,
):
    """Explicitly conditioned on an idle send path, which is why it is separate."""
    bridge = SlowBridge()
    adapter = _adapter(bridge)
    store, repo, scheduler, _ = _build(tmp_path, channel=adapter)
    row = _seed(repo, due_at=NOW - 60, text="nothing in flight")
    await scheduler.tick()
    assert any("nothing in flight" in t for t in bridge.sends)
    assert repo.get(row.id).status == DELIVERED
    store.close()


# --- 5.2 Counting and crash bounds ---------------------------------------


async def test_the_increment_is_visible_after_a_death_mid_send(tmp_path: Path):
    """The counter exists for exactly this: an attempt the process did not survive.

    Death is simulated by making the post-send write itself fail — which is the state
    a crash leaves behind, and unlike closing the connection it cannot be papered over
    by the store lazily reopening and carrying on.
    """
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - 60)

    def died(*args, **kwargs):
        raise RuntimeError("the process died before the post-send write")

    repo.mark_delivered = died
    with pytest.raises(RuntimeError):
        await scheduler.tick()
    assert len(channel.sent) == 1, "the send itself did happen"
    store.close()

    reopened, repo2 = _reopen(tmp_path)
    after = repo2.get(row.id)
    assert after.send_attempts == 1  # the pre-work commit is durable
    assert after.status == PENDING
    assert after.delivered_at is None
    reopened.close()


async def test_a_crash_loop_exits_to_abandoned_at_the_limit(tmp_path: Path):
    """And the exit is evaluated in the PRE-WORK transaction.

    That placement is the whole point: a crash is what prevents the post-send write,
    so a maximum evaluated there would never be evaluated on the path it bounds.
    Simulated by charging attempts without ever letting a post-send write run.
    """
    receipts = Receipts()
    store, repo, scheduler, channel = _build(tmp_path, receipts=receipts)
    row = _seed(repo, due_at=NOW - 60)
    # Two attempts already died before any post-send write.
    repo.charge_attempt(row.id)
    repo.charge_attempt(row.id)
    await scheduler.tick()
    after = repo.get(row.id)
    assert after.status == ABANDONED
    assert after.send_attempts == 0
    assert receipts.transitions(ABANDONED) == [row.id]
    # The exit leaves `reported_at` NULL so the SAME tick's summary can name it — and
    # that summary then marks it, which is why the end-of-tick value is set rather
    # than null. The null-at-the-exit half is asserted at the repository level, in
    # test_reminders_delivery_store.py, where the summary is not in the way.
    assert after.reported_at == NOW
    store.close()


async def test_an_abandoned_reminder_is_named_in_the_same_ticks_summary(
    tmp_path: Path,
):
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - 60, text="gave up on this one")
    repo.charge_attempt(row.id)
    repo.charge_attempt(row.id)
    await scheduler.tick()
    # No individual delivery — it abandoned in pre-work — and one summary naming it.
    assert len(channel.sent) == 1
    assert "gave up on this one" in channel.sent[0]
    assert repo.get(row.id).reported_at == NOW
    store.close()


async def test_an_abandoned_reminder_is_never_attempted_again(tmp_path: Path):
    clock = Clock()
    store, repo, scheduler, channel = _build(tmp_path, clock=clock)
    row = _seed(repo, due_at=NOW - 60)
    repo.charge_attempt(row.id)
    repo.charge_attempt(row.id)
    await scheduler.tick()
    assert repo.get(row.id).status == ABANDONED
    before = len(channel.sent)
    for _ in range(20):
        clock.advance(FLOOR)
        await scheduler.tick()
    assert len(channel.sent) == before
    store.close()


async def test_the_abandoned_exit_charges_no_second_attempt(tmp_path: Path):
    """Charged rows are exactly the selected rows.

    A row abandoning in pre-work joins the summary composition without a second
    increment, which is what keeps the charged set equal to the written set.
    """
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    repo.charge_attempt(row.id)
    repo.charge_attempt(row.id)
    await scheduler.tick()
    # It abandoned (counter cleared) and was then reported (counter cleared again).
    assert repo.get(row.id).send_attempts == 0
    store.close()


async def test_a_send_then_death_redelivers_within_the_bound_never_silence(
    tmp_path: Path,
):
    """Duplicate delivery is the accepted failure mode; silent loss is not.

    Every tick's send lands and every tick's post-send write then dies, so the counter
    accumulates across simulated restarts until the pre-work bound retires the row —
    which is the whole reason the bound is evaluated pre-work.
    """
    from henk.reminders.scheduler import SUMMARY_HEADING

    clock = Clock()
    store, repo, scheduler, channel = _build(tmp_path, clock=clock)
    row = _seed(repo, due_at=NOW - 60)

    def died(*args, **kwargs):
        raise RuntimeError("died after the send, before the mark")

    repo.mark_delivered = died
    for _ in range(CRASH_LIMIT + 2):
        try:
            await scheduler.tick()
        except RuntimeError:
            pass
        clock.advance(30)

    deliveries = [t for t in channel.sent if SUMMARY_HEADING not in t]
    assert len(deliveries) >= 1, "never silent"
    assert len(deliveries) <= CRASH_LIMIT, "duplicates bounded by the crash limit"
    after = repo.get(row.id)
    assert after.status == ABANDONED, "it must terminate, not retry forever"
    # And the owner is told it was given up on, rather than it vanishing.
    assert any(SUMMARY_HEADING in t and "call the plumber" in t for t in channel.sent)
    store.close()


# --- 5.3 Grace, summary, pacing ------------------------------------------


async def test_downtime_within_grace_delivers_late(tmp_path: Path):
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - GRACE + 60)  # inside the window
    await scheduler.tick()
    assert repo.get(row.id).status == DELIVERED_LATE
    assert render_instant(row.due_at, ZONE) in channel.sent[0]
    store.close()


async def test_beyond_grace_is_missed_and_summarised_not_replayed(tmp_path: Path):
    receipts = Receipts()
    store, repo, scheduler, channel = _build(tmp_path, receipts=receipts)
    row = _seed(repo, due_at=NOW - GRACE - 60, text="a day-old instruction")
    await scheduler.tick()
    after = repo.get(row.id)
    assert after.status == MISSED
    assert receipts.transitions(MISSED) == [row.id]
    # Exactly one message, and it is the summary — not the reminder delivered as if
    # current.
    assert len(channel.sent) == 1
    assert "a day-old instruction" in channel.sent[0]
    assert after.reported_at == NOW
    store.close()


async def test_the_grace_boundary_is_the_configured_window(tmp_path: Path):
    store, repo, scheduler, channel = _build(tmp_path)
    edge = _seed(repo, due_at=NOW - GRACE)  # exactly at the window: still in grace
    await scheduler.tick()
    assert repo.get(edge.id).status == DELIVERED_LATE
    store.close()


async def test_nothing_overdue_means_no_message_of_any_kind(tmp_path: Path):
    clock = Clock()
    store, repo, scheduler, channel = _build(tmp_path, clock=clock)
    _seed(repo, due_at=NOW + 86400)  # scheduled, not yet due
    for _ in range(100):
        await scheduler.tick()
        clock.advance(30)
    assert channel.calls == []
    store.close()


async def test_an_empty_store_sends_nothing_over_a_long_run(tmp_path: Path):
    clock = Clock()
    store, repo, scheduler, channel = _build(tmp_path, clock=clock)
    for _ in range(200):
        await scheduler.tick()
        clock.advance(3600)
    assert channel.calls == []
    store.close()


async def test_a_within_grace_backlog_is_paced_oldest_first_and_none_dropped(
    tmp_path: Path,
):
    clock = Clock()
    store, repo, scheduler, channel = _build(tmp_path, clock=clock)
    # An hour overdue, well inside the grace window — deliberately NOT close to the
    # grace boundary: this test advances the clock over 20 ticks, and rows seeded just
    # inside the window would cross it mid-drain and be summarised instead of
    # delivered, which would be a different scenario wearing this one's name.
    ids_by_due = {}
    for i in range(100):
        due = NOW - 3600 - (100 - i)  # row 0 is the oldest due
        ids_by_due[due] = _seed(repo, due_at=due, text=f"backlog {i}").id

    per_tick = []
    for _ in range(20):
        before = len(channel.sent)
        await scheduler.tick()
        per_tick.append(len(channel.sent) - before)
        clock.advance(30)

    assert max(per_tick) <= TICK_LIMIT, per_tick
    # Not one hundred-message blast: it drains over ten ticks.
    assert sum(per_tick) == 100
    assert len([n for n in per_tick if n > 0]) >= 10
    # Oldest-due first across the whole drain.
    delivered_order = [
        int(t.split("backlog ")[1].split()[0].rstrip(".,)")) for t in channel.sent
    ]
    assert delivered_order == sorted(delivered_order)
    # None dropped, and every row ended delivered.
    assert all(
        repo.get(rid).status == DELIVERED_LATE for rid in ids_by_due.values()
    )
    store.close()


async def test_rows_beyond_the_tick_limit_are_not_attempt_charged_while_waiting(
    tmp_path: Path,
):
    store, repo, scheduler, channel = _build(tmp_path)
    ids = [
        _seed(repo, due_at=NOW - GRACE + 60 + i, text=f"row {i}").id for i in range(30)
    ]
    await scheduler.tick()
    # The 20 unselected rows are untouched: not charged, not written, still eligible.
    untouched = [rid for rid in ids if repo.get(rid).status == PENDING]
    assert len(untouched) == 20
    for rid in untouched:
        row = repo.get(rid)
        assert row.send_attempts == 0
        assert row.delivered_at is None
        assert row.next_attempt_at == row.due_at
    store.close()


async def test_the_summary_names_every_unreported_row_with_no_item_bound(
    tmp_path: Path,
):
    """Driven with the largest backlog that can exist: composition may never omit one.

    Cut #1 removed the report item bound and its pagination, which is what dissolves
    the stranded-rows defect — a row omitted from composition would get no post-send
    write, so its counter would never clear and it would be charged again next tick.

    One hundred rows is not an arbitrary number: it is the pending cap, so it is the
    true worst case rather than a large-looking sample. (An earlier draft of this test
    seeded 120 and was refused by the cap, which is the store telling the truth.)
    """
    store, repo, scheduler, channel = _build(tmp_path)
    texts = [f"missed thing number {i}" for i in range(100)]
    for i, text in enumerate(texts):
        _seed(repo, due_at=NOW - GRACE - 1000 - i, text=text)
    await scheduler.tick()
    assert len(channel.sent) == 1, "one summary, however many rows"
    summary = channel.sent[0]
    missing = [t for t in texts if t not in summary]
    assert missing == [], missing
    store.close()


async def test_the_summary_names_rows_oldest_due_first(tmp_path: Path):
    store, repo, scheduler, channel = _build(tmp_path)
    for i in range(6):
        _seed(repo, due_at=NOW - GRACE - 100 - i * 50, text=f"row {i}")
    await scheduler.tick()
    summary = channel.sent[0]
    positions = [summary.index(f"row {i}") for i in range(6)]
    # row 5 is the oldest due (largest offset), so the order is reversed.
    assert positions == sorted(positions, reverse=True), positions
    store.close()


async def test_the_summary_identifies_an_abandoned_row_as_a_failed_delivery(
    tmp_path: Path,
):
    store, repo, scheduler, channel = _build(tmp_path)
    missed = _seed(repo, due_at=NOW - GRACE - 100, text="simply missed")
    abandoned = _seed(repo, due_at=NOW - 60, text="could not deliver")
    repo.charge_attempt(abandoned.id)
    repo.charge_attempt(abandoned.id)
    await scheduler.tick()
    summary = channel.sent[0]
    assert "simply missed" in summary
    assert "could not deliver" in summary
    # The abandoned row is marked as one Henk gave up on, distinguishably.
    tail = summary[summary.index("could not deliver"):]
    head = summary[summary.index("simply missed"):summary.index("could not deliver")]
    assert len(tail) > len("could not deliver"), "abandoned rows need their own marking"
    assert tail != head
    store.close()


async def test_reported_at_is_written_only_when_the_summary_is_delivered(
    tmp_path: Path,
):
    for outcome, expect_marked in (
        (SendOutcome.DELIVERED, True),
        (SendOutcome.PARTIAL, False),
        (SendOutcome.FAILED, False),
    ):
        path = tmp_path / outcome.value
        channel = OutcomeChannel()
        channel.default = outcome
        store, repo, scheduler, _ = _build(path, channel=channel)
        row = _seed(repo, due_at=NOW - GRACE - 100)
        await scheduler.tick()
        after = repo.get(row.id)
        assert (after.reported_at is not None) is expect_marked, outcome
        if not expect_marked:
            # And the floor schedules a retry rather than losing the row.
            assert after.next_attempt_at == NOW + FLOOR
            assert after.send_attempts == 0
        store.close()


async def test_the_summary_carries_no_failure_notice_on_any_outcome(tmp_path: Path):
    """It is itself the last-resort report; its failure must not spawn a second message."""
    for outcome in (SendOutcome.DELIVERED, SendOutcome.PARTIAL, SendOutcome.FAILED):
        path = tmp_path / outcome.value
        channel = OutcomeChannel()
        channel.default = outcome
        store, repo, scheduler, _ = _build(path, channel=channel)
        _seed(repo, due_at=NOW - GRACE - 100)
        await scheduler.tick()
        assert channel.calls[-1][1] is None, outcome
        store.close()


async def test_composition_names_exactly_the_selected_set(tmp_path: Path):
    """A row cooling on the floor is not renamed early."""
    store, repo, scheduler, channel = _build(tmp_path)
    eligible = _seed(repo, due_at=NOW - GRACE - 100, text="eligible now")
    cooling = _seed(repo, due_at=NOW - GRACE - 200, text="cooling on the floor")
    repo.mark_missed(cooling.id, now=NOW)
    repo.schedule_retry(cooling.id, next_attempt_at=NOW + FLOOR)
    await scheduler.tick()
    summary = channel.sent[0]
    assert "eligible now" in summary
    assert "cooling on the floor" not in summary
    # And the cooling row is untouched by this tick.
    assert repo.get(cooling.id).reported_at is None
    assert repo.get(cooling.id).send_attempts == 0
    store.close()


async def test_reported_rows_never_resurface(tmp_path: Path):
    clock = Clock()
    store, repo, scheduler, channel = _build(tmp_path, clock=clock)
    row = _seed(repo, due_at=NOW - GRACE - 100, text="told you once")
    await scheduler.tick()
    assert repo.get(row.id).reported_at is not None
    before = len(channel.sent)
    for _ in range(30):
        clock.advance(FLOOR)
        await scheduler.tick()
    assert len(channel.sent) == before
    store.close()


# --- 5.3 The report horizon and the outage case --------------------------


async def _run_until(scheduler, clock, *, step, predicate, limit=400):
    """Tick with the clock advancing by ``step`` until ``predicate`` or ``limit``."""
    for i in range(limit):
        if predicate():
            return i
        await scheduler.tick()
        clock.advance(step)
    return None


async def test_a_persistently_partial_summary_terminates_at_the_horizon(
    tmp_path: Path, caplog
):
    clock = Clock()
    channel = OutcomeChannel()
    channel.default = SendOutcome.PARTIAL
    store, repo, scheduler, _ = _build(tmp_path, channel=channel, clock=clock)
    row = _seed(repo, due_at=NOW - GRACE - 100, text="partially told, forever")

    with caplog.at_level(logging.ERROR, logger="henk.reminders.scheduler"):
        ticks = await _run_until(
            scheduler,
            clock,
            step=FLOOR,
            predicate=lambda: repo.get(row.id).reported_at is not None,
            limit=400,
        )
    assert ticks is not None, "the partial summary never terminated"
    # Bounded by the span to due_at + grace + horizon over the floor, plus the one
    # final attempt whose post-send write performs the give-up.
    assert len(channel.sent) <= HORIZON / FLOOR + 2, len(channel.sent)
    assert len(channel.sent) >= 2, "it should have retried, not given up at once"
    # The give-up is error-logged, and it is not a claim that the owner was told.
    assert any("hori" in r.message.lower() or "gave up" in r.message.lower()
               for r in caplog.records)
    store.close()


async def test_the_horizon_give_up_writes_no_new_audit_transition(tmp_path: Path):
    clock = Clock()
    channel = OutcomeChannel()
    channel.default = SendOutcome.PARTIAL
    receipts = Receipts()
    store, repo, scheduler, _ = _build(
        tmp_path, channel=channel, clock=clock, receipts=receipts
    )
    row = _seed(repo, due_at=NOW - GRACE - HORIZON - 100)
    await scheduler.tick()
    assert repo.get(row.id).reported_at is not None
    # `missed` is the only transition; the give-up terminates reporting and says so
    # in a log, not in the audit trail.
    assert [r["transition"] for r in receipts.records] == [MISSED]
    store.close()


async def test_a_stale_row_is_named_in_an_attempted_summary_before_any_give_up(
    tmp_path: Path,
):
    """The property that decides WHERE the horizon lives.

    A row seven days overdue is past `due_at + grace + horizon` on the very tick it
    becomes reportable. Evaluated post-send it is named once and then retired;
    evaluated in the pre-work transaction it would be retired **unnamed**, and
    "never delivered and never reported" is the one outcome this capability exists to
    make impossible.
    """
    channel = OutcomeChannel()
    channel.default = SendOutcome.PARTIAL
    store, repo, scheduler, _ = _build(tmp_path, channel=channel)
    row = _seed(repo, due_at=NOW - 7 * 86400, text="a week old and stale")
    await scheduler.tick()
    assert len(channel.sent) == 1
    assert "a week old and stale" in channel.sent[0], "retired without being named"
    assert repo.get(row.id).reported_at == NOW
    store.close()


async def test_a_channel_outage_never_forfeits_the_report(tmp_path: Path):
    """A wholly failed summary is never given up on a channel outcome.

    Driven down past the horizon and then recovered: the first eligible tick after
    recovery must name every unreported row.
    """
    clock = Clock()
    channel = OutcomeChannel()
    channel.default = SendOutcome.FAILED
    store, repo, scheduler, _ = _build(tmp_path, channel=channel, clock=clock)
    texts = [f"row {i} survived the outage" for i in range(4)]
    ids = [
        _seed(repo, due_at=NOW - GRACE - 100 - i, text=t).id
        for i, t in enumerate(texts)
    ]

    # Down for three times the horizon.
    deadline = NOW + 3 * HORIZON
    while clock.at < deadline:
        await scheduler.tick()
        clock.advance(FLOOR)
    # Nothing was retired while the channel was down.
    assert all(repo.get(rid).reported_at is None for rid in ids)
    attempts = len(channel.sent)
    assert attempts > 10, "it should have kept trying"

    channel.default = SendOutcome.DELIVERED
    await scheduler.tick()
    summary = channel.sent[-1]
    assert [t for t in texts if t not in summary] == []
    assert all(repo.get(rid).reported_at is not None for rid in ids)
    store.close()


async def test_a_report_crash_loop_gives_up_with_an_error_log(tmp_path: Path, caplog):
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - GRACE - 100)
    repo.mark_missed(row.id, now=NOW)
    repo.charge_attempt(row.id)
    repo.charge_attempt(row.id)
    with caplog.at_level(logging.ERROR, logger="henk.reminders.scheduler"):
        await scheduler.tick()
    after = repo.get(row.id)
    assert after.reported_at == NOW
    assert after.send_attempts == 0
    assert caplog.records, "the give-up must be loud"
    # No summary was sent for it: the row was retired in pre-work.
    assert channel.sent == []
    store.close()


# --- 5.4 Tick isolation --------------------------------------------------


async def test_a_store_error_mid_tick_rolls_back_and_the_next_tick_succeeds(
    tmp_path: Path,
):
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - 60)

    calls = {"n": 0}
    original = repo.select_due

    def exploding(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store error mid-tick")
        return original(**kwargs)

    repo.select_due = exploding
    with pytest.raises(RuntimeError):
        await scheduler.tick()
    # Nothing committed by the failed tick.
    assert repo.get(row.id).send_attempts == 0
    assert repo.get(row.id).status == PENDING
    # The next tick succeeds.
    await scheduler.tick()
    assert repo.get(row.id).status == DELIVERED
    store.close()


async def test_the_run_loop_survives_a_store_error(tmp_path: Path, caplog):
    store, repo, scheduler, channel = _build(tmp_path)
    row = _seed(repo, due_at=NOW - 60)

    calls = {"n": 0}
    original = repo.select_due

    def exploding(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store error mid-tick")
        return original(**kwargs)

    repo.select_due = exploding
    task = asyncio.create_task(scheduler.run())
    with caplog.at_level(logging.ERROR, logger="henk.reminders.scheduler"):
        for _ in range(50):
            await asyncio.sleep(0)
            if repo.get(row.id).status == DELIVERED:
                break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.get(row.id).status == DELIVERED
    assert any("tick" in r.message.lower() for r in caplog.records)
    store.close()


async def test_the_run_loop_survives_a_channel_exception(tmp_path: Path):
    channel = OutcomeChannel()
    channel.explode = RuntimeError("channel blew up")
    store, repo, scheduler, _ = _build(tmp_path, channel=channel)
    _seed(repo, due_at=NOW - 60)
    task = asyncio.create_task(scheduler.run())
    for _ in range(50):
        await asyncio.sleep(0)
        if len(channel.calls) >= 2:
            break
    assert not task.done(), "a channel exception killed the scheduler task"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.close()


async def test_the_run_loop_stops_cleanly_on_cancellation(tmp_path: Path):
    store, repo, scheduler, channel = _build(tmp_path)
    task = asyncio.create_task(scheduler.run())
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done()
    store.close()


async def test_one_tick_captures_now_exactly_once(tmp_path: Path):
    """A tick cannot disagree with itself.

    Driven by a clock that advances an hour on EVERY read: a scheduler comparing a
    due instant against one value and recording the delivery against another would
    produce a `delivered_at` that does not match the instant it selected on, and a
    row could be both "not yet due" and "past grace" in the same tick.
    """
    clock = SteppingClock(at=NOW, step=3600.0)
    store, repo, scheduler, channel = _build(tmp_path, clock=clock)
    row = _seed(repo, due_at=NOW - 60)
    reads_before = len(clock.reads)
    await scheduler.tick()
    reads = len(clock.reads) - reads_before
    assert reads == 1, f"the tick read the clock {reads} times"
    # And the delivery was recorded against exactly the instant the tick captured —
    # not against a later read. (Seeding consumed earlier reads, so the tick's instant
    # is NOT `NOW`; asserting `NOW` here would be asserting the fixture, not the
    # property.)
    captured = clock.reads[reads_before]
    assert repo.get(row.id).delivered_at == captured
    store.close()


async def test_a_tick_with_grace_delivery_and_summary_still_reads_the_clock_once(
    tmp_path: Path,
):
    """The busiest possible tick: all three stages in one, one clock read."""
    clock = SteppingClock(at=NOW, step=3600.0)
    store, repo, scheduler, channel = _build(tmp_path, clock=clock)
    stale = _seed(repo, due_at=NOW - GRACE - 100, text="past grace")
    due = _seed(repo, due_at=NOW - 600, text="due now")
    reads_before = len(clock.reads)
    await scheduler.tick()
    assert len(clock.reads) - reads_before == 1
    captured = clock.reads[reads_before]
    # Grace, delivery and the summary all recorded against the SAME instant — which is
    # the property: one tick cannot disagree with itself.
    assert repo.get(due.id).delivered_at == captured
    assert repo.get(stale.id).reported_at == captured
    store.close()


# --- Structural: instants only, and no scope spans an await --------------


def test_the_scheduler_reads_no_wall_clock_and_no_zone():
    """Instants only. Rendering goes through the shared renderer, nowhere else.

    The suite's process-timezone guard already covers `henk/reminders/`; this states
    the narrower rule for this module — it holds no zone of its own and constructs no
    datetime, so there is no second place a due time can be formatted.
    """
    import ast
    import inspect

    from henk.reminders import scheduler as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    for forbidden in ("ZoneInfo", "datetime", "strftime", "localtime", "tzset"):
        assert forbidden not in source, f"{forbidden} has no business here"
    # And no method is named `now` or `today` — the timezone guard flags any
    # zero-argument call by those names, and its own history says the fix is
    # renaming rather than exempting.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name.strip("_") not in ("now", "today"), node.name


def test_the_process_timezone_guard_covers_the_scheduler_module():
    from tests.test_reminders_timeparse import _TIMEZONE_LEAK_SCOPE, _leaky_calls

    assert any("henk/reminders" in s for s in _TIMEZONE_LEAK_SCOPE)
    path = Path(__file__).resolve().parent.parent / "henk/reminders/scheduler.py"
    assert path.exists()
    assert list(_leaky_calls(path)) == []


# --- 5.6 The fault-injection matrix, retargeted at the real store --------
#
# The model's transferable artifact was never the model — it was this matrix (design
# D11). Here it runs against the real repository and the real scheduler, with a fault
# injected at each stage boundary the design names and a tri-valued channel double
# crossed over it.
#
# The invariant under fault is CONSERVATION, not success: a fault is allowed to abandon
# a tick, log loudly, and cost the owner a duplicate. What it may never do is leave a
# row that is neither terminal nor still selectable — a row charged an attempt with no
# recording write, which is the shape that silently vanishes.

#: Every repository call the tick makes, in the order the tick makes them. Faulting
#: each one covers the pre-work commit (the three selection/write calls inside it), the
#: grace transition, the pre-send re-read, and each post-send write.
FAULT_POINTS = (
    "select_past_grace",
    "mark_missed",
    "select_due",
    "select_reportable",
    "charge_attempt",
    "mark_abandoned",
    "status_of",
    "mark_delivered",
    "schedule_retry",
    "mark_reported",
)

TRI_VALUED = (SendOutcome.DELIVERED, SendOutcome.PARTIAL, SendOutcome.FAILED)

#: Which channel outcomes actually reach each fault point, DECLARED rather than
#: discovered. Three of the ten writes only exist on one arm of the outcome mapping —
#: `mark_delivered` is unreachable when nothing is ever delivered, `schedule_retry`
#: when nothing ever fails, `mark_reported` when the summary neither lands nor hits the
#: horizon — and a matrix that quietly passed on those cells would be reporting
#: thirty checks while performing twenty-three. Declaring it means an unreachable cell
#: asserts it was NOT reached, so a change that makes one reachable (or stops reaching
#: one) fails here and has to be looked at.
REACHABLE = {
    "select_past_grace": TRI_VALUED,
    "mark_missed": TRI_VALUED,
    "select_due": TRI_VALUED,
    "select_reportable": TRI_VALUED,
    "charge_attempt": TRI_VALUED,
    # The crash bound is evaluated in pre-work, before any send, so the channel's
    # outcome cannot affect whether it fires. That it is reachable under all three is
    # itself the pre-work-placement property, restated as coverage.
    "mark_abandoned": TRI_VALUED,
    "status_of": TRI_VALUED,
    "mark_delivered": (SendOutcome.DELIVERED, SendOutcome.PARTIAL),
    "schedule_retry": (SendOutcome.PARTIAL, SendOutcome.FAILED),
    # Delivered summary, or the horizon give-up on a partial one. Never on a wholly
    # failed summary — which is the "a channel outage never forfeits the report"
    # property showing up as an absence.
    "mark_reported": (SendOutcome.DELIVERED, SendOutcome.PARTIAL),
}


def _at_rest(row) -> bool:
    """True when the row is terminal, or cooling with nothing owed to it.

    Three legitimate resting states, and one forbidden one:

    - delivered / delivered-late: done;
    - missed / abandoned with `reported_at` set: reported, or given up on loudly;
    - anything with `send_attempts == 0`: nothing is owed — a later tick will pick it
      up under the ordinary rules.

    The forbidden state is a row carrying a charge with no write behind it, because
    that is a row whose counter climbs on every tick until it is retired unnamed.
    """
    if row.status in (DELIVERED, DELIVERED_LATE):
        return True
    if row.status in (MISSED, ABANDONED) and row.reported_at is not None:
        return True
    return row.send_attempts == 0


@pytest.mark.parametrize("outcome", TRI_VALUED, ids=lambda o: o.value)
@pytest.mark.parametrize("fault", FAULT_POINTS)
async def test_a_fault_at_any_stage_boundary_loses_no_row(
    tmp_path: Path, fault: str, outcome: SendOutcome
):
    clock = Clock()
    channel = OutcomeChannel()
    channel.default = outcome
    store, repo, scheduler, _ = _build(tmp_path, channel=channel, clock=clock)
    # Four rows, chosen so that between them every fault point is reachable in one
    # run: delivery work, grace-then-report work, a row the crash bound retires in
    # pre-work, and a row already past grace + horizon so the give-up arm is live.
    due = _seed(repo, due_at=NOW - 600, text="delivery work")
    graced = _seed(repo, due_at=NOW - GRACE - 600, text="report work")
    at_limit = _seed(repo, due_at=NOW - 300, text="crash-bound work")
    for _ in range(CRASH_LIMIT - 1):
        repo.charge_attempt(at_limit.id)
    expired = _seed(repo, due_at=NOW - GRACE - HORIZON - 600, text="horizon work")
    repo.mark_missed(expired.id, now=NOW)
    rows = (due, graced, at_limit, expired)

    original = getattr(repo, fault)
    fired = {"n": 0}

    def faulty(*args, **kwargs):
        fired["n"] += 1
        if fired["n"] == 1:
            raise RuntimeError(f"injected fault at {fault}")
        return original(*args, **kwargs)

    setattr(repo, fault, faulty)

    # The faulted tick, then several healthy ones. `run()` is not used: the point is
    # per-tick isolation, and swallowing the exception here mirrors what run() does.
    for _ in range(8):
        try:
            await scheduler.tick()
        except Exception:
            pass
        clock.advance(FLOOR)

    if outcome in REACHABLE[fault]:
        assert fired["n"] >= 1, f"the fault at {fault} was never reached"
    else:
        assert fired["n"] == 0, (
            f"{fault} is declared unreachable under outcome {outcome.value} but was "
            "reached — the declaration or the code has changed"
        )
    for seeded in rows:
        row = repo.get(seeded.id)
        assert _at_rest(row), (
            f"fault at {fault} with outcome {outcome.value} left reminder "
            f"{row.id} charged with no write behind it: {row}"
        )
        assert row.send_attempts <= CRASH_LIMIT, row
    store.close()


@pytest.mark.parametrize("outcome", TRI_VALUED, ids=lambda o: o.value)
async def test_process_death_at_each_stage_loses_no_row(
    tmp_path: Path, outcome: SendOutcome
):
    """Death between the two transactions, then a restart, for each channel outcome.

    Each iteration is a fresh `Store` over the same file, which is what a restart
    actually is — the pre-work commit is on disk, the post-send write never ran.
    """
    channel = OutcomeChannel()
    channel.default = outcome
    clock = Clock()
    row_id = None

    for _ in range(CRASH_LIMIT + 3):
        store, repo, scheduler, _ = _build(
            tmp_path, channel=channel, clock=clock
        )
        if row_id is None:
            row_id = _seed(repo, due_at=NOW - 600, text="survives restarts").id
        # Kill every post-send write, so only the pre-work commit ever lands.
        for write in ("mark_delivered", "schedule_retry", "mark_reported"):
            setattr(repo, write, _dying(write))
        try:
            await scheduler.tick()
        except RuntimeError:
            pass
        store.close()
        clock.advance(FLOOR)

    reopened, repo2 = _reopen(tmp_path)
    final = repo2.get(row_id)
    # Bounded: the pre-work crash bound is the only thing that can stop this, which is
    # precisely why it is evaluated pre-work.
    assert final.send_attempts <= CRASH_LIMIT, final
    assert final.status in (PENDING, ABANDONED, MISSED), final
    reopened.close()


def _dying(name: str):
    def die(*args, **kwargs):
        raise RuntimeError(f"process died before {name}")

    return die
