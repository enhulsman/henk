# audit-log Specification (delta)

## MODIFIED Requirements

### Requirement: One append-only record per triage or owner session
The application layer (not the model) SHALL append audit records to a JSONL log on a dedicated named volume. An **event-triggered triage** SHALL produce exactly one record, written at the completion of the triage turn (not deferred to session close), so each incident is recorded promptly and independently. An **owner-initiated session** SHALL produce one record when the session ends (reset or idle expiry). Each record SHALL capture: the trigger (owner message or event, with event details), the tool calls made — each entry carrying an `executed` flag that is true when the invocation was permitted to proceed, derived by correlating the invocation with the gate's mutation receipts (never inferred from tool-result text; read-only and notify-only calls are `executed: true` by construction since they bypass the gate) — the diagnosis and confidence where the session produced triage output, whether a handoff was published (with its message id), the triage-arc compliance flag (`triage_arc_complete`, event-triggered records only), every mutating-tool authorization decision as an `approvals` entry (tool name, authorization tier, and outcome: `authorized`, `approved`, `denied`, `cancelled`, `timeout`, `suppressed`, `out-of-scope`, or `rejected-busy`), the `memory_hash` of the recall block the session received (null when none was injected), the outcome, and model plus token usage. A session in which a mutating tool was invoked SHALL NOT produce a record whose `approvals` list is empty. Existing records SHALL never be modified or deleted by the application.

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

#### Scenario: Per-instance decision recorded
- **WHEN** a per-instance mutating invocation is approved or denied during a session
- **THEN** that session's audit record contains an `approvals` entry with the tool name, tier `per-instance`, and the actual outcome

#### Scenario: Standing authorization leaves a receipt
- **WHEN** a standing-tier tool executes during a session
- **THEN** that session's audit record contains an `approvals` entry with the tool name, tier `standing`, and outcome `authorized`

#### Scenario: Denied attempt distinguishable from execution
- **WHEN** a mutating invocation is denied and the session's record is inspected
- **THEN** any `tool_calls` entry for that invocation carries `executed: false`, while executed calls carry `executed: true`

#### Scenario: Session memory state recorded
- **WHEN** a session that received a recall block produces its audit record
- **THEN** the record's `memory_hash` equals the hash of the block as injected

### Requirement: Schema is versioned
Every audit record SHALL carry a `schema_version` field. Any structural change to any record type SHALL increment the version and commit a corresponding JSON Schema document; every prior version's document SHALL remain committed so historical records validate against the version they declare. The current version is 3. Version 3 adds: the `authorization` record type (mutation receipts, including owner-command entries with `initiated_by`), the authorization-entry shape in session records' `approvals`, the `executed` flag on `tool_calls` entries, and the session record's `memory_hash` field.

#### Scenario: Version present
- **WHEN** any audit record is inspected
- **THEN** it contains a `schema_version` field identifying its schema

#### Scenario: New records declare the new version
- **WHEN** a record is written after this change
- **THEN** its `schema_version` identifies version 3, and it validates against that version's published schema document

#### Scenario: Old records remain valid
- **WHEN** a record written under a previous schema version is validated against that version's committed schema document
- **THEN** validation passes

## ADDED Requirements

### Requirement: Mutation receipts are durable at decision time
Every mutating-tool authorization decision SHALL be appended to the audit log as its own `authorization` record at the time of the decision — without waiting for turn or session completion, and without depending on graceful session close or graceful shutdown. Model-initiated records carry `initiated_by: "model"`, the tool name, its authorization tier, the outcome, the one-time reference, the turn type, and a timestamp. Every mutating owner command (`/remember`, `/forget`, `/capture`, `/inbox done <id>`) that changes state SHALL likewise write an `authorization` record at execution time with `initiated_by: "owner-command"`, `tier: null`, `turn_type: "command"`, outcome `authorized`, naming the command and a bounded summary of its effect. Read-only commands (`/memories`, `/inbox`, `/inbox all`) and mutating commands that changed nothing (an unmatched `/forget`, an unknown `/inbox done` id) SHALL NOT write one — receipts record mutations, and none occurred.

#### Scenario: Standing receipt survives a hard kill
- **WHEN** a standing-tier tool executes and the process is killed non-gracefully (SIGKILL) before any session close
- **THEN** the invocation's `authorization` record is already present in the log after restart

#### Scenario: Per-instance decision durable at decision time
- **WHEN** a per-instance invocation is denied
- **THEN** its `authorization` record exists in the log at the time of the decision, before the turn completes

#### Scenario: Owner command receipted
- **WHEN** the owner runs `/capture buy bike lights`
- **THEN** an `authorization` record exists with `initiated_by: "owner-command"` naming the command

#### Scenario: Destructive command receipted with its effect
- **WHEN** the owner runs `/forget backup` and two memories are removed
- **THEN** the command's `authorization` record names the command and the removal count

#### Scenario: No-op command writes no receipt
- **WHEN** the owner runs `/inbox done 9999` and no item has that id
- **THEN** no `authorization` record is written for it

### Requirement: Audit availability does not depend on event intake
The audit log SHALL be constructed and writable whenever the application runs, regardless of whether event intake is enabled. Disabling event intake SHALL NOT disable mutation receipts, owner-session records, or memory/inbox durability.

#### Scenario: Receipts written with events disabled
- **WHEN** event intake is disabled and a standing-tier tool executes
- **THEN** its `authorization` record is written to the audit log

## REMOVED Requirements

### Requirement: Schema version reflects the record-semantics change
**Reason**: Superseded — schema-version history (current version, increment-on-change, prior versions remain readable) now lives in the modified "Schema is versioned" requirement, so the spec does not accumulate one version-bump requirement per change.
**Migration**: Its obligations continue unchanged under "Schema is versioned"; the v2 schema document remains committed and v2 records still validate against it.
