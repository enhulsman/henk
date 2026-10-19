# Scrutiny record — `runbook-actions` proposal

**Reviewed:** 2026-10-19, three agent-run rounds, each by a fresh `project-scrutinizer`
reading the five artifacts against the base specs, the Henk code (`henk/events/pipeline.py`,
`identity.py`, `henk/agent/core.py`, `henk/gate/approval.py`, `henk/audit/logger.py`) and
the homelab docs. Each round's findings were applied to the artifacts before the next one.
`openspec validate runbook-actions --strict` passed after every round. The task-agent card
stops at three rounds. **Not APPROVED.** The round 3 fixes are applied but no reviewer has
read them, and the coordinator's own `/scrutinize` verdict is the one that counts.

| Round | Verdict | Blocking findings | What moved |
|---|---|---|---|
| 1 | NEEDS REWORK | 1 critical, 6 major, 7 minor | The 6 h cooldown swallowed the re-fire that the rate-limit escalation relied on, and swallowed the RESOLVED event that verification needed. A dropped held message broke the cap, the suppressed-count note and two unmodified incident-triage clauses. A multi-item debounce batch could drop an unrelated incident. Tool-raised taint allowed unbound restarts. Rate limits counted only `ok` runs. Per-instance was oversold against a compromised container. A `job`-label binding could hit the wrong host. |
| 2 | NEEDS REWORK | 1 critical, 3 major, 6 minor | The round-1 cooldown reset let the verifying RESOLVED become a triage of its own. The reset also erased recurrence framing. The dropped-triage test was vacuously true for v5 records. Verification matched only a held message, so a mid-turn RESOLVED was missed. |
| 3 | NEEDS REWORK | 0 critical, 2 major, 4 minor | A verified restart in a *sent* triage rebuilt as "dropped" after a restart. A relapse between a mid-turn verification and the drop was swallowed. Minors: cap-refund ordering, the no-recovery line naming verified items, ambiguous "counts as no announcement", and where the fixed-count increment sits in the write order. |

**What the rounds changed, all recorded in `design.md` D7–D9:**
- Verified silence is now specified against the cadence pipeline as it is
  (`evaluate`: cooldown, then cap, then batching):
  - the verification watcher runs ahead of debounce and cooldown and consumes the verifying
    RESOLVED, or marks a relapse;
  - cooldown and recurrence are two clocks, and a drop resets only cooldown;
  - a drop gives back the cap slot and returns the suppressed count;
  - a drop is an explicit `action` record (`phase: drop`), never inferred from
    verifications.
- A hold needs every item bound, restarted `ok` and verified; incomplete triages are never
  held; notes are applied only at send time.
- In any tainted session, restarts are limited to the bound set; tool taint gives an empty
  set.
- Rate limits count every published request at authorization, one per target per event
  turn.
- Bindings: single-alert frames only, and the `job` value must be proven unique to one rp5
  target. The claim is now "the model cannot choose the target", not "the alert is
  genuine".
- Owner-facing text: separate refused, relapse and two rate-limit lines. The 10-minute delay
  is disclosed. The fixed-count note rides only triage messages and replies.
- Targets: `finance-automation-bot` (not `finance-bot`), `homelab-docs-pull` excluded, the
  registry listed in the spec itself, and decision 2 states what per-instance does not
  protect against.

**Round 3 fixes, not re-reviewed:** explicit `phase: drop` records and the startup order
(drop records, then one store transaction for row delete plus fixed count, then
`rehydrate`); relapse interception with its own line; the no-recovery line limited to
unverified items; cap reconstruction "skips" a dropped triage, which neither counts as an
announcement nor changes the pending suppressed count; cap-refund ordering accepted as a
trade-off in D9.

**Still open for the review sitting:** every item under "Decisions that are yours",
especially 3 (event-turn standing actions) and 3b (`tainted-owner` per-instance prompts);
the container and job names (task 0.1 should run before decision 2 is signed); whether
the verified-silence machinery (D9) is worth its complexity, given that on day one only
three exporters can trigger it. The simpler alternative is decision 6's "message per fix".
