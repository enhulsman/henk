# Tasks: event-pipeline-durability

## 0. Prerequisite

- [ ] 0.1 Sync + archive henk-events (`/opsx:sync` + `/opsx:archive`) so its specs (`event-intake`, `incident-triage`, `audit-log`, and the v1.2 `agent-core`/`secure-deployment` deltas) are the baseline in `openspec/specs/` and this change's deltas apply against them. Blocked on henk-events 6.2/6.4.
- [x] 0.2 D5 session-scoping decision confirmed by owner (2026-07-24): **displace** — a new incident starts its own isolated session, displacing any open session (its triage record is already durable via D3). Context isolation is a firm SHALL; the collision scheduling is displace.

## 1. Tests first (from the delta spec scenarios; backends faked, clock injected)

- [x] 1.1 Intake offset persistence (event-intake): `EventIntake` seeded from a persisted offset resumes `subscribe(since=<offset>)`; first-start-with-no-checkpoint subscribes with no `since`; "event published while stopped → replayed once on restart" simulated by feeding a fake stream a `since`-gated backlog (event-intake: bounded replay). NOTE: the durable checkpoint ADVANCE happens at the core per-triage-flush site (D1 converged decision), not on intake yield — advance behaviour is covered in 1.5; intake only *reads* the seed here.
- [x] 1.2 Checkpoint store unit tests: write-then-read round-trips the last id across a simulated recreation (new instance, same path); atomic/last-write-wins semantics; missing file → `None`
- [x] 1.3 CadenceGate rehydration (incident-triage + event-intake): `EventPipeline` seeded from a list of audit records reconstructs `_last_triaged` (cooldown holds across restart), `_announce_times` (cap holds across restart), and `_last_handoff_ref` (recurrence framing + prior handoff across restart); wall-clock timestamps straddling a simulated restart drive correct suppress/announce/recurrence decisions (design D2, D4)
- [x] 1.4 Wall-clock cadence timekeeping (design D4): pipeline decisions use wall-clock `now`; a monotonic origin reset (restart) does not corrupt cooldown/cap arithmetic; debounce batching timing is unaffected
- [x] 1.5 Per-triage audit flush (audit-log + agent-core): an event turn writes exactly one record at turn completion with the session still open; two incidents in one open session yield two distinct records (no conflation); owner sessions still emit one record on close; a record exists after a simulated hard-kill (no `aclose()`); durable-before-next-event ordering
- [x] 1.6 Session scoping (agent-core): a new incident arriving while an unrelated session is active starts fresh (no prior-incident context in the composed turn / session identity changes); an owner reply after a triage continues that incident's session; `/new` still discards; encode the 0.2 decision for the owner-mid-interrogation collision case
- [x] 1.7 Cache-read usage (audit-log): `_StatsAccumulator` folds `cache_read_input_tokens` from a fake `ResultMessage.usage`; `SessionStats` and the audit `usage` object carry it; absent field → 0/None, not an error
- [x] 1.8 Schema version bump (audit-log): new records declare the new `schema_version` and validate against the new published schema; a record under the previous version validates against the previous schema (both schema files committed)
- [x] 1.9 Graceful shutdown (secure-deployment): SIGTERM routed to the graceful path triggers `App.run`'s finally → `core.aclose()` (assert the flush happened); SIGINT identical — driven with a fake loop/signal harness so no real signals are raised in the suite

## 2. Implementation (make 1.x pass)

- [x] 2.1 Checkpoint store: a small durable last-id store on the audit volume (path a sibling of `events.audit_path`), non-blocking writes mirroring `AuditLog`'s error discipline
- [x] 2.2 `EventIntake`: accept an initial offset that seeds `_last_id` so the first `subscribe` resumes with `since=<offset>`; `runtime.py` reads the checkpoint at startup and seeds intake. The durable checkpoint ADVANCE is owned by the core/coordinator (2.4), not intake (D1 converged decision — advance only when the outcome is durable)
- [x] 2.3 `EventPipeline`: switch cadence `now` to wall-clock; add a `rehydrate(records)` (or constructor seed) that rebuilds `_last_triaged`, `_announce_times`, `_last_handoff_ref` from audit records within the widest relevant window; `runtime.py` reads the audit tail and seeds the pipeline before the coordinator starts
- [x] 2.4 `AgentCore`: write the event-triage audit record at end of `_process_event` (decoupled from `_close_session`); apply the D5 session-scoping rule (new incident always starts fresh — displace; owner reply continues); ensure owner-session close still writes exactly one record and no double-write for event sessions. Advance the durable intake checkpoint to the batch's last-seen id **after** the record write succeeds (gated); an errored triage writes `outcome="error"` then advances. Handle a `CheckpointMarker` turn (suppression-only batch) by advancing the checkpoint in FIFO order. Coordinator (`coordinator.py`) attaches the batch offset to the event turn / enqueues the marker, and passes wall-clock `now` to `evaluate` (D4) while keeping monotonic debounce-deadline timing
- [x] 2.5 `_StatsAccumulator` + `SessionStats` + `session_record`/`usage`: capture and thread `cache_read_input_tokens`
- [x] 2.6 Audit schema: bump `SCHEMA_VERSION`, add `audit-record.v2.schema.json` (usage field + per-triage record semantics), keep v1 file for historical validation
- [x] 2.7 `__main__.py`: install SIGTERM/SIGINT handlers via `loop.add_signal_handler` that drive the existing graceful shutdown (`App.run` finally → `aclose()`)
- [x] 2.8 `runtime.py`: wire the checkpoint store and pipeline rehydration; no new config required (reuse `events.audit_path`'s volume); confirm `events.enabled: false` skips all of it (v1 behavior exactly)

## 3. Deploy and verify on rp5

> Owner-run constraint (from henk-events): rp5 sudo is restricted — deploy (`compose up -d`) and anything under `/opt` are owner-run. Prepare exact commands and hand them over.

- [ ] 3.1 Deploy the durability build (`compose up -d`); no compose/volume change expected — confirm checkpoint + state files initialize on the existing `henk_audit` volume
- [ ] 3.2 Deploy-verify (mirrors henk-events 5.3(e), extended):
  - (a) **restart mid-stream** — publish an event while Henk is stopped, restart within retention → event triaged **exactly once** after restart, and an **audit record for it is present** after restart
  - (b) **cap persistence** — reach the daily cap, restart, publish another triageable event → triaged + handed off but **no Signal send** (cap held across restart)
  - (c) **cooldown persistence** — triage an identity, restart, re-fire within cooldown → suppressed, suppression audit record present
  - (d) **graceful stop** — `docker stop` shows a clean exit (no `Exited 137`) and the open session's record flushed
  - (e) **cache-read usage** — a fresh triage's audit record shows a `cache_read_input_tokens` field populated
- [ ] 3.3 Confirm zero new ports/volumes/ACL grants vs the v1.2 stack (secure-deployment: no new infrastructure surface)

## 4. Wrap-up

- [x] 4.1 README: note the intake checkpoint + cadence rehydration behavior, the per-triage audit-record semantics, the `schema_version` bump, and graceful-shutdown behavior
- [ ] 4.2 Unblock henk-events 5.4 first-week watch (audit now records event triages) and re-tune debounce/cooldown/cap from real audit data
- [ ] 4.3 `/opsx:sync` + `/opsx:archive` this change
