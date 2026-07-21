# audit-log

## ADDED Requirements

### Requirement: One append-only record per agent session
The application layer (not the model) SHALL append exactly one JSONL record per agent session to an audit log on a dedicated named volume. Each record SHALL capture: the trigger (owner message or event, with event details), the tool calls made, the diagnosis and confidence where the session produced triage output, whether a handoff was published (with its message id), the triage-arc compliance flag (`triage_arc_complete`, event-triggered sessions only), any approval requests and their decisions, the session outcome, and model plus token usage. Existing records SHALL never be modified or deleted by the application.

#### Scenario: Triage session recorded
- **WHEN** an event-triggered triage session completes
- **THEN** one audit record exists containing the trigger event, tool calls, diagnosis + confidence, handoff message id, outcome, and model/token usage

#### Scenario: Owner conversation recorded
- **WHEN** an owner-initiated session ends (reset or idle expiry)
- **THEN** one audit record exists for it with trigger type owner-message

#### Scenario: Suppressed event recorded
- **WHEN** an event is suppressed by cooldown or the cadence cap
- **THEN** an audit record of the suppression exists

### Requirement: Schema is versioned
Every audit record SHALL carry a `schema_version` field. Any change to the record structure SHALL increment the version; readers SHALL be able to distinguish records across versions.

#### Scenario: Version present
- **WHEN** any audit record is inspected
- **THEN** it contains a `schema_version` field identifying its schema

### Requirement: Schema is a published artifact
The exact JSONL field names and types SHALL be defined in a versioned schema document committed to the repo (JSON Schema), and every record SHALL validate against the schema version it declares. The schema document is the transferable artifact — another project can validate its own records against it.

#### Scenario: Records validate against the published schema
- **WHEN** a written audit record is validated against the repo's schema document for its declared version
- **THEN** validation passes

### Requirement: Audit failures are loud but non-blocking
A failed audit write SHALL be logged at error level and SHALL NOT prevent message handling, triage, or replies. The audit volume SHALL be included in rp5's backup allowlist so records survive host loss.

#### Scenario: Audit volume unavailable
- **WHEN** the audit log cannot be written during a triage session
- **THEN** the triage message is still delivered and an error is logged

#### Scenario: Backup covers the audit volume
- **WHEN** the rp5 backup routine runs
- **THEN** the audit volume's contents are included in the backup output
