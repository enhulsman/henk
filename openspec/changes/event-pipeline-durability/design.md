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

- **The agent core advances it at the D3 per-triage-flush site**: after an event turn's audit record is written, the core checkpoints the batch's last-seen id — **gated on the audit write returning success**.
- **An errored triage still advances the cursor** after writing an `outcome="error"` record, so a poison event is not reprocessed forever.
- **A suppression-only batch** (all events cooled down, no triage turn) rides a **coordinator-enqueued `CheckpointMarker`** through the same serial core queue, so its advance is ordered behind any in-flight triage — the offset never advances past an event whose outcome (triage record or suppression record) isn't yet durable.
- **Freeze-on-failure durability latch (scrutiny CRITICAL).** Gating each advance on *its own* audit write is insufficient: FIFO guarantees a later batch is *processed* after a failed one, but not that it cannot *advance the cursor past* it. A later successful triage or `CheckpointMarker` would leapfrog the failed event, and because ntfy ids are opaque and non-orderable there is no per-offset high-water-mark that could recover it — the silent-drop bug this change closes would be reintroduced. So the barrier is **global and process-lifetime**: the first genuine flush failure sets `_checkpoint_blocked`, after which `_advance_checkpoint` is a no-op for *every* subsequent triage and marker. The cursor stays parked before the non-durable event, and the next restart replays from there.
- **One-shot degraded-durability notice.** A frozen checkpoint is invisible to an unattended agent, so latching also sends the owner a single Signal notice (once per process). The latch is set *before* the send so a send failure cannot leave it unset. A "failure" here means **audit configured and the write returned False** — an unconfigured audit sink also returns False but is a designed no-op and must neither latch nor notify.

Invariant: **the offset never advances past any event whose outcome isn't durable, in delivery order.** Exactly-once is preserved by the existing pipeline — replayed events flow through the same debounce/cooldown/cap path (bounded-replay guarantee) — and cooldown, rehydrated from the audit log (D2), absorbs any re-delivery of a boundary event after a crash. Trade-off accepted: the latch is deliberately coarse — one failed write parks the cursor for the whole process, so a persistent audit-write fault degrades into replay-on-restart plus an operator notice rather than silent loss.

### D2 — Rehydrate CadenceGate state from the persisted audit log

On startup, before the coordinator consumes events, `EventPipeline` is seeded from the audit log rather than a fresh empty state:

- `_last_triaged[identity]` ← the most recent `at` of an **event-triggered session (triage) record** touching that identity (`event[].identity_key`). **Suppression records are deliberately excluded** — a prior suppression never extends a cooldown in the live `evaluate` path, so rehydrating from them would over-suppress relative to steady-state semantics.
- `_announce_times` ← the `at` of announceable (`announceable: true`) event-triggered session records within the cap window; `_cap_suppressed_since_announce` ← the run of `announceable: false` triages since the last announce (an announce resets the run, since it surfaces the prior drops).
- `_last_handoff_ref[identity]` ← the `handoff_message_id` of the most recent triage for that identity, set only for triages inside the recurrence window and never cleared (matching live `note_handoff`).

Rehydration reads only records within the **widest** relevant window — the max of the default cooldown, the recurrence window, the cap window, **and every per-pattern `cooldown_overrides` entry** (scrutiny MAJOR: a chronic identity's override can outlast all three, and a triage that old must still arm its cooldown across a restart). Cooldown is armed for every triage inside that widest window even when it predates the recurrence window; the recurrence check downstream is a separate numeric comparison, so the wider arming cannot cause a spurious recurrence. Erring wide here is the safe direction: under-restoring cooldown causes a restart re-alert storm, over-restoring only delays one alert. This is feasible **only** because `at` is now populated (57d5894) and because per-triage records (D3) give one record per incident with its identity and handoff id. Chosen over a separate cadence-state snapshot file: the audit log is already the durable, backed-up source of truth, and a second store risks divergence. Trade-off: reconstruction couples the pipeline to the audit schema — accepted, and the schema is versioned.

### D3 — Flush one audit record per event triage, decoupled from session close

The audit record for an event triage is written at the end of the event turn (`_process_event`), not deferred to `_close_session`. This makes each incident durable immediately and ends the conflation where several incidents in one session collapse to a single record. Owner-initiated sessions keep the one-record-on-close model (a conversation is one unit). The audit-log cardinality contract changes from "one record per session" to "one record per event triage, plus one per owner session" — a schema-semantics change, so `schema_version` bumps.

Interaction with D5 (session scoping): flushing per-triage does **not** require closing the session, so owner interrogation of the incident still works — the record is durable while the session stays open for follow-ups.

### D4 — Wall-clock timekeeping for cadence, for cross-restart comparability

The persisted audit `at` values are wall-clock (`time.time`). `monotonic` resets to an arbitrary origin on every process start, so monotonic-based `_last_triaged`/`_announce_times` cannot be compared against rehydrated wall-clock timestamps. Cadence timekeeping (the `now` passed into `EventPipeline.evaluate`, and any pipeline-internal clock) therefore switches to wall-clock. Debounce *batching timing* in the coordinator may stay monotonic (it is a short in-process interval, never persisted). Confidence: moderate — this is the subtle correctness detail that makes D2 actually work; tests must assert cadence decisions using wall-clock timestamps that straddle a simulated restart.

### D5 — Event-turn session scoping: fresh session per incident (**confirmed: displace**)

henk-events D5 kept event turns in whatever session was open so owner replies interrogate the same conversation, but folded a *new* incident into an active session too — the source of cross-incident context bleed (observed live: a covert incident cross-referenced an earlier incident's handoff id from shared session context). Recommendation:

- A new incident (a fresh debounced event turn) SHALL start its own session rather than inheriting an unrelated incident's or an owner conversation's context.
- Owner replies following a triage message SHALL continue that incident's session (interrogation continuity preserved) under the existing idle/`/new` rules.

This isolates incidents (kills context bleed) while keeping the interrogation UX. The residual tradeoff — **if incident B arrives while the owner is mid-interrogation of incident A, B starts fresh and displaces A's session** (A's record is already durable via D3, but the owner's A-thread context is lost) — is the inverse of v1.2's behavior. **Owner confirmed displace as the interim default** (2026-07-23): context isolation is a firm SHALL, and the announcement names the focus switch explicitly. The proper fix for multi-incident UX is Signal quote-reply session routing (own future change); displace is the stopgap until then.

### D6 — SIGTERM handler drives the existing flush path

`__main__.py` installs a SIGTERM (and SIGINT) handler via `loop.add_signal_handler` that cancels the run so `App.run`'s existing `finally` (which cancels tasks and calls `core.aclose()`) executes within `docker stop`'s 10s grace. No new shutdown logic — SIGTERM is simply routed to the graceful path `aclose()` already provides. This is defense in depth with D3: even with per-triage flush, an owner session or an in-flight triage still flushes on clean shutdown instead of being SIGKILLed.

### D7 — Capture cache-read tokens

`_StatsAccumulator._add_tokens` (sdk_session.py) folds `cache_read_input_tokens` (and, if cheaply available, `cache_creation_input_tokens`) from the SDK `ResultMessage.usage` alongside the existing input/output totals; `SessionStats` and the audit `usage` object gain the field. Small, additive, and part of the schema bump in D3. `input_tokens` semantics (uncached only) are unchanged; the new field is additive so historical records stay valid readers.

### D8 — An unresumable checkpoint falls back to a full-retention replay

Found during the 2026-07-24 deploy-verify probe, after the rest of this change was already approved and deployed. `NtfyEventStream.subscribe` raises on a non-2xx, `EventIntake.events` catches it, backs off, and reconnects **with the same `since`** — so a checkpoint the server rejects (HTTP 400) is retried forever at the capped backoff interval. Intake dies permanently and silently: one WARNING line, no notice, no recovery. That is the precise failure class this change exists to eliminate, reached through the mechanism the change itself introduced.

`EventStreamError` now carries the HTTP `status`, and a 400 **with a `since` we could be blamed for** (not a cold subscribe, not the sentinel) triggers recovery: log at ERROR, set the cursor to ntfy's `all` sentinel, notify the owner once, and reconnect immediately — the sentinel is valid, so no backoff is needed, and a rejection *of* the sentinel falls through to the normal backoff rather than spinning.

Replay-all is chosen over a cold subscribe deliberately: a cold subscribe silently drops everything published while Henk was down, which is the original bug. Replay-all is bounded by the measured 72h retention and absorbed by cooldown/cap — the same benign path natural eviction already takes. It is self-healing: the first delivered event replaces the sentinel with a real id.

Likelihood is low — the checkpoint is only ever written from an ntfy-supplied `event.id`, `os.replace` prevents torn writes, and an empty file already degrades to `None` → cold subscribe. The realistic triggers are a volume restored from a truncated backup, a hand-edit, or a future ntfy id-format change. Impact is total and invisible, which is why it is worth the ~20 lines.

## Risks / Trade-offs

- **[Checkpoint write on the hot path]** → best-effort, non-blocking, and a single small-file write per event; event volume is low (curated sources + debounce). If it ever matters, throttle to write every N events / T seconds (the boundary event re-delivers harmlessly through cooldown).
- **[Audit-schema coupling for rehydration (D2)]** → the pipeline reads its own audit schema; a future schema change must keep the rehydration fields (`at`, `identity_key`, `announceable`, `handoff_message_id`) stable or bump the reader. Documented as a schema invariant.
- **[Rehydration cost at startup]** → bounded read of records within the cap/recurrence window only; audit records are small. If the log grows large, rehydration reads the tail, not the whole file.
- **[Wall-clock skew / NTP jumps (D4)]** → cadence windows are hours-scale; sub-second NTP corrections are immaterial. A large backward clock jump could briefly under-suppress — acceptable for a homelab and no worse than the current full-reset-on-restart.
- **[D5 displaces an owner's in-progress interrogation]** → open decision; if the owner prefers A-continuity, the alternative is to queue B behind A's session or open a parallel triage session (more machinery). Defaulting to isolation is the safer security/coherence choice.
- **[Per-triage records increase audit volume]** → one record per incident instead of one per session; incident volume is bounded by the curated source list and cadence layers. Net effect is *more faithful*, not noisier.

## Migration Plan

1. Implement + test (TDD from the delta scenarios); bump `schema_version` and publish the new schema file alongside v1 (keep v1 readable). **Not blocked by henk-events** — its code is already merged and deployed, so this change is TDD'd against the live code.
2. No infra change. Deploy to rp5 (`compose up -d --build` — code is `COPY`'d into the image, not bind-mounted, so a plain `up -d` runs stale code); checkpoint/state files initialize on first run on the existing `henk_audit` volume.
3. **Archive ordering (this is the only henk-events dependency).** These deltas MODIFY `audit-log`/`event-intake`/`incident-triage`, which enter `openspec/specs/` only when henk-events archives — so henk-events must sync+archive first. And henk-events' own 5.3(e)/5.3(d)/5.4 verification reads the restart-replay and audit paths this change fixes, so it cannot go fully green until this is deployed. Net sequence: deploy durability → run henk-events 5.3/5.4 green → archive henk-events → sync+archive durability.
4. Rollback: `events.enabled: false` restores v1 reactive behavior exactly (subscriber never starts, no checkpoint read); full rollback is redeploying the prior image tag. Checkpoint/state files are inert if unused and forward-compatible (a stale offset only over-replays within retention, absorbed by cooldown).

## Open Questions

- ~~**D5 tradeoff**~~ — **resolved:** owner confirmed *displace* (see D5). Signal quote-reply session routing is the eventual proper fix, tracked as its own future change.
- ~~**cache_creation tokens**~~ — **resolved:** shipped capturing `cache_read` only (the field behind the uncached-only undercount); `cache_creation` remains additive if the cost line ever needs it.
- **Checkpoint granularity:** per-event vs throttled. Shipped per-event (simplest, correct); revisit only if the hot-path write shows cost in the first-week watch.
- ~~**ntfy `since=<evicted-id>` semantics**~~ — **resolved by live probe (2026-07-24, vps ntfy).** Measured contract: a well-formed id (exactly 12 base62 chars) → **200**, and `since` is **exclusive** of that message; an id no longer cached → **200 plus the entire retained cache**, *not* an error; anything else → **400**. Retention measured at exactly **72h** (`expires - time = 259200s`). So eviction degrades to benign over-replay, absorbed by rehydrated cooldown and the cap — no fallback needed for the eviction case, and exclusivity is confirmed by the live restart test triaging the replayed event exactly once. The probe did, however, expose a real gap: a **malformed** checkpoint 400s, and the bare backoff loop retried the same value forever, silently killing intake. Closed by D8.
- **Approval-decision audit logging:** `approvals` is never threaded into `_write_audit_record`. Pre-existing gap, inert until the first mutating tool exists — follow-up, out of scope here.
