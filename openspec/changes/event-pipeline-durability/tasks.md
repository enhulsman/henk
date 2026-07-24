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
- [x] 1.10 Unresumable-checkpoint recovery (event-intake, design D8 — added after the 3.2 probe): a 400-class `since` rejection retries with the retention-replay sentinel instead of the rejected value and notifies the owner; an ordinary transport error keeps the offset (no replay storm on a blip); the fallback self-heals to a real id once events flow; a rejected sentinel takes the normal backoff instead of spinning; a cold-subscribe 400 is not misread as a bad checkpoint

## 2. Implementation (make 1.x pass)

- [x] 2.1 Checkpoint store: a small durable last-id store on the audit volume (path a sibling of `events.audit_path`), non-blocking writes mirroring `AuditLog`'s error discipline
- [x] 2.2 `EventIntake`: accept an initial offset that seeds `_last_id` so the first `subscribe` resumes with `since=<offset>`; `runtime.py` reads the checkpoint at startup and seeds intake. The durable checkpoint ADVANCE is owned by the core/coordinator (2.4), not intake (D1 converged decision — advance only when the outcome is durable)
- [x] 2.3 `EventPipeline`: switch cadence `now` to wall-clock; add a `rehydrate(records)` (or constructor seed) that rebuilds `_last_triaged`, `_announce_times`, `_last_handoff_ref` from audit records within the widest relevant window; `runtime.py` reads the audit tail and seeds the pipeline before the coordinator starts
- [x] 2.4 `AgentCore`: write the event-triage audit record at end of `_process_event` (decoupled from `_close_session`); apply the D5 session-scoping rule (new incident always starts fresh — displace; owner reply continues); ensure owner-session close still writes exactly one record and no double-write for event sessions. Advance the durable intake checkpoint to the batch's last-seen id **after** the record write succeeds (gated); an errored triage writes `outcome="error"` then advances. Handle a `CheckpointMarker` turn (suppression-only batch) by advancing the checkpoint in FIFO order. Coordinator (`coordinator.py`) attaches the batch offset to the event turn / enqueues the marker, and passes wall-clock `now` to `evaluate` (D4) while keeping monotonic debounce-deadline timing
- [x] 2.5 `_StatsAccumulator` + `SessionStats` + `session_record`/`usage`: capture and thread `cache_read_input_tokens`
- [x] 2.6 Audit schema: bump `SCHEMA_VERSION`, add `audit-record.v2.schema.json` (usage field + per-triage record semantics), keep v1 file for historical validation
- [x] 2.7 `__main__.py`: install SIGTERM/SIGINT handlers via `loop.add_signal_handler` that drive the existing graceful shutdown (`App.run` finally → `aclose()`)
- [x] 2.8 `runtime.py`: wire the checkpoint store and pipeline rehydration; no new config required (reuse `events.audit_path`'s volume); confirm `events.enabled: false` skips all of it (v1 behavior exactly)
- [x] 2.9 Unresumable-checkpoint recovery (D8): `EventStreamError` carries the HTTP `status`; `NtfyEventStream` surfaces it from `HTTPStatusError`; `EventIntake` falls back to the `RETENTION_REPLAY_SINCE` sentinel on a 400-class rejection of a real `since`, logs at ERROR, and fires an `on_since_rejected` callback; `runtime.py` wires that callback to a Signal notice

## 3. Deploy and verify on rp5

> Owner-run constraint (from henk-events): rp5 sudo is restricted — deploy (`compose up -d`) and anything under `/opt` are owner-run. Prepare exact commands and hand them over.

- [x] 3.1 Deploy the durability build (`compose up -d --build`); no compose/volume change. Confirmed 2026-07-24: checkpoint initialized on the existing `henk_audit` volume at `/data/audit/intake-offset`. (`--build` is required — code is `COPY`'d into the image, not bind-mounted.)
- [x] 3.2 Deploy-verify — run live 2026-07-24 against rp5 with real Gatus-form events on `henk-events`:
  - [x] (a) **restart mid-stream** — event B (`3i7pyY8kFnG5`) published while stopped; startup logged `?since=ETrzngVQ3F1v`, B triaged **exactly once** (1 handoff, 1 Signal send), audit record present. **The headline fix, proven end-to-end.**
  - [ ] (b) **cap persistence** — NOT VERIFIED. Reaching the 3/24h cap needs 3 distinct announceable identities plus a restart; deferred to the 4.2 first-week watch, which exercises it on real traffic. Cap rehydration is covered by unit tests (1.3) but has no live evidence.
  - [x] (c) **cooldown persistence** — `probe-alpha` triaged 18:15, restart 18:36 wiped memory, re-fire suppressed with a `suppression | reason=cooldown` record. Proves `rehydrate()` reconstructed cooldown from the audit log.
  - [x] (d) **graceful stop** — `docker compose stop henk` returned `exit=0` in **0.72s** (was: 10s hang → `Exited 137`).
  - [x] (e) **cache-read usage** — fresh record: `input_tokens: 6`, `cache_read_input_tokens: 53287`. The old accounting under-reported input by ~4 orders of magnitude.
  - [x] (f) **ntfy `since` contract probed** (deploy gate): 12-char id → 200 and **exclusive**; uncached id → 200 + full cache (benign, not an error); anything else → 400. Retention measured at exactly 72h. Exposed the D8 wedge — see 3.4.
- [x] 3.3 Zero new ports/volumes/ACL grants vs the v1.2 stack — no compose change in this deploy; the checkpoint reuses the existing `henk_audit` volume.
- [ ] 3.4 **Redeploy for D8** (`compose up -d --build`) and confirm normal startup. The 3.2 run validated the pre-D8 build; D8 touches only the `since`-rejection path, so 3.2's results stand, but D8 itself ships unverified in production until this redeploy.

## 4. Wrap-up

- [x] 4.1 README: note the intake checkpoint + cadence rehydration behavior, the per-triage audit-record semantics, the `schema_version` bump, and graceful-shutdown behavior
- [ ] 4.2 Unblock henk-events 5.4 first-week watch (audit now records event triages) and re-tune debounce/cooldown/cap from real audit data
- [ ] 4.3 `/opsx:sync` + `/opsx:archive` this change
