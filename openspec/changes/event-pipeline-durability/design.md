# Design: event-pipeline-durability

## Context

henk-events v1.2 is deployed on rp5 and works in steady state. The 2026-07-23 live 5.3 deploy-verify surfaced three defects with one root cause: **all event-pipeline state is in-memory, and rp5 restarts are non-graceful (SIGKILL, `Exited 137`)**. The relevant current code:

- `EventIntake` (intake.py) keeps `self._last_id` in memory and reconnects with `since=self._last_id`; `runtime.py` constructs `EventIntake(stream)` with no persisted offset, so a fresh process starts at `since=None` and drops downtime events.
- `EventPipeline` (pipeline.py) — the CadenceGate — keeps `_last_triaged` (cooldown), `_announce_times` (cap window), `_cap_suppressed_since_announce`, and `_last_handoff_ref` (recurrence) in memory. `EventCoordinator` drives `evaluate(batch, now)` with `now` from `time.monotonic` (coordinator.py default clock).
- `AgentCore._flush_audit` runs only from `_close_session` (idle expiry / `/new` / `aclose()`). Event turns reuse one session for up to `idle_timeout_seconds=3600`. `App.run`'s `finally` calls `aclose()`, but `python -m henk` (`__main__.py`) has no SIGTERM handler, so `asyncio.run` never unwinds `finally` under `docker stop`.
- `AuditLog.write` (logger.py) stamps `at` with wall-clock `time.time`; the `_StatsAccumulator` (sdk_session.py) folds `input_tokens`/`output_tokens` from the SDK `ResultMessage` but not cache-read tokens.

Verified facts this design leans on: the `at`/stats fixes (57d5894) work when a record flushes; idle-close flushes correctly in steady state; the `henk_audit` volume is already in the rp5 backup allowlist; ntfy retention is 72h (verified 2026-07-22).

## Goals / Non-Goals

**Goals:**

- No event is silently dropped across a restart within ntfy's retention window.
- The daily cadence cap and per-identity cooldowns hold across restarts (no re-arming, no cap reset).
- Every event triage produces a durable audit record promptly, independent of session close and surviving SIGKILL.
- `docker stop` shuts Henk down cleanly (SIGTERM → flush) within the grace period.
- Audit `usage` reflects true cost including cache-read tokens.

**Non-Goals:**

- Any new tool, mutation, topic, port, or ACL change.
- A general-purpose durable state store or database — flat checkpoint files on the existing audit volume suffice.
- Replaying beyond ntfy's retention window (accepted: persisting conditions re-alert).
- Audit log rotation/compaction (deferred until size warrants; unchanged from v1.2).

## Decisions

### D1 — Intake offset checkpoint on the audit volume; **advance only when the outcome is durable**

The last-seen event id is checkpointed to a small file on the audit volume (e.g. `/data/audit/intake-offset`); on startup `runtime.py` reads it and seeds `EventIntake`'s starting `_last_id` so the first `subscribe(since=...)` resumes from the checkpoint. Writes are best-effort and non-blocking (same discipline as the audit writer): a checkpoint failure logs at ERROR and never blocks intake.

**The checkpoint advances at the point the batch's outcome becomes durable, in delivery order — NOT on intake yield** (converged decision, scrutiny round 3; on-yield would advance past an in-flight event and re-open the silent-drop window this change closes). Concretely:

- **The agent core advances it at the D3 per-triage-flush site**: after an event turn's audit record is written, the core checkpoints the batch's last-seen id — **gated on the audit write returning success** (a failed write leaves the checkpoint, so the event replays).
- **An errored triage still advances the cursor** after writing an `outcome="error"` record, so a poison event is not reprocessed forever.
- **A suppression-only batch** (all events cooled down, no triage turn) rides a **coordinator-enqueued `CheckpointMarker`** through the same serial core queue, so its advance is ordered behind any in-flight triage — the offset never advances past an event whose outcome (triage record or suppression record) isn't yet durable.

Invariant: **the offset never advances past any event whose outcome isn't durable, in delivery order.** Exactly-once is preserved by the existing pipeline — replayed events flow through the same debounce/cooldown/cap path (bounded-replay guarantee) — and cooldown, rehydrated from the audit log (D2), absorbs any re-delivery of a boundary event after a crash.

### D2 — Rehydrate CadenceGate state from the persisted audit log

On startup, before the coordinator consumes events, `EventPipeline` is seeded from the audit log rather than a fresh empty state:

- `_last_triaged[identity]` ← the most recent `at` of any session/suppression record touching that identity (session records carry `event[].identity_key`; suppression records carry `identity_key`).
- `_announce_times` ← the `at` of announceable (`announceable: true`) event-triggered session records within the cap window.
- `_last_handoff_ref[identity]` ← the `handoff_message_id` of the most recent triage for that identity.

Rehydration reads only records within the widest relevant window (recurrence window / cap window), so it is bounded. This is feasible **only** because `at` is now populated (57d5894) and because per-triage records (D3) give one record per incident with its identity and handoff id. Chosen over a separate cadence-state snapshot file: the audit log is already the durable, backed-up source of truth, and a second store risks divergence. Trade-off: reconstruction couples the pipeline to the audit schema — accepted, and the schema is versioned.

### D3 — Flush one audit record per event triage, decoupled from session close

The audit record for an event triage is written at the end of the event turn (`_process_event`), not deferred to `_close_session`. This makes each incident durable immediately and ends the conflation where several incidents in one session collapse to a single record. Owner-initiated sessions keep the one-record-on-close model (a conversation is one unit). The audit-log cardinality contract changes from "one record per session" to "one record per event triage, plus one per owner session" — a schema-semantics change, so `schema_version` bumps.

Interaction with D5 (session scoping): flushing per-triage does **not** require closing the session, so owner interrogation of the incident still works — the record is durable while the session stays open for follow-ups.

### D4 — Wall-clock timekeeping for cadence, for cross-restart comparability

The persisted audit `at` values are wall-clock (`time.time`). `monotonic` resets to an arbitrary origin on every process start, so monotonic-based `_last_triaged`/`_announce_times` cannot be compared against rehydrated wall-clock timestamps. Cadence timekeeping (the `now` passed into `EventPipeline.evaluate`, and any pipeline-internal clock) therefore switches to wall-clock. Debounce *batching timing* in the coordinator may stay monotonic (it is a short in-process interval, never persisted). Confidence: moderate — this is the subtle correctness detail that makes D2 actually work; tests must assert cadence decisions using wall-clock timestamps that straddle a simulated restart.

### D5 — Event-turn session scoping: fresh session per incident (recommended; open decision)

henk-events D5 kept event turns in whatever session was open so owner replies interrogate the same conversation, but folded a *new* incident into an active session too — the source of cross-incident context bleed (observed live: a covert incident cross-referenced an earlier incident's handoff id from shared session context). Recommendation:

- A new incident (a fresh debounced event turn) SHALL start its own session rather than inheriting an unrelated incident's or an owner conversation's context.
- Owner replies following a triage message SHALL continue that incident's session (interrogation continuity preserved) under the existing idle/`/new` rules.

This isolates incidents (kills context bleed) while keeping the interrogation UX. The residual tradeoff — **if incident B arrives while the owner is mid-interrogation of incident A, B starts fresh and displaces A's session** (A's record is already durable via D3, but the owner's A-thread context is lost) — is the inverse of v1.2's behavior and is flagged as an open decision (see Open Questions). Confidence: moderate; this is a UX judgment the owner should confirm.

### D6 — SIGTERM handler drives the existing flush path

`__main__.py` installs a SIGTERM (and SIGINT) handler via `loop.add_signal_handler` that cancels the run so `App.run`'s existing `finally` (which cancels tasks and calls `core.aclose()`) executes within `docker stop`'s 10s grace. No new shutdown logic — SIGTERM is simply routed to the graceful path `aclose()` already provides. This is defense in depth with D3: even with per-triage flush, an owner session or an in-flight triage still flushes on clean shutdown instead of being SIGKILLed.

### D7 — Capture cache-read tokens

`_StatsAccumulator._add_tokens` (sdk_session.py) folds `cache_read_input_tokens` (and, if cheaply available, `cache_creation_input_tokens`) from the SDK `ResultMessage.usage` alongside the existing input/output totals; `SessionStats` and the audit `usage` object gain the field. Small, additive, and part of the schema bump in D3. `input_tokens` semantics (uncached only) are unchanged; the new field is additive so historical records stay valid readers.

## Risks / Trade-offs

- **[Checkpoint write on the hot path]** → best-effort, non-blocking, and a single small-file write per event; event volume is low (curated sources + debounce). If it ever matters, throttle to write every N events / T seconds (the boundary event re-delivers harmlessly through cooldown).
- **[Audit-schema coupling for rehydration (D2)]** → the pipeline reads its own audit schema; a future schema change must keep the rehydration fields (`at`, `identity_key`, `announceable`, `handoff_message_id`) stable or bump the reader. Documented as a schema invariant.
- **[Rehydration cost at startup]** → bounded read of records within the cap/recurrence window only; audit records are small. If the log grows large, rehydration reads the tail, not the whole file.
- **[Wall-clock skew / NTP jumps (D4)]** → cadence windows are hours-scale; sub-second NTP corrections are immaterial. A large backward clock jump could briefly under-suppress — acceptable for a homelab and no worse than the current full-reset-on-restart.
- **[D5 displaces an owner's in-progress interrogation]** → open decision; if the owner prefers A-continuity, the alternative is to queue B behind A's session or open a parallel triage session (more machinery). Defaulting to isolation is the safer security/coherence choice.
- **[Per-triage records increase audit volume]** → one record per incident instead of one per session; incident volume is bounded by the curated source list and cadence layers. Net effect is *more faithful*, not noisier.

## Migration Plan

1. henk-events sync + archive first (so deltas land on the v1.2 baseline).
2. Implement + test (TDD from the delta scenarios); bump `schema_version` and publish the new schema file alongside v1 (keep v1 readable).
3. No infra change. Deploy to rp5 (`compose up -d`); checkpoint/state files initialize on first run on the existing `henk_audit` volume.
4. Rollback: `events.enabled: false` restores v1 reactive behavior exactly (subscriber never starts, no checkpoint read); full rollback is redeploying the prior image tag. Checkpoint/state files are inert if unused and forward-compatible (a stale offset only over-replays within retention, absorbed by cooldown).

## Open Questions

- **D5 tradeoff:** when a new incident arrives while the owner is mid-interrogation of a prior incident, should the new incident displace the session (recommended: isolation), queue behind it, or open a parallel triage session? Needs owner confirmation — it is a UX judgment, not a correctness one.
- **Checkpoint granularity:** per-event vs throttled. Default to per-event (simplest, correct); revisit only if the hot-path write shows cost in the first-week watch.
- **cache_creation tokens:** capture only `cache_read`, or also `cache_creation`? Read is the one that matters for the "uncached-only undercount" finding; creation is a smaller, one-off cost. Default: capture read; add creation only if the cost line needs it.
