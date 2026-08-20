# audit-log Specification (delta)

## MODIFIED Requirements

### Requirement: Schema is versioned
Every audit record SHALL carry a `schema_version` field. Any structural change to any record type SHALL increment the version and commit a corresponding JSON Schema document; every prior version's document SHALL remain committed so historical records validate against the version they declare. The current version is 4. Version 4 adds: the `reminder` record type (one per lifecycle transition) and the `scheduler` value for `initiated_by`. Version 3 added: the `authorization` record type (mutation receipts, including owner-command entries with `initiated_by`), the authorization-entry shape in session records' `approvals`, the `executed` flag on `tool_calls` entries, and the session record's `memory_hash` field.

#### Scenario: Version present
- **WHEN** any audit record is inspected
- **THEN** it contains a `schema_version` field identifying its schema

#### Scenario: New records declare the new version
- **WHEN** a record is written after this change
- **THEN** its `schema_version` identifies version 4, and it validates against that version's published schema document

#### Scenario: Old records remain valid
- **WHEN** a record written under a previous schema version is validated against that version's committed schema document
- **THEN** validation passes

## ADDED Requirements

### Requirement: Reminder lifecycle records are durable at each transition
Every reminder lifecycle transition SHALL be appended to the audit log as its own `reminder` record at the moment of the transition — without waiting for any turn or session, and without depending on graceful shutdown. The status SHALL be one of `scheduled`, `delivered`, `delivered-late`, `missed`, `cancelled`, or `abandoned`. Each record SHALL carry the reminder's id, its due time, the status, `initiated_by` (`model` for a `remind` tool call, `owner-command` for `/remind` and `/reminders cancel`, `scheduler` for every delivery, missed, and abandoned transition), and a timestamp. A `reminder` record SHALL NOT contain the reminder's text: the store holds the content, the audit log holds the evidence, and the log is read and shared in contexts where owner-personal free text does not belong. A scheduling attempt rejected by validation or a cap SHALL NOT write a record — receipts record state changes, and none occurred.

#### Scenario: Scheduling receipted at decision time
- **WHEN** a reminder is scheduled and the process is killed non-gracefully (SIGKILL) immediately afterwards
- **THEN** a `reminder` record with status `scheduled`, the reminder's id, its due time, and the scheduling initiator is already present in the log after restart

#### Scenario: Delivery receipted by the scheduler
- **WHEN** a reminder is delivered
- **THEN** a `reminder` record exists with status `delivered` (or `delivered-late`) and `initiated_by: "scheduler"`

#### Scenario: Cancellation receipted as an owner command
- **WHEN** the owner cancels a pending reminder
- **THEN** a `reminder` record exists with status `cancelled` and `initiated_by: "owner-command"`

#### Scenario: Records carry no reminder text
- **WHEN** any `reminder` record is inspected
- **THEN** it contains the reminder's id, due time, status, initiator, and timestamp, and does not contain the reminder's text

#### Scenario: Rejected schedule writes nothing
- **WHEN** a scheduling attempt is rejected because the time is in the past or the pending cap is reached
- **THEN** no `reminder` record is written for it

#### Scenario: Abandoned delivery is recorded
- **WHEN** delivery fails up to the attempt maximum and the reminder is abandoned
- **THEN** a `reminder` record with status `abandoned` exists
