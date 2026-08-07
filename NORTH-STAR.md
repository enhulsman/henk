# Henk — North Star

**Settled 2026-08-07.** This document records what Henk is *for*. It supersedes the
2026-07-20 "proactive event-to-dialogue agent" framing and the 2026-08-02 "homelab
hub" refinement. Specs in `openspec/specs/` remain the binding record of behaviour;
this document is the *why* above them — spec Purposes should be filled from it, and
a spec requirement that contradicts it gets amended deliberately, never silently.

## The statement

> **Henk is a personal assistant with homelab hands — the place where small
> operational and personal work goes to get handled.** He remembers what he's told,
> captures thoughts into a durable inbox, answers "what's the state of X / anything
> waiting on me?" across the homelab and the owner's working sessions, and when
> something breaks he investigates with real depth and — for pre-authorized
> actions — fixes it himself, reaching the owner only when a decision genuinely
> needs them. Incident triage is one *input* to Henk; it is not his identity. He is
> simultaneously the reference implementation for agent patterns that transfer to
> the owner's work-side agents (memory, permission circles, approvals with
> receipts), kept generic by the client-data wall.

## The attention contract

The owner's attention is the scarcest resource in the system, and the failure mode
to design against is Henk becoming another notification stream that gets "marked as
read". Therefore:

- **Every Henk message must either genuinely need the owner, or be something the
  owner asked for.** Owner-scheduled sends (reminders) are fine; system-scheduled
  digests, heartbeats, and "all is well" messages remain banned.
- **Mundane-and-fixable gets fixed, not flagged.** Autonomy (standing-authorized
  actions) is first an *anti-noise* measure: every verb granted standing
  authorization is one message class that stops existing.
- **Informational-but-not-actionable goes to the record** (audit log, handoff
  topic), never to the owner's inbox. The existing cap/suppression machinery
  already embodies this; it generalizes to all future message classes.

## The permission model (the transferable artifact)

Two axes, both default-deny, both enumerated:

**Action axis — authorization tier is a property of the *named action*, not the
action class.** There is no generic "restart a service"; there is
`restart_container:<name>`, each individually enumerated and individually tiered:

| Tier | Meaning | Examples (owner-sorted 2026-08-07) |
|---|---|---|
| **Standing** | Henk acts without asking; a receipt is always written | restart named (non-critical) container, wake the workstation (WoL), append to the capture inbox, add a todo |
| **Per-instance** | Inline owner approval, single-use, argument-bound, fail-closed | restart a named systemd service, create a Taiga ticket, any modify (with pre-authorized allowlists as a middle path) |
| **Never** (= unregistered) | Not in the registry; default-deny hook refuses it | raw shell, SSH, deletes (unless deliberately promoted to per-instance), everything unenumerated |

**Data axis — circles with explicit allowlist membership.** Client data lives only
on client-issued machines — a physical wall that stands regardless of policy. The
owner's own data (personal *and* own-work metadata: session status, todos, calendar)
is shareable with Henk by explicit allowlist, store by store. Credentials of any
kind are structurally off-limits. An empty allowlist always surfaces nothing.

**The theorem connecting the axes:** how much data an agent may see scales with how
constrained its output reach is. Henk's external outputs are structurally
owner-only (Signal to one identity, deny-all ntfy topics) — the lethal-trifecta
comms leg stays cut — which is *why* his data circles can widen safely.

## Architecture principles

1. **The closed toolset is the security boundary.** Every capability is a named,
   owner-reviewed, registry-classified tool. Henk never authors or registers his
   own tools/skills; capability grows by **Henk proposes, owner disposes** —
   drafted specs through the normal review workflow. Operational knowledge, by
   contrast, accretes freely — as *memory*, not code.
2. **Continuity by rebuild, not by long-lived sessions.** Durable memory +
   conversation replay make session loss cheap (rp5 restarts are routine). Memory
   is a capped, type-namespaced store of short facts; writes are explicit
   (owner command or agent tool-call — auditable), with ambient learning as a
   pruned growth path.
3. **Publisher-push over deny-all topics; zero inbound, ever.** New data sources
   (e.g. workstation session status) publish to a topic Henk reads; filtering
   happens *at the publisher*, where the trust lives. No new listener, port, or
   inbound grant.
4. **Ambient context comes from process environment and configuration, never from
   model-supplied arguments.** No tool accepts a recipient, topic, or identity
   parameter.
5. **Receipts always.** Every action — especially standing-authorized ones —
   writes an append-only audit record including who/what authorized it. An agent
   that acts without asking must be *more* accountable, not less.

## What Henk is not

- Not a pager, digest bot, or dashboard — see the attention contract.
- Not the sole delivery path for any critical alert (he cools down, caps, and can
  be down).
- Not a self-extending framework, and never a host for third-party skill
  marketplaces.
- Not a holder of client data or of credentials beyond his own scoped tokens.

## Roadmap (direction, not commitment — each item is its own OpenSpec change)

| # | Change | Core contents |
|---|---|---|
| 1 | Memory & capture | Memory store + recall; capture inbox (first mutating tool, standing tier); approval gate → three-tier model; approval decisions threaded into audit records |
| 2 | Reminders | Owner-scheduled delivery; natural-language time parsing; amends the no-timers clause to "owner-scheduled yes, system-scheduled no" |
| 3 | Read depth | Named, allowlisted Gatus/Prometheus queries; homelab-docs corpus tool |
| 4 | Session awareness | Workstation-side publisher → deny-all sessions topic; publisher-side filtering |
| 5 | Runbook actions | The standing/per-instance verb registry made real (curated action API, never raw shell) |

Hygiene carried by whichever change touches each spec: fill placeholder spec
Purposes from this document; reconcile the known spec-vs-deployed divergences.

Design question open for change 1: the capture inbox's home — Henk-native store
vs. a small personal-inbox service with an API/MCP face that other sources
(vault todos) also push into. Direct writes into the notes vault are off the
table (sync-corruption risk, established 2026-07-21).
