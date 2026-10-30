## MODIFIED Requirements

### Requirement: Schema is versioned
Every audit record SHALL carry a `schema_version` field. Any structural change to any
record type SHALL increment the version and commit a corresponding JSON Schema document.
Every prior version's document SHALL remain committed, so historical records validate
against the version they declare. The current version is 6.

Version 6 adds three record types: `calendar` (one per calendar write), `hold` (one per
event held during a maintenance window) and `hold-release` (one per released window). It
SHALL NOT rename, retype, or change the meaning of any version-5 field or record type. A
reader of version-5 records therefore reads version-6 records correctly, and cadence
rehydration, which reads only event-triage session records, reconstructs the same state
from a log mixing both versions as from its version-5 projection.

Version 5 added four optional, nullable fields to the session record: `profile` (`chat` or
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
- **THEN** its `schema_version` identifies version 6, and it validates against that version's published schema document

#### Scenario: Old records remain valid
- **WHEN** a record written under a previous schema version is validated against that version's committed schema document
- **THEN** validation passes

#### Scenario: Delivery transitions validate before delivery exists
- **WHEN** a `reminder` record carrying a delivery-half transition is validated against version 4's document
- **THEN** validation passes

#### Scenario: A mixed-version log rehydrates identically
- **WHEN** cadence state is rehydrated from a log containing version-4 and version-5 event-triage records
- **THEN** the cooldown, recurrence and cap state equal the state rehydrated from the same log with every version-5-only field removed

#### Scenario: Hold records do not disturb rehydration
- **WHEN** cadence state is rehydrated from a log containing `hold` and `hold-release` records beside event-triage records
- **THEN** the state equals the state rehydrated from the same log with those records removed

## ADDED Requirements

### Requirement: A calendar write is receipted twice, without its title
A `calendar_create_event` call SHALL produce the `authorization` record the gate writes at decision time and, only after Google confirms the insert, a separate `calendar` record carrying `transition: "created"`, the Google event id, the start and end exactly as sent (RFC 3339 instants with offset), `initiated_by: "model"`, the authorization's one-time reference, and a timestamp. The `calendar` record SHALL NOT contain the event's title. A refused or failed insert SHALL NOT write a `calendar` record, so an approval with no matching `calendar` record is evidence of an approved write that did not happen.

#### Scenario: Created event receipted
- **WHEN** the owner approves an event and Google returns its id
- **THEN** the log carries an `authorization` record with outcome `approved` and a `calendar` record with that event id and the same reference, and the `calendar` record does not contain the title

#### Scenario: Failed insert leaves the asymmetry visible
- **WHEN** the owner approves an event and the insert returns 403
- **THEN** the log carries the `authorization` record and no `calendar` record

### Requirement: Holds and releases are recorded without payloads
Every held event SHALL produce a `hold` record, written when it is held, carrying the identity key, the event state, the event id, the window id, the window end known at hold time, the publish time, and a timestamp. Every released window SHALL produce exactly one `hold-release` record, written when the window is marked released (event-intake spec), carrying the window id, the number of distinct held identities, how many of those recovered, how many were forwarded, and a timestamp. A duplicate delivery of an already-held event SHALL NOT write a second `hold` record. Neither record SHALL contain the event's title, message, or raw payload, nor the window's title.

#### Scenario: Hold record carries no payload
- **WHEN** an event with message text M is held
- **THEN** its `hold` record contains its identity key and window id and does not contain M

#### Scenario: Release counts recorded
- **WHEN** a window holding 4 identities is released with 3 recovered
- **THEN** a `hold-release` record shows held 4, recovered 3, forwarded 1
