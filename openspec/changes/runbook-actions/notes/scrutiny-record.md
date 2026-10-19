# Scrutiny record — `runbook-actions` proposal

**Reviewed:** 2026-10-19, four agent-run rounds, each by a fresh `project-scrutinizer`
reading the five artifacts against the base specs, the Henk code (`henk/events/pipeline.py`,
`identity.py`, `henk/agent/core.py`, `henk/gate/approval.py`, `henk/audit/logger.py`) and
the homelab docs. Each round's findings were applied to the artifacts before the next one.
`openspec validate runbook-actions --strict` passed after every round. The task-agent card
stopped at three rounds; the supplied round-4 review verified the round-3 fixes and returned
**APPROVED WITH CONCERNS**, conditional on R1 and R2 landing before decisions 3 and 7
are signed. Those replacements are now applied. Owner decisions remain open.

| Round | Verdict | Blocking findings | What moved |
|---|---|---|---|
| 1 | NEEDS REWORK | 1 critical, 6 major, 7 minor | The 6 h cooldown swallowed the re-fire that the rate-limit escalation relied on, and swallowed the RESOLVED event that verification needed. A dropped held message broke the cap, the suppressed-count note and two unmodified incident-triage clauses. A multi-item debounce batch could drop an unrelated incident. Tool-raised taint allowed unbound restarts. Rate limits counted only `ok` runs. Per-instance was oversold against a compromised container. A `job`-label binding could hit the wrong host. |
| 2 | NEEDS REWORK | 1 critical, 3 major, 6 minor | The round-1 cooldown reset let the verifying RESOLVED become a triage of its own. The reset also erased recurrence framing. The dropped-triage test was vacuously true for v5 records. Verification matched only a held message, so a mid-turn RESOLVED was missed. |
| 3 | NEEDS REWORK | 0 critical, 2 major, 4 minor | A verified restart in a *sent* triage rebuilt as "dropped" after a restart. A relapse between a mid-turn verification and the drop was swallowed. Minors: cap-refund ordering, the no-recovery line naming verified items, ambiguous "counts as no announcement", and where the fixed-count increment sits in the write order. |
| 4 | APPROVED WITH CONCERNS | 0 critical, 2 major, 7 minor | R1/F1 requires FIRING for a binding and adds the RESOLVED denial scenario; R2/F2 offers no silence in v1 as decision 7(b). All seven mechanical MINOR fixes (F3–F9) applied; disposition below. |

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

**Round 3 fixes, verified by round 4:** explicit `phase: drop` records and the startup order
(drop records, then one store transaction for row delete plus fixed count, then
`rehydrate`); relapse interception with its own line; the no-recovery line limited to
unverified items; cap reconstruction "skips" a dropped triage, which neither counts as an
announcement nor changes the pending suppressed count; cap-refund ordering accepted as a
trade-off in D9.

**Still open for the review sitting:** every item under "Decisions that are yours",
especially 3 (event-turn standing actions) and 3b (`tainted-owner` per-instance prompts);
the container and job names (task 0.1 should run before decision 2 is signed); whether
the verified-silence machinery (D9) is worth its complexity, given that on day one only
three exporters can trigger it. Decision 7(b) now offers the simpler staged path: no
silence in v1, always send with the action line, and D9 as a follow-up. Decision 6 remains
the separate receipt-format choice.

## Round 4 — applied review

Source: `/home/wsl/Coding/agent-week-2026-10-17/reports/reviews/035-round4.md`.
Verdict: **APPROVED WITH CONCERNS**; two MAJOR findings (F1/R1 and F2/R2),
seven MINOR findings (F3–F9).

Applied:

- **F1 / R1:** exact replacement binding rule requires derived state FIRING; RESOLVED
  and unknown states bind nothing. Added the exact RESOLVED scenario, mirrored D7 and
  task 5.1, and made the positive binding scenario explicitly FIRING.
- **F2 / R2:** exact decision-7 replacement, including option (b): no silence in v1,
  always send with the successful-action line, and verified silence (D9) as a follow-up.
- **F3:** end verification registration on send or drop, then pass events through unchanged;
  mirrored in D9 Send.
- **F4:** corrected design context to four egress ports and D1's rp5 grant to 8080 only;
  identified the undeployed base-spec 8000 grant.
- **F5:** task 0.3 accepts any WoL sender and may follow task 4.3.
- **F6:** split decision 2 into 2a standing, 2b per-instance and 2c excluded, preserving
  target lists and the caveat once under 2b; task 0.1 settles names before signing.
- **F7:** added the exact follow-up-turn standing-restart disclosure and `no restarts`
  option to 3b, before Confidence.
- **F8:** task 4.1 uses the real `agent-job` test seams with only ntfy faked; task 8.4
  adds arity and sudo list-denial checks as owner-run instructions only.
- **F9:** D9 includes `relapsed` verification records, and the requirement explicitly
  records each verification and each relapse.

Not applied:

- **No numbered finding omitted.** All requested MAJOR and mechanical MINOR fixes landed.
- **R2's conditional removal of D9 machinery:** not triggered; the owner has not chosen
  7(b). Requirements, scenarios, configuration, tables and tasks for verified silence remain
  the proposed 7(a) design. If 7(b) is chosen, prune those artifacts and preserve D9 as
  deferred before implementation, as the review directs.
- **Review questions and live checks:** no new decisions, implementation or host checks;
  these remain for the owner sitting or later apply. This pass edits only this change.

Validation: `openspec validate runbook-actions --strict` returned rc=0 with output
`Change 'runbook-actions' is valid` (telemetry disabled; no network or host access).
