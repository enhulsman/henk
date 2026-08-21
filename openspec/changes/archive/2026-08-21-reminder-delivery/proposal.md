# Proposal — Reminder Delivery

## Why

`reminders-core` is deployed and deliberately inert: Henk can accept, store, cancel and list
reminders but structurally cannot keep the promise they encode, which is why the flag ships
false. This change is the clock that delivers — the polling scheduler, the outcome
bookkeeping, the catch-up for downtime, the delivered-reminder note, and the cadence
amendment that makes an owner-scheduled send the *only* exception to "never on a timer". It
is the second half the reminders spec Purpose already names ("the clock that delivers is
specified separately, and reminders ship disabled until it exists"), and it is what allows
`reminders.enabled` to be flipped on rp5.

The scope is the **cut** version from `openspec/changes/archive/2026-08-21-reminders-superseded/notes/README.md`: the fresh
review found the original delivery design over-engineered by about a third, with all six late
criticals living in the excess. What remains: poll, select due, send one message per due
reminder, record the outcome, one retry floor, a crash-attempt bound, grace → missed,
`reported_at`, and the catch-up summary. This change also **inherits outbound send
serialization** from `channel-integrity`'s design D5, and the send-latency measurement that
design demanded before any bound is taken: it exists
(`notes/send-latency-measurement.md` — 29 days of rp5 data, n=82, max 1.087 s).

## What Changes

- **A polling reminder scheduler** runs as a task alongside the core worker when reminders
  are enabled: every tick it selects due work, delivers each due reminder verbatim as its own
  proactive message (no session, no model, no tokens), and records the outcome durably.
- **Delivery bookkeeping becomes live.** The columns `reminders-core` created but never wrote
  — `next_attempt_at`, `send_attempts`, `delivered_at`, `reported_at` — get their writers:
  pre-work and post-send transactions on `Store.transaction()`, a fixed retry floor for
  failed sends, a crash-attempt maximum evaluated in the pre-work transaction, and the
  `delivered` / `delivered-late` / `missed` / `abandoned` transitions with their audit
  records (already enumerated by audit schema v4 — **no version bump**).
- **Catch-up, always named, bounded where it is owner-visible:** overdue-within-grace
  delivers late stating its original due time, paced by a per-tick delivery limit rather
  than burst; overdue-beyond-grace goes to `missed` and is named in a catch-up summary (no
  item bound, no pagination — the message splits like any long send, which dissolves the
  stranded-rows defect the README carries as open defect #1). A wholly failed summary
  retries on the floor until the channel recovers — a channel outage never forfeits the
  report; the owner-visible loops terminate instead: at the crash-attempt limit, and — for
  a summary that keeps returning partial — at a report horizon evaluated post-send, after
  the rows were named at least once.
- **Outbound sends are serialized** in the channel adapter, so chunks of concurrent senders
  can no longer interleave. Justified now rather than in `channel-integrity` because the
  scheduler is the first real cross-task sender, and priced by the measurement: ~1.1 s per
  chunk at the observed maximum, ~20 s for the worst reply shape that exists in practice —
  additive to the poll interval, honestly unbounded in chunk count until a recommended
  follow-up bounds `/inbox all` (design D6). The delivery-timing scenarios are amended
  accordingly (the option D5 explicitly offered alongside delivery-path priority).
- **The delivered-reminder note:** a delivered reminder the owner has not yet replied about
  is injected once into the next owner turn as a delimited data block (window-bounded,
  count-bounded, durable via `surfaced_at`, never on event turns, never tainting).
- **The cadence amendment:** `incident-triage`'s "never on a timer" gains its settled
  two-class enumeration — owner-scheduled delivery is owner-initiated content whose delivery
  moment is deferred; it consumes no incident cap; system-scheduled digests/heartbeats stay
  banned.
- **Delivery is app-initiated and outside the approval gate**, authority having been granted
  at scheduling; a pending approval is unaffected by a delivery.
- **Enablement on rp5** (hard stop, owner go): set `owner.timezone` and `reminders.enabled`
  in rp5's local config, deploy, verify end-to-end, and give the README's tool table its
  deferred pass — the follow-up `reminders-core` recorded as riding this exact flip.

## Capabilities

### New Capabilities

*(none — delivery completes the existing `reminders` capability rather than introducing one)*

### Modified Capabilities

- `reminders`: adds the delivery half — scheduler ticks and due selection, verbatim
  sessionless delivery, outcome recording with the two-budget separation, retry floor,
  crash-attempt bound, grace → missed, the catch-up summary with `reported_at`, and the
  delivered-reminder note's data contract; amends the delivery-timing scenario for the
  serialized send path.
- `channel-adapter`: outbound sends are serialized — concurrent senders' chunks never
  interleave; the failure notice already specified as "within the same serialized sequence"
  gets the sequence it names.
- `agent-core`: the scheduler runs alongside the core worker (started/cancelled with it,
  never enqueues turns, tick failures isolated); owner turns carry the delivered-reminder
  block when an unsurfaced delivery exists.
- `incident-triage`: the cadence requirement's two-class enumeration (owner-scheduled
  delivery is not a timer in the banned sense; reminder deliveries do not consume the
  incident cap).
- `approval-gate`: scheduled delivery is app-initiated and outside the gate; a delivery
  leaves a pending approval intact.
- `secure-deployment`: the scheduler introduces no inbound surface — no listener, no port,
  no new volume or grant.

*(No `audit-log` delta: schema v4 already defines the complete transition enumeration and
`initiated_by: scheduler` precisely so delivery ships without a version increment.)*

## Impact

- **Code:** new `henk/reminders/scheduler.py` (tick loop, selector, transactions, catch-up
  composition); `henk/store/reminders.py` gains the delivery-bookkeeping writes;
  `henk/channel/signal.py` + `henk/channel/base.py` gain send serialization;
  `henk/agent/core.py` gains the delivered-reminder block composition; `henk/runtime.py` /
  `henk/app.py` start and cancel the scheduler task; `henk/config.py` `RemindersConfig`
  gains the delivery knobs (poll interval, retry floor, crash-attempt maximum, grace window,
  lateness threshold, report horizon, per-tick delivery limit, note window/count).
- **Constraints inherited:** every new column already exists (`reminders-core` shipped the
  complete final column set); audit v4 already validates every transition this change
  writes; `Store.transaction()` and `tests/test_store_transaction.py` are the contract the
  pre-work/post-send transactions build on; no store call may be dispatched off the event
  loop (a grep-based test enforces the single-connection assumption).
- **Verification:** the fault-injection matrix from
  `openspec/changes/archive/2026-08-21-reminders-superseded/notes/verify_selector_invariants.py` is retargeted at the real
  store and scheduler; the model itself is updated to the cut design first, since a defect
  found in the model is unfixed until the requirement text changes.
- **Deployment:** no new infrastructure surface. Enabling is a host-side edit to rp5's
  locally-modified `config.yaml` (`reminders.enabled`, `owner.timezone`) plus a rebuild;
  rollback is setting the flag false — stored rows are untouched either way.
- **Not touched:** `owner-acknowledgement` (independent, nothing here depends on it);
  recurrence, snooze, edit-in-place (non-goals since the original design); the original
  `openspec/changes/archive/2026-08-21-reminders-superseded/` draft remains superseded and is not implemented.
