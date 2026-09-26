## MODIFIED Requirements

### Requirement: Schema is versioned
Every audit record SHALL carry a `schema_version` field. Any structural change to any
record type SHALL increment the version and commit a corresponding JSON Schema document.
Every prior version's document SHALL remain committed, so historical records validate
against the version they declare. The current version is 5.

Version 5 adds four optional, nullable fields to the session record: `profile` (`chat` or
`event`), `effort`, `recording_id`, and `prior_handoff_ids`. It documents the session
`outcome` values `completed`, `error`, `refused` and `no-reply`. It permits a non-null
`memory_hash` on event-triage records. Version 5 SHALL NOT rename, retype, or change the
meaning of any version-4 field. A reader of version-4 records therefore reads version-5
records correctly: cadence rehydration reconstructs the same state from a log mixing both
versions as from its version-4 projection.

Version 4 added the `reminder` record type (one per lifecycle transition) and the
`scheduler` value for `initiated_by`. Its `reminder` record defines the **complete**
transition enumeration for the capability, including the transitions only the
reminder-delivery half writes, so that shipping delivery required no further version
increment. A schema document is a validation contract, not an inventory of what the
current build emits.

Version 3 added:
- the `authorization` record type (mutation receipts, including owner-command entries
  with `initiated_by`);
- the authorization-entry shape in session records' `approvals`;
- the `executed` flag on `tool_calls` entries;
- the session record's `memory_hash` field.

#### Scenario: Version present
- **WHEN** any audit record is inspected
- **THEN** it contains a `schema_version` field identifying its schema

#### Scenario: New records declare the new version
- **WHEN** a record is written after this change
- **THEN** its `schema_version` identifies version 5, and it validates against that version's published schema document

#### Scenario: Old records remain valid
- **WHEN** a record written under a previous schema version is validated against that version's committed schema document
- **THEN** validation passes

#### Scenario: Delivery transitions validate before delivery exists
- **WHEN** a `reminder` record carrying a delivery-half transition is validated against version 4's document
- **THEN** validation passes

#### Scenario: A mixed-version log rehydrates identically
- **WHEN** cadence state is rehydrated from a log containing version-4 and version-5 event-triage records
- **THEN** the cooldown, recurrence and cap state equal the state rehydrated from the same log with every version-5-only field removed

## ADDED Requirements

### Requirement: Session records name their profile and event-triage records link their evidence
Every session record SHALL carry the `profile` and `effort` of the session factory that
created its session. The value is `event` for an event triage and for any owner
continuation inside a triage session, and `chat` otherwise.

Every event-triage session record SHALL also carry:
- the `recording_id` of that triage's recording (triage-replay spec), or null when no
  recording was written;
- the `prior_handoff_ids` of the retained handoffs its digest showed, as an empty list
  when none were shown.

Records that are not event triages, owner continuations inside a triage session included,
SHALL carry a null `recording_id` and null `prior_handoff_ids`. These fields are
references, not content. No recording content, no handoff text, and no tool result text
SHALL enter an audit record through them.

#### Scenario: A triage record links its recording and history
- **WHEN** an event triage that showed two prior handoffs completes and its recording is written
- **THEN** its audit record carries profile `event`, the effort used, the recording's id, and the two handoffs' ids

#### Scenario: A failed recording leaves a null link, not a false one
- **WHEN** an event triage completes but its recording could not be written
- **THEN** its audit record carries `recording_id: null`

#### Scenario: Non-triage records carry null evidence links
- **WHEN** a plain owner session's record and an owner continuation's record inside a triage session are inspected
- **THEN** both carry `recording_id: null` and `prior_handoff_ids: null`, the first with profile `chat` and the second with profile `event`

#### Scenario: References carry no content
- **WHEN** any event-triage record is inspected
- **THEN** it contains no substring of the recording's composed content, of any retained handoff document, or of any tool result other than the handoff message id already captured
