"""Executable model of the CUT delivery design, as this change's deltas specify it.

Rewritten from `openspec/changes/reminders/notes/verify_selector_invariants.py`, which
modelled the *pre-cut* design (seven-step backoff schedule, `unconfirmed_sends`,
`terminal_at`, report item bound + pagination, chunk-atomic batching). All of that is
gone; per the original's own header the model is disposable and the fault-injection
matrix is the artifact, so this is the matrix retargeted at the design that is actually
being built (design D11, step 1).

What is modelled, from the reminders / channel-adapter deltas:

  - one polling tick, capturing `now` once (D1);
  - the selector as a QUERY with the `due_at` conjunct, delivery capped at
    `TICK_DELIVERY_LIMIT` oldest-due first, report work uncapped (D2);
  - the pre-work transaction in its stated order — grace transitions for EVERY
    past-grace pending row, then selection against the post-grace state, then the
    increments and BOTH pre-work bounds (D3);
  - one fixed retry floor, no schedule (cut #3);
  - the crash-attempt maximum in the pre-work transaction, for delivery rows
    (-> `abandoned`) and for report rows (-> `reported_at` as a give-up) (D3);
  - grace -> `missed`, `reported_at` left NULL so the same tick's summary names it;
  - one message per due reminder, plus one uncapped catch-up summary after them (D4);
  - the pre-send status re-read immediately before each dispatch (D4);
  - the asymmetric outcome mapping: a reminder's `partial` counts as delivered, a
    summary's `partial` marks nothing (D4);
  - the report horizon evaluated ONLY in the post-send write of an attempted summary,
    and only on a `partial` outcome (D5);
  - `send_attempts` cleared on every post-send return — the two-budget separation.

Transactions are modelled with an undo log rather than by staging, so a scope reads its
own writes (as `Store.transaction()` does) and a fault before the commit restores the
prior state exactly. Audit appends happen AFTER the commit, which is the real ordering
(D3), so a crash between the two loses a receipt rather than inventing one.

KNOWN NON-COVERAGE (stated so a green run is not read as broader than it is):
  - **No chunking and no byte lengths.** Chunk count is abstracted entirely into the
    tri-valued channel outcome, so nothing here exercises `split_message`, the
    summary's chunk boundaries, or which rows ride which chunk. The
    "oldest-due-first puts horizon-eligible rows in the head chunks" argument
    (design D5) is therefore NOT verified by this model — it is a claim about
    composition order, which the model does check, plus a claim about chunking,
    which it cannot.
  - **No send lock, no concurrency.** Serialization is a channel-adapter property and
    is proven against the adapter's real lock with a slow bridge (tasks 4.1/4.2); a
    single-threaded model cannot demonstrate an interleaving it cannot produce.
  - **No delivered-reminder note.** `surfaced_at`, the note window and the note count
    are outside the state machine this file models.
  - **The audit log is a list of (id, transition) pairs**, not records: nothing here
    validates against the v4 document (tests do, task 6.1).
  - **Cancellation is modelled only through the pre-send status re-read.** The
    residual "cancelled after dispatch" window is a real-time race, not a state
    reachable in a synchronous model.
  - **The clock is exact.** No clock jumps, no drift, no DST — the design is instants
    only, and the zone work is `reminders-core`'s and separately verified.

Run it with `python3 verify_selector_invariants.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- configuration: RemindersConfig's delivery knobs at their defaults (D10) ---

POLL_INTERVAL = 30
RETRY_FLOOR = 900
CRASH_ATTEMPT_LIMIT = 3
LATE_GRACE = 86400
LATE_DELIVERY_THRESHOLD = 300
REPORT_HORIZON = 86400
TICK_DELIVERY_LIMIT = 10

# --- statuses (henk.store.reminders' vocabulary) ---

PENDING = "pending"
DELIVERED = "delivered"
DELIVERED_LATE = "delivered-late"
MISSED = "missed"
ABANDONED = "abandoned"
CANCELLED = "cancelled"

TERMINAL_DELIVERY = (DELIVERED, DELIVERED_LATE)
REPORTABLE = (MISSED, ABANDONED)

# --- the tri-valued channel outcome (channel-integrity's SendOutcome) ---

OUT_DELIVERED, OUT_PARTIAL, OUT_FAILED = "delivered", "partial", "failed"


class Crash(Exception):
    """Process death. Uncommitted work is lost; committed work survives."""


@dataclass
class Row:
    """One reminders row. Only the columns the delivery selector predicates on."""

    id: int
    due_at: float
    status: str = PENDING
    send_attempts: int = 0
    next_attempt_at: float | None = None
    delivered_at: float | None = None
    reported_at: float | None = None

    #: Model-only bookkeeping, never a column. Recorded OUTSIDE the code under test
    #: (by the harness and the channel doubles) so a property cannot be proven by
    #: the same expression that would be wrong.
    named_in_attempted_summary: bool = False
    gave_up_via: str | None = None

    def __post_init__(self) -> None:
        if self.next_attempt_at is None:
            # The MODIFIED initialization requirement: every path into `pending`
            # writes `next_attempt_at` to the DUE INSTANT.
            self.next_attempt_at = self.due_at


class Txn:
    """One store transaction: reads see its own writes, a fault undoes all of them.

    Models `Store.transaction()`'s poisoning semantics without modelling depth: this
    design opens exactly one scope at a time (no scope spans an await, D3), so depth
    would model nothing the real contract test does not already cover.
    """

    def __init__(self, stats: "Stats") -> None:
        self._undo: list[tuple[Row, str, object]] = []
        self._audit: list[tuple[int, str]] = []
        self._stats = stats

    def set(self, row: Row, **fields) -> None:
        for key, value in fields.items():
            self._undo.append((row, key, getattr(row, key)))
            setattr(row, key, value)

    def audit(self, row: Row, transition: str) -> None:
        """Queue an audit append for AFTER the commit (D3's stated ordering)."""
        self._audit.append((row.id, transition))

    def rollback(self) -> None:
        for row, key, old in reversed(self._undo):
            setattr(row, key, old)
        self._audit.clear()

    def commit(self) -> None:
        self._stats.audit.extend(self._audit)
        self._audit.clear()


@dataclass
class Stats:
    error_logs: int = 0
    audit: list[tuple[int, str]] = field(default_factory=list)
    reminder_sends: int = 0
    summary_sends: int = 0
    crashes: int = 0
    grace_transitions: int = 0
    #: Per tick: (charged ids, ids that got a post-send write). Equal on every
    #: fault-free tick is what dissolves README open defect #1 (a charged row that
    #: composition omitted). A crash breaks the equality by construction, which is
    #: the crash bound's whole job, so the check is asserted only on clean runs.
    charged_vs_written: list[tuple[frozenset, frozenset]] = field(default_factory=list)

    def transitions(self, transition: str) -> list[int]:
        return [rid for rid, t in self.audit if t == transition]


# ------------------------------------------------------------------ the selector

def select_delivery(rows, now, limit=TICK_DELIVERY_LIMIT):
    """D2's delivery selector, including the `due_at` conjunct and the per-tick cap."""
    due = [
        r
        for r in rows
        if r.status == PENDING
        and r.next_attempt_at is not None
        and r.next_attempt_at <= now
        and r.due_at <= now  # the conjunct: never deliver before the due instant
    ]
    due.sort(key=lambda r: (r.due_at, r.id))
    return due[:limit]


def select_report(rows, now):
    """D2's report selector. Uncapped: the summary is one message however many rows."""
    rep = [
        r
        for r in rows
        if r.status in REPORTABLE
        and r.reported_at is None
        and r.next_attempt_at is not None
        and r.next_attempt_at <= now
    ]
    rep.sort(key=lambda r: (r.due_at, r.id))
    return rep


def selected(rows, now):
    """Everything the selector would still pick up — the quiescence predicate."""
    return select_delivery(rows, now, limit=10**9) + select_report(rows, now)


# ------------------------------------------------------------------ one tick

def tick(
    rows,
    now,
    *,
    reminder_channel,
    summary_channel,
    crash_at=None,
    stats,
    interject=None,
):
    """One scheduler tick. `now` is captured by the caller, once (D1)."""
    charged: set[int] = set()
    written: set[int] = set()

    # ---------------- pre-work transaction (one scope, no await inside) --------
    txn = Txn(stats)
    try:
        if crash_at == "pre-work":
            raise Crash()

        # 1. Grace transitions for EVERY past-grace pending row, selected or not.
        past_grace = [
            r for r in rows if r.status == PENDING and now > r.due_at + LATE_GRACE
        ]
        if past_grace and crash_at == "grace":
            raise Crash()
        for r in past_grace:
            txn.set(
                r,
                status=MISSED,
                send_attempts=0,
                next_attempt_at=now,
                reported_at=None,
            )
            txn.audit(r, MISSED)
            stats.grace_transitions += 1

        # 2. Selection against the POST-GRACE state.
        delivery = select_delivery(rows, now)
        report = select_report(rows, now)

        # 3. Increments for exactly the selected rows, then both pre-work bounds.
        for r in delivery + report:
            txn.set(r, send_attempts=r.send_attempts + 1)
            charged.add(r.id)

        abandoned_now: list[Row] = []
        for r in list(delivery):
            if r.send_attempts >= CRASH_ATTEMPT_LIMIT:
                # A pending row at the limit exits to `abandoned`, counters cleared,
                # `next_attempt_at` set (it decides report eligibility), `reported_at`
                # left NULL so THIS tick's summary names it — without a second
                # increment.
                txn.set(
                    r,
                    status=ABANDONED,
                    send_attempts=0,
                    next_attempt_at=now,
                )
                txn.audit(r, ABANDONED)
                delivery.remove(r)
                abandoned_now.append(r)
        for r in list(report):
            if r.send_attempts >= CRASH_ATTEMPT_LIMIT:
                # The report path's crash give-up: terminate reporting with an error
                # log. No audit transition — nothing about the row changed except
                # that Henk stopped trying to tell the owner.
                txn.set(r, reported_at=now, send_attempts=0, gave_up_via="crash")
                stats.error_logs += 1
                report.remove(r)
                written.add(r.id)

        if crash_at == "pre-work-commit":
            raise Crash()
    except Crash:
        txn.rollback()
        stats.crashes += 1
        stats.error_logs += 1
        return
    txn.commit()

    if interject is not None:
        # A commit landing from elsewhere between the pre-work transaction and the
        # dispatch loop — a `/reminders cancel` is the case that matters.
        interject(rows, now)

    # ---------------- sends: no transaction is held across any of them ---------
    for index, r in enumerate(delivery):
        try:
            if crash_at == f"send{index}":
                raise Crash()
            # The pre-send status re-read (D4): the selection is stale by however
            # long the earlier sends in this tick held the send lock.
            if r.status != PENDING:
                continue
            late = now > r.due_at + LATE_DELIVERY_THRESHOLD
            # A first-or-crash attempt is recognizable without new state: its
            # `next_attempt_at` still equals `due_at`. A floor retry's is later, and
            # passes NO notice (D4).
            notice = r.next_attempt_at is not None and r.next_attempt_at <= r.due_at
            stats.reminder_sends += 1
            outcome = reminder_channel(r, now, notice=notice)
            if crash_at == f"post{index}":
                raise Crash()

            post = Txn(stats)
            try:
                if outcome in (OUT_DELIVERED, OUT_PARTIAL):
                    status = DELIVERED_LATE if late else DELIVERED
                    post.set(r, status=status, delivered_at=now, send_attempts=0)
                    post.audit(r, status)
                    if outcome == OUT_PARTIAL:
                        # Recorded as delivered with `detail: "partial"` and an error
                        # log; never re-sent (D4's asymmetry, reminder side).
                        stats.error_logs += 1
                else:
                    post.set(r, next_attempt_at=now + RETRY_FLOOR, send_attempts=0)
                    stats.error_logs += 1
                if crash_at == f"post-commit{index}":
                    raise Crash()
            except Crash:
                post.rollback()
                raise
            post.commit()
            written.add(r.id)
        except Crash:
            stats.crashes += 1
            stats.error_logs += 1
            return

    # ---------------- the catch-up summary: one message, uncapped -------------
    named = report + abandoned_now
    named.sort(key=lambda r: (r.due_at, r.id))  # selection order, oldest-due first
    if named:
        try:
            if crash_at == "send-summary":
                raise Crash()
            stats.summary_sends += 1
            outcome = summary_channel(named, now)
            for r in named:
                # Harness truth, not a column: the row WAS named in a summary that
                # was actually attempted. Set here, outside the post-send write, so
                # the conservation property does not read the same expression the
                # give-up exit writes.
                r.named_in_attempted_summary = True
            if crash_at == "post-summary":
                raise Crash()

            post = Txn(stats)
            try:
                for r in named:
                    post.set(r, send_attempts=0)
                    if outcome == OUT_DELIVERED:
                        post.set(r, reported_at=now)
                    elif outcome == OUT_PARTIAL and now > (
                        r.due_at + LATE_GRACE + REPORT_HORIZON
                    ):
                        # The report horizon, evaluated HERE and nowhere else: in the
                        # post-send write of an ATTEMPTED summary, on a partial
                        # outcome only (D5). A failed summary gives up on nothing.
                        post.set(r, reported_at=now, gave_up_via="horizon")
                        stats.error_logs += 1
                    else:
                        post.set(r, next_attempt_at=now + RETRY_FLOOR)
                        if outcome == OUT_FAILED:
                            stats.error_logs += 1
                if crash_at == "post-commit-summary":
                    raise Crash()
            except Crash:
                post.rollback()
                raise
            post.commit()
            written.update(r.id for r in named)
        except Crash:
            stats.crashes += 1
            stats.error_logs += 1
            return

    stats.charged_vs_written.append((frozenset(charged), frozenset(written)))


def run(
    rows,
    *,
    reminder_channel=None,
    summary_channel=None,
    crash_every=None,
    ticks=400,
    stats=None,
    start=0.0,
    interject=None,
):
    stats = stats if stats is not None else Stats()
    reminder_channel = reminder_channel or always_reminder(OUT_DELIVERED)
    summary_channel = summary_channel or always_summary(OUT_DELIVERED)
    now = start
    for _ in range(ticks):
        now += POLL_INTERVAL
        tick(
            rows,
            now,
            reminder_channel=reminder_channel,
            summary_channel=summary_channel,
            crash_at=crash_every,
            stats=stats,
            interject=interject,
        )
    return now, stats


# ------------------------------------------------------------------ doubles

def always_reminder(outcome, acked=None):
    """A reminder is one text: its delivered head IS the reminder, so a partial acks."""

    def send(row, now, *, notice):
        if outcome in (OUT_DELIVERED, OUT_PARTIAL) and acked is not None:
            acked.append(row.id)
        return outcome

    return send


def always_summary(outcome, acked=None, *, head_fraction=0.5):
    """A summary's partial delivers only its HEAD: the lost tail is other reminders.

    `head_fraction` is what makes the summary's partial materially different from a
    reminder's — the rows in the tail were never told to the owner, which is the whole
    reason `reported_at` is withheld on a partial (D4).
    """

    def send(named, now):
        if acked is not None:
            if outcome == OUT_DELIVERED:
                acked.extend(r.id for r in named)
            elif outcome == OUT_PARTIAL:
                cut = max(1, int(len(named) * head_fraction))
                acked.extend(r.id for r in named[:cut])
        return outcome

    return send


def recovering_summary(acked, *, down_until):
    """Fails while the channel is down, then delivers. Drives the outage property."""

    def send(named, now):
        if now < down_until:
            return OUT_FAILED
        acked.extend(r.id for r in named)
        return OUT_DELIVERED

    return send


def missed_row(rid, due_at, *, reported_at=None):
    """A row seeded straight into `missed`, as a restart would find it.

    Seeding the report path directly rather than driving it through grace is
    deliberate for the properties that are ABOUT the report path: it makes the
    horizon anchor (`due_at + grace + horizon`) reachable without first spending
    a grace window of ticks, and it is the state a restart genuinely finds.
    """
    return Row(rid, due_at=due_at, status=MISSED, next_attempt_at=due_at,
               reported_at=reported_at)


# ------------------------------------------------------------------ properties

#: Derived: a delivery row needs at most CRASH_ATTEMPT_LIMIT ticks to exhaust the
#: delivery crash budget, then at most CRASH_ATTEMPT_LIMIT more for the report budget
#: — each of those ticks eligible immediately, since the exits set
#: `next_attempt_at = now`. Plus slack for the grace tick and the summary tick.
N_TERMINATION = 2 * CRASH_ATTEMPT_LIMIT + 4

#: Ticks needed for the horizon to be reachable from a row due at t=0:
#: (grace + horizon) / poll, plus slack.
N_HORIZON = int((LATE_GRACE + REPORT_HORIZON) / POLL_INTERVAL) + 200


def prop_termination_under_crash():
    """A crash at ANY stage from the pre-work commit onward terminates within N ticks.

    Rows are backdated past the grace window so the grace path is actually reachable,
    and kept under TICK_DELIVERY_LIMIT so pacing is not what the bound measures.
    """
    out = {}
    # `pre-work`, `grace` and `pre-work-commit` are deliberately ABSENT: all three
    # sit before the pre-work commit, so nothing is ever persisted and no bound can
    # be reached. Filing them here would assert the wrong property — they belong to
    # prop_detectability_before_the_pre_work_commit, which is where they are checked.
    stages = (
        "send0",
        "post0",
        "post-commit0",
        "send-summary",
        "post-summary",
        "post-commit-summary",
    )
    for stage in stages:
        rows = [Row(i, due_at=-10 * LATE_GRACE) for i in range(6)]
        now, stats = run(
            rows, crash_every=stage, ticks=N_TERMINATION, stats=Stats()
        )
        left = selected(rows, now + 10**7)
        out[stage] = (
            "terminates"
            if not left
            else f"{len(left)} left (attempts={[r.send_attempts for r in left]})"
        )
    return out


def prop_termination_under_partial_summary():
    """The HORIZON property: a deterministically partial summary terminates.

    Every summary send returns `partial` forever. Each named row must reach the
    horizon give-up — written in the post-send write of an ATTEMPTED summary — and
    the total number of summary sends must be bounded.

    Two arms, because the horizon's anchor (`due_at + grace`) coincides with the
    moment of reportability only on the first:
      - **fresh**: a pending row whose sends fail, so grace runs on time and it
        becomes reportable at `due_at + grace`. Exposure ≈ horizon / floor.
      - **grace never ran**: a row already `missed` at `due_at` (what an `abandoned`
        row's anchor looks like, since nothing waits a grace window to abandon).
        Exposure ≈ (grace + horizon) / floor — twice as many, still bounded.
    """
    out = {}

    fresh = [Row(i, due_at=0.0) for i in range(4)]
    now, stats = run(
        fresh,
        reminder_channel=always_reminder(OUT_FAILED),
        summary_channel=always_summary(OUT_PARTIAL),
        ticks=N_HORIZON,
        stats=Stats(),
    )
    out["fresh"] = {
        "terminates": not selected(fresh, now + 10**7),
        "summary_sends": stats.summary_sends,
        "bound": int(REPORT_HORIZON / RETRY_FLOOR),
        "within_bound": stats.summary_sends <= REPORT_HORIZON / RETRY_FLOOR + 2,
        "gave_up_via": sorted({r.gave_up_via for r in fresh}),
        "all_named_before_give_up": all(
            r.named_in_attempted_summary for r in fresh if r.gave_up_via == "horizon"
        ),
        "error_logged_per_give_up": stats.error_logs >= len(fresh),
    }

    # The same anchor arithmetic, reached the way production reaches it: a row that
    # exits to `abandoned` via the crash bound becomes reportable within
    # CRASH_ATTEMPT_LIMIT ticks of its due instant, because nothing waits a grace
    # window to abandon. Driven rather than seeded so the exposure number below is
    # the model's output and not this comment's arithmetic.
    abandoned = [Row(i, due_at=0.0) for i in range(4)]
    now, stats = run(
        abandoned,
        reminder_channel=always_reminder(OUT_DELIVERED),
        summary_channel=always_summary(OUT_PARTIAL),
        crash_every="post0",
        ticks=N_HORIZON,
        stats=Stats(),
    )
    out["abandoned_anchor"] = {
        "status": sorted({r.status for r in abandoned}),
        "terminates": not selected(abandoned, now + 10**7),
        "summary_sends": stats.summary_sends,
        "missed_row_bound": int(REPORT_HORIZON / RETRY_FLOOR),
        "actual_bound": int((LATE_GRACE + REPORT_HORIZON) / RETRY_FLOOR),
        "exceeds_the_missed_row_bound": stats.summary_sends
        > REPORT_HORIZON / RETRY_FLOOR + 1,
        "within_the_actual_bound": stats.summary_sends
        <= (LATE_GRACE + REPORT_HORIZON) / RETRY_FLOOR + 1,
        "gave_up_via": sorted({r.gave_up_via for r in abandoned}),
    }

    seeded = [missed_row(i, 0.0) for i in range(4)]
    now, stats = run(
        seeded,
        summary_channel=always_summary(OUT_PARTIAL),
        ticks=N_HORIZON,
        stats=Stats(),
    )
    out["grace_never_ran"] = {
        "terminates": not selected(seeded, now + 10**7),
        "summary_sends": stats.summary_sends,
        "bound": int((LATE_GRACE + REPORT_HORIZON) / RETRY_FLOOR),
        "within_bound": stats.summary_sends
        <= (LATE_GRACE + REPORT_HORIZON) / RETRY_FLOOR + 2,
        "gave_up_via": sorted({r.gave_up_via for r in seeded}),
        "all_named_before_give_up": all(
            r.named_in_attempted_summary for r in seeded if r.gave_up_via == "horizon"
        ),
    }
    return out


def prop_stale_rows_are_named_before_any_give_up():
    """A row that arrives ALREADY stale is named once before the horizon can fire.

    This is the property that decides WHERE the horizon lives. Evaluated post-send it
    holds; moved into the pre-work transaction it fails, because the row is past
    `due_at + grace + horizon` on the very tick it becomes reportable.
    """
    stale = 7 * 86400  # a week older than the grace window plus the horizon
    rows = [Row(i, due_at=-stale) for i in range(3)]
    acked: list[int] = []
    now, stats = run(
        rows,
        summary_channel=always_summary(OUT_PARTIAL, acked),
        ticks=5,
        stats=Stats(),
    )
    return {
        "named": all(r.named_in_attempted_summary for r in rows),
        "summary_sends": stats.summary_sends,
        "all_reported": all(r.reported_at is not None for r in rows),
        "gave_up_via": sorted({r.gave_up_via for r in rows}),
        "terminates": not selected(rows, now + 10**7),
    }


def prop_channel_outage_never_forfeits_the_report():
    """A WHOLLY failed summary is never given up on a channel outcome.

    The channel is down far past the horizon, then recovers: every unreported row must
    still be named and marked. This is the case where termination is IMPOSSIBLE by
    design, so the property is detectability (an error log per attempt) plus the
    guarantee that nothing was retired while it was down.
    """
    rows = [missed_row(i, 0.0) for i in range(3)]
    down_until = LATE_GRACE + 3 * REPORT_HORIZON
    acked: list[int] = []
    now, stats = run(
        rows,
        summary_channel=recovering_summary(acked, down_until=down_until),
        ticks=int(down_until / POLL_INTERVAL) + 200,
        stats=Stats(),
    )
    return {
        "nothing_given_up": all(r.gave_up_via is None for r in rows),
        "all_reported_after_recovery": all(r.reported_at is not None for r in rows),
        "all_acked": sorted(set(acked)) == sorted(r.id for r in rows),
        "error_logs": stats.error_logs,
        "quiescent_after": not selected(rows, now + 10**7),
    }


def prop_detectability_before_the_pre_work_commit():
    """A fault before the pre-work COMMIT is unbounded but LOUD, and loses nothing.

    Both pre-commit stages are checked — the selector/arithmetic region and the grace
    transition — because both are inside the transaction whose commit they prevent.
    """
    out = {}
    for stage in ("pre-work", "grace", "pre-work-commit"):
        rows = [Row(i, due_at=-10 * LATE_GRACE) for i in range(4)]
        acked: list[int] = []
        _, stats = run(
            rows,
            reminder_channel=always_reminder(OUT_DELIVERED, acked),
            crash_every=stage,
            ticks=50,
            stats=Stats(),
        )
        out[stage] = {
            "logs_per_tick": stats.error_logs / 50,
            "owner_visible": len(acked),
            "still_selected": bool(selected(rows, 10**9)),
            "nothing_committed": all(
                r.status == PENDING and r.send_attempts == 0 for r in rows
            ),
            "no_audit_records": stats.audit == [],
        }
    return out


def prop_quiescence_under_channel_failure():
    """Under a permanently failing channel, sends are paced by the floor, not the tick.

    A `pending` reminder rides the floor until grace, then becomes `missed` and joins
    a summary that retries on the floor forever. Nothing is owner-visible and
    `send_attempts` never grows, because every post-send write clears it — which is
    what keeps the crash bound from firing on channel failure.
    """
    rows = [Row(0, due_at=0.0)]
    acked: list[int] = []
    ticks = int((LATE_GRACE + 5 * REPORT_HORIZON) / POLL_INTERVAL)
    now, stats = run(
        rows,
        reminder_channel=always_reminder(OUT_FAILED, acked),
        summary_channel=always_summary(OUT_FAILED, acked),
        ticks=ticks,
        stats=Stats(),
    )
    row = rows[0]
    span = now - POLL_INTERVAL
    return {
        "status": row.status,
        "never_abandoned_for_channel_failure": row.status != ABANDONED,
        "attempts_never_grew": row.send_attempts == 0,
        "owner_visible": len(acked),
        "still_selected": bool(selected(rows, 10**9)),
        "sends": stats.reminder_sends + stats.summary_sends,
        "floor_paced": (stats.reminder_sends + stats.summary_sends)
        <= span / RETRY_FLOOR + 3,
        "logs_per_send": round(
            stats.error_logs / max(1, stats.reminder_sends + stats.summary_sends), 2
        ),
    }


def prop_conservation(bug=None):
    """Success set == acknowledged set, with truth taken from the doubles.

    Three claims in one, because they are one invariant seen from three sides:
      - every row recorded delivered was acknowledged by the reminder double;
      - every row marked reported WITHOUT a give-up was acknowledged by the summary
        double (so `reported_at` never asserts the owner was told when they were not);
      - every row that reached a terminal REPORT state was either delivered or named
        in at least one attempted summary — with the ONE stated exception, the
        pre-work crash-limit give-up, which is the residual design D3 prices.
    """
    # Deliberately mixed ages: the first half is overdue but WITHIN grace, so the
    # delivery half of the machine actually runs; the second half is past grace, so
    # the report half does. A single age exercises only one of them — the first draft
    # of this property backdated everything and silently proved nothing about
    # delivery, since every row was `missed` before the first send.
    # The second half is past grace but NOT past `grace + horizon`: at
    # `-2 * LATE_GRACE` (the obvious choice) every row is horizon-eligible on its
    # first partial summary, every `reported_at` is a give-up, and the
    # "reported never lies" half of the property goes vacuous at zero rows.
    rows = [Row(i, due_at=-600.0 - i) for i in range(12)] + [
        Row(12 + i, due_at=-(LATE_GRACE + 600.0) - i) for i in range(12)
    ]
    r_acked: list[int] = []
    s_acked: list[int] = []
    reminder_channel = _alternating_reminder(r_acked)
    summary_channel = _alternating_summary(s_acked)
    if bug == "outcome-lies":
        # Oracle check: the outcome variable lies, and the write believes it. The
        # doubles never record, so conservation must FAIL — proving the property is
        # not tautological.
        reminder_channel = always_reminder(OUT_DELIVERED)
        summary_channel = always_summary(OUT_DELIVERED)
    now, stats = run(
        rows,
        reminder_channel=reminder_channel,
        summary_channel=summary_channel,
        ticks=400,
        stats=Stats(),
    )
    delivered = {r.id for r in rows if r.status in TERMINAL_DELIVERY}
    reported_honestly = {
        r.id for r in rows if r.reported_at is not None and r.gave_up_via is None
    }
    unnamed_terminal = [
        (r.id, r.gave_up_via)
        for r in rows
        if r.reported_at is not None
        and not r.named_in_attempted_summary
        and r.status not in TERMINAL_DELIVERY
    ]
    return {
        "delivered_ran": len(delivered),
        # Subset, not equality, and in this direction on purpose: a row acked in the
        # HEAD of a partial summary was genuinely told to the owner but is correctly
        # left unreported (the tail rows were not, and the summary marks all or
        # none). So acked ⊋ reported is expected; reported ⊄ acked would mean
        # `reported_at` asserting the owner was told when they were not.
        "delivered_all_acked": delivered <= set(r_acked),
        "reported_all_acked": reported_honestly <= set(s_acked),
        "reported_ran": len(reported_honestly),
        "max_never_exceeded": all(
            r.send_attempts <= CRASH_ATTEMPT_LIMIT for r in rows
        ),
        # Every unnamed terminal report row must be a crash-limit give-up: the one
        # exception the design states. A horizon give-up here would be a defect.
        "unnamed_terminal_are_crash_give_ups": all(
            via == "crash" for _, via in unnamed_terminal
        ),
        "unnamed_terminal_count": len(unnamed_terminal),
        "quiescent": not selected(rows, now + 10**7),
    }


def _alternating_reminder(acked):
    n = {"i": 0}

    def send(row, now, *, notice):
        n["i"] += 1
        outcome = (OUT_DELIVERED, OUT_FAILED, OUT_PARTIAL)[n["i"] % 3]
        if outcome in (OUT_DELIVERED, OUT_PARTIAL):
            acked.append(row.id)
        return outcome

    return send


def _alternating_summary(acked):
    n = {"i": 0}

    def send(named, now):
        n["i"] += 1
        outcome = (OUT_DELIVERED, OUT_FAILED, OUT_PARTIAL)[n["i"] % 3]
        if outcome == OUT_DELIVERED:
            acked.extend(r.id for r in named)
        elif outcome == OUT_PARTIAL:
            cut = max(1, len(named) // 2)
            acked.extend(r.id for r in named[:cut])
        return outcome

    return send


def prop_partial_handling():
    """The asymmetry, both sides, in one run each.

    A reminder's partial is mostly-delivered content -> `delivered`, never re-sent.
    A summary's partial is undelivered ROWS -> nothing marked, floor retry.
    """
    reminders = [Row(i, due_at=0.0) for i in range(3)]
    _, r_stats = run(
        reminders,
        reminder_channel=always_reminder(OUT_PARTIAL),
        ticks=4,
        stats=Stats(),
    )
    missed = [missed_row(i, 0.0) for i in range(3)]
    _, s_stats = run(
        missed,
        summary_channel=always_summary(OUT_PARTIAL),
        ticks=2,
        stats=Stats(),
    )
    return {
        "reminder_partial_is_delivered": all(
            r.status == DELIVERED for r in reminders
        ),
        "reminder_never_resent": r_stats.reminder_sends == len(reminders),
        "reminder_partial_error_logged": r_stats.error_logs >= len(reminders),
        "summary_partial_marks_nothing": all(r.reported_at is None for r in missed),
        "summary_retries_on_the_floor": all(
            r.next_attempt_at == POLL_INTERVAL + RETRY_FLOOR for r in missed
        ),
        "summary_carried_no_notice": True,  # structural: the summary send takes none
    }


def prop_no_early_delivery():
    """The `due_at` conjunct: an eligible `next_attempt_at` never delivers early."""
    week = 7 * 86400
    rows = [Row(0, due_at=week)]
    rows[0].next_attempt_at = 0.0  # the schema default's meaning: eligible NOW
    acked: list[int] = []
    _, stats = run(
        rows,
        reminder_channel=always_reminder(OUT_DELIVERED, acked),
        ticks=200,
        stats=Stats(),
    )
    return {
        "nothing_sent": stats.reminder_sends == 0,
        "owner_visible": len(acked),
        "still_pending": rows[0].status == PENDING,
        "uncharged": rows[0].send_attempts == 0,
    }


def prop_pacing():
    """A within-grace backlog is paced, and unselected rows are neither charged nor written."""
    rows = [Row(i, due_at=-3600.0 - i) for i in range(100)]
    rows.sort(key=lambda r: r.due_at)
    acked: list[int] = []
    per_tick: list[int] = []
    stats = Stats()
    now = 0.0
    for _ in range(30):
        now += POLL_INTERVAL
        before = len(acked)
        tick(
            rows,
            now,
            reminder_channel=always_reminder(OUT_DELIVERED, acked),
            summary_channel=always_summary(OUT_DELIVERED),
            stats=stats,
            crash_at=None,
        )
        per_tick.append(len(acked) - before)
    return {
        "max_per_tick": max(per_tick),
        "within_limit": max(per_tick) <= TICK_DELIVERY_LIMIT,
        "all_delivered": all(r.status in TERMINAL_DELIVERY for r in rows),
        "none_dropped": len(set(acked)) == len(rows),
        "delivered_oldest_first": acked == sorted(
            acked, key=lambda i: next(r.due_at for r in rows if r.id == i)
        ),
        "ticks_to_drain": next(
            (i + 1 for i, n in enumerate(per_tick) if sum(per_tick[: i + 1]) == 100),
            None,
        ),
    }


def prop_charged_implies_written():
    """No row is ever attempt-charged without a post-send write (README defect #1).

    Cut #1 removed the report item bound, so composition can no longer omit a charged
    row. On every fault-free tick the charged set and the written set are equal.
    """
    rows = [Row(i, due_at=-2 * LATE_GRACE) for i in range(40)]
    _, stats = run(
        rows,
        reminder_channel=_alternating_reminder([]),
        summary_channel=_alternating_summary([]),
        ticks=300,
        stats=Stats(),
    )
    mismatches = [
        (sorted(c), sorted(w)) for c, w in stats.charged_vs_written if c != w
    ]
    return {"ticks_checked": len(stats.charged_vs_written),
            "mismatches": len(mismatches),
            "first": mismatches[0] if mismatches else None}


def prop_delivered_summary_whose_write_is_lost():
    """The composition the review could only reason about.

    A summary whose send returns `delivered` but whose post-send transaction never
    commits must re-loop into the crash bound and lose no row — the owner sees
    duplicates, which is the settled direction, and the rows terminate.
    """
    rows = [missed_row(i, 0.0) for i in range(3)]
    acked: list[int] = []
    now, stats = run(
        rows,
        summary_channel=always_summary(OUT_DELIVERED, acked),
        crash_every="post-commit-summary",
        ticks=N_TERMINATION,
        stats=Stats(),
    )
    return {
        "terminates": not selected(rows, now + 10**7),
        "no_row_lost": all(r.reported_at is not None for r in rows),
        "owner_saw_them": sorted(set(acked)) == sorted(r.id for r in rows),
        "duplicates": stats.summary_sends,
        "bounded_by_crash_limit": stats.summary_sends <= CRASH_ATTEMPT_LIMIT,
        "gave_up_via": sorted({r.gave_up_via for r in rows}),
        "named_first": all(r.named_in_attempted_summary for r in rows),
    }


def prop_report_crash_give_up_can_precede_any_naming():
    """The ONE residual: a report row can be retired without ever being named.

    Repeated process death at the reporting stage — killed after the pre-work commit
    and before the summary is dispatched — charges the row on every tick and never
    names it. At `CRASH_ATTEMPT_LIMIT` the pre-work give-up writes `reported_at` with
    an error log, for a row no summary ever carried.

    This is inherent to the settled crash-maximum placement (design D3): the bound
    must be pre-work, because a crash is what prevents the post-send write. Design
    D1's Goals price it explicitly ("a report row retired by the pre-work crash-limit
    give-up after repeated process deaths at the reporting stage"). The HORIZON
    give-up carries no such residual, which is the whole reason it lives post-send.

    Stated as a property rather than a comment so the exception is asserted where the
    invariant is, and so a future change that widens it is caught here.
    """
    rows = [missed_row(i, 0.0) for i in range(3)]
    now, stats = run(
        rows,
        summary_channel=always_summary(OUT_DELIVERED),
        crash_every="send-summary",
        ticks=N_TERMINATION,
        stats=Stats(),
    )
    return {
        "terminates": not selected(rows, now + 10**7),
        "summaries_attempted": stats.summary_sends,
        "never_named": all(not r.named_in_attempted_summary for r in rows),
        "retired": all(r.reported_at is not None for r in rows),
        "gave_up_via": sorted({r.gave_up_via for r in rows}),
        "error_logged": stats.error_logs >= len(rows),
        "no_audit_transition_for_the_give_up": stats.transitions("reported") == [],
    }


def prop_crash_between_send_and_mark():
    """Kill after the send, before the mark: redelivered within the bound, never lost."""
    rows = [Row(0, due_at=0.0)]
    acked: list[int] = []
    now, stats = run(
        rows,
        reminder_channel=always_reminder(OUT_DELIVERED, acked),
        crash_every="post0",
        ticks=N_TERMINATION,
        stats=Stats(),
    )
    return {
        "duplicates": len(acked),
        "bounded_by_crash_limit": len(acked) <= CRASH_ATTEMPT_LIMIT,
        "never_silent": len(acked) >= 1,
        "final_status": rows[0].status,
        "named_in_summary": rows[0].named_in_attempted_summary,
        "terminates": not selected(rows, now + 10**7),
    }


def prop_notice_is_not_repeated():
    """The notice rides first-or-crash attempts only, never a floor retry (D4)."""
    notices: list[int] = []

    def send(row, now, *, notice):
        if notice:
            notices.append(row.id)
        return OUT_FAILED

    rows = [Row(0, due_at=0.0)]
    ticks = int(LATE_GRACE / POLL_INTERVAL) + 10
    _, stats = run(
        rows, reminder_channel=send, ticks=ticks, stats=Stats()
    )
    return {
        "sends": stats.reminder_sends,
        "notices": len(notices),
        "at_most_crash_limit": len(notices) <= CRASH_ATTEMPT_LIMIT,
        "first_attempt_carried_one": len(notices) >= 1,
        "ended_missed": rows[0].status == MISSED,
    }


def prop_cancellation_between_selection_and_dispatch():
    """A cancellation committing after selection is skipped by the pre-send re-read."""
    rows = [Row(i, due_at=0.0) for i in range(3)]
    acked: list[int] = []

    def cancel_row_1(rs, now):
        rs[1].status = CANCELLED

    _, stats = run(
        rows,
        reminder_channel=always_reminder(OUT_DELIVERED, acked),
        ticks=2,
        stats=Stats(),
        interject=cancel_row_1,
    )
    return {
        "cancelled_not_sent": 1 not in acked,
        "others_delivered": sorted(set(acked)) == [0, 2],
        "cancelled_status": rows[1].status,
        "sends": stats.reminder_sends,
    }


def prop_composition_names_exactly_the_selected_set():
    """A row cooling on the floor is not renamed early; abandoned-this-tick joins."""
    # One row already missed and eligible, one missed but cooling on the floor.
    eligible = missed_row(0, 0.0)
    cooling = Row(1, due_at=0.0, status=MISSED, next_attempt_at=10**6)
    # One pending row at the crash limit, which abandons in this tick's pre-work.
    at_limit = Row(2, due_at=0.0, send_attempts=CRASH_ATTEMPT_LIMIT - 1)
    rows = [eligible, cooling, at_limit]
    composed: list[list[int]] = []

    def summary(named, now):
        composed.append([r.id for r in named])
        return OUT_DELIVERED

    tick(
        rows,
        POLL_INTERVAL,
        reminder_channel=always_reminder(OUT_DELIVERED),
        summary_channel=summary,
        stats=Stats(),
    )
    return {
        "composed": composed,
        "names_eligible_and_abandoned": composed == [[0, 2]],
        "cooling_row_untouched": cooling.reported_at is None
        and cooling.send_attempts == 0,
        "abandoned_status": at_limit.status,
    }


if __name__ == "__main__":
    print("N_TERMINATION =", N_TERMINATION, " N_HORIZON =", N_HORIZON)
    print()
    print("TERMINATION/crash      ", prop_termination_under_crash())
    print("TERMINATION/partial-sum", prop_termination_under_partial_summary())
    print("STALE-NAMED-FIRST      ", prop_stale_rows_are_named_before_any_give_up())
    print("OUTAGE-NEVER-FORFEITS  ", prop_channel_outage_never_forfeits_the_report())
    print("DETECTABILITY          ", prop_detectability_before_the_pre_work_commit())
    print("QUIESCENCE             ", prop_quiescence_under_channel_failure())
    print("CONSERVATION           ", prop_conservation())
    print("  oracle (outcome lies)", prop_conservation(bug="outcome-lies"))
    print("PARTIAL-HANDLING       ", prop_partial_handling())
    print("NO-EARLY-DELIVERY      ", prop_no_early_delivery())
    print("PACING                 ", prop_pacing())
    print("CHARGED=>WRITTEN       ", prop_charged_implies_written())
    print("DELIVERED-WRITE-LOST   ", prop_delivered_summary_whose_write_is_lost())
    print("RESIDUAL/report-crash  ", prop_report_crash_give_up_can_precede_any_naming())
    print("CRASH-SEND-THEN-MARK   ", prop_crash_between_send_and_mark())
    print("NOTICE-NOT-REPEATED    ", prop_notice_is_not_repeated())
    print("CANCEL-BEFORE-DISPATCH ", prop_cancellation_between_selection_and_dispatch())
    print("COMPOSITION-SET        ", prop_composition_names_exactly_the_selected_set())
