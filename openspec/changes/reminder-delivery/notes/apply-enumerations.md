# Apply record — reminder-delivery

> ## State, and what is left — read this first
>
> **In progress.** Started 2026-08-20. Pre-change suite baseline: **1269 passed, 12
> deselected** (the 12 are the opt-in `dst_sweep` zones).
> `openspec validate --changes reminder-delivery --strict` passes.
>
> Group 1 (the model rewrite — the gate) is **DONE**, and it moved the spec: three text
> defects found, all three fixed in the deltas and the design **before group 2 existed**,
> which is what task 1.1's "cheapest moment a spec edit will ever have" means in practice.
> The findings are recorded below with the property that produced each.

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
