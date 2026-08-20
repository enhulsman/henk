"""The delivery half of the reminders repository (reminder-delivery groups 3.1/3.2).

From the reminders delta's selector, transaction, counting, grace, crash-bound and
report requirements. Real `sqlite3` files throughout, never a double: the two-
transaction structure this change rests on is a property of the driver, and process
death is simulated by dropping the connection and reopening the file.

Two rules shape almost every test below, and both come from the design:

- **The selector is a query, so every exit is re-checked by re-running the selector
  against a REOPENED store**, not by reading the row back. An exit that only looks
  right in the row it just wrote is exactly the bug the "every exit writes state the
  selector tests" requirement exists to prevent — a helper (`_selects_nothing_for`)
  does the reopen so no test can forget it.
- **No transaction scope spans an await**, so every method here is synchronous and
  transaction-agnostic: standalone it is one atomic write, inside the scheduler's
  pre-work scope it joins that one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from henk.store import Store
from henk.store.reminders import (
    ABANDONED,
    CANCELLED,
    DELIVERED,
    DELIVERED_LATE,
    MISSED,
    PENDING,
    ReminderStore,
)

#: A fixed instant well clear of any DST transition. These tests are about storage;
#: the clock and the zone are group 5's and reminders-core's respectively.
NOW = 1_787_000_000.0
TZ = "Europe/Amsterdam"

GRACE = 86400.0
FLOOR = 900.0
HORIZON = 86400.0
CRASH_LIMIT = 3
TICK_LIMIT = 10


def _open(path: Path, *, clock=None) -> tuple[Store, ReminderStore]:
    store = Store(path, clock=clock or (lambda: NOW))
    return store, ReminderStore(store)


def _fresh(tmp_path: Path, *, clock=None) -> tuple[Store, ReminderStore]:
    return _open(tmp_path / "store" / "henk.db", clock=clock)


def _seed(repo: ReminderStore, *, due_at: float, text: str = "call the plumber"):
    return repo.schedule(text, due_at=due_at, due_tz=TZ, input_spec="+1h")


def _reopen(tmp_path: Path) -> tuple[Store, ReminderStore]:
    """Reopen the same file, as a restarted process would find it."""
    return _open(tmp_path / "store" / "henk.db")


def _selects_nothing_for(tmp_path: Path, reminder_id: int, *, now: float) -> bool:
    """True when a REOPENED store's selector picks the row up for no work at all.

    The reopen is the point: an exit held only in the process's memory, or written to
    a column the selector does not predicate on, passes a read-back of the row and
    fails here.
    """
    store, repo = _reopen(tmp_path)
    try:
        due = {r.id for r in repo.select_due(now=now, limit=TICK_LIMIT)}
        report = {r.id for r in repo.select_reportable(now=now)}
        grace = {r.id for r in repo.select_past_grace(now=now, grace=GRACE)}
        return reminder_id not in (due | report | grace)
    finally:
        store.close()


# --- 3.1 The delivery selector -------------------------------------------


def test_a_due_pending_row_is_selected(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    assert [r.id for r in repo.select_due(now=NOW, limit=TICK_LIMIT)] == [row.id]
    store.close()


def test_a_row_whose_due_instant_has_not_passed_is_not_selected(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW + 3600)
    assert repo.select_due(now=NOW, limit=TICK_LIMIT) == ()
    assert repo.get(row.id).status == PENDING
    store.close()


def test_a_future_due_row_with_an_eligible_next_attempt_at_is_never_selected(
    tmp_path: Path,
):
    """The `due_at` conjunct, which is the whole reason it exists.

    `next_attempt_at` is `NOT NULL DEFAULT 0`, and 0 means *eligible now*. A
    single-column selector is therefore one initialization bug away from delivering
    every future reminder on the first tick — so the conjunct is asserted against a
    row whose `next_attempt_at` has been forced to the schema default while its due
    instant is a week out.
    """
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW + 7 * 86400)
    with store.transaction() as conn:
        conn.execute("UPDATE reminders SET next_attempt_at = 0 WHERE id = ?", (row.id,))
    assert repo.get(row.id).next_attempt_at == 0.0
    assert repo.select_due(now=NOW, limit=TICK_LIMIT) == ()
    # And it stays unselected however far `now` advances short of the due instant.
    assert repo.select_due(now=NOW + 7 * 86400 - 1, limit=TICK_LIMIT) == ()
    assert [r.id for r in repo.select_due(now=NOW + 7 * 86400, limit=TICK_LIMIT)] == [
        row.id
    ]
    store.close()


def test_a_row_cooling_on_the_floor_is_not_selected_until_the_floor_elapses(
    tmp_path: Path,
):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    repo.schedule_retry(row.id, next_attempt_at=NOW + FLOOR)
    assert repo.select_due(now=NOW, limit=TICK_LIMIT) == ()
    assert repo.select_due(now=NOW + FLOOR - 1, limit=TICK_LIMIT) == ()
    assert [r.id for r in repo.select_due(now=NOW + FLOOR, limit=TICK_LIMIT)] == [row.id]
    store.close()


def test_a_cancelled_row_is_never_selected(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    repo.cancel(row.id)
    assert repo.select_due(now=NOW, limit=TICK_LIMIT) == ()
    assert repo.select_reportable(now=NOW) == ()
    store.close()


def test_delivery_selection_is_capped_oldest_due_first(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    # Twenty-five rows, all due, deliberately inserted newest-due first so insertion
    # order and due order disagree — a selector that forgot ORDER BY would pass if
    # they agreed.
    ids_by_due = {}
    for offset in range(25):
        due = NOW - 60 - offset
        ids_by_due[due] = _seed(repo, due_at=due, text=f"reminder {offset}").id
    expected = [ids_by_due[due] for due in sorted(ids_by_due)][:TICK_LIMIT]
    assert [r.id for r in repo.select_due(now=NOW, limit=TICK_LIMIT)] == expected
    store.close()


def test_unselected_rows_are_neither_charged_nor_written(tmp_path: Path):
    """Pacing costs nothing in bookkeeping: the tail is untouched, not deferred.

    This is what makes the per-tick cap safe — an unselected row is indistinguishable
    from one that was never due, so it needs no state of its own.
    """
    store, repo = _fresh(tmp_path)
    rows = [_seed(repo, due_at=NOW - 60 - i, text=f"r{i}") for i in range(25)]
    selected = {r.id for r in repo.select_due(now=NOW, limit=TICK_LIMIT)}
    for row in rows:
        if row.id in selected:
            continue
        after = repo.get(row.id)
        assert after.status == PENDING
        assert after.send_attempts == 0
        assert after.delivered_at is None
        assert after.reported_at is None
        assert after.next_attempt_at == row.due_at
    store.close()


def test_report_selection_is_uncapped(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    ids = []
    for i in range(30):
        row = _seed(repo, due_at=NOW - 10_000 - i, text=f"r{i}")
        repo.mark_missed(row.id, now=NOW)
        ids.append(row.id)
    # One message however many rows it names, so no bound here at all.
    assert len(repo.select_reportable(now=NOW)) == 30
    store.close()


def test_report_selection_takes_missed_and_abandoned_but_not_delivered(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    missed = _seed(repo, due_at=NOW - 10_000, text="missed")
    abandoned = _seed(repo, due_at=NOW - 9_000, text="abandoned")
    delivered = _seed(repo, due_at=NOW - 8_000, text="delivered")
    cancelled = _seed(repo, due_at=NOW - 7_000, text="cancelled")
    repo.mark_missed(missed.id, now=NOW)
    repo.mark_abandoned(abandoned.id, now=NOW)
    repo.mark_delivered(delivered.id, now=NOW, late=True)
    repo.cancel(cancelled.id)
    assert {r.id for r in repo.select_reportable(now=NOW)} == {missed.id, abandoned.id}
    store.close()


def test_a_reported_row_is_never_selected_again(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 10_000)
    repo.mark_missed(row.id, now=NOW)
    repo.mark_reported([row.id], now=NOW)
    assert repo.select_reportable(now=NOW) == ()
    assert repo.select_reportable(now=NOW + 10**7) == ()
    store.close()


def test_report_selection_is_ordered_oldest_due_first(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    ids_by_due = {}
    for offset in range(8):
        due = NOW - 10_000 + offset * 7  # insertion order == due order ascending
        row = _seed(repo, due_at=due, text=f"r{offset}")
        repo.mark_missed(row.id, now=NOW)
        ids_by_due[due] = row.id
    # Reverse one pair's insertion order so ORDER BY is doing the work.
    late_due = NOW - 20_000
    first = _seed(repo, due_at=late_due, text="oldest, inserted last")
    repo.mark_missed(first.id, now=NOW)
    expected = [first.id] + [ids_by_due[d] for d in sorted(ids_by_due)]
    assert [r.id for r in repo.select_reportable(now=NOW)] == expected
    store.close()


def test_a_reportable_row_cooling_on_the_floor_is_not_selected(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 10_000)
    repo.mark_missed(row.id, now=NOW)
    repo.schedule_retry(row.id, next_attempt_at=NOW + FLOOR)
    assert repo.select_reportable(now=NOW) == ()
    assert [r.id for r in repo.select_reportable(now=NOW + FLOOR)] == [row.id]
    store.close()


def test_past_grace_selection_covers_every_pending_row_not_just_the_capped_ones(
    tmp_path: Path,
):
    """Grace applies to EVERY past-grace pending row, selected or not.

    A grace pass bounded by the delivery cap would leave the 11th-oldest row pending
    past its window forever, delivering a day-old instruction as if current.
    """
    store, repo = _fresh(tmp_path)
    ids = [_seed(repo, due_at=NOW - GRACE - 100 - i, text=f"r{i}").id for i in range(25)]
    past = {r.id for r in repo.select_past_grace(now=NOW, grace=GRACE)}
    assert past == set(ids)
    # Boundary: exactly at the window is NOT past it (the requirement says "more
    # than the grace window before the captured instant").
    fresh = _seed(repo, due_at=NOW - GRACE, text="exactly at the boundary")
    assert fresh.id not in {
        r.id for r in repo.select_past_grace(now=NOW, grace=GRACE)
    }
    store.close()


# --- 3.1 Every exit writes state the selector tests ----------------------


@pytest.mark.parametrize(
    "exit_name",
    ["delivered", "delivered-late", "missed", "abandoned", "reported", "give-up"],
)
def test_every_exit_is_invisible_to_a_reopened_selector(tmp_path: Path, exit_name: str):
    """One test per exit, each re-checked against a reopened store's selector.

    `missed` and `abandoned` are exits from DELIVERY work only — they remain
    selectable as REPORT work by design, so those two assert the narrower property
    (never selected for delivery again) while the terminal four assert the full one.
    """
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 10_000)
    if exit_name == "delivered":
        repo.mark_delivered(row.id, now=NOW, late=False)
    elif exit_name == "delivered-late":
        repo.mark_delivered(row.id, now=NOW, late=True)
    elif exit_name == "missed":
        repo.mark_missed(row.id, now=NOW)
    elif exit_name == "abandoned":
        repo.mark_abandoned(row.id, now=NOW)
    elif exit_name == "reported":
        repo.mark_missed(row.id, now=NOW)
        repo.mark_reported([row.id], now=NOW)
    elif exit_name == "give-up":
        repo.mark_abandoned(row.id, now=NOW)
        repo.mark_reported([row.id], now=NOW)
    store.close()

    reopened, reopened_repo = _reopen(tmp_path)
    try:
        # No exit is ever selected for DELIVERY again.
        assert reopened_repo.select_due(now=NOW + 10**7, limit=TICK_LIMIT) == ()
        assert reopened_repo.select_past_grace(now=NOW + 10**7, grace=GRACE) == ()
    finally:
        reopened.close()

    if exit_name in ("missed", "abandoned"):
        # Deliberately still reportable: that is the exit's whole purpose.
        assert not _selects_nothing_for(tmp_path, row.id, now=NOW + 10**7)
        store2, repo2 = _reopen(tmp_path)
        assert [r.id for r in repo2.select_reportable(now=NOW + 10**7)] == [row.id]
        store2.close()
    else:
        assert _selects_nothing_for(tmp_path, row.id, now=NOW + 10**7)


def test_the_terminal_statuses_are_what_the_exits_actually_write(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    cases = {
        DELIVERED: lambda r: repo.mark_delivered(r, now=NOW, late=False),
        DELIVERED_LATE: lambda r: repo.mark_delivered(r, now=NOW, late=True),
        MISSED: lambda r: repo.mark_missed(r, now=NOW),
        ABANDONED: lambda r: repo.mark_abandoned(r, now=NOW),
    }
    for status, write in cases.items():
        row = _seed(repo, due_at=NOW - 10_000, text=f"to become {status}")
        write(row.id)
        assert repo.get(row.id).status == status
    store.close()


def test_a_delivery_exit_records_when_it_happened_and_clears_the_counter(
    tmp_path: Path,
):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 10_000)
    repo.charge_attempt(row.id)
    repo.mark_delivered(row.id, now=NOW, late=True)
    after = repo.get(row.id)
    assert after.status == DELIVERED_LATE
    assert after.delivered_at == NOW
    assert after.send_attempts == 0
    # `reported_at` is the REPORT path's column: a delivered reminder is never
    # reported, and writing it here would hide the row from a summary that should
    # never have named it in the first place.
    assert after.reported_at is None
    store.close()


def test_the_grace_exit_clears_the_counter_and_leaves_reported_at_null(tmp_path: Path):
    """`reported_at` left NULL is what lets the SAME tick's summary name the row."""
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - GRACE - 100)
    repo.charge_attempt(row.id)
    assert repo.get(row.id).send_attempts == 1
    repo.mark_missed(row.id, now=NOW)
    after = repo.get(row.id)
    assert after.status == MISSED
    assert after.send_attempts == 0
    assert after.reported_at is None
    assert after.next_attempt_at == NOW
    assert [r.id for r in repo.select_reportable(now=NOW)] == [row.id]
    store.close()


def test_the_abandoned_exit_leaves_the_row_reportable_in_the_same_tick(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 100)
    for _ in range(CRASH_LIMIT):
        repo.charge_attempt(row.id)
    repo.mark_abandoned(row.id, now=NOW)
    after = repo.get(row.id)
    assert after.status == ABANDONED
    assert after.send_attempts == 0
    assert after.reported_at is None
    assert after.next_attempt_at == NOW
    assert [r.id for r in repo.select_reportable(now=NOW)] == [row.id]
    store.close()


def test_mark_reported_writes_every_named_row_and_clears_their_counters(
    tmp_path: Path,
):
    store, repo = _fresh(tmp_path)
    ids = []
    for i in range(5):
        row = _seed(repo, due_at=NOW - 10_000 - i, text=f"r{i}")
        repo.mark_missed(row.id, now=NOW)
        repo.charge_attempt(row.id)
        ids.append(row.id)
    repo.mark_reported(ids, now=NOW)
    for rid in ids:
        after = repo.get(rid)
        assert after.reported_at == NOW
        assert after.send_attempts == 0
    assert repo.select_reportable(now=NOW) == ()
    store.close()


def test_mark_reported_on_an_empty_list_is_a_no_op(tmp_path: Path):
    # The scheduler composes no summary when nothing is selected, but a defensive
    # empty call must not become `WHERE id IN ()` or a full-table write.
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 10_000)
    repo.mark_missed(row.id, now=NOW)
    repo.mark_reported([], now=NOW)
    assert repo.get(row.id).reported_at is None
    store.close()


# --- 3.1 The pre-send status re-read -------------------------------------


def test_status_of_reads_the_committed_status(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    assert repo.status_of(row.id) == PENDING
    repo.cancel(row.id)
    assert repo.status_of(row.id) == CANCELLED
    assert repo.status_of(row.id + 1000) is None
    store.close()


def test_status_of_sees_a_cancellation_committed_after_selection(tmp_path: Path):
    """The stale-selection window the pre-send re-read closes.

    Selection happens in the pre-work transaction; the send happens after it commits.
    A cancellation committing in between must be visible to the re-read, or the
    scheduler dispatches a row the owner has already withdrawn.
    """
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    selected = repo.select_due(now=NOW, limit=TICK_LIMIT)
    assert [r.id for r in selected] == [row.id]
    repo.cancel(row.id)  # commits between selection and dispatch
    assert repo.status_of(row.id) == CANCELLED
    store.close()


# --- 3.1 Counting ---------------------------------------------------------


def test_charge_attempt_returns_the_incremented_count(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    assert repo.charge_attempt(row.id) == 1
    assert repo.charge_attempt(row.id) == 2
    assert repo.charge_attempt(row.id) == 3
    assert repo.get(row.id).send_attempts == 3
    store.close()


def test_charge_attempt_on_a_missing_row_returns_zero(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    assert repo.charge_attempt(4242) == 0
    store.close()


def test_schedule_retry_clears_the_counter(tmp_path: Path):
    """The two-budget separation: a post-send write always clears `send_attempts`.

    This is what keeps a channel outage from ever reaching the crash bound.
    """
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    repo.charge_attempt(row.id)
    repo.schedule_retry(row.id, next_attempt_at=NOW + FLOOR)
    after = repo.get(row.id)
    assert after.send_attempts == 0
    assert after.next_attempt_at == NOW + FLOOR
    assert after.status == PENDING  # a floor retry keeps the row pending
    store.close()


def test_a_channel_failure_loop_never_grows_the_counter(tmp_path: Path):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    now = NOW
    for _ in range(50):
        assert repo.charge_attempt(row.id) == 1  # always 1: the last write cleared it
        repo.schedule_retry(row.id, next_attempt_at=now + FLOOR)
        now += FLOOR
    assert repo.get(row.id).send_attempts == 0
    assert repo.get(row.id).status == PENDING
    store.close()


# --- 3.2 The two transactions --------------------------------------------


def test_the_whole_pre_work_scope_commits_as_one(tmp_path: Path):
    """Grace, then selection against the post-grace state, then increments."""
    store, repo = _fresh(tmp_path)
    stale = _seed(repo, due_at=NOW - GRACE - 100, text="past grace")
    due = _seed(repo, due_at=NOW - 60, text="due now")

    with store.transaction():
        for row in repo.select_past_grace(now=NOW, grace=GRACE):
            repo.mark_missed(row.id, now=NOW)
        selected = repo.select_due(now=NOW, limit=TICK_LIMIT)
        for row in selected:
            repo.charge_attempt(row.id)

    # Selection ran against the POST-grace state, so the stale row is not delivery
    # work — it is report work, in the same tick.
    assert [r.id for r in selected] == [due.id]
    store.close()

    reopened, repo2 = _reopen(tmp_path)
    assert repo2.get(stale.id).status == MISSED
    assert repo2.get(due.id).send_attempts == 1
    assert [r.id for r in repo2.select_reportable(now=NOW)] == [stale.id]
    reopened.close()


def test_a_failure_anywhere_in_the_pre_work_scope_rolls_the_whole_tick_back(
    tmp_path: Path,
):
    store, repo = _fresh(tmp_path)
    stale = _seed(repo, due_at=NOW - GRACE - 100, text="past grace")
    due = _seed(repo, due_at=NOW - 60, text="due now")

    with pytest.raises(RuntimeError):
        with store.transaction():
            repo.mark_missed(stale.id, now=NOW)
            repo.charge_attempt(due.id)
            raise RuntimeError("store error mid-tick")

    store.close()
    reopened, repo2 = _reopen(tmp_path)
    # Nothing committed: the tick is abandoned whole, and the next tick retries it.
    assert repo2.get(stale.id).status == PENDING
    assert repo2.get(due.id).send_attempts == 0
    reopened.close()


def test_a_swallowed_inner_failure_still_rolls_the_pre_work_back(tmp_path: Path):
    """`Store.transaction()` poisons on any nested failure, and delivery relies on it.

    Half a tick's pre-work committed because an inner error was caught is precisely
    the shape that would charge an attempt without an exit.
    """
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 60)

    with store.transaction():
        repo.charge_attempt(row.id)
        try:
            with store.transaction():
                repo.mark_missed(row.id, now=NOW)
                raise RuntimeError("inner failure, swallowed by the caller")
        except RuntimeError:
            pass

    store.close()
    reopened, repo2 = _reopen(tmp_path)
    assert repo2.get(row.id).status == PENDING
    assert repo2.get(row.id).send_attempts == 0
    reopened.close()


def test_the_pre_work_increment_survives_process_death_before_the_post_send_write(
    tmp_path: Path,
):
    """The counter exists for exactly this: an attempt the process did not survive.

    Death is simulated by closing the connection between the two transactions and
    reopening the file — the pre-work commit is durable, the post-send write never
    ran.
    """
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    with store.transaction():
        repo.charge_attempt(row.id)
    store.close()  # the process dies here, mid-send

    reopened, repo2 = _reopen(tmp_path)
    after = repo2.get(row.id)
    assert after.send_attempts == 1
    assert after.status == PENDING
    assert after.delivered_at is None
    # And it is eligible again immediately, because nothing moved the floor.
    assert [r.id for r in repo2.select_due(now=NOW, limit=TICK_LIMIT)] == [row.id]
    reopened.close()


@pytest.mark.parametrize(
    "post_send",
    ["delivered", "delivered-late", "retry", "reported"],
)
def test_every_post_send_write_clears_the_counter(tmp_path: Path, post_send: str):
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 10_000)
    with store.transaction():
        repo.charge_attempt(row.id)
    assert repo.get(row.id).send_attempts == 1

    with store.transaction():
        if post_send == "delivered":
            repo.mark_delivered(row.id, now=NOW, late=False)
        elif post_send == "delivered-late":
            repo.mark_delivered(row.id, now=NOW, late=True)
        elif post_send == "retry":
            repo.schedule_retry(row.id, next_attempt_at=NOW + FLOOR)
        elif post_send == "reported":
            repo.mark_missed(row.id, now=NOW)
            repo.mark_reported([row.id], now=NOW)

    store.close()
    reopened, repo2 = _reopen(tmp_path)
    assert repo2.get(row.id).send_attempts == 0
    reopened.close()


def test_both_pre_work_give_up_exits_commit_with_the_rest_of_the_scope(tmp_path: Path):
    """The crash-limit give-up, on both a delivery row and a report row.

    Both live in the pre-work transaction (the report horizon does not — it is
    post-send, and group 5 owns it), so both must commit or roll back with the
    increments beside them.
    """
    store, repo = _fresh(tmp_path)
    delivery = _seed(repo, due_at=NOW - 60, text="crash-looping delivery")
    report = _seed(repo, due_at=NOW - 10_000, text="crash-looping report")
    repo.mark_missed(report.id, now=NOW)

    with store.transaction():
        for row in (delivery, report):
            for _ in range(CRASH_LIMIT - 1):
                repo.charge_attempt(row.id)
        assert repo.charge_attempt(delivery.id) == CRASH_LIMIT
        assert repo.charge_attempt(report.id) == CRASH_LIMIT
        repo.mark_abandoned(delivery.id, now=NOW)
        repo.mark_reported([report.id], now=NOW)

    store.close()
    reopened, repo2 = _reopen(tmp_path)
    abandoned = repo2.get(delivery.id)
    assert abandoned.status == ABANDONED
    assert abandoned.send_attempts == 0
    assert abandoned.reported_at is None  # still to be named in this tick's summary
    given_up = repo2.get(report.id)
    assert given_up.reported_at == NOW
    assert given_up.send_attempts == 0
    assert given_up.status == MISSED  # the give-up terminates reporting, not the row
    reopened.close()


def test_no_delivery_write_touches_the_owners_words_or_the_due_instant(tmp_path: Path):
    """Nothing this change writes may rewrite what the owner asked for."""
    store, repo = _fresh(tmp_path)
    row = _seed(repo, due_at=NOW - 60, text="pick up the parcel before six")
    for write in (
        lambda: repo.charge_attempt(row.id),
        lambda: repo.schedule_retry(row.id, next_attempt_at=NOW + FLOOR),
        lambda: repo.mark_missed(row.id, now=NOW),
        lambda: repo.mark_abandoned(row.id, now=NOW),
        lambda: repo.mark_reported([row.id], now=NOW),
        lambda: repo.mark_delivered(row.id, now=NOW, late=True),
        lambda: repo.mark_surfaced([row.id], now=NOW),
    ):
        write()
        after = repo.get(row.id)
        assert after.text == "pick up the parcel before six"
        assert after.due_at == row.due_at
        assert after.due_tz == row.due_tz
        assert after.input_spec == row.input_spec
        assert after.created_at == row.created_at
    store.close()
