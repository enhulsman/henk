# agent-core (delta)

## MODIFIED Requirements

### Requirement: Event-triggered turns share the conversation lane
A triageable event SHALL be enqueued as an event turn in the same serial per-owner queue as inbound messages — event and owner turns SHALL never run concurrently. A **new incident** (a fresh debounced event turn) SHALL start its own session rather than inheriting the context of an unrelated prior incident or an owner conversation, so no incident's context bleeds into another's triage. An owner reply following a triage message SHALL continue that incident's session, so follow-up questions resolve against the incident context under the existing continuity, `/new`, and idle-expiry rules. Sessions started or continued by an event turn otherwise follow the existing continuity, `/new`, and idle-expiry rules.

#### Scenario: Event arrives while an owner turn is running
- **WHEN** an event turn is enqueued while an owner message is mid-turn
- **THEN** the event turn runs after the owner turn completes, and neither is dropped

#### Scenario: New incident does not inherit prior context
- **WHEN** an event turn for one incident is processed while a session started by a different incident (or by an owner conversation) is still active
- **THEN** the new incident's triage runs with no context from the prior session

#### Scenario: Owner resets after a triage message
- **WHEN** the owner sends `/new` after receiving a triage message
- **THEN** the triage session is discarded and the next message starts fresh, exactly as for owner-initiated sessions

#### Scenario: Owner interrogates the incident
- **WHEN** the owner replies with a follow-up shortly after a triage message
- **THEN** the reply resolves against that incident's session context

## ADDED Requirements

### Requirement: Event-triage audit record is written at triage completion
On completion of an event turn, the agent core SHALL write that triage's audit record immediately, without waiting for session close. Keeping the session open for owner interrogation SHALL NOT delay or suppress the record.

#### Scenario: Record written before session close
- **WHEN** an event triage turn completes and its session remains open awaiting owner follow-up
- **THEN** the triage's audit record has already been written to the log
