# Design: henk-events

## Context

Henk v1 runs on rp5 as a three-container stack (agent + signal-cli-rest-api + Tailscale sidecar, `tag:henk`, zero inbound), reactive-only: owner DM → agent turn → reply. The real boundary is a default-deny `PreToolUse` hook; the registered toolset is `homelab_health`, `todo_read`, `notify` — all read-only/notify-class. The direction memo (`henk-long-run-direction`, 2026-07-20/21) settled the v1.2 shape: sensors publish events to ntfy; Henk subscribes, triages, and starts the conversation. Key verified facts this design leans on:

- vps ntfy: auth default-access deny-all, 72h retention, 5MB attachments; Henk's ntfy user is currently **write-only on `homie-henk`**.
- vps:2586 is already in `tag:henk`'s egress allowlist → subscribing requires **no ACL change**.
- Only `HealthEtl*` Prom rules actually fired in 15 days; all 22 Prom rules are unrouted; Grafana (vps) already runs with a Discord contact point; there is no Alertmanager.
- Gatus (rp5) already supports ntfy alerting.
- Owner cadence constraint: condition-triggered only, a few unprompted messages/week max, no digests. Mutations: none in this change.

## Goals / Non-Goals

**Goals:**

- Events from Gatus + a curated Prom subset reach Henk without opening any inbound path.
- Every incident becomes one debounced Signal conversation ending in the triage arc (diagnosis + confidence, suggested fix, pickup path).
- Full triage doc retrievable from any Claude session via `henk-pickup` (pull-based, zero new services).
- Append-only, schema-versioned audit record per agent session — the Anamata-transferable artifact.

**Non-Goals:**

- Any mutating tool or runbook action (v1.4+); the approval gate stays dormant but wired.
- herdr subscriber plugin, vps-side git archiver, personal-ops nudges (v1.3).
- `taiga_read` (v1.1), Alertmanager, any new always-on service.
- Multi-owner, group chats, second channels.

## Decisions

### D1 — Transport: ntfy subscribe, never an inbound webhook

Henk opens an outbound streaming connection (ntfy JSON/WebSocket subscribe) to the events topic on vps:2586. Alternatives rejected: an inbound webhook receiver (breaks the zero-inbound invariant, needs an ACL PR) and direct Prometheus/Gatus polling from Henk (time-triggered scanning, exactly the cadence model the owner rejected, and it makes Henk the sensor instead of the dialogue layer).

### D2 — Prom routing via a Grafana contact point; Gatus alerts natively

Grafana already runs on the vps and provides a generic **Webhook contact point** that targets ntfy's HTTP publish API (there is no native ntfy contact point) — a notification template formats the payload to the sensor-routing contract (source, alert name, state in the title); a notification policy routes only the curated subset (`HealthEtl*`, backup freshness, disk >85%, swap pressure — owner-approved, chronic swap explicitly included). Gatus gets an ntfy alerting block pointed at the same topic. Alertmanager is deployed only if contact-point fidelity proves insufficient (constraint 4: no new always-on services without payoff).

### D3 — Two new deny-all topics; one extended credential

- `henk-events`: sensors (Grafana, Gatus, future crons) write; Henk reads.
- `henk-handoffs`: Henk writes; owner's admin account + `henk-pickup` read.

The existing `henk` ntfy user is extended server-side (read on events, write on handoffs) rather than minting a second identity — one credential in `.env`, scoping enforced by ntfy's per-topic ACLs, and the instance stays default-deny so topic names are not secrets. `notify`'s topic (`homie-henk`) is unchanged.

### D4 — Event payloads are data; the prompt wall is explicit

Event content (alert names, annotations, Gatus messages) enters the triage prompt only inside a clearly delimited untrusted-data block; the system prompt states that event text is sensor output, never instructions. The structural guarantees don't move: the PreToolUse default-deny hook and read-only registry mean a hostile payload can at worst waste a conversation, not act. (Trifecta legs cut: comms remain owner-only; tools remain read-only.)

### D5 — Event turns join the existing serial conversation lane

An event (post-debounce) is enqueued as an **event turn** in the same per-owner serial queue as inbound messages — no second concurrency model. Session rules are unchanged: if no session is active or idle-expired (the normal case), the turn starts a fresh session; if the owner is mid-conversation, the triage turn runs in that session so the dialogue stays coherent. Owner replies after a triage message continue that session under existing continuity rules; `/new` and idle expiry behave as in v1. Alternative rejected: a dedicated triage session pool — more machinery, and it would let an unprompted message land mid-conversation with context Henk's replies then can't see.

### D6 — Debounce, dedup, and a hard cadence cap (defense in depth)

Three layers keep Signal quiet:

1. **Debounce window** (default 120s, config): events arriving within the window collapse into one event turn carrying all of them ("alert storm → one conversation").
2. **Per-alert cooldown** (default 6h, configurable **per alert-identity pattern** — chronic identities like swap pressure ship with a 24h override; keyed on the stable identity from the event-intake derivation rules): a re-fire inside cooldown never starts a new conversation — it is audit-logged only. This is what makes chronic swap-pressure inclusion livable. A repeat that *survives* cooldown but was triaged within the recurrence window gets recurrence framing: keep it brief, reference the prior handoff, skip full evidence re-gathering.
3. **Hard cap** (default 3 unprompted conversation starts per 24h, config): cap-overflow incidents still run their full triage session (audit record + handoff published) but Signal delivery is suppressed; the next announceable message notes "N further incidents suppressed, see handoffs". **The cap bounds unprompted-message volume (annoyance), not token spend** — token spend is bounded upstream by the curated source list, debounce, and cooldown.

### D7 — Handoff publishing is a registered notify-class tool

`publish_handoff` is a new registered tool: fixed topic (`henk-handoffs`), no destination parameters, `[AI]` labeling like `notify`. The triage system prompt instructs the agent to author the handoff doc (trigger, evidence, diagnosis + confidence, fix, pickup instructions) and call the tool, then reference it in the Signal message's pickup path. Tool-shaped (vs. automatic plumbing) because the doc is agent-authored content and a tool call lands in the audit record. Notify-class → no approval gate, same as `notify` — publishing to a deny-all topic the owner controls is the same capability class already granted.

### D8 — Audit log is app-layer, not model-layer

The dispatcher/session layer appends one JSONL record per agent session to `/data/audit/` on a new named volume (`henk_audit`), capturing: `schema_version`, trigger (owner-message | event + event details), tool calls made, diagnosis + confidence (parsed from triage output where present), handoff message id, approval requests/decisions, outcome, model + token usage. The model never writes the log. Audit write failure is loud (process log at ERROR) but never blocks message handling — availability of triage beats completeness of audit for a read-only agent. Volume joins the `BACKUP_VOLUMES` allowlist in `pi5-backup.sh` (precedent: task 5.2 of henk-v1).

### D9 — `henk-pickup` is a poll CLI in claude-config, not a service

A small script in `~/.claude-config/bin` (precedent: `homelab-health`) hits ntfy's poll endpoint (`/henk-handoffs/json?poll=1&since=...`) with the owner's read credential and prints the latest handoff (or all within retention, `--list`). Pull-based, works from any host on the tailnet, zero daemons. The retention limit (72h, VERIFIED 2026-07-22 — messages and, after a config bump, attachments) is accepted: handoffs are working notes, the audit log is the record.

### D10 — Replay on reconnect, bounded

The subscriber tracks the last-seen message id and reconnects with `since=<id>` (backoff on failure, same pattern as the Signal bridge reconnect). Replayed events flow through the same debounce/cooldown/cap pipeline, so a reconnect after downtime produces at most one catch-up conversation, not a backlog storm.

## Risks / Trade-offs

- **[Grafana contact point misconfigured → event-Henk is silently mute]** → deploy-verify includes an end-to-end synthetic alert from *each* source (Grafana test-fire, Gatus manual trigger) observed as a Signal message.
- **[Chronic alerts (swap) re-fire forever]** → per-alert cooldown (D6.2); cooldown value tunable in config without redeploy logic changes.
- **[Alert payload injection]** → D4 posture + structural PreToolUse deny; deploy-verify includes a hostile-payload event asserting no out-of-registry tool call.
- **[Missed events while Henk is down longer than ntfy retention]** → accepted; Gatus/Grafana keep re-firing on persisting conditions, so a live incident re-alerts after cooldown. Retention itself is verified (not assumed) in task 1.2 — the ntfy default of 12h with a non-persistent cache would gut both replay (D10) and henk-pickup (D9).
- **[Audit log grows unbounded]** → JSONL on its own volume, size visible to `homelab_health`'s disk metrics; rotation deferred until size warrants (records are small).
- **[Token burn from event chatter]** → curated source list + D6 layers; task retained to watch `/usage` in the first week; model droppable to Haiku via existing config.
- **[henk-v1 specs not yet synced]** → sync + archive henk-v1 before `/opsx:apply` of this change so deltas land on the v1 baseline (also listed as task 0).

## Migration Plan

1. Owner prep (vps): create deny-all topics, extend `henk` ntfy user grants, add Grafana contact point + notification policy, add Gatus alerting block. Verify with `curl` publish/subscribe smoke checks.
2. Repo: implement + test (TDD from specs), add `henk_audit` volume to compose, new config keys (`events_topic`, `handoffs_topic`, debounce/cooldown/cap).
3. Deploy to rp5 (`compose up -d`), extend `pi5-backup.sh` allowlist, run deploy-verify checklist.
4. `henk-pickup` lands in the claude-config repo.
5. Rollback: config flag `events.enabled: false` (subscriber never starts → v1 behavior exactly); full rollback is redeploying the previous image tag. No schema/data migration to unwind; new topics are inert if unused.

## Open Questions

- Debounce/cooldown/cap defaults above are informed guesses (confidence: moderate) — tune from the first week's audit log rather than debating now; all three are config values.
