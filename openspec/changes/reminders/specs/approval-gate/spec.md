# approval-gate Specification (delta)

## ADDED Requirements

### Requirement: Reminder tools carry a declared tier and scope
The `remind` tool SHALL be registered as mutating, authorization tier **standing**, turn scope **owner-only** — the same containment argument as `capture` and `store_memory`: an additive write into a Henk-local store whose only external effect is a message to the configured owner, receipted every time. It SHALL therefore execute without an approval prompt in an untainted owner session, and SHALL be denied with outcome `out-of-scope` during any event turn and during any turn of a tainted session, exactly as the existing turn-scope requirement specifies. `reminders_read` SHALL be registered read-only and SHALL bypass the gate. The standing-tier kill-switch SHALL apply to `remind` unchanged: with demotion enabled, scheduling a reminder requires inline approval.

#### Scenario: Scheduling executes without a prompt
- **WHEN** the agent invokes `remind` in an untainted owner session
- **THEN** the reminder is stored, no approval prompt is sent, and the authorization is reported for the audit record with tier `standing` and outcome `authorized`

#### Scenario: Untrusted event input cannot schedule a message
- **WHEN** an event payload instructs Henk to set a reminder and the event turn is processed
- **THEN** the invocation is denied with outcome `out-of-scope`, no channel message is sent, and no reminder is stored

#### Scenario: Tainted session cannot schedule
- **WHEN** the owner follows up on a triage message in the session the event turn started and the agent then invokes `remind`
- **THEN** the invocation is denied with outcome `out-of-scope` and the tool result names the incident taint and the `/remind` command as the remedy

#### Scenario: Kill-switch demotes scheduling too
- **WHEN** the demotion flag is enabled and the agent invokes `remind`
- **THEN** an approval prompt is sent and the reminder is stored only on an approval keyword

### Requirement: Scheduled delivery is app-initiated and outside the gate
Delivery of a due reminder SHALL NOT pass through the approval gate. Its authority was granted when the reminder was scheduled — by the owner directly through a command, or by a gate-authorized standing-tier invocation in an untainted owner turn — and the gate governs model-initiated invocations only. The scheduler SHALL therefore send without occupying or consulting the pending-approval slot, and a pending approval SHALL be unaffected by a delivery. Accountability for the send comes from its `reminder` audit record (audit-log spec), not from an approval.

#### Scenario: Delivery does not prompt
- **WHEN** a reminder comes due
- **THEN** it is delivered with no approval prompt, and no approval record is created for the send

#### Scenario: Delivery leaves a pending approval intact
- **WHEN** a reminder is delivered while an approval prompt is pending
- **THEN** the pending approval is unchanged and still resolvable by the owner's next approval or denial keyword

#### Scenario: Every delivery is traceable to a schedule
- **WHEN** any delivered reminder's audit trail is inspected
- **THEN** it contains both the `scheduled` record naming who scheduled it and the `delivered` record naming the scheduler
