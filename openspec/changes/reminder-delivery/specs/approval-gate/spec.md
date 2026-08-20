# approval-gate Specification (delta)

## ADDED Requirements

### Requirement: Scheduled delivery is app-initiated and outside the gate
Delivery of a due reminder SHALL NOT pass through the approval gate. Its authority was granted when the reminder was scheduled — by the owner directly through a command, or by a gate-authorized standing-tier invocation in an untainted owner turn whose confirmation echoed the resolved due time. The gate governs model-initiated invocations only; the scheduler, like an owner command, is not one. The scheduler SHALL therefore send without occupying or consulting the pending-approval slot, a pending approval SHALL be unaffected by a delivery, and a delivered reminder SHALL NOT be classifiable as an approval prompt (it carries the reminder marker, and the gate classifies inbound text only). Accountability for every delivery comes from its `reminder` audit record (audit-log spec), not from an approval.

#### Scenario: Delivery does not prompt
- **WHEN** a reminder comes due
- **THEN** it is delivered with no approval prompt, and no approval record is created for the send

#### Scenario: Delivery leaves a pending approval intact
- **WHEN** a reminder is delivered while an approval prompt is pending
- **THEN** the pending approval is unchanged and still resolvable by the owner's next approval or denial keyword

#### Scenario: Every delivery is traceable to a schedule
- **WHEN** any delivered reminder's audit trail is inspected
- **THEN** it contains both the `scheduled` record naming who scheduled it and the delivery record naming the scheduler
