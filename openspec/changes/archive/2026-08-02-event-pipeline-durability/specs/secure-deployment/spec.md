# secure-deployment (delta)

## ADDED Requirements

### Requirement: Graceful shutdown within the container stop grace period
The process SHALL handle SIGTERM (as sent by `docker stop`) by unwinding its shutdown path — cancelling the receive loop and coordinator, and flushing the open session's audit record — within the container's stop grace period, so state is flushed cleanly rather than lost to a SIGKILL escalation. SIGINT SHALL behave identically for interactive shutdown.

#### Scenario: docker stop flushes cleanly
- **WHEN** the container receives SIGTERM while a session is open
- **THEN** the open session's audit record is flushed and the process exits within the stop grace period without escalating to SIGKILL

#### Scenario: Intake offset persisted at shutdown
- **WHEN** the process is stopped gracefully
- **THEN** the last-seen event id checkpoint on the audit volume reflects the most recently processed event, so the next start resumes correctly

### Requirement: Pipeline checkpoints share the backed-up audit volume
Durable pipeline state (the intake offset checkpoint and any cadence-rehydration source) SHALL live on the existing backed-up audit volume; this change SHALL NOT add a new volume, published port, listening socket, or ACL/egress grant.

#### Scenario: No new infrastructure surface
- **WHEN** the deployed stack's volumes, ports, and ACL grants are audited before and after this change
- **THEN** they are identical except for new files on the existing audit volume
