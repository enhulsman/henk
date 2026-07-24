# Tasks: henk-events

## 1. Prerequisites (repo hygiene + infra, outside this codebase)

- [x] 1.1 Sync + archive henk-v1 so this change's delta specs (agent-core, channel-adapter, secure-deployment) land against the v1 baseline in `openspec/specs/` — DONE 2026-07-21 (`openspec archive henk-v1`; 5 specs synced, archived as `2026-07-21-henk-v1`)
- [x] 1.2 ntfy admin on vps — DONE 2026-07-22 (owner-run, guided). Retention VERIFIED: `cache-duration: 72h` + SQLite cache-file were already set; `attachment-expiry-duration` raised 24h→72h (handoff docs >4KB publish as attachments). Grants: `henk` += read `henk-events` + write `henk-handoffs` (existing rp5 token unchanged); new `henk-sensors` (write-only henk-events; token embedded in Grafana contact point + Gatus provider-overrides); new `henk-pickup` (read-only henk-handoffs). Full 403/200 probe matrix passed (anon publish 403; sensors publish 200; pickup read 200 / write 403; henk read henk-events 200 from rp5). BONUS security fix: Gatus was publishing with an **admin** token — migrated to a scoped `gatus` user (write-only homelab-alerts), old admin token revoked after smoke-verify.
- [x] 1.3 Grafana — DONE 2026-07-22 via API provisioning script (NOT clickwork; script saved to `~/.claude-config/provisioning/grafana-henk-events.sh`). Webhook contact point `henk-events` → `http://VPS-TS-IP:2586/henk-events?template=grafana` (ntfy 2.23.0 renders the Grafana JSON; Bearer = henk-sensors token); 4 Grafana-managed rules in folder `henk` group `henk-events` (HenkHealthEtl 10m, HenkBackupFreshness 5m, HenkDiskPressure 15m, HenkSwapPressure 15m — combined-OR per family, `noDataState: OK`, label `route=henk-events`); child notification policy on `route=henk-events`, continue=false. End-to-end verified with a synthetic `vector(1)` rule (HenkProvisionSmoke event arrived on the topic; rule deleted after). Payload does NOT match the idealized title contract — real formats captured in `tests/fixtures/ntfy_events/` for the per-source identity rules.
- [x] 1.4 Gatus — DONE 2026-07-22. All 9 alerting endpoints (5×tier-1 thr-2, 4×tier-2 thr-5) got a cloned ntfy alert with `provider-override: {topic: henk-events, token: <henk-sensors>, priority: 3}` (yq clone preserving per-tier thresholds + descriptions; config backup at `/opt/gatus/config/config.yaml.bak.2026-07-22`). Smoke-verified with a throwaway always-failing endpoint: phone alert via homelab-alerts (new gatus token) AND henk-events event both arrived; smoke endpoint removed. GOTCHAS for posterity: Gatus panics without ≥1 `conditions` per endpoint; `default-alert.enabled: false` means alerts must set `enabled: true` explicitly or they're silently mute; `mikefarah/yq` container needs `-u root` on root-owned configs. Native Gatus title is `Gatus: {group}/{endpoint}` with state in the body — fixture captured.

## 2. Tests first (from spec scenarios, backends faked)

- [x] 2.1 event-intake tests against a fake ntfy stream (REAL payload fixtures captured in `tests/fixtures/ntfy_events/` — Gatus + Grafana live formats; see its README for the resolved-variant note): subscribe/receive, reconnect-with-`since` replay (exactly-once through the pipeline), payload-as-data (hostile payload never yields an out-of-registry tool call), arrival-time debounce collapse (10 events → 1 turn; replayed backlog → 1 catch-up turn), per-identity cooldown suppression incl. per-pattern overrides, stable-identity derivation (same alert → same key; nonconforming event → deterministic fallback key), intake failure leaves DM path functional
- [x] 2.2 incident-triage tests with the SDK mocked: triageable event → triage session + proactive message when announceable; cap-overflow triageable event → session runs, handoff + audit emitted, NO Signal send, suppressed count noted in next announceable message; triage arc present (diagnosis + confidence, fix, pickup path) and arc-miss detection sets `triage_arc_complete: false` without blocking delivery; recurrence within the window → brief message referencing the prior handoff; no timer-triggered sends; owner reply continues the triage session
- [x] 2.3 triage-handoff tests: `publish_handoff` has no destination parameters, `[AI]` label enforced, doc content contract (trigger/evidence/diagnosis+confidence/fix/pickup), cap-suppressed (non-announceable) incidents still publish
- [x] 2.4 audit-log tests: one record per session (owner and event triggers), suppression records, `schema_version` present, records validate against the published JSON Schema, `triage_arc_complete` on event-triggered records, append-only behavior, write failure is loud but non-blocking
- [x] 2.5 agent-core delta tests: typed turns — event turn queued behind a running owner turn; fresh session when idle; event turn content carries the delimited untrusted-data block + triage framing while owner turns carry neither; event-turn output goes to the proactive send (suppressed when non-announceable); `/new` after triage discards incident context
- [x] 2.6 channel-adapter delta tests: proactive send reaches owner with no inbound trigger; no arbitrary-recipient parameter; long proactive message split in order

## 3. Implementation (make 2.x pass)

- [x] 3.1 `henk/events/` subscriber: ntfy JSON/WebSocket subscribe with scoped credential, last-seen-id tracking, backoff reconnect with `since` replay
- [x] 3.2 Triageable/announceable pipeline: arrival-time debounce window + per-identity cooldown (with per-pattern overrides, chronic identities like swap → 24h) + recurrence-window detection + daily hard cap gating Signal delivery only (all config-driven; cap-overflow incidents still run triage, publish handoffs, and get audit records); stable-identity derivation per source with normalized-title fallback
- [x] 3.3 Event turn integration in the dispatcher: typed-turn queue refactor (`Queue[str]` → typed owner/event turns carrying event metadata + announceable flag), enqueue into the existing serial lane, fresh-vs-active session rule, event-turn content composition (delimited untrusted-data block + triage framing: arc mandate, recurrence note, handoff instruction), event-turn output → proactive send (suppressed when non-announceable); base system prompt updated ONLY to enumerate `publish_handoff` — no triage instructions in the base prompt
- [x] 3.4 `publish_handoff` tool (notify-class, fixed topic, `[AI]` label) registered through the existing registry/gate machinery
- [x] 3.5 Proactive owner-directed send on the adapter contract + Signal implementation (reuse existing send + splitting)
- [x] 3.6 App-layer audit logger: author audit-record JSON Schema v1 as a repo file (the transferable artifact); writer validates records against it in tests; one record per session, suppression records, `triage_arc_complete` arc check on event-triggered sessions, non-blocking error handling; wire into dispatcher/session lifecycle
- [x] 3.7 Config additions: `events.enabled` (rollback flag), topics, debounce/cooldown/cap values; `.env.example` updated for the extended ntfy credential

## 4. Pickup CLI (claude-config repo)

- [x] 4.1 `henk-pickup` in `~/.claude-config/bin`: poll `henk-handoffs` via ntfy JSON poll endpoint with owner read credential; latest by default, `--list` for retention window, honest empty-state; shellcheck clean (specs/triage-handoff)

## 5. Deploy and verify on rp5

> **Owner-run constraint (2026-07-22):** sudo on rp5 is restricted — anything under `/opt` or requiring root (5.1 deploy, 5.2 backup script) is executed by the owner. Prepare exact commands and hand them over; do not assume passwordless sudo over Tailscale SSH.

- [ ] 5.1 Compose update: `henk_audit` named volume mounted at the audit path; deploy with `compose up -d`; confirm audit records appear and survive `compose down && up`
- [ ] 5.2 Extend `pi5-backup.sh` `BACKUP_VOLUMES` allowlist with `henk_audit`; test run shows the tarball on the VPS receive side
- [ ] 5.3 Deploy-verify checklist — **6 of 8 verified (2026-07-24)**; (e) required `event-pipeline-durability` to land first, which it now has:
  - [x] (a) synthetic Gatus failure → Signal message with full triage arc — repeatedly demonstrated (`probe-alpha`/`bravo`/`charlie`), each with diagnosis + confidence, fix, and pickup path
  - [ ] (b) Grafana test-fire → same — **owner-run** (needs the Grafana UI/API); the Gatus path is proven, this confirms the second sensor's format parses
  - [ ] (c) 10-event storm → one conversation — not re-run on the durability build; prior evidence from the 2026-07-23 session only. Costs 10 fresh identities and a burst of audit noise
  - [ ] (d) hostile-payload event → no out-of-registry tool call — prior evidence from the 2026-07-23 session; not yet re-run on the durability build
  - [x] (e) restart mid-stream → replayed event triaged exactly once — PASSED 2026-07-24: published while stopped, startup resumed `?since=<checkpoint>`, one triage, one handoff, one Signal, one audit record
  - [x] (f) anonymous publish to both topics rejected — 403 on POST **and** GET for both `henk-events` and `henk-handoffs` (deny-all confirmed in both directions)
  - [x] (g) `henk-pickup` retrieves the handoff from the workstation — returned the latest handoff with a complete triage arc
  - [x] (h) ACL/ports audit shows zero new exposure vs v1 — `henk` and the tailscale sidecar publish **no ports**; `signal-cli-rest-api`'s `8080/tcp` is an `EXPOSE` with no host mapping; no henk-related listener on any host interface
- [ ] 5.4 Watch first-week behavior: unprompted-message count vs owner cadence constraint, `/usage` for token burn; tune debounce/cooldown/cap from audit-log data (design D6 defaults are guesses)

## 6. Wrap-up

- [x] 6.1 README update: event flow diagram, new config keys, triage-arc contract, `henk-pickup` usage, rollback flag
- [ ] 6.2 `/docs-update`: homelab docs — new topics + grants on vps ntfy, Grafana contact point, Gatus alerting, Henk's proactive role, audit volume in backup list
- [x] 6.3 Update memory `henk-long-run-direction` (v1.2 shipped; carry v1.3 items forward)
- [ ] 6.4 Flip `enhulsman/henk` public (owner decision 2026-07-22: portfolio receipt; history pre-scrubbed via git-filter-repo, hygiene rules in CLAUDE.md). Pre-flip audit of commits added since the scrub: `git log e4ae1b8..HEAD -p | grep -nE '100\.[0-9]+\.[0-9]+\.[0-9]+|private|tk_[A-Za-z0-9]{8}|\+31[0-9]{9}'` must be empty + a gitleaks pass; then `gh repo edit enhulsman/henk --visibility public`. Pair with a portfolio project card on hulsman.dev/projects.
