# audit-log Specification

## Purpose
TBD - created by archiving change henk-events. Update Purpose after archive.
## Requirements
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
A failed audit write SHALL be logged at error level and SHALL NOT prevent message handling, triage, or replies. The audit volume SHALL be included in rp5's backup allowlist so records survive host loss. An event-triage record SHALL be durable (flushed to the audit volume) before the pipeline processes the next event, and SHALL NOT depend on graceful session close or graceful process shutdown to be written.

#### Scenario: Audit volume unavailable
- **WHEN** the audit log cannot be written during a triage session
- **THEN** the triage message is still delivered and an error is logged

#### Scenario: Backup covers the audit volume
- **WHEN** the rp5 backup routine runs
- **THEN** the audit volume's contents are included in the backup output

#### Scenario: Triage record survives a hard kill
- **WHEN** an event is triaged and the process is then killed non-gracefully (SIGKILL) before any session close
- **THEN** the triage's audit record is already present in the log after restart

### Requirement: One append-only record per triage or owner session
The application layer (not the model) SHALL append audit records to a JSONL log on a dedicated named volume. An **event-triggered triage** SHALL produce exactly one record, written at the completion of the triage turn (not deferred to session close), so each incident is recorded promptly and independently. An **owner-initiated session** SHALL produce one record when the session ends (reset or idle expiry). Each record SHALL capture: the trigger (owner message or event, with event details), the tool calls made, the diagnosis and confidence where the session produced triage output, whether a handoff was published (with its message id), the triage-arc compliance flag (`triage_arc_complete`, event-triggered records only), any approval requests and their decisions, the outcome, and model plus token usage. Existing records SHALL never be modified or deleted by the application.

#### Scenario: Triage session recorded promptly
- **WHEN** an event-triggered triage turn completes
- **THEN** one audit record for it exists containing the trigger event, tool calls, diagnosis + confidence, handoff message id, outcome, and model/token usage — before the next event is processed

#### Scenario: Two incidents in one session are recorded separately
- **WHEN** two triageable incidents are handled while a single agent session remains open
- **THEN** two distinct audit records exist, one per incident, not one conflated record

#### Scenario: Owner conversation recorded
- **WHEN** an owner-initiated session ends (reset or idle expiry)
- **THEN** one audit record exists for it with trigger type owner-message

#### Scenario: Suppressed event recorded
- **WHEN** an event is suppressed by cooldown or the cadence cap
- **THEN** an audit record of the suppression exists

### Requirement: Usage accounts for cache-read tokens
The `usage` object in a session record SHALL include cache-read input tokens (`cache_read_input_tokens`) in addition to uncached `input_tokens` and `output_tokens`, so cost accounting reflects prompt caching. The `input_tokens` field retains its existing meaning (uncached input only); the cache-read field is additive.

#### Scenario: Cache-read tokens captured
- **WHEN** a session that benefited from prompt caching completes and its audit record is inspected
- **THEN** the `usage` object reports a non-negative cache-read input token count alongside the uncached input and output token counts

### Requirement: Schema version reflects the record-semantics change
The record-cardinality change (one record per event triage rather than one per event session) and the added usage field SHALL be reflected by an incremented `schema_version` and a corresponding published JSON Schema document committed to the repo. The prior schema version SHALL remain readable so historical records still validate against the version they declare.

#### Scenario: New records declare the new version
- **WHEN** a record is written after this change
- **THEN** its `schema_version` identifies the new schema, and it validates against that version's published schema document

#### Scenario: Old records remain valid
- **WHEN** a record written under the previous schema version is validated against that version's schema document
- **THEN** validation passes

