# Design — Reminder Delivery

## Context

`reminders-core` shipped the store (complete final column set, `Store.transaction()`), the
time model, the tools and commands — inert behind `reminders.enabled: false`. This change is
the clock: the first path where Henk speaks because a clock said so, and the first real
second sender on the channel. Required reading, in order:

- `openspec/changes/reminders/notes/README.md` — the cut list, the two open defects, the
  settled-decisions list. Everything under "Settled — do not re-litigate" is inherited here
  verbatim and is not re-argued.
- `channel-integrity` design D5/D6 (archive 2026-08-20) — why serialization was deferred to
  this change, the two rejected bounds, and the consequence for the delivery-timing scenario.
- `notes/send-latency-measurement.md` (this change) — the measurement D5 demanded before any
  bound: 29 days of rp5 bridge-side data, n=82, median 162 ms, p95 812 ms, **max 1.087 s**,
  zero failures. The numbers below cite it.
- `reminders-core`'s as-built (`archive/2026-08-20-reminders-core/notes/apply-enumerations.md`)
  — what the store half already proved in production, and the standing store-error watch.

What already exists and is reused unchanged: `send_proactive` with the caller-supplied
failure notice and the tri-valued `SendOutcome`; `Store.transaction()` (reentrant, poisoning;
`tests/test_store_transaction.py` is the contract); audit schema **v4, which already
enumerates every delivery transition and `initiated_by: scheduler`** — this change bumps no
schema version; the 13-column reminders table where `next_attempt_at` is `NOT NULL DEFAULT 0`
and nothing yet writes the delivery columns; the shared renderer `render_instant`; the
coordinator precedent for a supervised task beside the core worker.

## Goals / Non-Goals

**Goals:**

- Every `pending` reminder whose instant passes is delivered verbatim, or the owner is told
  it could not be — never silence. Duplicate delivery is the accepted failure mode; silent
  loss is not. (Two bounded, error-logged residuals: a row whose every naming rode the lost
  tail of a persistently partial summary past the report horizon — made nearly unreachable
  by oldest-first composition, priced in Risks — and a report row retired by the pre-work
  crash-limit give-up after repeated process deaths at the reporting stage, inherent to the
  settled crash-maximum placement.)
- Delivery costs zero tokens, creates no session, and cannot reword what the owner scheduled.
- Every terminal transition is durably receipted at the moment it happens, and every exit
  writes state the selector tests — no in-memory exits, because the selector is a query.
- Concurrent senders' chunks never interleave, at a head-of-line cost priced from measurement
  rather than estimate.
- The attention contract's "never on a timer" survives as a two-class enumeration a reader
  can still audit: owner-scheduled content, delivered late by design, and nothing else.

**Non-Goals:**

- Recurrence, snooze, edit-in-place, reminder priorities (unchanged from the original design).
- Delivery-path priority and send pagination — designed, and rejected below on the measured
  numbers (D6). Recorded so the next change does not re-derive them.
- The retry/report machinery the cut list removed: the seven-step backoff schedule,
  `unconfirmed_sends`, `terminal_at`, chunk-atomic batching, the report item bound and its
  pagination. Rounds 6–11 produced six consecutive criticals inside exactly this machinery.
- Acting on a non-delivered approval prompt (channel-integrity D6 — still waiting on
  `owner-acknowledgement`'s corroboration).
- `owner-acknowledgement` itself: nothing here depends on it.

## Decisions

### D1 — A polling scheduler with no startup special case

An async task ticks every `poll_interval_seconds` (default 30). Each tick captures the
current instant **once**, opens the pre-work transaction, sends, and records outcomes. There
is no separate startup catch-up pass: the first tick's selector finds whatever downtime left
behind, because catch-up is a property of the selector, not a boot ritual. Polling rather
than sleep-until-next-due: no wake-up bookkeeping on schedule/cancel, robust to clock jumps
and suspend/resume, and one poll interval of latency is the honest reading of "at 18:00".

### D2 — The selector is a query, and every exit writes the state it tests

Selected per tick, oldest-due first:

- `pending` rows with `next_attempt_at <= now` **and `due_at <= now`** — delivery work,
  capped at the per-tick delivery limit (default 10);
- `missed` and `abandoned` rows with `reported_at IS NULL` and `next_attempt_at <= now` —
  report work, uncapped (the summary is one message regardless of row count).

The `due_at` conjunct exists because `next_attempt_at` is doing two jobs — retry scheduling
and eligibility — and the invariant that makes the single-column version safe
(`next_attempt_at` initialized to the due instant on every path into `pending`) lived only in
code until this change. It is now a stated requirement (the MODIFIED initialization
requirement in the reminders delta), **and** the selector carries the conjunct anyway:
defence in depth costs one SQL clause, and it makes early delivery unrepresentable rather
than merely unlikely. The schema default for `next_attempt_at` is `0` — *eligible now* — so
a single-column selector is one initialization bug away from delivering every future
reminder on the first tick.

The per-tick delivery cap paces a within-grace backlog (a sub-24 h outage delivers late
individually — up to the pending cap of 100 rows) into bounded bursts instead of one
hundred-message blast: unselected rows are untouched — not charged, not written — and remain
eligible, so pacing costs nothing in bookkeeping. The message *count* is unchanged by
design: each is an owner-authored promise; only the arrival rate is bounded.

Exits: delivery success writes `delivered`/`delivered-late`; permanent give-up writes
`abandoned`; grace expiry writes `missed`; reporting writes `reported_at`. Each is a column
the selector predicates on (settled).

### D3 — Two transactions per tick, and the crash maximum lives in the first

**Pre-work transaction** (one `Store.transaction()` scope), in a stated order — grace first,
then selection against the post-grace state, then increments and bounds for the selected
rows: apply grace transitions to **every** `pending` row past the window, selected or not
(`now > due_at + late_grace_seconds` → `missed`, audit record, `send_attempts` cleared,
`next_attempt_at = now`, `reported_at` left NULL so the same tick's summary names them);
select; increment `send_attempts` for every selected row; evaluate the crash maximum
(`crash_attempt_limit`, default 3) **here, beside the increment** — a `pending` row at the
limit exits to `abandoned` (audit record, counters cleared, `reported_at` NULL so the summary
names it as a delivery Henk gave up on — it joins that composition without a second
increment: charged rows are exactly the selected rows); a `missed`/`abandoned` report row at
the limit gets `reported_at = now` written as its give-up exit, with an error log. The
crash-attempt give-up is the **only** give-up evaluated pre-work: D5's report horizon is
evaluated in the post-send write instead, because a pre-work horizon would retire a row that
arrived already stale — a restart after long downtime — before any summary ever named it,
and "never delivered and never reported" is the one outcome this capability exists to make
impossible. The two bounds live on opposite sides of the send for symmetric reasons: a crash
is what prevents the post-send write, so its bound must be pre-work; a channel outcome
exists only after the send returns, so its bound can and must be post-send. Settled and
re-verified by the model at
84-vs-3: a crash is what prevents the post-send transaction, so a maximum evaluated there is
never evaluated on the path it exists to bound.

**Post-send transaction(s)**: write the outcome per sent message — status, `delivered_at`,
`reported_at` for the summary's rows — and **clear `send_attempts` on every return**, success
or failure. That is the two-budget separation (settled): `send_attempts` accumulates only
when a post-send write does not happen — process death being the case it exists for, a
persistently failing post-send store write the degenerate cousin that exits through the same
bound. Channel failures are governed by the retry floor and the grace window instead, which
is how "not 2,880" is kept without a backoff schedule.

**No transaction scope spans an await.** `Store.transaction()` is reentrant by *instance*
depth, not per task; the moment two tasks share the store, a scope held across an await could
join the other task's transaction and be silently rolled back with it. This change introduces
the second concurrent writer, so the invariant becomes load-bearing for the first time: the
pre-work transaction closes before the first send's await, every post-send write opens its
own scope, and an AST guard (modelled on the process-timezone guard) fails the suite on any
`await` inside a `with …transaction()` body anywhere in the codebase.

Audit records are appended beside the state writes at each transition (the JSONL log is not
in the SQLite transaction; a crash between commit and append loses one receipt, the same
exposure every existing receipt has).

### D4 — One message per due reminder; outcome mapping is asymmetric by content

Each due reminder is its own `send_proactive`: the fixed reminder marker, the stored text
**verbatim**, and — when the delivering tick runs more than `late_delivery_threshold_seconds`
(default 300) past the due instant — its original due time via `render_instant`. Status
becomes `delivered` or `delivered-late` accordingly. The typical tick carries one reminder;
the outage case is the summary's job — so no batching, no measure-before-add, no straddle
rule (cut #4); a within-grace backlog is paced by D2's per-tick cap.

**Immediately before dispatching each send, the row's status is re-read** (a synchronous
store call — no await sits between the re-read and the dispatch) and a row no longer
`pending` is skipped: the pre-work selection is stale by however long the earlier sends in
the tick held the lock, which D6's serialization makes tens of seconds, not microseconds. A
cancellation that commits *after* dispatch — during the lock wait or mid-flight — may still
deliver; the post-send write records `delivered`, because the message factually reached the
owner, and the audit trail carries both transitions. The residual window is one send
sequence, stated in Risks.

The caller-supplied failure notice is the scheduler's because the scheduler is the layer
that knows what was being sent (channel-integrity D2) — and it must survive two facts the
first draft missed: for a realistic single-chunk reminder the adapter returns `failed`, not
`partial`, so "part of this reminder could not be delivered" would be false (nothing was
delivered); and the notice is a separate short send that can succeed while the reminder's
content fails, so an every-retry notice is an every-15-minutes notice. So: the notice reads
that a reminder **could not be fully delivered** (truthful for both shapes) and names the
reminder's rendered due time (the owner's handle on which promise it concerns), and it is
supplied only on first or crash-retried attempts — floor retries pass no notice
(`send_proactive`'s `failure_notice` is already optional). A first-or-crash attempt is
recognizable without new state: its `next_attempt_at` still equals `due_at` (the MODIFIED
initialization requirement), while a floor retry's is later. Worst case per reminder:
`crash_attempt_limit` notices per grace window, not 96.

Outcome mapping:

- `delivered` → `delivered`/`delivered-late`.
- `partial` → **also `delivered`/`delivered-late`**, error-logged, and the audit record
  carries `detail: "partial"` (v4's free `detail` property — no version bump), so the
  degraded delivery is durable, not just a log line. A reminder is one text; the head that
  landed *is* the reminder, the notice told the owner it was cut, and a retry would re-send
  the whole text as a duplicate. (In production this arm is nearly unreachable — a 500-char
  reminder is one chunk, and one chunk fails whole — so the asymmetry's weight rests on the
  summary side.)
- `failed` → row stays `pending`, `next_attempt_at = now + retry_floor_seconds` (default
  900), counters cleared. One fixed floor, no schedule (cut #3).

**The summary maps `partial` differently, and the asymmetry is the point.** The catch-up
summary is one message naming exactly this tick's report set — the selected report rows
(null `reported_at`, `next_attempt_at <= now`) plus rows that exited to `abandoned` in the
same tick's pre-work, and no others, so a row cooling on the floor is not renamed early —
with no item bound and no pagination (cut #1, which dissolves open defect #1: composition
can no longer omit a charged row, so no row is ever incremented without a post-send write).
Long summaries split into chunks like any long send, and the summary carries **no failure
notice**: it is itself the last-resort report, and its failure must not spawn a second
owner-visible message. On `delivered` → `reported_at = now` for every named row. On
`partial` or `failed` → `reported_at` stays NULL for **all** of them and the floor schedules
a retry: the summary carries N distinct promises, the lost tail is *other reminders*, and a
duplicated head beats a row that silently vanishes (duplicate-beats-loss, settled). A
reminder's partial is mostly-delivered content; a summary's partial is undelivered rows.
Retrying-on-partial is owner-visible each time (the head lands again), so it is bounded by
D5's report horizon, evaluated post-send on the rows just named — the asymmetry keeps
duplicate-beats-loss per row without buying an unbounded loop.

Delivery order within a tick: individual reminders oldest-due first, then the summary — the
timely message before the stale news.

### D5 — Quiescence and termination, stated per failure mode

- **Channel down, reminder pending:** retries on the 15-minute floor until the grace window
  expires, then exits to `missed` and joins the summary — bounded sends (~96/day worst
  case), guaranteed exit. While the channel is fully down nothing is owner-visible (the
  notice's own single-attempt send fails too); when only the reminder's content fails and
  the short notice lands, the owner sees at most `crash_attempt_limit` notices, because
  floor retries carry none (D4).
- **Crash loop:** `send_attempts` survives death, hits the limit in ≤ `crash_attempt_limit`
  deliveries, exits to `abandoned`, gets named in the summary. Owner-visible duplicates are
  bounded by the same limit.
- **Summary not delivered:** the two channel outcomes cost the owner differently and are
  bounded differently. A **`failed`** summary (nothing landed — no chunk, and the summary
  carries no failure notice) retries on the floor indefinitely: costless to the owner,
  error-logged per attempt, lands on channel recovery — the last-resort promise is never
  given up on a channel outage, and this remains the model's "detectability where
  termination is impossible" case. A **`partial`** summary re-delivers its head chunks on
  every retry, so it is bounded by the report horizon (`report_horizon_seconds`, default
  86400): in the **post-send write** of the attempted summary, any named row older than
  `due_at + grace + horizon` takes the same give-up exit the crash limit already defines —
  `reported_at` written, error logged, no new audit transition. Post-send placement is what
  makes the bound safe: every row that reaches the horizon give-up was named in at least one
  attempted summary, where a pre-work horizon would have silently retired rows that arrived
  already stale, unnamed. The horizon is anchored on `due_at + grace`, and that anchor is the
  moment of reportability only for a row whose grace transition ran on time — so the per-row
  attempt count is **three-valued, and the model measured each** (its `abandoned_anchor`
  arm): a row that went `missed` on time gets ~`horizon / floor` ≈ 96 (measured 97 — the
  extra is the final attempt in whose post-send write the give-up is written); a row that
  exited to `abandoned` becomes reportable within a few ticks of its due instant, because
  nothing waits a grace window to abandon, so its anchor sits a full grace window further out
  and it gets ~`(grace + horizon) / floor` ≈ 192 (measured 193) — the **largest** of the
  three; a row that arrived already stale after long downtime gets exactly one (measured 1).
  This design's first draft quoted the 96 for every row, which understated the `abandoned`
  case by 2×; the model is what caught it. The bound is per row, not per summary: a rolling
  stream of newly-missed rows keeps the summary retrying on the newcomers' account, so the
  absolute worst is the pending cap times the largest per-row figure (100 × ~192) — bounded,
  where the previous design was not, and never yet observed on this host (29 days, zero
  non-201). Composition order makes the residual nearly
  unreachable in practice: the summary names rows in selection order, oldest-due first, so
  the horizon-eligible rows sit in the head chunks — exactly the part a partial send did
  deliver.
- **Store error mid-tick:** the transaction rolls back (poisoned scope), the tick is
  abandoned with an error log, the next tick retries. The scheduler task survives every
  per-tick exception; it is cancelled only on shutdown.

### D6 — Serialization is a plain send lock, and the measurement is why

An `asyncio.Lock` in the adapter around `_send_serialized`, covering both `send` and
`send_proactive` (they already share that one path, and channel-integrity D3 anticipated the
lock by ruling out wrapper re-entry). Chunks of concurrent sends can no longer interleave,
and the failure notice now genuinely fires "within the same serialized sequence" as the spec
already words it.

The mechanism choice is the one D5 left open, decided on data:

- **Head-of-line cost, healthy path (measured per chunk, honest about N):** the measurement
  bounds the per-chunk send — 1.087 s worst observed, 162 ms median — and bounds nothing
  about chunk count. `N` is **unbounded today**, exactly as channel-integrity D5 stated: the
  *bounded* worst message is the ~18-chunk `/memories` reply (the store caps admit ~35 KB),
  holding the lock ~20 s at the observed per-chunk maximum, ~3 s typically; but `/inbox all`
  renders every open item with no render bound anywhere (`commands.py` passes `limit=None`;
  the inbox module's contract is "no eviction, ever"), so a neglected inbox makes the healthy
  hold `N × 1.087 s` with `N` uncapped — a few hundred undrained captures put the hold past
  the poll interval. The healthy-path hold is therefore **unbounded in principle, ~20 s for
  every reply shape that exists in practice** — and the wait is *additive* to the poll
  interval (worst owner-visible lateness = selection latency + hold), not absorbed by it.
  The reminder's *consequence* is bounded regardless of N: the retry floor re-attempts, the
  grace window bounds the outcome, and a reminder that waited delivers rather than skips.
  D5's 90–144 s estimate assumed 5–8 s per chunk; a month of data puts the slow mode at
  0.68–1.09 s — off ~7× on the variable the measurement covers, which is why the remaining
  exposure is the *count* variable, priced here from code rather than inherited. The right
  fix for pathological N is bounding the reply, not prioritizing around it: a
  `recall_render_limit`-style render bound on `/inbox all` is recorded as a **recommended
  follow-up** to `capture-inbox` (a ~20-line change, deliberately not smuggled into this
  one), and task 11.1 records rp5's actual open-item count so the live exposure is a number,
  not a guess.
- **Delivery-path priority** (D5's preferred candidate, sight unseen): a two-tier lock
  letting a reminder pre-empt at a chunk boundary buys back the hold minus one chunk — ~19 s
  against today's bounded replies, more against a pathological `/inbox all`. Rejected
  anyway: under a *degraded* bridge it rescues nothing (the reminder's own send is then just
  as slow, so delivery latency is dominated by bridge health, not queue position); on the
  healthy path the buy-back matters only when a reply is pathologically long, and the cheap,
  content-preserving fix for that is bounding the reply at its source (above) rather than a
  priority protocol inside the one code path whose excess machinery generated six criticals.
  If the follow-up bound is declined and long holds materialize in practice, the recorded
  fallback is pagination, not priority.
- **Bounded hold / chunk cap:** already rejected in channel-integrity D5 (unreachable
  `failed` outcome; discards owner content on the healthy path). Not re-litigated.
- **Paginate rather than discard** stays the recorded fallback if interleaving must ever be
  fixed *without* any hold: release every N chunks and re-acquire. Not built — nothing
  currently needs it.

**Consequence, handled rather than left implicit (D5's carried obligation):** the delivery
requirement is written for the send path as it now is — delivery never waits on the *turn
queue* (no session, no model), and may wait on an in-flight send. That wait is bounded per
chunk by the existing retry ceiling (`max_send_attempts × send_timeout + backoff` ≈
33 s/chunk, config-priced) and in total only by the in-flight message's chunk count; the
requirement states the owner-visible lateness bound **additively** (poll interval + pacing
+ hold), and its delivery-within-a-tick scenario is explicitly conditioned on no send being
in flight. No test may prove non-blocking with a cooperative double: the contract test must
drive the real lock with a slow fake bridge.

**The degraded case is bounded, not fixed:** a 20-chunk reply on a dead bridge holds the lock
for minutes and a due reminder waits behind it. Accepted: the reminder is late either way on
a dead bridge, the retry floor re-attempts it, and the grace window bounds the wait's
consequence. The measurement note's "not established" list says exactly what this data cannot
promise.

`send_timeout_seconds` stays 10.0 — ~9× headroom over the worst observed send; the
measurement supports touching it in neither direction.

### D7 — The delivered-reminder note, unchanged in substance from the original D5

Delivery leaves `surfaced_at` NULL; the next **owner** turn is prefixed with a delimited
block listing unsurfaced deliveries within `note_window_seconds` (default 43200), newest
first, at most `note_max_items` (default 10), framed as "messages Henk sent you" — data,
never instructions. Composing the turn writes `surfaced_at` in the same breath, so the note
appears exactly once and survives a restart between delivery and reply. Event turns never
carry it. It does not taint: reminder text is owner-authored, owner-echoed, or
model-composed inside an untainted owner turn (`remind` is owner-turn-only and denied in
tainted sessions — approval-gate, unchanged) — the same provenance as the recall block, and
it gets the same data framing. A
crash between marking surfaced and the reply losing the note is accepted: the delivery
itself already reached the owner; the note is context, not the promise.

### D8 — Delivery is app-initiated and outside the gate

Authority was granted at scheduling — by the owner's command directly, or by a
gate-authorized standing-tier tool call whose echo the owner read. The gate governs
model-initiated invocations; the scheduler, like an owner command, is not one. A delivery
neither consults nor occupies the pending-approval slot, and cannot be mistaken for a prompt
(fixed marker; the gate classifies inbound text only). Accountability is the `reminder`
audit record, which v4 already validates. Stated in the approval-gate delta because "a
message reached the owner and no approval was involved" is the sentence a security reader
must not have to reconstruct.

### D9 — The cadence amendment is the settled two-class enumeration

`incident-triage`'s "never on a timer" becomes: announceable incidents (capped, condition-
triggered) and owner-scheduled reminder deliveries (bounded by the pending cap, traceable to
an owner-authored schedule) — and nothing else. Reminder deliveries do not consume the
incident cap; system-scheduled digests, heartbeats and "all is well" messages stay banned in
so many words. The enumeration wording was settled in review and is carried, not reopened.

### D10 — Configuration only narrows, and every knob has a scenario

`RemindersConfig` gains: `poll_interval_seconds` (30), `retry_floor_seconds` (900),
`crash_attempt_limit` (3), `late_grace_seconds` (86400), `late_delivery_threshold_seconds`
(300), `report_horizon_seconds` (86400 — D5's report-termination bound),
`tick_delivery_limit` (10 — D2's backlog pacing), `note_window_seconds` (43200),
`note_max_items` (10). All validated at load (positive, sane orderings — the late threshold
below the grace window, the report horizon above the retry floor). Deliberately absent,
again:
any knob for tier, scope, or recipient. `crash_attempt_limit` is named unlike the bridge's
pre-existing `max_send_attempts` HTTP retry budget precisely because they count different
things and the reminders-core notes already flagged the collision risk.

### D11 — The model is updated with the spec, then its matrix is retargeted

`verify_selector_invariants.py` modelled the *pre-cut* design (backoff schedule,
`unconfirmed_sends`, `terminal_at`, report bounds). Per its own header, the model is
disposable and the fault-injection matrix is the artifact. Order of work: (1) rewrite the
model to this design and re-verify termination, detectability, quiescence, conservation and
partial handling — a defect found there is unfixed until the requirement text changes;
(2) retarget the matrix at the real store and scheduler as tests: fault injection at pre-work
commit, grace transition, each send, each post-send commit, with a tri-valued channel double,
plus the process-death cases (kill between send and mark; kill mid-pre-work). The model's
report-termination property now covers the horizon: a channel double returning `partial` on
every summary send must yield bounded total sends and the give-up exit. The suite must
include watching the key assertions go red under deliberate mutation — reminders-core's
"green tests are not evidence" lesson, inherited as method — with the mutation list extended
to: drop the selector's `due_at` conjunct, remove the report horizon, skip the pre-send
status re-read, and hold a transaction across an await.

## Risks / Trade-offs

- **[A reminder delayed behind a long reply]** → healthy path: ~20 s hold for every reply
  shape that exists in practice, additive to the poll interval; degraded bridge: bounded by
  config (~33 s/chunk ceiling) per chunk; pathological `/inbox all`: unbounded in N until
  the recommended follow-up render bound lands (D6). In every case the reminder is
  re-attempted by the floor and the consequence is bounded by grace. Accepted per D6.
- **[Duplicate deliveries across crashes]** → bounded by `crash_attempt_limit` (settled:
  duplicate beats loss); the abandoned exit is named to the owner in the summary.
- **[A deterministically partial summary loops owner-visibly]** → bounded by the report
  horizon (D5): at most ~`(grace + horizon) / floor` ≈ 192 head-re-deliveries **per row**
  (≈ 96 for a row that went `missed` on time; the 192 is an `abandoned` row, whose anchor sits
  a grace window further out — see D5, measured by the model), absolutely
  bounded by the pending cap times that (a rolling stream of newly-missed rows can keep the
  summary retrying on the newcomers' account), then the give-up exit with an error log —
  against *unbounded, forever* without the horizon. The per-retry duplication itself is
  accepted per D4's asymmetry (the alternative silently un-names rows); never yet observed
  on this host (29 days, zero non-201), so the bound is insurance, not a live cost.
- **[A report given up by the horizon]** → only after the row was named in at least one
  attempted summary (post-send placement, D5), and only on a partial outcome — a wholly
  failed summary is never given up on a channel outcome, so no channel outage of any length
  forfeits the report. The residual drop is a row whose every naming rode the lost tail of a
  persistently partial summary past its horizon: error-logged each time, never silent, and
  reachable only through the failure mode the harvest has never observed.
- **[Cancel racing an in-flight send]** → the pre-send status re-read (D4) closes the
  stale-selection window, which D6's lock would otherwise stretch to tens of seconds (the
  first draft called it sub-second — wrong once sends serialize). The residual window is one
  send sequence: a cancellation committing after dispatch may still deliver, the post-send
  write records `delivered` (the message factually reached the owner; recording `cancelled`
  would be false), and the audit trail carries both transitions. Not worth a transaction
  held across a network await (which `Store.transaction()`'s single-connection assumption
  forbids anyway).
- **[A within-grace backlog bursts at the owner]** → paced by `tick_delivery_limit` (D2):
  100 stored reminders due during a sub-grace outage arrive as at most 10 messages per
  30 s tick, ~5 minutes to drain — not one 100-message blast in ~16 s. The total count is
  unchanged by design: each message is an owner-authored promise; the cap bounds arrival
  rate, and the pending cap (100) bounds the total.
- **[The scheduler shares the event loop with turns]** → it must never call the store off
  the loop (the grep-based single-connection test already fails on that shape) and its
  per-tick work is milliseconds of SQLite plus awaited sends; the send lock, not the CPU, is
  the shared resource.
- **[Clock jumps]** → polling absorbs both directions: forward delivers on the next tick,
  backward delays rather than loses. `now` is captured once per tick, so one tick cannot
  disagree with itself.
- **[First enablement meets a backlog of stored test reminders]** → rp5's store already
  holds rows from the inert period's testing? It does not — the kill switch stored nothing
  (verified in the reminders-core deploy record: `/remind` refused). First tick on a fresh
  enable finds an empty or owner-authored schedule only.
- **[The note block grows the owner-turn prefix]** → count- and window-bounded; absent when
  empty; delimited as data alongside the existing recall block and time header.

## Migration Plan

1. Land the change; everything is behind `reminders.enabled`, which rp5's config does not
   carry — the deploy is inert for delivery, and (unlike reminders-core) the store port is
   already live, so this deploy carries **no** unflagged behaviour change except the send
   lock, whose only observable effect is that concurrent chunks stop interleaving.
2. Re-run the harvest in `notes/send-latency-measurement.md` and the two standing watches
   (channel-integrity's `partial`/`failed` grep; reminders-core's store-error grep) against
   the extended window before enabling.
3. **Hard stop — owner go.** Host-side edit to rp5's `config.yaml`: `owner.timezone`
   (Region/Location key; startup fails loudly if wrong — that guard shipped) and
   `reminders.enabled: true`. Rebuild once.
4. Verify end-to-end on the live system: `/remind +2m` delivers verbatim with the marker;
   the follow-up turn carries the note; a reminder scheduled, container stopped past its due
   time, restarted within grace → `delivered-late` stating the original due time; one forced
   beyond grace → `missed` + summary; audit records for each transition validate against v4;
   `/reminders` and cancel/reinstate still behave.
5. Give `README.md` its deferred pass (tools table, command list) — the follow-up
   reminders-core recorded as riding this exact flip, no earlier.
6. **Rollback:** `reminders.enabled: false` and restart — scheduler never starts, rows
   untouched. The send lock has no flag by design (it is a strict correction, same argument
   as channel-integrity's fixes); rolling it back is reverting the image.

## Open Questions

- **Marker wording** ("⏰ Reminder:" / the summary's heading) — product detail settled at
  apply time; the spec requires only distinguishability from triage messages and the
  original-due-time statement on late/missed items.
- **Whether the note should also mention abandoned reminders** (the summary already names
  them once). Leaning no — the summary is their surface, and the note is scoped to
  *deliveries* — but cheap to revisit if the first abandoned reminder in practice feels
  under-told.
