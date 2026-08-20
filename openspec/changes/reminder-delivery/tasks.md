# Tasks — Reminder Delivery

**Read first:** `design.md`; `notes/send-latency-measurement.md`;
`openspec/changes/reminders/notes/README.md` (the cut list and the settled list are binding);
`channel-integrity` design D5/D6. TDD throughout: every task-group's tests come from this
change's spec scenarios before its implementation, and no concurrency property may be proven
with a cooperative double.

## 1. The model, updated with the spec (design D11 — before any code)

Group 1 is the apply session's first act and its gate: if 1.2 disagrees with the delta text
on any property, the delta is edited THEN — before group 2 exists, recorded in the apply
notes — which is deliberately the cheapest moment a spec edit will ever have.

- [x] 1.1 Rewrite `openspec/changes/reminders/notes/verify_selector_invariants.py` into this
      change's `notes/` to model the CUT design exactly: no backoff schedule, no
      `unconfirmed_sends`, no `terminal_at`, no report item bound; one retry floor, crash
      maximum pre-work, grace → missed, per-tick delivery pacing, the selector's `due_at`
      conjunct, summary marks `reported_at` only on `delivered`, and BOTH report give-up
      exits in their stated places — the crash limit in pre-work, the report horizon in the
      post-send write of a partial summary. Keep the stated-non-coverage header current.
- [x] 1.2 Re-run its properties — termination under crash faults, **termination under a
      deterministically partial summary send (the horizon property)**, detectability where
      termination is impossible (the wholly-failed summary, which retries forever by
      design), quiescence under channel failure, conservation — now including: **every row
      that reaches a terminal report state was either delivered or named in at least one
      attempted summary** — and partial handling, with faults at every stage boundary
      (pre-work commit, grace transition, each send, each post-send commit; tri-valued
      channel double). Walk the composition the review could only reason about: a summary
      whose send returns `delivered` but whose post-send transaction fails must re-loop into
      the crash bound, never lose rows. A defect found here is fixed by changing the
      requirement text in this change's deltas FIRST, then the model.

## 2. Configuration

- [x] 2.1 Tests: `RemindersConfig` gains `poll_interval_seconds` (30), `retry_floor_seconds`
      (900), `crash_attempt_limit` (3), `late_grace_seconds` (86400),
      `late_delivery_threshold_seconds` (300), `report_horizon_seconds` (86400),
      `tick_delivery_limit` (10), `note_window_seconds` (43200), `note_max_items` (10); each
      non-positive value fails load naming the setting; threshold ≥ grace fails load;
      horizon ≤ floor fails load; no widening knob exists (assert the config surface by
      field enumeration). The
      system-prompt hash tests must pass untouched — these knobs do not reach the prompt.
- [x] 2.2 Implement in `henk/config.py`. `crash_attempt_limit` is deliberately NOT named
      `max_send_attempts` — that name is the bridge's HTTP retry budget.

## 3. Store: the delivery writes

- [x] 3.1 Pre-flight: re-grep `tests/test_reminders_inert.py` (and any other guard the grep
      surfaces) and reconcile the expiry list in 3.3 against what actually exists — the
      enumeration is re-derived, never trusted. Then tests from the selector scenarios:
      selection returns `pending` rows with `next_attempt_at <= now` **and `due_at <= now`**
      (a future-due reminder with an eligible `next_attempt_at` is never selected — assert
      it), capped at `tick_delivery_limit` oldest-due first with uncharged unselected rows,
      and `missed`/`abandoned` rows with `reported_at IS NULL` and `next_attempt_at <= now`,
      uncapped; every exit (delivered, delivered-late, missed, abandoned, reported, give-up)
      is re-checked by re-running the selector against a reopened store, not by reading back
      the row alone.
- [x] 3.2 Tests for the two transactions on `Store.transaction()`: pre-work (grace
      transitions for ALL past-grace pending rows, then selection, then increments +
      both give-up exits — crash limit and report horizon) commits or rolls back as one;
      post-send writes clear `send_attempts` on every return; a poisoned inner scope rolls
      back the whole tick's pre-work. Process death is simulated by dropping the connection
      between the transactions, then reopening the store.
- [x] 3.3 **Deliberately expire the reminders-core inertness guards — BEFORE writing the
      repository methods, and enumerated in full** (the reminders-core archive refers to
      this as "task 3.4"; renumbered here so the expiry precedes the code it unblocks):
      - `test_nothing_in_this_change_writes_a_delivery_column` (the parametrized
        grep guard): replace with its successor — with reminders **disabled**, no scheduler
        task exists and no runtime path writes `surfaced_at` / `send_attempts` /
        `delivered_at` / `reported_at`.
      - `test_the_reminder_repository_writes_only_status_and_next_attempt_at` (AST guard on
        `UPDATE reminders`): expires outright — its successor is 3.1's selector/exit suite,
        which asserts what the repository DOES write per exit.
      - `test_there_is_no_scheduler_and_no_send_in_this_change` (asserts
        `henk/reminders/scheduler.py` does not exist): expires outright — its successor is
        the disabled-path assertion above plus group 8's no-task-when-disabled tests.
      - `test_no_cadence_amendment_rode_along` **survives untouched**: design D9 is spec
        text plus scheduler behaviour, no `PipelineConfig` field — the implementation must
        keep it green, and that is a deliberate constraint, not an accident.
      Report every edit and reason in the apply notes, per the edited-tests rule; task 10.1
      expects exactly these three and no others.
- [x] 3.4 Implement the repository methods in `henk/store/reminders.py`, transaction-agnostic
      like every sibling. No store call off the event loop (the existing grep-based test is
      the guard; do not weaken it).
- [x] 3.5 Add the transaction/await AST guard (design D3): a test, modelled on the
      process-timezone guard, that fails on any `await` inside a `with …transaction()` body
      anywhere in `henk/`. Watch it go red by temporarily introducing one, per the
      guard-never-seen-to-fail rule.

## 4. Channel: send serialization

- [x] 4.1 Tests from the channel-adapter delta scenarios, against `SignalAdapter`'s real lock
      with a slow `FakeBridge` (per-send `await` that actually yields): two concurrent
      multi-chunk sends never interleave chunks; both complete with their own outcomes; the
      failure notice lands before the waiting sender's first chunk; a waiting send is never
      dropped. `conftest.FakeChannel` is forbidden for these — it has no lock.
- [x] 4.2 Watch 4.1 go red first against the current lockless adapter (the interleaving must
      be demonstrated, not assumed), then implement: one `asyncio.Lock` in `SignalAdapter`
      around `_send_serialized`'s body, covering the notice. No lock in `send`/`send_proactive`
      wrappers (D3's re-entry argument), no hold timer, no chunk cap, no priority tier.
- [x] 4.3 Assert the existing channel contract tests pass untouched — serialization is
      additive for every existing single-sender caller.

## 5. Scheduler: tick, delivery, grace, summary

- [ ] 5.1 Tests from the delivery scenarios: due → delivered within a tick, verbatim, marked;
      late (past threshold) states original due time via `render_instant` and records
      `delivered-late`; cancelled never delivers; **cancellation committing between selection
      and dispatch is skipped by the pre-send status re-read (drive it with the real lock
      held by a slow fake bridge); cancellation after dispatch records `delivered` with both
      audit records**; a future-due reminder is never delivered however many ticks run;
      oldest-due first; one message per reminder; individual deliveries before the summary;
      `failed` → floor retry, no attempt before the floor; single-reminder `partial` →
      delivered + error log, never re-sent; **the failure notice names the rendered due time,
      reads "could not be fully delivered", accompanies first/crash attempts only — a
      persistent failure yields no notice on floor retries**. The three delivery-timing
      scenarios are owned here and MUST run against the adapter's real lock with a slow fake
      bridge, never `conftest.FakeChannel`: delivery does not wait on a turn; delivery waits
      on an in-flight send but is never skipped or interleaved; within-a-tick delivery when
      nothing is in flight.
- [ ] 5.2 Tests from the counting/crash scenarios: increment visible after a simulated death
      mid-send; counter cleared on every return; crash loop exits to `abandoned` at the limit
      inside pre-work; abandoned is named in the summary; send-then-mark death → redelivery
      within the bound, never silence.
- [ ] 5.3 Tests from the grace/summary/pacing scenarios: within-grace → `delivered-late`;
      beyond → `missed` + summary; nothing overdue → zero messages; **a 100-row within-grace
      backlog is delivered at most `tick_delivery_limit` per tick, oldest-due first, none
      dropped and none attempt-charged while unselected**; summary names every unreported row
      (drive with 100+ rows and assert none omitted from composition); `reported_at` written
      only on `delivered`; `partial`/`failed` summary marks nothing and retries on the floor;
      **the summary carries no failure notice on any outcome; a deterministically-partial
      summary terminates — per-row sends bounded by horizon/floor, the give-up exit written
      in the post-send write of an attempted summary, with an error log; a wholly failed
      summary is NEVER given up on a channel outcome — drive the channel down past the
      horizon, recover it, and assert the next summary names every unreported row; a stale
      backlog (rows due longer ago than grace + horizon, e.g. a 7-day-old row) is named in
      an attempted summary before any give-up can fire**; composition names exactly the
      selected report rows plus same-tick abandoned exits — a row cooling on the floor is
      not renamed early; the summary names rows in selection order, oldest-due first;
      reported rows never resurface; report crash-loop give-up writes `reported_at` + error
      log.
- [ ] 5.4 Tests for tick isolation: a store error mid-tick rolls back and the next tick
      succeeds; a channel exception does not kill the task; a tick captures `now` once
      (inject a stepping clock and assert one tick cannot disagree with itself).
- [ ] 5.5 Implement `henk/reminders/scheduler.py`. Instants only (epoch seconds) — no wall
      clocks, no zone reads; rendering goes through `render_instant`. The AST-based
      process-timezone guard's scope must cover the new module — and it fails on ANY zero-arg
      call named `now` or `today`, so do not name a scheduler method `self._now()`; the
      guard's own history says the fix is renaming, not exempting.
- [ ] 5.6 Retarget the model's fault-injection matrix at the real store + scheduler (design
      D11): faults at pre-work commit, grace transition, each send, each post-send commit,
      tri-valued channel double, process-death cases. Then mutation-check the key assertions
      — at minimum: drop the pre-work increment, evaluate the crash max post-send, mark
      `reported_at` on `partial`, skip the grace clear of `send_attempts`, **drop the
      selector's `due_at` conjunct, remove the report horizon, move the horizon check into
      the pre-work transaction (the stale-rows-named-first scenario must go red), skip the
      pre-send status re-read, hold a transaction across an await** — and watch each go
      red. Record survivors and what they revealed.

## 6. Audit records

- [ ] 6.1 Tests: `delivered`, `delivered-late`, `missed`, `abandoned` records carry
      `initiated_by: "scheduler"`, the id, due time and timestamp, no reminder text, and
      validate against the **existing v4 document — assert `SCHEMA_VERSION == 4` is
      untouched**; a partial-mapped delivery's record carries `detail: "partial"` (v4's free
      `detail` property — no version bump); records are appended at the transition (kill
      after the transition commit, find the record after restart); a rejected/failed send
      writes no transition record.
- [ ] 6.2 Implement using the existing `reminder_record` builder; appends sit beside the
      state writes per design D3.

## 7. The delivered-reminder note

- [ ] 7.1 Tests from the note scenarios: follow-up turn carries the delimited block (framed
      as sent messages, never instructions); at most once (`surfaced_at` durable — assert
      across a store reopen); window- and count-bounded, newest first; event turns never;
      no taint (a `capture` after the block executes normally); absent when empty; injected
      even when the recall block was already given.
- [ ] 7.2 Implement the block composition in the owner-turn path beside the recall block and
      time header; `surfaced_at` written at composition time.

## 8. Runtime wiring

- [ ] 8.1 Tests: enabled → scheduler task starts with the app and is cancelled on shutdown
      with nothing left pending; disabled → no task exists; scheduler failure leaves replies
      and triage working; core failure leaves the scheduler ticking; **the scheduler is
      handed the same adapter instance as the core** — the send lock is instance state, and a
      second adapter would satisfy every channel scenario while serializing nothing.
- [ ] 8.2 Wire in `henk/runtime.py` / `henk/app.py` following the coordinator precedent.

## 9. Cross-capability contracts (the deltas with no code of their own)

- [ ] 9.1 Cadence (incident-triage delta): a reminder delivery leaves the announceable-
      incident cap count unchanged; with reminders enabled and nothing due, a long simulated
      run sends zero unprompted messages (no digest, heartbeat, or "all is well" path
      exists to fire).
- [ ] 9.2 Gate (approval-gate delta): a delivery while an approval is pending sends the
      reminder, creates no approval record, and leaves the pending approval resolvable by
      the owner's next keyword; a delivered reminder's audit trail carries both the
      `scheduled` record and the delivery record (traceability scenario).
- [ ] 9.3 Re-enablement (reminders delta): with stored reminders due while disabled,
      re-enabling delivers within-grace rows late (stating original due times) and misses +
      summarises beyond-grace rows — drive with two app instances sharing one store across a
      flag flip, and use a **stale** offset (due longer ago than grace + horizon, e.g.
      3+ days) so the test exercises the case where reportability arrives later than
      `due_at + grace`, not just the 25-hour one.
- [ ] 9.4 Surface (secure-deployment delta): assert at test level that the scheduler module
      opens no socket, registers no handler, and ticks only from the in-process clock —
      the runtime inspection half lands in 11.2.

## 10. Verification

- [ ] 10.1 Full suite green, including `pytest -m dst_sweep`. Record pre- and post-change
      counts. Enumerate every edited existing test with its reason — expected: exactly the
      three 3.3 expiries; anything else must be justified. Produce the scenario→test table
      (every scenario in every delta names at least one test), like reminders-core's 9.2
      table.
- [ ] 10.2 `openspec validate --changes reminder-delivery --strict` passes.
- [ ] 10.3 Settled-list check against the implementation (the reminders README list plus this
      change's D4 asymmetry), recorded like reminders-core's 9.2 table. Include the anchor
      sweep: for every numeric or ordering claim in the deltas (selector conjunct, horizon
      anchor, composition order, notice recognition), name the requirement or config value
      that makes it true — a claim true only because of an unwritten code fact is a defect
      of the class that produced three findings in review.
- [ ] 10.4 Publication safety: `.githooks/pre-commit` pattern layer over every added line; no
      tailnet IPs, tokens, or real phone numbers; commit split with each commit verified
      green in isolation by export-and-overlay, not import-graph reasoning.

## 11. Deploy and enable (hard stop — owner go required)

- [ ] 11.1 Re-run the latency harvest (`notes/send-latency-measurement.md`) and both standing
      watches (channel-integrity `partial`/`failed`; reminders-core store-error grep) over
      the extended window; **record each container's start time beside its grep so the
      coverage window is stated, not inferred** (docker logs reset on recreation). Record
      rp5's open inbox item count (one query) — it prices design D6's unbounded-`/inbox all`
      exposure as a number. Anything non-empty is real.
- [ ] 11.2 Deploy the code (still inert — rp5 config carries no `reminders` section). Verify
      the three silent-no-op tells from the reminders-core apply record: no reminder tool
      registers, `/remind`//`/reminders` reply "not configured", no time header is composed.
      Confirm the only live behaviour change is the send lock; exercise one long reply to
      confirm chunks still arrive in order; inspect the container's listening sockets and
      confirm they are unchanged (secure-deployment scenario).
- [ ] 11.3 **STOP — owner go.** Host-side edit to rp5 `config.yaml`: `owner.timezone`
      (Region/Location key) + `reminders.enabled: true`. One restart. Startup must NOT fail;
      if it does, the validation error names the bad setting — fix, don't bypass.
- [ ] 11.4 End-to-end on the live system, per the design's migration step 4: `/remind +2m`
      delivers verbatim with the marker; the follow-up turn carries the note; stop-past-due
      restart within grace → `delivered-late` stating original due; a forced beyond-grace row
      → `missed` + summary — backdate it **beyond grace + horizon (> 48 h at defaults)**, so
      the live check exercises the stale-row path where a pre-work horizon would have gone
      silent; a `/reminders cancel` issued while a delivery is in flight
      behaves per the race rules; audit records validate; `/reminders` list/cancel/reinstate
      unchanged. Record each in the As-built.
- [ ] 11.5 Give `README.md` its deferred pass — tools table, `henk/tools/` row, owner-command
      list now including `/remind` and `/reminders` — the follow-up reminders-core recorded
      as riding this exact flip.
- [ ] 11.6 `/opsx:archive` with the deploy verification recorded. Update the reminders spec
      Purpose at archive time (it still says "the clock that delivers is specified
      separately") — after this change, it isn't. Add the delivered-reminder watch
      (`abandoned` / summary / give-up greps) as a NEW standing watch for the delivery path;
      channel-integrity's `partial`/`failed` watch **stays open** — it covers the reply and
      triage paths, whose outcomes still have no durable consumer until
      `owner-acknowledgement` provides one.
