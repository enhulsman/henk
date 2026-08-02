# Proposal: event-pipeline-durability

## Why

The henk-events v1.2 event pipeline works end-to-end in steady state, but the live 5.3 deploy-verify (2026-07-23) confirmed three defects that share **one root cause: every piece of event-pipeline state is in-memory, and shutdown on rp5 is non-graceful (SIGKILL, `Exited 137`)**. The result is that a restart — routine on a homelab — silently loses reliability and gutspoils the audit trail:

- **Dropped incidents (priority, silent).** `EventIntake._last_id` (intake.py) is in-memory and `runtime.py` builds `EventIntake(stream)` with no persisted offset, so after a container restart intake resubscribes with **no `since`** and silently drops every event published during the downtime — no triage, no handoff, no audit record. Confirmed live: an event published while Henk was stopped was never triaged after restart. Because the event never enters the pipeline, the drop is invisible.
- **Cadence caps and cooldowns reset on restart.** `EventPipeline` (pipeline.py, the CadenceGate) holds the daily cap window (`_announce_times`), per-identity cooldowns (`_last_triaged`), and recurrence refs (`_last_handoff_ref`) in memory only. Every restart re-arms cooled-down identities and resets the hard cap — observed live: three redeploys on 2026-07-23 let cap-overflow events deliver on Signal, exceeding the owner's cadence constraint.
- **The audit records almost nothing for events (most consequential).** The audit record flushes only on graceful session close (`_close_session` → `_flush_audit`, via idle expiry / `/new` / `aclose()`). But event turns reuse one long-lived session (idle_timeout=3600s), and rp5 restarts are SIGKILL so `aclose()` never runs. Result: five event triages on 2026-07-23 published handoffs but wrote **zero** audit records. The audit log is the charter's transferable artifact (the approval-decision logging especially) — this defect guts it, and it blocks the henk-events 5.4 first-week watch.

Steady-state flushing itself is **not** broken: an overnight idle-close correctly wrote records and dropped stale context, and the `at`/stats fixes (57d5894) work when a record does flush. The problem is **timeliness and restart-durability**, not the flush mechanism.

## What Changes

- **Durable intake offset.** Checkpoint the last-seen event id to the audit volume and resume the ntfy subscription with `since=<checkpoint>` on startup, so events published during downtime (within ntfy's retention window) are replayed exactly once through the existing debounce/cooldown/cap pipeline. This is the priority: a dropped incident is a silent reliability failure.
- **Durable / rehydrated cadence state.** On startup, reconstruct the CadenceGate's cap window, per-identity cooldowns, and recurrence refs from the persisted audit log — feasible now that `at` is populated (57d5894) — so a restart no longer re-arms cooldowns or resets the daily cap.
- **Per-triage audit flush.** Write an audit record at the completion of each event triage, decoupled from session close, so an incident is recorded promptly and survives a restart. This also ends the audit conflation where multiple incidents sharing one session collapse into a single record.
- **Graceful shutdown.** Install a SIGTERM handler that unwinds `App.run`'s existing flush path (`aclose()`), so `docker stop`'s 10s grace flushes cleanly instead of escalating to SIGKILL and losing in-flight state.
- **Session scoping (decision, see design).** Reconsider whether event turns should reuse one long-lived session (idle_timeout=1h). Reuse drives both audit conflation (addressed above) and cross-incident context bleed (an unrelated incident inheriting an earlier incident's context — observed live). The design recommends a fresh session per incident, with owner-interrogation continuity preserved; the interrogation-vs-isolation tradeoff is flagged as an open decision.
- **Full cost accounting (tiny extra).** `usage.input_tokens` currently counts only uncached input; capture `cache_read` tokens in the audit record so cost accounting reflects prompt caching.
- **No new capabilities, no new tools, no mutations.** This is a hardening change to the existing v1.2 pipeline. Egress, ports, ACLs, and the toolset are unchanged.

## Capabilities

### Modified Capabilities

- `event-intake`: adds a durable last-seen-id checkpoint and startup resume-with-`since`; adds durable/rehydrated per-identity cooldown state so cooldowns survive restart.
- `incident-triage`: the cadence cap window and recurrence refs are rehydrated from the persisted audit log on startup, so the hard cap and recurrence framing survive restart.
- `audit-log`: an audit record is written per event triage at triage completion (decoupled from session close) and is durable before the next event is processed; `usage` captures cache-read tokens.
- `agent-core`: event-turn session scoping is redefined so a new incident does not inherit a prior incident's session context, while owner replies still interrogate the incident that produced the triage message.
- `secure-deployment`: the container SHALL shut down gracefully within `docker stop`'s grace period (SIGTERM handled) so pipeline state and the audit record flush before exit.

> Note: henk-events has not been synced to `openspec/specs/` yet (`/opsx:sync` + `/opsx:archive` pending on its 6.2/6.4 tasks). Sync + archive henk-events before applying this change so the deltas above land against the v1.2 baseline (`event-intake`, `incident-triage`, `audit-log`, and the v1.2 `agent-core`/`secure-deployment` deltas). This mirrors henk-events' own "sync henk-v1 first" note.

## Impact

- **Repo code:** `henk/events/intake.py` (accept a persisted starting offset + notify a checkpoint sink on each event); a small durable checkpoint/state store on the audit volume; `henk/events/pipeline.py` (rehydrate cooldown/cap/recurrence; switch cadence timekeeping to wall-clock so persisted timestamps are comparable across restarts); `henk/agent/core.py` (per-event-triage flush, session-scoping change); `henk/audit/logger.py` + the v1 JSON Schema (usage gains `cache_read_input_tokens`; record cardinality changes from one-per-session to one-per-triage for event sessions); `henk/agent/sdk_session.py` (capture cache-read usage); `henk/__main__.py` (SIGTERM handler); `henk/runtime.py` (wire the checkpoint store + rehydration).
- **Audit schema:** `usage` gains a cache-read field; the "one record per session" cardinality for event-triggered sessions becomes "one record per triage." This is a schema-semantics change — bump `schema_version` and update `audit-record.v*.schema.json`.
- **Infra:** none. No new volumes (checkpoint/state files live on the existing `henk_audit` volume, already in the backup allowlist), no new topics, no ACL/port/egress change.
- **Deploy-verify:** extends henk-events 5.3(e) — restart mid-stream must yield exactly-once triage AND an audit record present after restart; plus a cap-persistence check across restart.
- **Docs:** no documented-infrastructure change (no new ports/services/topics); no `/docs-update` required beyond a note that the audit volume now also holds pipeline checkpoints.
- **Security posture:** unchanged. Read-only toolset, owner-only comms, zero inbound, scoped secrets — all identical to v1.2.
