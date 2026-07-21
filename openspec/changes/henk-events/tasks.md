# Tasks: henk-events

## 1. Prerequisites (repo hygiene + infra, outside this codebase)

- [x] 1.1 Sync + archive henk-v1 so this change's delta specs (agent-core, channel-adapter, secure-deployment) land against the v1 baseline in `openspec/specs/` — DONE 2026-07-21 (`openspec archive henk-v1`; 5 specs synced, archived as `2026-07-21-henk-v1`)
- [ ] 1.2 ntfy admin on vps — FIRST verify retention: check the server config for `cache-duration` (target 72h; ntfy default is 12h) and persistent SQLite `cache-file`, reconfigure if needed and record the verified value in design.md (D9/D10 depend on it). Then: create deny-all topics `henk-events` + `henk-handoffs`; extend the `henk` ntfy user per-topic grants (read on events, write on handoffs; `homie-henk` publish unchanged); verify with curl that anonymous publish is rejected and Henk's credential can subscribe to events / publish to handoffs (specs/sensor-routing, secure-deployment delta)
- [ ] 1.3 Grafana **generic Webhook contact point** (no native ntfy integration) targeting ntfy's HTTP publish API + notification policy on vps routing exactly the curated subset — `HealthEtl*`, backup freshness, disk >85%, swap pressure — to `henk-events`; notification template MUST put source (Grafana/Prometheus), alert name, and firing/resolved state in the ntfy title per the sensor-routing payload contract; test-fire from Grafana, see the event arrive, and verify the payload matches the contract (specs/sensor-routing)
- [ ] 1.4 Gatus alerting block on rp5 → `henk-events`, with alert description/placeholders producing the contract title (source, endpoint name, state); trigger a synthetic endpoint failure, see the event arrive, and verify the payload matches the contract (specs/sensor-routing)

## 2. Tests first (from spec scenarios, backends faked)

- [ ] 2.1 event-intake tests against a fake ntfy stream: subscribe/receive, reconnect-with-`since` replay (exactly-once through the pipeline), payload-as-data (hostile payload never yields an out-of-registry tool call), arrival-time debounce collapse (10 events → 1 turn; replayed backlog → 1 catch-up turn), per-identity cooldown suppression incl. per-pattern overrides, stable-identity derivation (same alert → same key; nonconforming event → deterministic fallback key), intake failure leaves DM path functional
- [ ] 2.2 incident-triage tests with the SDK mocked: triageable event → triage session + proactive message when announceable; cap-overflow triageable event → session runs, handoff + audit emitted, NO Signal send, suppressed count noted in next announceable message; triage arc present (diagnosis + confidence, fix, pickup path) and arc-miss detection sets `triage_arc_complete: false` without blocking delivery; recurrence within the window → brief message referencing the prior handoff; no timer-triggered sends; owner reply continues the triage session
- [ ] 2.3 triage-handoff tests: `publish_handoff` has no destination parameters, `[AI]` label enforced, doc content contract (trigger/evidence/diagnosis+confidence/fix/pickup), cap-suppressed (non-announceable) incidents still publish
- [ ] 2.4 audit-log tests: one record per session (owner and event triggers), suppression records, `schema_version` present, records validate against the published JSON Schema, `triage_arc_complete` on event-triggered records, append-only behavior, write failure is loud but non-blocking
- [ ] 2.5 agent-core delta tests: typed turns — event turn queued behind a running owner turn; fresh session when idle; event turn content carries the delimited untrusted-data block + triage framing while owner turns carry neither; event-turn output goes to the proactive send (suppressed when non-announceable); `/new` after triage discards incident context
- [ ] 2.6 channel-adapter delta tests: proactive send reaches owner with no inbound trigger; no arbitrary-recipient parameter; long proactive message split in order

## 3. Implementation (make 2.x pass)

- [ ] 3.1 `henk/events/` subscriber: ntfy JSON/WebSocket subscribe with scoped credential, last-seen-id tracking, backoff reconnect with `since` replay
- [ ] 3.2 Triageable/announceable pipeline: arrival-time debounce window + per-identity cooldown (with per-pattern overrides, chronic identities like swap → 24h) + recurrence-window detection + daily hard cap gating Signal delivery only (all config-driven; cap-overflow incidents still run triage, publish handoffs, and get audit records); stable-identity derivation per source with normalized-title fallback
- [ ] 3.3 Event turn integration in the dispatcher: typed-turn queue refactor (`Queue[str]` → typed owner/event turns carrying event metadata + announceable flag), enqueue into the existing serial lane, fresh-vs-active session rule, event-turn content composition (delimited untrusted-data block + triage framing: arc mandate, recurrence note, handoff instruction), event-turn output → proactive send (suppressed when non-announceable); base system prompt updated ONLY to enumerate `publish_handoff` — no triage instructions in the base prompt
- [ ] 3.4 `publish_handoff` tool (notify-class, fixed topic, `[AI]` label) registered through the existing registry/gate machinery
- [ ] 3.5 Proactive owner-directed send on the adapter contract + Signal implementation (reuse existing send + splitting)
- [ ] 3.6 App-layer audit logger: author audit-record JSON Schema v1 as a repo file (the transferable artifact); writer validates records against it in tests; one record per session, suppression records, `triage_arc_complete` arc check on event-triggered sessions, non-blocking error handling; wire into dispatcher/session lifecycle
- [ ] 3.7 Config additions: `events.enabled` (rollback flag), topics, debounce/cooldown/cap values; `.env.example` updated for the extended ntfy credential

## 4. Pickup CLI (claude-config repo)

- [ ] 4.1 `henk-pickup` in `~/.claude-config/bin`: poll `henk-handoffs` via ntfy JSON poll endpoint with owner read credential; latest by default, `--list` for retention window, honest empty-state; shellcheck clean (specs/triage-handoff)

## 5. Deploy and verify on rp5

- [ ] 5.1 Compose update: `henk_audit` named volume mounted at the audit path; deploy with `compose up -d`; confirm audit records appear and survive `compose down && up`
- [ ] 5.2 Extend `pi5-backup.sh` `BACKUP_VOLUMES` allowlist with `henk_audit`; test run shows the tarball on the VPS receive side
- [ ] 5.3 Deploy-verify checklist: (a) synthetic Gatus failure → Signal message with full triage arc; (b) Grafana test-fire → same; (c) 10-event storm → one conversation; (d) hostile-payload event → no out-of-registry tool call in transcript/audit; (e) restart mid-stream → replayed event triaged exactly once; (f) anonymous publish to both topics rejected; (g) `henk-pickup` retrieves the handoff from the workstation; (h) ACL/ports audit shows zero new exposure vs v1
- [ ] 5.4 Watch first-week behavior: unprompted-message count vs owner cadence constraint, `/usage` for token burn; tune debounce/cooldown/cap from audit-log data (design D6 defaults are guesses)

## 6. Wrap-up

- [ ] 6.1 README update: event flow diagram, new config keys, triage-arc contract, `henk-pickup` usage, rollback flag
- [ ] 6.2 `/docs-update`: homelab docs — new topics + grants on vps ntfy, Grafana contact point, Gatus alerting, Henk's proactive role, audit volume in backup list
- [ ] 6.3 Update memory `henk-long-run-direction` (v1.2 shipped; carry v1.3 items forward)
