# audit-log Specification (delta)

## MODIFIED Requirements

### Requirement: Schema is versioned
Every audit record SHALL carry a `schema_version` field. Any structural change to any record type SHALL increment the version and commit a corresponding JSON Schema document; every prior version's document SHALL remain committed so historical records validate against the version they declare. The current version is 4. Version 4 adds: the `reminder` record type (one per lifecycle transition) and the `scheduler` value for `initiated_by`. Version 4's `reminder` record SHALL define the **complete** transition enumeration for the capability, including the transitions only the reminder-delivery half writes, so that shipping delivery requires no further version increment — a schema document is a validation contract, not an inventory of what the current build emits. Version 3 added: the `authorization` record type (mutation receipts, including owner-command entries with `initiated_by`), the authorization-entry shape in session records' `approvals`, the `executed` flag on `tool_calls` entries, and the session record's `memory_hash` field.

#### Scenario: Version present
- **WHEN** any audit record is inspected
- **THEN** it contains a `schema_version` field identifying its schema

#### Scenario: New records declare the new version
- **WHEN** a record is written after this change
- **THEN** its `schema_version` identifies version 4, and it validates against that version's published schema document

#### Scenario: Old records remain valid
- **WHEN** a record written under a previous schema version is validated against that version's committed schema document
- **THEN** validation passes

#### Scenario: Delivery transitions validate before delivery exists
- **WHEN** a `reminder` record carrying a delivery-half transition is validated against version 4's document
- **THEN** validation passes

## ADDED Requirements

### Requirement: Reminder lifecycle records are durable at each transition
Every reminder lifecycle transition SHALL be appended to the audit log as its own `reminder` record at the moment of the transition — without waiting for any turn or session, and without depending on graceful shutdown. The record's transition SHALL be one of `scheduled`, `cancelled`, `reinstated`, `delivered`, `delivered-late`, `missed`, or `abandoned`; this is the name of a transition, not a stored row status (a reinstated reminder's row status is `pending`). Each record SHALL carry the reminder's id, its due time, the transition, `initiated_by` (`model` for a tool call, `owner-command` for a command, `scheduler` for every delivery, missed, and abandoned transition), and a timestamp.

A `reminder` record SHALL NOT contain the reminder's text: the store holds the content, the audit log holds the evidence, and the log is read and shared in contexts where owner-personal free text does not belong. A scheduling, cancellation, or reinstatement attempt rejected by validation, by a cap, or by an unknown id SHALL NOT write a record — receipts record state changes, and none occurred.

The lifecycle record SHALL be appended **after** the store transaction that performs the transition commits, so the log never claims a transition the store did not make; a crash between the two costs a receipt for a real transition, which is the preferable direction. This ordering SHALL NOT change the existing decision-time ordering of `authorization` receipts, which are written when the gate decides and therefore precede execution.

#### Scenario: Scheduling receipted at transition time
- **WHEN** a reminder is scheduled and the process is killed non-gracefully (SIGKILL) immediately afterwards
- **THEN** a `reminder` record with transition `scheduled`, the reminder's id, its due time, and the initiator is already present in the log after restart

#### Scenario: Cancellation receipted with its initiator
- **WHEN** the agent cancels a pending reminder with the tool, and the owner cancels another with the command
- **THEN** the log carries a `cancelled` record with `initiated_by: "model"` for the first and one with `initiated_by: "owner-command"` for the second

#### Scenario: Reinstatement is its own record
- **WHEN** the owner reinstates a cancelled reminder
- **THEN** a `reminder` record with transition `reinstated` and `initiated_by: "owner-command"` exists

#### Scenario: Records carry no reminder text
- **WHEN** any `reminder` record is inspected
- **THEN** it contains the reminder's id, due time, transition, initiator, and timestamp, and does not contain the reminder's text

#### Scenario: Rejected attempts write nothing
- **WHEN** a scheduling attempt is rejected because the time is in the past, or a cancellation names an unknown id
- **THEN** no `reminder` record is written for it

### Requirement: A mutating reminder tool call writes two records for two questions
A `remind` or `cancel_reminder` tool call SHALL produce **both** an `authorization` record — answering whether the agent was permitted to act, written when the gate decides — **and** a `reminder` record answering what changed, written at the transition. The two SHALL NOT be collapsed into one record: they answer different questions, are written at different moments, and one existing without the other is itself evidence (an authorization with no transition means the tool was allowed and then failed).

#### Scenario: Both records exist for one tool call
- **WHEN** the agent successfully invokes `remind`
- **THEN** the log carries an `authorization` record for the tool with tier `standing` and outcome `authorized`, and a separate `reminder` record with transition `scheduled`

#### Scenario: An authorized call that failed leaves the asymmetry visible
- **WHEN** the agent invokes `remind` and the store write then fails
- **THEN** the log carries the `authorization` record and no `reminder` record
