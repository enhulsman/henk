# Proposal: henk-events

## Why

Henk v1 as deployed is reactive-only, and for this owner that's near-redundant: they carry a terminal in their pocket and cover reactive use themselves. The decided long-run identity (2026-07-20 brainstorm, memory `henk-long-run-direction`) is an **event-to-dialogue layer**: Henk notices, does the first 90 seconds of triage, and *starts* a Signal conversation the owner can interrogate. Without this, v1 stays an architecture/security achievement with little daily value.

This is roadmap step v1.2. It must include sensor plumbing, not just subscription: verified 2026-07-19, only `HealthEtl*` Prometheus rules fired in 15 days and all 22 Prom rules are unrouted — a Gatus-only event-Henk would be mute.

## What Changes

- **Sensor plumbing (infra):** route Gatus alerts and a curated, owner-approved Prometheus subset (`HealthEtl*`, backup freshness, disk >85%, swap pressure) to a new deny-all ntfy events topic — Prom side via a Grafana contact point (already runs; no Alertmanager, no new service).
- **Event intake:** Henk subscribes to the events topic over his existing egress (vps:2586 is already in the `tag:henk` allowlist — **no ACL change, no inbound webhook**; the zero-inbound posture holds). Event payloads are treated as data, never instructions; alert storms are debounced into one conversation.
- **Incident triage:** an event starts an agent session that produces a Signal message to the owner ending with the triage arc: (a) diagnosis + confidence, (b) suggested fix, (c) a pickup path. Owner replies interrogate that same conversation. Cadence is condition-triggered only — no daily brief, no "all is well" digests, a few unprompted messages/week max.
- **Triage handoff:** Henk publishes the full triage doc to a second deny-all ntfy handoffs topic (write-only publish, same capability class as `notify`), and a small `henk-pickup` CLI in `~/.claude-config/bin` polls that topic so any Claude session on any host can retrieve the latest handoff on demand.
- **Audit log:** an append-only, schema-versioned JSONL log on a Henk volume — one record per agent session (trigger event, tool calls, diagnosis + confidence, handoff published, approval requests/decisions, outcome, model + token usage). The schema itself is the Anamata-transferable artifact. Volume added to the rp5 backup allowlist.
- **Still zero mutations.** Prove event-driven triage before any write tool. Runbook-action self-fix (v1.4+), the herdr subscriber plugin and vps-side archiver (v1.3), personal-ops nudges (v1.3), and `taiga_read` (v1.1) stay out of scope.

## Capabilities

### New Capabilities

- `sensor-routing`: which alert sources feed the events topic and how — Gatus alerting config and the Grafana contact point for the curated Prom subset; event message format expectations.
- `event-intake`: Henk's ntfy subscription — connect/reconnect over existing egress, payload-as-data posture, dedup and storm debouncing, behavior when the events topic is unreachable.
- `incident-triage`: event → agent session → owner conversation; the mandatory triage arc (diagnosis + confidence, fix, pickup path); condition-triggered cadence rules; how owner replies continue the triage conversation.
- `triage-handoff`: the handoff doc's content contract, publishing to the deny-all handoffs topic, and the `henk-pickup` CLI's retrieval behavior.
- `audit-log`: the append-only JSONL record schema, versioning, write guarantees, and backup coverage.

### Modified Capabilities

- `agent-core`: sessions can now be initiated by an event, not only by an inbound owner message; event-triggered sessions define their interaction with serial processing, idle expiry, and `/new`.
- `channel-adapter`: the adapter contract gains proactive owner-directed sends (agent-initiated messages not tied to an inbound message); allowlist and DM-only rules unchanged.
- `secure-deployment`: scoped-secrets inventory grows by one ntfy **subscribe** credential (Henk's current ntfy user is write-only; the new events/handoffs topics are deny-all) and the audit-log volume joins the backup allowlist. Egress grants are explicitly unchanged.

> Note: henk-v1's specs have not been synced to `openspec/specs/` yet (`/opsx:sync` + `/opsx:archive` pending). Sync henk-v1 before applying this change so the deltas above land against the v1 baseline.

## Impact

- **Repo code:** new event subscriber + debouncer, triage session flow, handoff publisher, audit logger; config additions (topics, cadence caps, curated-source list).
- **Infra (owner prep, outside this repo):** ntfy admin on vps — create deny-all events + handoffs topics, mint a subscribe-capable token for Henk; Grafana contact point + alert routing for the curated Prom subset; Gatus alerting → events topic; `henk-pickup` CLI lands in the claude-config repo (precedent: `homelab-health`); `pi5-backup.sh` volume allowlist gains the audit volume.
- **No Tailscale ACL PR:** vps:2586 egress is already granted to `tag:henk`; no new ports, no inbound.
- **Dependencies:** unchanged (Agent SDK, signal-cli-rest-api, ntfy). Subscription-limit exposure grows with event volume — bounded by debouncing and the curated source list.
- **Docs:** homelab docs need the events/handoffs topics, sensor routing, and Henk's new proactive role (`/docs-update` after implementation).
- **Tier W untouched;** personal-ops content over Henk/Signal is Tier 1 per owner decision. Graduated-autonomy gate taxonomy, when it comes (v1.4+), is described by the owner — the NorthStar doc is never fetched.
