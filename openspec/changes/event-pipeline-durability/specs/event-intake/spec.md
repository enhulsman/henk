# event-intake (delta)

## MODIFIED Requirements

### Requirement: Reconnect with bounded replay
When the subscription drops, Henk SHALL reconnect with backoff and resume from the last-seen message id so events published during the outage (within ntfy's retention window) are received exactly once. The last-seen message id SHALL be persisted to durable storage on the audit volume as it advances, and on process startup the subscription SHALL resume from the persisted id — so events published while Henk was **stopped** (not merely disconnected) are also replayed on restart, within the retention window. When no checkpoint exists (first ever start), the subscription starts without a `since`. Replayed events SHALL flow through the same debounce, cooldown, and cadence-cap pipeline as live events.

#### Scenario: Event during a disconnection
- **WHEN** the subscription is down while one event is published, and Henk reconnects within the retention window
- **THEN** the event is processed once, and exactly one triage conversation results

#### Scenario: Event published while the process is stopped
- **WHEN** an event is published while the Henk process is not running, and the process restarts within the retention window
- **THEN** on startup the subscription resumes from the persisted last-seen id, the event is replayed, and it is triaged exactly once

#### Scenario: Backlog after downtime does not storm
- **WHEN** Henk reconnects (or restarts) after downtime during which many events accumulated
- **THEN** the replayed events are collapsed by the debounce/cooldown/cap pipeline rather than each producing its own unprompted message

#### Scenario: First start with no checkpoint
- **WHEN** the process starts and no persisted offset exists
- **THEN** the subscription starts without a `since` and begins tracking a new checkpoint

### Requirement: Storm debounce and per-alert cooldown
Events arriving within the configured debounce window SHALL be collapsed into a single event turn carrying all of them. The debounce window is measured on arrival at the intake, not on the event's original timestamp — replayed events therefore collapse into at most one catch-up turn. A re-fire of the same alert identity within its configured cooldown SHALL NOT start a new conversation; it SHALL still be recorded in the audit log. Cooldown SHALL be configurable per alert-identity pattern so chronic identities can carry longer cooldowns. **Per-identity cooldown state SHALL survive a process restart** — a re-fire within cooldown after a restart SHALL still be suppressed, reconstructed from durable state — so a restart does not re-arm cooled-down identities.

#### Scenario: Alert storm collapses
- **WHEN** ten events arrive within one debounce window
- **THEN** exactly one triage conversation is started, covering the storm

#### Scenario: Chronic alert re-fires inside cooldown
- **WHEN** an alert with the same identity fires again within its cooldown period
- **THEN** no new unprompted message is sent, and an audit record of the suppressed event exists

#### Scenario: Cooldown survives a restart
- **WHEN** an alert identity was triaged, the process restarts, and the same identity re-fires while still inside its cooldown
- **THEN** the re-fire is suppressed (no new conversation) and an audit record of the suppression exists

## ADDED Requirements

### Requirement: Intake offset checkpoint is durable and non-blocking
The intake SHALL persist the last-seen event id to the audit volume such that the checkpoint survives container recreation. A failure to write the checkpoint SHALL be logged at error level and SHALL NOT block event intake, triage, or the reactive owner-DM path.

#### Scenario: Checkpoint survives recreation
- **WHEN** the stack is torn down and recreated after events have been processed
- **THEN** the persisted last-seen id is present on the audit volume and is used to resume the subscription

#### Scenario: Checkpoint write failure is non-fatal
- **WHEN** the checkpoint cannot be written
- **THEN** the failure is logged and intake continues processing events
