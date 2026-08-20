# Apply record — reminder-delivery

> ## State, and what is left — read this first
>
> **Groups 1–10 are COMPLETE and green. Group 11 stops at 11.1.** Applied 2026-08-20.
>
> | | |
> |---|---|
> | suite | **1503 passed, 12 deselected** (baseline 1269 → **+234**) |
> | `pytest -m dst_sweep` | 12 passed |
> | `openspec validate --strict` | passes |
> | commits | **11**, every one verified green **in isolation** (10.4) |
> | tasks | 32 complete, 1 partial (11.1), 5 remaining (all gated on the owner) |
>
> **What is left, and why none of it is code:**
>
> - **11.1 is partial.** Both standing watches are empty and the latency harvest was
>   re-run (unchanged — there have been no sends since it was first taken). The one
>   measurement it asks for and could NOT be taken is rp5's open inbox item count: it
>   needs `docker exec`, which rp5 puts behind PASSWD sudo, and this session has no tty.
>   The exact command is recorded below. It prices a recommended follow-up and does not
>   gate anything.
> - **11.2 was not attempted, and not because of caution.** `sudo -n docker compose`
>   returns "a password is required", so the rebuild is unreachable from here. Two further
>   blockers, each sufficient alone: the eleven commits are unpushed (publishing to a
>   publication-bound repo is the owner's call), and one of 11.2's own checks — confirming
>   a long reply's chunks still arrive in order — needs a human reading a Signal thread.
>   That check matters, because the send lock is this deploy's **only** unflagged
>   behaviour change.
> - **11.3–11.6** are the hard stop and what follows it: the rp5 `config.yaml` edit
>   (`owner.timezone` + `reminders.enabled: true`), the live end-to-end pass, the README's
>   deferred tools-table pass, and archive.
>
> **Four findings moved the spec or the schema during this apply**, three of them from the
> model before any code existed. Each is written up below with the property that produced
> it: the grace requirement's unqualified SHALL (falsified by a reachable crash loop), two
> wrong numbers for the report horizon's per-row attempt bound, and a collision between
> the design's `detail: "partial"` and an existing no-free-text guard.

Produced at `/opsx:apply` time by re-running the checks the task list requires, because the
suite moves. A later reviewer should be able to check each list against the diff.

---

## Group 1 — the model, updated with the spec (design D11)

### 1.1 The rewrite

`openspec/changes/reminder-delivery/notes/verify_selector_invariants.py` replaces the
`reminders` draft's model, which modelled the **pre-cut** design. What came out, and what
went in:

| removed (pre-cut machinery) | replaced by |
|---|---|
| `SCHEDULE = [30, 60, 120, 300, 900, 3600, 14400]` + `backoff()` | one `RETRY_FLOOR = 900` (cut #3) |
| `unconfirmed_sends` column | nothing — the two-budget separation needs one counter |
| `terminal_at` column | nothing (cut #2) |
| `compose()` greedy measure-before-add + per-batch sends | one message per due reminder, then one uncapped summary (cut #4, cut #1) |
| report item bound / "and N more" | uncapped composition (cut #1, which dissolves README open defect #1) |
| single-column selector | `next_attempt_at <= now` **and** `due_at <= now` (D2's conjunct) |
| unbounded delivery per tick | `TICK_DELIVERY_LIMIT = 10`, oldest-due first, unselected rows uncharged |
| partial handled as failed for the whole batch | the D4 asymmetry: reminder partial → delivered; summary partial → marks nothing |
| one give-up (crash max) | **both** give-ups, in their stated places: crash max in pre-work, report horizon in the post-send write |

Structural changes that matter to the properties, not just the state:

- **Transactions are modelled with an undo log** (`Txn`), so a scope reads its own writes
  the way `Store.transaction()` does and a fault before the commit restores the prior state
  exactly. The old model raised *before* mutating, which cannot distinguish "crashed before
  the commit" from "crashed after it" — and that distinction is the entire argument for
  where each give-up lives.
- **Audit appends are queued inside the transaction and flushed after the commit**, which is
  D3's stated ordering. A crash between the two loses a receipt rather than inventing one,
  and `no_audit_records` asserts it.
- **The channel doubles take `now`**, so the outage property can bring the channel back up at
  a stated instant instead of counting calls.
- **Harness truth is recorded outside the code under test.** `named_in_attempted_summary` is
  set by the tick's send loop, never by the post-send write, so the conservation property
  cannot be satisfied by the same expression that would be wrong. The oracle arm
  (`prop_conservation(bug="outcome-lies")`) confirms it discriminates: `delivered_all_acked`
  and `reported_all_acked` both go **False** under a lying outcome variable.

The stated-non-coverage header was rewritten for this design: no chunking or byte lengths
(so D5's "oldest-first puts horizon-eligible rows in the head chunks" is **not** verified
here — the composition-order half is, the chunking half cannot be), no send lock or
concurrency, no delivered-reminder note, audit as pairs rather than records, cancellation
only through the pre-send re-read, and an exact clock.

### 1.2 The re-run, and the three defects it found

All properties hold as reported by `python3 verify_selector_invariants.py`. Faults were
injected at every stage boundary named in the task: `pre-work`, `grace`, `pre-work-commit`,
`send{i}`, `post{i}`, `post-commit{i}`, `send-summary`, `post-summary`,
`post-commit-summary`, against a tri-valued channel double.

One correction to the *model's own* filing, carried over from the old file's lesson: the
three pre-commit stages (`pre-work`, `grace`, `pre-work-commit`) are **not** termination
cases and were moved to the detectability property. A crash at or before the pre-work commit
persists nothing, so no bound can ever be reached; filing them under termination asserted the
wrong property. (The old model had already learned this for two of the three; the commit
boundary is the third.)

#### Finding 1 — the grace requirement's unqualified SHALL is false

`prop_report_crash_give_up_can_precede_any_naming` drives three consecutive process deaths
between the pre-work commit and the summary's dispatch (`crash_every="send-summary"`).
Result: `{'summaries_attempted': 0, 'never_named': True, 'retired': True, 'gave_up_via':
['crash'], 'error_logged': True}` — the rows are retired by the pre-work crash-limit give-up
having **never been named in any summary**.

The reminders delta said, without qualification:

> No overdue reminder SHALL reach a terminal status without either being delivered or being
> named in an attempted catch-up summary.

Design D1's Goals already prices this residual ("a report row retired by the pre-work
crash-limit give-up after repeated process deaths at the reporting stage, inherent to the
settled crash-maximum placement"), so the **design** was consistent and the **requirement
text** overstated. Fixed in the delta: the SHALL now carries exactly one stated exception,
notes that the exit is error-logged and crash-limit-bounded, forbids reading it as an
assertion that the owner was told, and points out that the report horizon carries no
equivalent residual — which is why *it* is evaluated post-send. The existing
"Report crash loop terminates" scenario already covers the mechanism, so no new scenario was
needed.

#### Finding 2 — "≈ 96 attempts per row" is 2× low for an `abandoned` row

The horizon's anchor is `due_at + grace + horizon`. That anchor is the moment of
reportability only for a row whose grace transition ran on time. A row that exits to
`abandoned` becomes reportable within `crash_attempt_limit` ticks of its **due instant** —
nothing waits a grace window to abandon — so its anchor sits a full grace window further out.

`prop_termination_under_partial_summary` measures all three classes, and the
`abandoned_anchor` arm drives a genuinely abandoned row rather than seeding one:

| class | measured summary sends | bound |
|---|---|---|
| went `missed` on time | **97** | `horizon / floor` = 96 |
| exited to `abandoned` | **193** | `(grace + horizon) / floor` = 192 |
| arrived already stale | **1** | one attempt, by construction |

`exceeds_the_missed_row_bound: True`, `within_the_actual_bound: True`. Both the delta and
design D5 quoted the single figure 96, and design's Risks entry quoted `100 × ~96` for the
absolute worst. Fixed in all three places: the count is stated as three-valued, the largest
per-row figure is ~192, and the absolute worst is `100 × ~192`. Still bounded — which is the
property — but the number was wrong.

#### Finding 3 — "bounded by horizon over floor" is off by the give-up attempt itself

The 97-vs-96 above is not noise: post-send placement means a row's give-up is written in the
post-send transaction of one **final** send, so the count is always the span-over-floor
*plus one*. The delta's persistently-partial scenario said "each named row's summary sends
are bounded by the report horizon over the retry floor", which a test derived literally from
it would assert as `<= 96` and watch fail at 97. Fixed: the scenario's THEN now names the
span from reportability to `due_at + grace + horizon` over the floor, **plus the one final
attempt whose post-send write performs the give-up**.

### Properties as re-run (all holding)

| property | what it establishes |
|---|---|
| `TERMINATION/crash` | every stage from the pre-work commit onward terminates within `N_TERMINATION` = 10 ticks |
| `TERMINATION/partial-sum` | the horizon property, three classes, each bounded and each named before its give-up |
| `STALE-NAMED-FIRST` | a week-old row is named in an attempted summary before any give-up — the property that decides *where* the horizon lives |
| `OUTAGE-NEVER-FORFEITS` | channel down past 3× the horizon, then recovered: nothing given up, every row named and marked, 1152 error logs on the way |
| `DETECTABILITY` | all three pre-commit stages: nothing committed, no audit records, no owner-visible send, one log per tick, still selected |
| `QUIESCENCE` | permanent channel failure: never abandoned, counter never grew, sends floor-paced, one log per send |
| `CONSERVATION` | delivered ⊆ acked, reported-without-give-up ⊆ acked, max never exceeded, zero unnamed terminals outside Finding 1's class — and the oracle arm fails as it must |
| `PARTIAL-HANDLING` | both sides of D4's asymmetry in one run each |
| `NO-EARLY-DELIVERY` | the `due_at` conjunct: `next_attempt_at = 0` (the schema default's meaning) on a row due in a week sends nothing, charges nothing |
| `PACING` | 100 within-grace rows drain at ≤ 10/tick in 10 ticks, oldest-due first, none dropped, none charged while unselected |
| `CHARGED=>WRITTEN` | 300 fault-free ticks, 0 mismatches — README open defect #1 is dissolved, not merely unlikely |
| `DELIVERED-WRITE-LOST` | the composition the review could only reason about: a delivered summary whose post-send write never commits re-loops into the crash bound, loses no row, terminates in 2 duplicates |
| `RESIDUAL/report-crash` | Finding 1, asserted where the invariant is |
| `CRASH-SEND-THEN-MARK` | killed after each send: ≥ 1 delivery, ≤ crash limit duplicates, never silent |
| `NOTICE-NOT-REPEATED` | 96 floor-retried sends over a grace window produce **1** notice |
| `CANCEL-BEFORE-DISPATCH` | a cancellation committing after selection is skipped by the pre-send re-read; its neighbours still deliver |
| `COMPOSITION-SET` | composition names exactly the eligible report row plus the same-tick abandoned exit; a row cooling on the floor is untouched and unrenamed |

### Gate outcome

Group 1's gate is satisfied: the model agrees with the delta text on every property, **after**
three edits to the delta and design text. `openspec validate --changes reminder-delivery
--strict` passes with the edits in place. Group 2 may begin.

---

## Group 2 — configuration

Nine knobs on `RemindersConfig`, with load-time validation. Suite after: **1306** (+37).
The prompt-hash tests in `test_reminders_inert.py` passed **untouched**, which was the
acceptance condition — these knobs do not reach the system prompt, and
`test_the_delivery_knobs_do_not_reach_the_system_prompt` now states that directly so a
future interpolation fails with a readable message instead of an opaque hash mismatch.

Two implementation notes worth the diff:

- **`_DELIVERY_SETTINGS` is a table both the read path and the validator iterate.** Nine
  hand-written read lines plus nine hand-written validation lines is the shape where a knob
  gets added to one and not the other; iterating one table makes that drift unrepresentable.
- **Validation is unconditional, not gated on `reminders.enabled`.** A bad value that only
  surfaces when someone flips the flag surfaces on rp5, over SSH, at the worst moment. Both
  ordering constraints (threshold < grace, horizon > floor) are checked the same way, and
  every message names the setting because the error text is all the operator gets.

One test of mine was wrong and was corrected rather than accommodated:
`test_the_crash_attempt_limit_is_not_named_like_the_bridge_retry_budget` asserted
`max_send_attempts` was a field of `SignalConfig`. It is not — it is a `SignalAdapter`
constructor parameter, and config carries no key for it at all. The test now pins that
(and that a future promotion of it to config must not land in `RemindersConfig`).

---

## Group 3 — the store's delivery writes

Suite after: **1345** (+39 over group 2). New file
`tests/test_reminders_delivery_store.py` (41 tests), plus the transaction/await guard and
its self-test in `tests/test_store_transaction.py`.

### 3.1 Pre-flight — the guard enumeration, re-derived

`grep -rn "surfaced_at\|send_attempts\|delivered_at\|reported_at\|scheduler" tests/` returns
six files. Re-derived rather than trusted, and the result **confirms task 3.3's list exactly**:

| file | what it is | disposition |
|---|---|---|
| `test_reminders_inert.py` | the three inertness guards + the cadence guard | three expired (3.3), one survives |
| `test_reminders_store.py` | asserts the 13-column set exists and `next_attempt_at` is `NOT NULL DEFAULT 0` | **schema** assertion, not an inertness guard — survives untouched |
| `test_audit_v4.py` | asserts v4 already validates every delivery transition and the `scheduler` initiator | forward-looking; this change must keep it green (task 6.1) |
| `test_audit_receipts.py` | `SCHEMA_VERSION == 4` | ditto — no version bump here |
| `test_channel_adapter.py` | `max_send_attempts=3` in adapter fixtures | the bridge's retry budget, unrelated |
| `test_config_reminders.py` | group 2's own new tests | n/a |

So: exactly three expiries, no fourth. No guard outside `test_reminders_inert.py` needed
touching.

### 3.3 The expiries, each with its successor

Per the edited-tests rule. All three are recorded **in place** as a comment block in
`test_reminders_inert.py` rather than silently deleted, because "this test vanished" and
"this test was retired deliberately" look identical in a diff a year later.

| expired guard | why it had to go | successor |
|---|---|---|
| `test_nothing_in_this_change_writes_a_delivery_column` (parametrized ×4) | a grep asserting no `UPDATE`/`INSERT` literal in `henk/` names the four columns. Writing them *is* this change. | `test_a_disabled_run_writes_none_of_the_delivery_columns` — a **stronger** claim than the grep made: it drives a real disabled startup over a real file, so it catches a write reached through a path no literal names. Plus `test_the_delivery_columns_are_written_only_by_the_scheduler_and_the_note`, which narrows the grep rather than dropping it (only `reminders.py` / `scheduler.py` may write them). Plus group 8's no-task-when-disabled. |
| `test_the_reminder_repository_writes_only_status_and_next_attempt_at` | an AST guard over every `UPDATE reminders` literal, asserting the repository writes almost nothing. | `tests/test_reminders_delivery_store.py`'s selector-and-exit suite, which asserts what the repository **does** write per exit. The half worth keeping — that no write touches the owner's words or the due instant — is now `test_no_delivery_write_touches_the_owners_words_or_the_due_instant`, run over all seven delivery writes. |
| `test_there_is_no_scheduler_and_no_send_in_this_change` | asserted `henk/reminders/scheduler.py` does not exist. | the module is this change's subject; the disabled-path assertion above plus group 8. |

`test_no_cadence_amendment_rode_along` **survives untouched and green**, as the task requires.
The file's module docstring was updated, since its claim 2 ("this change writes none of
delivery's columns") is no longer the claim the file makes.

**Expected total for task 10.1: exactly these three, and no others.**

### 3.4 The repository surface

Eleven methods, all synchronous and all transaction-agnostic. Selection is three queries
(`select_due`, `select_reportable`, `select_past_grace`) plus the one-column `status_of` for
the pre-send re-read; writes are `charge_attempt`, `mark_delivered`, `schedule_retry`,
`mark_missed`, `mark_abandoned`, `mark_reported`, `mark_surfaced`, and the note's
`unsurfaced_deliveries` read.

Decisions the tests pin:

- **`select_due` carries both conjuncts.** `test_a_future_due_row_with_an_eligible_next_attempt_at_is_never_selected`
  forces `next_attempt_at = 0` (the schema default, meaning *eligible now*) on a row due in a
  week and asserts nothing is selected — the exact bug the conjunct exists for.
- **`select_past_grace` is not bounded by the delivery cap.** 25 past-grace rows, all
  returned; a cap here would leave the eleventh-oldest pending past its window forever.
- **`mark_reported` / `mark_surfaced` take a set and write it in one statement.** The
  summary's outcome applies to all its rows or none, and an empty set returns 0 without ever
  emitting a bare `IN ()`.
- **`charge_attempt` reads the incremented value back inside its own transaction** and returns
  it, because the crash bound is evaluated against that value and must not be inferred.
- **No store call went off the event loop**: the pre-existing `to_thread|run_in_executor` grep
  is still green and was not weakened.

### 3.5 The transaction/await guard, watched failing twice

`test_no_transaction_scope_spans_an_await` walks every `with …transaction()` body in `henk/`
and fails on any `await`, `async for`, or `async with` lexically inside it. Matched on the
called name `transaction` rather than the receiver, for the same reason the process-timezone
guard matches `now`/`today` that way — an alias or a helper slips past a receiver check, and a
false positive is loud and cheap.

Seen to fail, both ways:

1. **Durably, in the suite.** `test_the_await_in_transaction_guard_detects_every_shape_it_claims_to`
   runs the detector over six offending fixtures (plain await; aliased receiver; await nested
   in a branch; `async for`; `async with`; a coroutine *defined* inside the scope) and four
   clean ones (await after the scope; await before it; a non-transaction `with` awaiting
   freely; post-send writes each in their own scope). This is the part that stays.
2. **Once, against real source.** A temporary async method with a transaction scope held
   across an await was added to `henk/store/reminders.py`; the guard failed naming
   `henk/store/reminders.py:594`, then the defect was reverted and the suite re-confirmed
   green at 1345. Worth recording that the *first* attempt at this was a `SyntaxError`, not a
   guard failure — `await` in a synchronous method never reaches the AST check, so a defect
   planted in the wrong kind of method would have "proved" the guard works without exercising
   it at all.

---

## Group 4 — channel send serialization

Suite after: **1356** (+11). `tests/test_channel_adapter.py` is **purely additive** —
273 insertions, 0 deletions, confirmed by `git diff --stat` — which is task 4.3's claim
asserted rather than assumed: serialization is additive for every existing single-sender
caller, and not one existing contract test was touched to accommodate it.

### 4.2 The interleaving, demonstrated before it was fixed

The task required the defect be shown, not assumed. Against the lockless adapter, with a
bridge whose `send` actually suspends:

```
test_concurrent_multi_chunk_sends_do_not_interleave
  AssertionError: chunks interleaved: ABABAB

test_ten_concurrent_senders_all_stay_contiguous
  AssertionError: a sender's chunks were split: ABCDEFGHIJABCDEFGHIJABCDEFGHIJ
```

Seven of the eleven new tests were red; the four that passed lockless are the
release-the-lock ones, which cannot fail when there is no lock to strand anyone on. After
the change, all eleven pass.

### The double, and why `conftest.FakeChannel` is banned here

`SlowBridge.send` records the chunk and then does exactly one `await asyncio.sleep(0)`.
That single suspension is the whole design: asyncio's ready queue is round-robin, so two
gathered senders each yielding once per chunk alternate **strictly**, which makes the
interleaving deterministic rather than lucky. Without a suspension there is no
interleaving to prevent and every assertion below would pass against the lockless
adapter — which is precisely how a serialization test fools itself. `conftest.FakeChannel`
has no lock and never suspends mid-send, so it would satisfy all eleven while serializing
nothing; the reminders README names that trap explicitly and the delta has a scenario for
it ("enforced by the adapter, not by caller convention").

### What the eleven cover

Beyond the delta's four scenarios: a **reply against a proactive send** (the pairing that
actually occurs — the scheduler's delivery racing an owner reply; a lock on one path only
would pass a reply-vs-reply test and fail in production), two proactive senders, **ten**
concurrent senders (a lock that serialises pairs may still admit a gap), a failing sender
not stranding the waiter, an unforeseen exception still releasing the lock (a leaked lock
would hang every later send on the process — a worse failure than the exception), and two
structural tests.

The structural pair is worth its keep:

- `test_the_lock_is_the_whole_mechanism_and_nothing_more` — exactly one `.Lock()` in the
  module, no `wait_for`/`timeout` around it (that would be a hold timer), and none of
  `max_chunks` / `chunk_cap` / `priority` / `hold_timeout` anywhere in the source. All
  three were designed and rejected — the bounded hold and chunk cap in channel-integrity
  D5, delivery-path priority in this change's D6 — so a future change adding one has to
  argue with the decision rather than around it.
- `test_the_lock_wraps_the_shared_sequence_not_the_two_wrappers` — asserts the set of
  methods holding the lock is exactly `{"_send_serialized"}`. In the wrappers instead, the
  failure notice would sit outside the critical section and a waiting sender's chunks could
  land between a truncated message and the banner explaining it; in both places, deadlock.

### Wiring consequence, recorded here because group 8 has to honour it

The lock is **instance state**. A second `SignalAdapter` over the same bridge satisfies
every test above while serializing nothing, so the scheduler must be handed the *same*
adapter instance the agent core holds. Task 8.1 asserts that, and the reason is written
into the lock's own comment so the next reader of `signal.py` finds it there.

---

## Group 5 — the scheduler

Suite after: **1444** (+88). New: `henk/reminders/scheduler.py`,
`tests/test_reminders_scheduler.py` (87 tests).

### Marker and heading wording (design's first Open Question, settled here)

The design left the marker wording to apply time, requiring only distinguishability
from a triage message plus the original-due-time statement on late and missed items:

| surface | wording |
|---|---|
| on-time delivery | `⏰ Reminder: {text}` |
| late delivery | `⏰ Reminder (was due {rendered}): {text}` |
| summary heading | `⏰ Catch-up: reminders that came due but were not delivered on time.` |
| summary row | `• {rendered} — {text}` |
| abandoned row | the same, plus ` (I tried to send this one and gave up.)` |
| failure notice | `[⚠ the reminder due {rendered} could not be fully delivered]` |

The stored text always appears **unchanged** after the prefix. The abandoned suffix
exists because the distinction matters to the owner: a `missed` row means "you were
away", an `abandoned` one means "I could not reach you", and one message carries both.

### Finding 4 — `detail: "partial"` collided with an existing no-text guard

Design D4 says the partial-delivery audit record carries `detail: "partial"`, calling it
"v4's free `detail` property — no version bump". But `reminders-core` shipped
`test_a_reminder_record_carries_no_reminder_text`, which asserted `detail` is **absent**
from a reminder record — and for a good reason: `detail` is free text, so on a reminder
record it is the one property through which the reminder's own wording could reach a log
that "gets read and pasted around".

So the design's mechanism and the existing guard's purpose were in direct conflict, and
the full suite caught it the moment the scheduler started emitting the detail.

Resolved by making the guard's *intent* enforceable rather than dropping either side: the
v4 document's reminder branch now constrains `detail` to a **closed enum**
(`["partial", null]`) with its own description. The property is available to the design,
and free text on a reminder record is now refused by the contract rather than by a test.
An authorization record's free-text `detail` is untouched.

Deliberately **not** a version bump, and the reasoning is worth stating: this tightens
what v4 accepts, and no reminder record carrying a `detail` value has ever been written,
so nothing already on disk becomes invalid. The old assertion was replaced by
`test_a_reminder_records_detail_is_a_closed_vocabulary_not_free_text`, which drives four
smuggling attempts (`"buy bread"`, `"partial: buy bread"`, `"PARTIAL"`, `""`) through the
document and asserts each is refused.

### Two test bugs of mine, and one real constraint

Worth recording because in each case the code was right and the test was wrong:

- **Clock tests asserted the fixture, not the property.** Two tests asserted
  `delivered_at == NOW` under a clock that advances on every read — but seeding consumes
  reads, so the tick's captured instant is not `NOW`. Fixed to assert against
  `clock.reads[reads_before]`, which is the actual property: the write is recorded
  against exactly the instant the tick captured.
- **A 120-row backlog is unreachable.** `test_the_summary_names_every_unreported_row`
  seeded 120 rows and was refused by the pending cap. 100 is not a large-looking
  sample — it *is* the cap, so it is the true worst case. The store was telling the
  truth and the test was asking for something the system cannot produce.
- **The abandoned row's `reported_at` is set by the end of its tick.** A test asserted it
  null after a full tick; the exit leaves it null so the *same tick's summary* can name
  it, and that summary then marks it. The null-at-the-exit half belongs at the
  repository level, where no summary is in the way, and that is where it is asserted.

### 5.6 The fault-injection matrix, retargeted

Ten fault points × three channel outcomes, against the real store and scheduler, plus a
process-death arm per outcome. The fixture carries four rows so every fault point is
reachable in one run: delivery work, grace-then-report work, a row the crash bound
retires in pre-work, and a row already past grace + horizon.

The invariant under fault is **conservation, not success**: a fault may abandon a tick,
log loudly and cost a duplicate; it may never leave a row that is neither terminal nor
still selectable — a row charged an attempt with no recording write, which is the shape
that silently vanishes.

Seven of the thirty cells initially failed with "the fault was never reached", which was
the matrix over-claiming rather than the code misbehaving: three of the ten writes only
exist on one arm of the outcome mapping. Rather than skipping those cells, reachability
is now **declared** (`REACHABLE`) and the unreachable ones assert they were *not*
reached — so the matrix reports thirty checks and performs thirty. Two of those
declarations are properties in their own right:

- `mark_abandoned` is reachable under all three outcomes, because the crash bound is
  evaluated pre-work and no channel outcome can affect whether it fires. That is the
  pre-work-placement argument restated as coverage.
- `mark_reported` is unreachable under a wholly `failed` summary — which is "a channel
  outage never forfeits the report" showing up as an absence.

### 5.6 The mutations — nine applied, nine red, zero survivors

Task 5.6 lists nine mutations under "at minimum" (the apply brief called it eight; all
nine listed were run). Each was applied by exact string replacement against pristine
source, the suite run, then reverted — a replacement that failed to match aborts loudly
rather than passing as a no-op no-op mutation.

| mutation | verdict | what caught it |
|---|---|---|
| 1. drop the pre-work increment | RED, 9 failed | the crash-bound suite + `mark_abandoned` matrix cells |
| 2. evaluate the crash max post-send | RED, 9 failed | same, and `test_a_send_then_death_redelivers_within_the_bound_never_silence` — the bound never fires on the path it exists to bound |
| 3. mark `reported_at` on a partial summary | RED, 3 failed | `test_reported_at_is_written_only_when_the_summary_is_delivered` |
| 4. skip the grace clear of `send_attempts` | RED, 1 failed | `test_the_grace_exit_clears_the_counter_and_leaves_reported_at_null` |
| 5. drop the selector's `due_at` conjunct | RED, 2 failed | both future-delivery tests, at scheduler and store level |
| 6. remove the report horizon | RED, 4 failed | the termination and give-up suite |
| 7. move the horizon into the pre-work transaction | RED, 3 failed | **`test_a_stale_row_is_named_in_an_attempted_summary_before_any_give_up`** — the scenario the task named, plus `test_a_channel_outage_never_forfeits_the_report` |
| 8. skip the pre-send status re-read | RED, 4 failed | the cancellation race + all three `status_of` matrix cells |
| 9. hold a transaction across an await | RED, 1 failed | the AST guard from 3.5, statically |

**No survivors.** Mutation 7 is the one worth dwelling on: moving the horizon check three
lines earlier — into the transaction where the crash bound correctly lives — silently
converts "every row is named at least once" into "a row that arrived stale is retired
unnamed". That is a one-line defect with no runtime symptom, and it is the single
strongest argument for the post-send placement being spec text rather than a code detail.

---

## Group 6 — audit records

Suite after: **1459** (+15). New: `tests/test_reminders_delivery_audit.py`.

**An honest TDD deviation, recorded rather than glossed:** 6.2's implementation landed
inside group 5, because the scheduler cannot record an outcome without it — the receipt
call sites are part of the delivery path, not a layer on top of it. So 6.1's tests were
written after that code and passed on first run rather than going red first. What they
add is not the behaviour but the *contract*: they drive the scheduler through the **real**
`AuditLog` and `ReminderReceipts` onto a real file, then read the JSONL back and validate
each record against the committed v4 document. Group 5's tests used a collecting double,
which proves neither the durability nor the validation.

What the fifteen establish:

- each of `delivered` / `delivered-late` / `missed` / `abandoned` writes exactly **one**
  record, validating against v4, carrying the id, the due **instant** (not a rendered
  string — a receipt must not move when the configured zone does), `initiated_by:
  "scheduler"`, and a timestamp;
- **no record carries the reminder's text.** Checked against the raw file bytes with a
  deliberately unmistakable string, so a nested or renamed field would still be caught;
- a `partial` delivery carries `detail: "partial"`; a clean one carries `detail: null`;
- a failed send writes **no** record, and neither does a give-up exit — `reported_at`
  written as a give-up is Henk stopping, not a transition, and putting it in the trail
  would make the trail assert something false;
- the record survives the process: written, store closed, then found by a reader sharing
  nothing with the writer;
- the traceability scenario (approval-gate delta): a delivered reminder's trail carries
  the `scheduled` record naming who asked and the delivery record naming the scheduler;
- the cancellation race carries **both** transitions, so the sequence is reconstructible
  rather than looking like a contradiction;
- an audit write failure does not stop the delivery. The owner's reminder matters more
  than its paperwork.

## Group 7 — the delivered-reminder note

Suite after: **1478** (+19). New: `henk/reminders/note.py`,
`tests/test_reminders_note.py`. `henk/agent/core.py` gains a `deliveries` provider.

Composition order in the owner turn, outermost first: **time header, recall block,
delivered-reminder block, the owner's message.** The note sits closest to the owner's text
because it is the context their message most likely refers to.

Two design points worth stating, because both are places a plausible implementation goes
wrong:

- **Composing and marking are one step**, inside `block()`. A block returned but not
  marked shows twice; marked but not returned is lost entirely. Both are failures, so
  neither half happens without the other.
- **The note is NOT gated on a per-session flag**, unlike recall. "Surfaced already" is
  durable state in the store, which is what lets a delivery landing mid-conversation reach
  the owner's next turn. A per-session flag here would silently swallow every delivery
  after the first turn of a long session — and it would have passed a naive test, because
  the obvious test only sends one message.

The line renders both instants (`Sent {when}` plus `(was due {then})`) and omits the
second when they coincide, since repeating one rendered time twice on a line reads like a
bug. Event turns never receive it, and an event turn does not consume an unsurfaced
delivery either — asserted, because "not shown" and "not consumed" are different claims.

## Group 8 — runtime wiring

Suite after: **1492** (+14). New: `tests/test_reminders_runtime.py`. `henk/app.py` and
`henk/runtime.py` wire the scheduler beside the coordinator.

### A real defect found while wiring: a dead background task crashed the shutdown

`App.run`'s reaping loop caught only `CancelledError`. A background task that had already
died on its own — the exact case the agent-core delta's isolation requirement is about —
would have its exception **re-raised** by `await task` in the `finally`, turning one
subsystem's failure into a crash during shutdown and masking whatever actually ended the
message stream. This predates the change (the coordinator had the same exposure) and was
found only because the isolation tests drove it. Now reaped and logged; each task owns its
own error handling, and this is only the funeral.

### The wiring assertion that matters most

`test_the_scheduler_gets_the_same_adapter_instance_as_the_core` asserts **identity**, not
equality, on three references (`app._scheduler._channel is app._core._channel is
app._adapter`). The send lock is instance state: a second `SignalAdapter` over the same
bridge has its own lock, so every serialization test in the suite would still pass — they
each build one adapter and drive it — while production interleaved the scheduler's chunks
with the core's. Green suite, broken behaviour, no test able to see it. That is why the
task list singled this out and why the lock's own comment in `signal.py` says so too.

Also asserted: one repository and one resolver shared by the scheduler, the owner
commands and the note (two resolvers could render one due time two ways, leaving the owner
to adjudicate); the bounds come from config; and with the capability disabled there is no
scheduler, no note provider, and no time header — the three tells the reminders-core
deploy record uses to call a deploy inert.

### 9.4's test-level half, landed here

`test_the_scheduler_module_opens_no_socket_and_registers_no_handler` walks the module's
imports and calls and refuses `socket` / `http` / `httpx` / `websockets` / `signal` /
`selectors`, plus any `bind` / `listen` / `connect` / `add_signal_handler` /
`create_server` / `open_connection` / `add_reader` call.
`test_a_tick_can_only_be_caused_by_the_clock` pins the public surface to exactly
`{"run", "tick"}` and drives the whole inbound path to confirm no message reaches the
scheduler. The runtime half — comparing the container's listening sockets before and
after — is task 11.2's.

---

## Group 9 — the cross-capability contracts

Suite after: **1503** (+11). New: `tests/test_reminders_cross_capability.py`.

Four capabilities gain requirements here without gaining any implementation. A
requirement whose only evidence is that nobody wrote the offending code is a requirement
that quietly stops being true, so each got a test that fails if someone writes it.

**9.1 Cadence.** The cap is measured on either side of five deliveries and one summary
and found unmoved, and a week of hourly ticks with one future reminder sends **zero**
messages — the two-class enumeration only holds if the second class is genuinely empty
when the owner scheduled nothing. Plus the structural counterpart to reminders-core's
"no cadence amendment rode along": `PipelineConfig` has no reminder field, the pipeline
module never says "reminder", and the scheduler never says `pipeline` / `cap_per_24h` /
`announceable` / `EventTurn`. A knob there would make reminder volume a cadence concern,
which is exactly what "they do not consume the cap" denies.

**9.2 The gate.** One test carries three claims because they are one property seen three
ways: a real `ApprovalGate` is driven to a genuinely pending approval, a reminder is then
delivered, and afterwards the reminder arrived, no prompt was sent, no authorization
record was written, and the approval is **still resolvable** — the owner's "yes" is
accepted and the awaited decision comes back approved. That last part is what makes the
exemption safe rather than merely convenient: a delivery that quietly consumed the
pending slot would strand a mutation the owner had already been asked about. A second
test delivers a reminder whose text is literally `"yes"` and confirms it cannot be
mistaken for an approval — the gate classifies inbound text only, and there is nothing
pending to attach it to. A third pins that the scheduler's constructor has no `gate`
parameter and its source never mentions one.

**9.3 Re-enablement.** Two store lifetimes over one file across a flag flip, with the
**stale** offset the task demanded (three days, older than grace + horizon) rather than
the easy 25-hour one: the within-grace row is delivered late stating its original due
time, the stale row is missed and named in the summary, and the individual delivery
precedes the summary. Crossed with the horizon's worst case in a third test — re-enabled
after five days of downtime *and* a summary that only partly lands — where the row must
still be named once. Under a pre-work horizon it would have been retired silently on that
very tick. A fourth test asserts a disabled lifetime leaves every delivery column exactly
as the owner left it, since re-enablement has to find the rows unchanged.

**9.4 Surface.** Landed in group 8's file (see above). The runtime half is task 11.2's.

---

## Group 10 — verification

### 10.1 Suite counts

| | count |
|---|---|
| pre-change baseline (at `3fed4bd`) | **1269 passed, 12 deselected** |
| post-change | **1503 passed, 12 deselected** |
| added by this change | **+234** |
| `pytest -m dst_sweep` | **12 passed, 1503 deselected** |

### 10.1 Every edited existing test, with its reason

Derived from the diff rather than from memory: for each test file touched since
`3fed4bd`, the count of **deleted** lines is the signal — an added test changes nothing
that existed before.

| file | deleted lines | what came out |
|---|---|---|
| `tests/test_reminders_inert.py` | 59 | the **three** task-3.3 expiries, and nothing else |
| `tests/test_audit_v4.py` | **1** | `"detail"` removed from one forbidden-key tuple |
| `tests/test_channel_adapter.py` | 0 | purely additive (+273) |
| `tests/test_config_reminders.py` | 0 | purely additive (+197) |
| `tests/test_store_transaction.py` | 0 | purely additive (+162) |
| the five new files | 0 | new |

So: **the three expected expiries, plus exactly one other single-line edit**, which
task 10.1 requires be justified. It is Finding 4 (group 5): the removed line asserted
`detail` was absent from a reminder record, which the design's `detail: "partial"` makes
impossible. It was not dropped but **replaced by a stronger constraint** — the v4
document now pins a reminder record's `detail` to a closed enum, so the guarantee the old
line protected (no free text on a reminder record) is enforced by the contract rather
than by a test, and four smuggling attempts are asserted to be refused.

Nothing else in any existing test was weakened, reworded, or relaxed.

### 10.1 Scenario → test

All **77** scenarios across the six deltas, with **130** test citations. Every cited name
was machine-verified against `pytest --collect-only`, which caught two citations of mine
that named tests that did not exist — the reason the table is generated and checked
rather than hand-written.

| capability | scenario | test(s) |
|---|---|---|
| reminders | Due reminder is delivered within a tick | `test_a_due_reminder_is_delivered_within_a_tick_verbatim_and_marked`<br>`test_a_due_reminder_is_delivered_within_a_tick_when_nothing_is_in_flight` |
| reminders | A future reminder is never delivered early | `test_a_future_reminder_is_never_delivered_however_many_ticks_run`<br>`test_a_future_due_row_with_an_eligible_next_attempt_at_is_never_selected` |
| reminders | A within-grace backlog is paced, not burst | `test_a_within_grace_backlog_is_paced_oldest_first_and_none_dropped`<br>`test_rows_beyond_the_tick_limit_are_not_attempt_charged_while_waiting` |
| reminders | Scheduler survives a store error | `test_a_store_error_mid_tick_rolls_back_and_the_next_tick_succeeds`<br>`test_the_run_loop_survives_a_store_error` |
| reminders | Restart needs no special pass | `test_the_pre_work_increment_survives_process_death_before_the_post_send_write`<br>`test_re_enabling_catches_up_under_the_ordinary_grace_rules` |
| reminders | Reminder delivered on time | `test_a_due_reminder_is_delivered_within_a_tick_verbatim_and_marked`<br>`test_an_on_time_delivery_does_not_state_a_due_time` |
| reminders | Text is not rewritten | `test_the_stored_text_is_never_rewritten`<br>`test_no_delivery_write_touches_the_owners_words_or_the_due_instant` |
| reminders | Late delivery states its original due time | `test_a_late_delivery_states_its_original_due_time_and_records_late`<br>`test_the_lateness_boundary_is_the_configured_threshold` |
| reminders | Cancelled reminders never deliver | `test_a_cancelled_reminder_is_never_delivered`<br>`test_a_cancelled_row_is_never_selected` |
| reminders | Cancellation between selection and dispatch is honoured | `test_a_cancellation_between_selection_and_dispatch_is_skipped`<br>`test_status_of_sees_a_cancellation_committed_after_selection` |
| reminders | Cancellation that loses the race is recorded honestly | `test_a_cancellation_after_dispatch_records_delivered`<br>`test_a_cancelled_then_delivered_row_carries_both_transitions` |
| reminders | Persistent failure does not repeat the notice | `test_a_persistent_failure_carries_no_notice_on_floor_retries`<br>`test_the_failure_notice_names_the_due_time_and_says_not_fully_delivered`<br>`test_a_crash_retried_attempt_still_carries_the_notice` |
| reminders | Delivery does not wait on a turn | `test_delivery_does_not_wait_on_a_turn` |
| reminders | Delivery waits on an in-flight send but is never skipped | `test_delivery_waits_on_an_in_flight_send_but_is_never_skipped` |
| reminders | A pending approval does not suppress delivery | `test_a_delivery_while_an_approval_is_pending_sends_and_prompts_nothing` |
| reminders | An attempt that crashes mid-send is counted | `test_the_increment_is_visible_after_a_death_mid_send`<br>`test_the_pre_work_increment_survives_process_death_before_the_post_send_write` |
| reminders | Channel failures do not accumulate attempts | `test_a_channel_failure_loop_never_grows_the_counter`<br>`test_every_post_send_write_clears_the_counter` |
| reminders | Crash between send and mark redelivers, never silently discards | `test_a_send_then_death_redelivers_within_the_bound_never_silence`<br>`test_process_death_at_each_stage_loses_no_row` |
| reminders | A crash loop terminates at the limit | `test_a_crash_loop_exits_to_abandoned_at_the_limit`<br>`test_an_abandoned_reminder_is_never_attempted_again` |
| reminders | The exit is evaluated on the path it bounds | `test_both_pre_work_give_up_exits_commit_with_the_rest_of_the_scope`<br>`test_a_send_then_death_redelivers_within_the_bound_never_silence` |
| reminders | Abandonment is surfaced, not silent | `test_an_abandoned_reminder_is_named_in_the_same_ticks_summary`<br>`test_the_summary_identifies_an_abandoned_row_as_a_failed_delivery` |
| reminders | Transient failure retries after the floor | `test_a_failed_send_leaves_the_row_pending_on_the_floor`<br>`test_a_transient_failure_delivers_on_the_first_tick_after_the_floor` |
| reminders | Partial reminder delivery is not re-sent | `test_a_partial_reminder_is_recorded_delivered_and_never_re_sent`<br>`test_a_partial_delivery_records_detail_partial` |
| reminders | Downtime within grace delivers late | `test_downtime_within_grace_delivers_late`<br>`test_the_grace_boundary_is_the_configured_window` |
| reminders | Downtime beyond grace is missed, not replayed | `test_beyond_grace_is_missed_and_summarised_not_replayed` |
| reminders | Nothing overdue means silence | `test_nothing_overdue_means_no_message_of_any_kind`<br>`test_an_empty_store_sends_nothing_over_a_long_run` |
| reminders | One summary names everything | `test_the_summary_identifies_an_abandoned_row_as_a_failed_delivery`<br>`test_mark_reported_writes_every_named_row_and_clears_their_counters` |
| reminders | A failed summary marks nothing | `test_reported_at_is_written_only_when_the_summary_is_delivered` |
| reminders | A partial summary marks nothing | `test_reported_at_is_written_only_when_the_summary_is_delivered` |
| reminders | A large backlog is fully named | `test_the_summary_names_every_unreported_row_with_no_item_bound`<br>`test_report_selection_is_uncapped` |
| reminders | Reported rows never resurface | `test_reported_rows_never_resurface`<br>`test_a_reported_row_is_never_selected_again` |
| reminders | Report crash loop terminates | `test_a_report_crash_loop_gives_up_with_an_error_log` |
| reminders | A persistently partial summary terminates at the horizon | `test_a_persistently_partial_summary_terminates_at_the_horizon`<br>`test_the_horizon_give_up_writes_no_new_audit_transition` |
| reminders | A channel outage never forfeits the report | `test_a_channel_outage_never_forfeits_the_report` |
| reminders | Stale rows are named before any give-up | `test_a_stale_row_is_named_in_an_attempted_summary_before_any_give_up`<br>`test_the_stale_row_is_named_even_when_the_summary_only_partly_lands` |
| reminders | Exits survive restart | `test_every_exit_is_invisible_to_a_reopened_selector`<br>`test_every_exit_is_invisible_to_a_reopened_selector` |
| reminders | Follow-up resolves against the delivered reminder | `test_the_follow_up_turn_carries_the_block`<br>`test_the_block_names_the_reminder_text_and_when_it_was_sent` |
| reminders | Surfaced exactly once | `test_a_delivery_is_surfaced_at_most_once`<br>`test_a_second_owner_turn_carries_no_block_for_the_same_delivery` |
| reminders | Surfacing survives a restart | `test_surfacing_survives_a_restart` |
| reminders | Stale delivery does not resurface | `test_a_delivery_older_than_the_window_is_not_surfaced`<br>`test_the_window_boundary_includes_a_delivery_exactly_at_it` |
| reminders | Event turns never carry it | `test_an_event_turn_never_carries_the_block` |
| reminders | The note does not taint the session | `test_the_block_does_not_taint_the_session` |
| reminders | Invalid delivery configuration fails startup | `test_a_non_positive_delivery_setting_fails_load_naming_the_setting`<br>`test_the_lateness_threshold_must_sit_below_the_grace_window`<br>`test_the_report_horizon_must_sit_above_the_retry_floor` |
| reminders | No widening knob exists | `test_the_delivery_config_surface_is_exactly_this_and_nothing_wider`<br>`test_no_delivery_knob_names_a_recipient_channel_or_audit_exemption` |
| reminders | Scheduling sets the selector column to the due instant | `test_scheduling_by_either_source_writes_next_attempt_at`<br>*(scenario inherited; test predates this change)* |
| reminders | Reinstating sets the selector column to the due instant | `test_reinstating_writes_next_attempt_at`<br>`test_reinstate_returns_it_to_pending_and_writes_next_attempt_at`<br>*(scenario inherited; test predates this change)* |
| reminders | Disabled means inert, not destructive | `test_a_disabled_run_writes_none_of_the_delivery_columns`<br>`test_disabled_wires_no_scheduler_and_no_note`<br>`test_stored_reminders_are_untouched_by_a_disabled_run` |
| reminders | Disabled by default | `test_reminders_absent_entirely_means_disabled`<br>*(scenario inherited; test predates this change)* |
| reminders | Re-enabling restores access to stored reminders | `test_a_disabled_lifetime_leaves_every_delivery_column_untouched` |
| reminders | Re-enabling catches up under the grace rules | `test_re_enabling_catches_up_under_the_ordinary_grace_rules`<br>`test_re_enablement_surfaces_the_delivery_in_the_next_owner_turn` |
| channel-adapter | Concurrent sends do not interleave | `test_concurrent_multi_chunk_sends_do_not_interleave`<br>`test_ten_concurrent_senders_all_stay_contiguous` |
| channel-adapter | A waiting send is delivered, not dropped | `test_a_waiting_send_is_delivered_not_dropped_or_truncated`<br>`test_a_failing_sender_does_not_strand_the_waiting_one` |
| channel-adapter | The notice cannot be separated from its chunks | `test_the_failure_notice_lands_before_the_waiting_senders_first_chunk` |
| channel-adapter | Serialization is enforced by the adapter, not by caller convention | `test_the_lock_wraps_the_shared_sequence_not_the_two_wrappers`<br>`test_the_lock_is_the_whole_mechanism_and_nothing_more` |
| agent-core | Event turn framed for triage | `test_owner_turn_has_no_triage_framing_event_turn_does`<br>`test_an_event_turn_never_carries_the_block` |
| agent-core | Owner turn unaffected | `test_owner_turn_has_no_triage_framing_event_turn_does`<br>*(scenario inherited; test predates this change)* |
| agent-core | Every owner turn knows the time | `test_every_owner_turn_carries_a_header_for_its_own_turn`<br>*(scenario inherited; test predates this change)* |
| agent-core | No header when reminders are disabled | `test_no_owner_turn_carries_a_header_when_reminders_are_disabled`<br>`test_disabled_wires_no_scheduler_and_no_note` |
| agent-core | First owner turn carries recall | `test_first_owner_turn_is_prefixed_with_the_block`<br>*(scenario inherited; test predates this change)* |
| agent-core | Delivered reminder reaches a mid-session turn | `test_the_block_is_injected_even_when_recall_was_already_given` |
| agent-core | Non-announceable event turn output suppressed | `test_non_announceable_event_output_is_suppressed`<br>*(scenario inherited; test predates this change)* |
| agent-core | Scheduler starts and stops with the app | `test_the_scheduler_runs_for_the_apps_lifetime_and_is_cancelled_cleanly`<br>`test_the_scheduler_and_the_coordinator_both_run_and_both_stop` |
| agent-core | Scheduler failure does not take the app down | `test_a_scheduler_failure_leaves_replies_working`<br>`test_a_scheduler_failure_leaves_the_coordinator_running`<br>`test_the_run_loop_survives_a_channel_exception` |
| agent-core | No scheduler task when disabled | `test_disabled_wires_no_scheduler_and_no_note`<br>`test_no_scheduler_task_is_created_when_none_is_wired` |
| approval-gate | Delivery does not prompt | `test_a_delivery_while_an_approval_is_pending_sends_and_prompts_nothing`<br>`test_the_scheduler_never_touches_the_gate` |
| approval-gate | Delivery leaves a pending approval intact | `test_a_delivery_while_an_approval_is_pending_sends_and_prompts_nothing` |
| approval-gate | Every delivery is traceable to a schedule | `test_a_delivered_reminders_trail_carries_both_records` |
| incident-triage | Quiet homelab means silence | `test_nothing_due_means_zero_unprompted_messages_over_a_long_run`<br>`test_an_empty_store_sends_nothing_over_a_long_run` |
| incident-triage | No system-scheduled message exists | `test_nothing_due_means_zero_unprompted_messages_over_a_long_run`<br>`test_the_pipeline_has_no_reminder_surface_at_all` |
| incident-triage | Reminder delivery does not consume the incident cap | `test_a_reminder_delivery_does_not_consume_the_incident_cap`<br>`test_a_catch_up_summary_does_not_consume_the_incident_cap` |
| incident-triage | Suppressed count surfaces later | `test_suppressed_count_surfaces_on_next_announceable_message`<br>*(scenario inherited; test predates this change)* |
| incident-triage | Cap holds across a restart | `test_rehydrated_cap_holds_across_restart`<br>*(scenario inherited; test predates this change)* |
| incident-triage | Suppressed triage cannot prompt | `test_per_instance_attempt_in_a_suppressed_turn_is_silent`<br>*(scenario inherited; test predates this change)* |
| secure-deployment | No new infrastructure surface | `test_the_scheduler_module_opens_no_socket_and_registers_no_handler`<br>*(+ runtime half at deploy, task 11.2)* |
| secure-deployment | Reminders add no listener | `test_the_scheduler_module_opens_no_socket_and_registers_no_handler`<br>*(+ runtime half at deploy, task 11.2)* |
| secure-deployment | The timezone database resolves inside the image | `test_no_module_in_scope_reads_the_process_timezone`<br>*(+ runtime half at deploy, task 11.2)* |
| secure-deployment | The scheduler cannot be triggered from outside | `test_a_tick_can_only_be_caused_by_the_clock`<br>`test_the_scheduler_module_opens_no_socket_and_registers_no_handler` |

### 10.2 `openspec validate --changes reminder-delivery --strict`

Passes (3 items: `owner-acknowledgement`, `reminder-delivery`, `reminders` — the latter
two being the superseded draft and this change).

### 10.3 The settled list, checked against the implementation

The reminders README's "Settled — do not re-litigate" list, plus this change's D4
asymmetry. Each row names where the settled decision now lives in code.

| settled decision | where it lives | verified by |
|---|---|---|
| the two-budget separation (`send_attempts` cleared on any return, so it accumulates only across process death) | every post-send write clears it: `mark_delivered`, `schedule_retry`, `mark_reported` | `test_every_post_send_write_clears_the_counter`, `test_a_channel_failure_loop_never_grows_the_counter` |
| the crash maximum in the **pre-work** transaction, not post-send | `ReminderScheduler._pre_work`, beside `charge_attempt` | `test_a_crash_loop_exits_to_abandoned_at_the_limit`; mutation 2 goes red |
| `next_attempt_at` initialized on every path into `pending` | `ReminderStore.schedule` / `_transition`, unchanged from reminders-core | `test_scheduling_by_either_source_writes_next_attempt_at` (inherited) |
| exits must **write** state the selector tests | ten repository writes, no in-memory exit anywhere | `test_every_exit_is_invisible_to_a_reopened_selector` (6 exits, each re-checked against a reopened store) |
| the cadence amendment's two-class enumeration | incident-triage delta text + the scheduler bypassing the pipeline | `test_a_reminder_delivery_does_not_consume_the_incident_cap`, `test_the_pipeline_has_no_reminder_surface_at_all` |
| the audit log's two-records-for-two-questions rule | `authorization` (gate) and `reminder` (transition) stay separate; the give-up writes neither | `test_a_delivered_reminders_trail_carries_both_records`, `test_a_summary_that_is_not_delivered_writes_no_report_record` |
| the resolved-time echo with the weekday | `render_instant`, reused unchanged by every delivery surface | delivery, summary and note all render through it |
| **B ships the complete final column set** | no DDL in this change at all | `test_every_designed_column_exists_after_first_connect` (inherited) |
| duplicate-beats-loss | `partial` → delivered for a reminder; `partial` marks nothing for a summary | `test_a_partial_reminder_is_recorded_delivered_and_never_re_sent`, `test_reported_at_is_written_only_when_the_summary_is_delivered` |
| cut #1 (no report item bound / pagination) | `select_reportable` is uncapped; composition names the whole set | `test_the_summary_names_every_unreported_row_with_no_item_bound`, `CHARGED=>WRITTEN` in the model |
| cut #2 (`terminal_at`) | never added | the column set is unchanged |
| cut #3 (one fixed floor, no schedule) | `retry_floor_seconds`, one value | `test_a_failed_send_leaves_the_row_pending_on_the_floor` |
| cut #4 (one message per due reminder) | `_deliver` per row, no batching | `test_reminders_are_delivered_oldest_due_first_one_message_each` |
| cuts #5/#6 (no `reschedule_reminder`, no reinstate **tool**) | registry untouched by this change | `test_the_registry_has_no_reinstate_reschedule_edit_or_delete_tool` (inherited) |
| **D4's asymmetry** (a reminder's partial is content; a summary's partial is rows) | `_deliver` vs `_report`, deliberately different | mutation 3 goes red; `test_reported_at_is_written_only_when_the_summary_is_delivered` |

### 10.3 The anchor sweep

For every numeric or ordering claim in the deltas: the requirement or config value that
makes it true. A claim true only because of an unwritten code fact is the defect class
that produced three findings in the review, so this is checked rather than assumed.

| claim | anchored by |
|---|---|
| poll interval 30 s, delivery cap 10/tick | `RemindersConfig.poll_interval_seconds` / `tick_delivery_limit`, both named in the polling-scheduler requirement |
| lateness threshold 300 s, grace 24 h | `late_delivery_threshold_seconds` / `late_grace_seconds`, named in their own requirements, with the ordering constraint validated at load |
| retry floor 900 s | `retry_floor_seconds`, named in the failed-send requirement |
| crash limit 3 | `crash_attempt_limit`, named in the crash-bound requirement |
| report horizon 86400 s | `report_horizon_seconds`, named in the report requirement, with `horizon > floor` validated at load |
| note window 12 h, note count 10 | `note_window_seconds` / `note_max_items`, named in the note requirement |
| **the selector's `due_at` conjunct** | stated twice on purpose: in the polling-scheduler requirement ("a reminder is never delivered before its due instant, whatever `next_attempt_at` holds") **and** in the MODIFIED initialization requirement, which is what makes the single-column version unsafe |
| **the horizon anchor** (`due_at + grace + horizon`) | stated verbatim in the report requirement, together with the post-send placement and the reason for it |
| **composition order** (oldest-due first) | stated in the report requirement, with its consequence — horizon-eligible rows land in the head chunks |
| delivery order (individual reminders, then the summary) | stated in the verbatim-delivery requirement |
| **notice recognition** (first-or-crash vs floor retry) | derivable from spec text alone, and deliberately so: initialization writes `next_attempt_at = due_at`, a floor retry sets it **later**, and "nothing may ever set it earlier" is a SHALL. So `next_attempt_at == due_at` ⟺ not-a-floor-retry, with no extra column and no unwritten code fact |
| "at most `crash_attempt_limit` notices per grace window" | the notice rule plus the crash bound, both requirements |
| per-row horizon attempt counts (≈96 / ≈192 / exactly 1) | now stated three-valued in the report requirement — this was **Finding 2**, and before the fix the number was anchored on nothing |
| the pending cap 100 | `reminders-core`'s `max_pending`; also the reason a 120-row backlog is untestable |
| ~33 s/chunk degraded ceiling | `signal.max_send_attempts × send_timeout_seconds` + backoff, cited in the channel-adapter delta |
| ~1.1 s/chunk healthy | `notes/send-latency-measurement.md` (n=82, 29 days), cited in the channel-adapter delta |
| "the summary carries no failure notice" | stated in the report requirement |
| grace/lateness boundary strictness ("more than X before/after") | stated in both requirements; both boundaries have a test at the exact value |

**One thing deliberately NOT anchored, flagged so it is not mistaken for a requirement:**
the *relative order* of the three owner-turn blocks (time header, recall, delivered
reminders) is unspecified in the agent-core delta, and the implementation's choice —
time, recall, note, then the owner's message — is an implementation decision, not a
contract. `test_the_follow_up_turn_carries_the_block` asserts the owner's text comes
last, which is stricter than any delta requires. That is fine for a test but should not
be read as spec.

### 10.4 Publication safety

Every **added** line across the ten commits (6,915 of them) scanned for the four
repo-specific shapes plus the general ones:

| check | result |
|---|---|
| tailnet IPs (Tailscale's CGNAT range) | none |
| other private IPs (`10.*`, `192.168.*`, `172.16–31.*`) | none |
| phone numbers | only `+31600000000` and `+31611111111` — **both pre-existing placeholders**, present in 9 and 1 files respectively at `3fed4bd` |
| account UUIDs | none |
| token-shaped literals (`tskey-`, `sk-…`, `ghp_`, bearer, api-key) | none |
| `gitleaks detect --log-opts=3fed4bd..HEAD` | **no leaks found**, 10 commits scanned |

The pre-commit hook (`core.hooksPath=.githooks`) ran clean on every commit; no
`--no-verify` was used at any point, and no finding needed rewording.

---

## Group 11 — pre-deploy measurements (11.1)

Taken 2026-08-20 ~17:26 CEST over Tailscale SSH, read-only throughout.

### Container start times, stated beside each grep

The task asked for these explicitly, and the reason is that `docker logs` resets when a
container is recreated — so a grep's *coverage window* is the container's uptime, not the
change's. Without the start time an empty grep looks far stronger than it is.

| container | started | uptime | what its log covers |
|---|---|---|---|
| `henk-henk-1` | 2026-08-20 12:59:43 CEST | **~4.5 h** | both henk-side watches |
| `henk-signal-cli-rest-api-1` | 2026-07-22 16:12:36 CEST | **~4 weeks** | the latency harvest |
| `henk-tailscale-1` | 2026-07-22 16:12:36 CEST | ~4 weeks | — |

So the henk-side watches are **weak evidence** (4.5 hours, and the container was recreated
at today's reminders-core deploy); the bridge-side month is the strong half. That is the
same caveat the measurement note already carried, now with the number attached.

### Both standing watches — empty

| watch | command | result | coverage |
|---|---|---|---|
| channel-integrity `partial`/`failed`/`giving up` | `docker logs henk-henk-1 \| grep -aiE "partial\|not delivered\|giving up\|outcome="` | **0 matches** | ~4.5 h |
| reminders-core store errors | same log, `grep -acE "StoreError\|could not (store\|read\|update\|count)\|database is locked\|sqlite"` | **0 matches** | ~4.5 h |
| any `ERROR` line at all | same log | **0** (15 log lines total) | ~4.5 h |

Fifteen log lines total is itself the story: the container has been up 4.5 hours and done
essentially nothing, because nobody has talked to Henk since the deploy. Anything
non-empty would have been real; nothing was, but very little was possible.

### The latency harvest, re-run — unchanged, and honestly so

```
n = 82     window 2026/07/22 15:55:51 -> 2026/08/20 11:57:46
status codes: {201: 82}
min 118ms  median 162ms  mean 295ms
p75 258  p90 730  p95 812  p99 1087  MAX 1087ms
over 1s: 2   over 2s: 0   over 6s: 0   over 10s: 0
```

**Byte-identical to `notes/send-latency-measurement.md`.** The window did not extend
because there have been **no sends at all** since the note was taken this morning — so the
re-run confirms the recorded numbers rather than adding to them. Stated plainly because
"re-ran the harvest" could otherwise be read as "extended the window", and it did not.
The design's `~1.1 s` per-chunk figure and the `send_timeout_seconds = 10.0` headroom
(~9×) both stand on exactly the evidence already recorded.

### rp5's listening sockets — the BEFORE snapshot for 11.2

**41 listening sockets** captured via `sudo -n ss -H -tulnp`, stored redacted in the apply
scratchpad. Not reproduced here: the raw output contains rp5's tailnet IPv4 and IPv6
addresses, which this repo does not carry (redacted to `RP5-TS-IP` / `RP5-TS-IP6` before
anything was written toward the repo, and the redaction asserted clean). Task 11.2
compares the count and the port set after the deploy; the secure-deployment scenario is
"unchanged", so the comparison is what matters rather than the list.

### The one measurement 11.1 asked for and could NOT be taken

**rp5's open inbox item count** — which prices design D6's unbounded-`/inbox all`
exposure as a number rather than a guess — needs to read the SQLite store, and every route
to it requires a password:

```
(ALL) NOPASSWD: /usr/bin/docker ps|logs|images|stats|info, /usr/bin/ss, /usr/bin/ip …
(ALL) PASSWD:   /usr/bin/docker *          <-- exec and inspect land here
```

`docker exec` and `docker inspect` are deliberately **not** in the read-only NOPASSWD
allowlist, which is correct — `exec` is not read-only — and this session has no tty for a
sudo prompt. The same constraint blocked reminders-core's task 3.5.

The command to run from a real terminal, read-only (`mode=ro`):

```bash
ssh rp5 'sudo docker exec henk-henk-1 python3 -c "
import sqlite3
c = sqlite3.connect(\"file:/data/audit/henk-store.db?mode=ro\", uri=True)
print(\"inbox open   \", c.execute(\"SELECT COUNT(*) FROM inbox WHERE status=?\",(\"open\",)).fetchone()[0])
print(\"inbox bytes  \", c.execute(\"SELECT COALESCE(SUM(LENGTH(text)),0) FROM inbox WHERE status=?\",(\"open\",)).fetchone()[0])
print(\"reminders    \", c.execute(\"SELECT COUNT(*) FROM reminders\").fetchone()[0])
"'
```

**What is still known without it:** the exposure is a *hold-time* concern, not a
correctness one. D6 already states the healthy-path hold is unbounded in chunk count until
`/inbox all` gains a render bound, and the reminder's consequence is bounded regardless —
the retry floor re-attempts and the grace window bounds the outcome. So this number
prices a **recommended follow-up** to `capture-inbox`, and its absence does not gate the
deploy. It should be taken before that follow-up is scoped.

## Group 11 — 11.2 NOT DONE, and why it is not a judgement call

The task list says 11.3 is the hard stop and 11.2 is the last step before it. **11.2 could
not be executed from this session**, and the reason is the host's own access control
rather than caution:

```
$ ssh rp5 'sudo -n docker compose version'
sudo: a password is required
```

`docker compose` resolves to `/usr/bin/docker *`, which rp5's sudoers puts behind
**PASSWD**. Only `docker ps|logs|images|stats|info`, `ss` and `ip` are NOPASSWD, and this
session has no tty for a prompt. So the rebuild is unreachable, exactly as `docker exec`
was for the inbox count above.

Two further blockers, either of which would be sufficient on its own:

1. **The commits are not pushed.** rp5 deploys by pulling `origin/main`; this session has
   eleven unpushed commits. Pushing to a public-identity, publication-bound repo is the
   owner's call, not a step to slip into a verification task.
2. **One of 11.2's own checks needs a human on Signal.** "Exercise one long reply to
   confirm chunks still arrive in order" requires a real conversation with Henk from the
   owner's account. The send lock is the *only* unflagged behaviour change in this deploy,
   so that check is the deploy's main event — and it is not automatable from here.

### What 11.2 needs, in order, from a real terminal

```bash
# 1. publish (owner's call)
git push origin main

# 2. rp5: pull and rebuild once
ssh rp5 'cd /home/pi/Coding/henk && git pull && sudo docker compose up -d --build'
#    NOTE: rp5's config.yaml is LOCALLY MODIFIED by design. If the pull refuses,
#    that is the protection working — see the memory on rp5's local config.

# 3. the three silent-no-op tells (reminders is still absent from rp5's config)
ssh rp5 'sudo -n docker logs henk-henk-1 2>&1 | tail -30'   # no ConfigError
#    then over Signal: `/remind +2m x` and `/reminders` must BOTH reply
#    "not configured"; no reminder tool registers; no time header is composed.

# 4. the only live behaviour change: the send lock
#    over Signal: `/memories` (or any multi-chunk reply) — chunks must arrive
#    contiguous and in order. This is the check that matters.

# 5. listening sockets unchanged (secure-deployment scenario)
ssh rp5 'sudo -n ss -H -tulnp | awk "{print \$1, \$5}" | sort -u | wc -l'
#    BEFORE this deploy: 41   (full redacted list in the apply scratchpad)
```

**Everything before group 11 is complete and green.** The code is inert on rp5 either
way: rp5's `config.yaml` carries no `reminders` section, so the scheduler cannot start
even once the image is rebuilt — which is why the deploy is safe to do unattended *as a
deploy*, and why its one real risk (the send lock) is the thing that needs a human to
look at a Signal thread.

### 10.4 The commit split, each commit green **in isolation**

Verified by **export-and-overlay**, not by reasoning about the import graph. For each of
the eleven commits, `git archive <sha>` was extracted into a clean directory and the suite
run there with the repo's interpreter. Before each run the harness asserts that `henk`
resolves to the **exported** tree rather than the working copy — without that check every
result below would be meaningless, since a stray `PYTHONPATH` would silently test HEAD
eleven times and report eleven greens.

Import-graph reasoning would have missed the thing that actually breaks a split: a test
landing one commit before the code it exercises, or an assertion still pointing at a guard
that expires in the next commit. Commit 3 is the case in point — it *drops* the count from
1306 to 1302, because retiring three inertness guards removes six tests and adds two. A
split that had put those expiries in the same commit as the repository writes would have
hidden whether the retirement stood on its own.

| # | commit | subject | suite in isolation |
|---|---|---|---|
| 1 | `d6932a4` | docs(openspec): model the cut delivery design, fix three text defects | **1269** passed |
| 2 | `68afc0d` | feat(reminders): add the delivery configuration knobs | **1306** passed |
| 3 | `3c281e7` | test(reminders): retire the inertness guards for delivery | **1302** passed |
| 4 | `0e46a15` | feat(reminders): add the delivery selector and outcome writes | **1345** passed |
| 5 | `8719f6d` | fix(channel): serialize outbound sends so chunks cannot interleave | **1356** passed |
| 6 | `fa41b7a` | fix(audit): pin a reminder record's detail to a closed vocabulary | **1357** passed |
| 7 | `ac40070` | feat(reminders): add the polling delivery scheduler | **1444** passed |
| 8 | `631c5f4` | test(reminders): cover the delivery path's audit receipts | **1459** passed |
| 9 | `22f6f68` | feat(reminders): tell Henk about the reminders he just sent | **1478** passed |
| 10 | `3813736` | feat(reminders): run the delivery scheduler beside the core worker | **1492** passed |
| 11 | `0b7e5fc` | test(reminders): cover the cross-capability delivery contracts | **1503** passed |

**ALL COMMITS GREEN IN ISOLATION.** The monotonic climb — 1269 → 1306 → 1302 → 1345 →
1356 → 1357 → 1444 → 1459 → 1478 → 1492 → 1503 — is the split's own evidence: every
commit is a complete, self-consistent state of the repository, and the one non-monotonic
step has a stated reason.
