## MODIFIED Requirements

### Requirement: Schema is versioned
Every audit record SHALL carry a `schema_version` field. Any structural change to any
record type SHALL increment the version and commit a corresponding JSON Schema document.
Every prior version's document SHALL remain committed, so historical records validate
against the version they declare. The current version is 6.

Version 6 adds the `action` record type (one per runbook executor result, one per
verification, and one per dropped message's request id, runbook-actions), the optional nullable `request_id` and `target` fields on
`authorization` records and on session records' `approvals` entries, the optional nullable
`result` field on those entries, the authorization outcomes `rate-limited` and
`unregistered`, and the `application` value for `initiated_by` (verifications only). Version 6 SHALL NOT rename, retype, or change the meaning of any version-5
field. A reader of version-5 records therefore reads version-6 records correctly. Cadence
rehydration reconstructs the same state from a log mixing both versions as from its
version-5 projection, with one deliberate exception: a triage that carries at least one
runbook request id, all verified and its message dropped (runbook-actions), arms no
cooldown and is skipped by cap reconstruction; it still counts for recurrence. Only an
explicit `phase` `drop` record marks a drop.

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

#### Scenario: A mixed v5/v6 log rehydrates identically
- **WHEN** cadence state is rehydrated from a log containing version-5 records and version-6 records including `action` records, none of which marks a triage as dropped
- **THEN** the cooldown, recurrence and cap state equal the state rehydrated from the same log with every `action` record and every version-6-only field removed

#### Scenario: A dropped triage is the one rehydration difference
- **WHEN** the log also holds a triage record whose runbook `request_id`s have `action` records with `phase` `drop`, and another whose restart was verified but which was sent
- **THEN** the first arms no cooldown, is skipped by cap reconstruction, and still counts for recurrence; the second rebuilds exactly as in the version-5 projection; and every other part of the rebuilt state equals the version-5 projection's

### Requirement: One append-only record per triage or owner session
The application layer (not the model) SHALL append audit records to a JSONL log on a dedicated named volume. An **event-triggered triage** SHALL produce exactly one record, written at the completion of the triage turn (not deferred to session close), so each incident is recorded promptly and independently. An **owner-initiated session** SHALL produce one record when the session ends (reset or idle expiry). Each record SHALL capture: the trigger (owner message or event, with event details), the tool calls made — each entry carrying an `executed` flag that is true when the invocation was permitted to proceed, derived by correlating the invocation with the gate's mutation receipts (never inferred from tool-result text; read-only and notify-only calls are `executed: true` by construction since they bypass the gate) — the diagnosis and confidence where the session produced triage output, whether a handoff was published (with its message id), the triage-arc compliance flag (`triage_arc_complete`, event-triggered records only), every mutating-tool authorization decision as an `approvals` entry (tool name, authorization tier, and outcome: `authorized`, `approved`, `denied`, `cancelled`, `timeout`, `suppressed`, `out-of-scope`, `rejected-busy`, `rate-limited`, or `unregistered`; for a runbook action also the `target`, the `request_id` when a request was published, and the executor `result` as the session last saw it), the `memory_hash` of the recall block the session received (null when none was injected), the outcome, and model plus token usage. A session in which a mutating tool was invoked SHALL NOT produce a record whose `approvals` list is empty. Existing records SHALL never be modified or deleted by the application.

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

#### Scenario: Runbook action in the session record
- **WHEN** a session in which `restart_container` executed for `wordle` with result `ok` produces its record
- **THEN** its `approvals` entry carries the tool name, tier `standing`, outcome `authorized`, target `wordle`, the request id, and result `ok`

### Requirement: Mutation receipts are durable at decision time
Every mutating-tool authorization decision SHALL be appended to the audit log as its own `authorization` record at the time of the decision — without waiting for turn or session completion, and without depending on graceful session close or graceful shutdown. Model-initiated records carry `initiated_by: "model"`, the tool name, its authorization tier, the outcome, the one-time reference, the turn type, and a timestamp; a runbook action's record additionally carries its `target` and, when the decision permits publishing, the `request_id` the request will carry, so the decision and the host result join on it. Every mutating owner command (`/remember`, `/forget`, `/capture`, `/inbox done <id>`) that changes state SHALL likewise write an `authorization` record at execution time with `initiated_by: "owner-command"`, `tier: null`, `turn_type: "command"`, outcome `authorized`, naming the command and a bounded summary of its effect. Read-only commands (`/memories`, `/inbox`, `/inbox all`) and mutating commands that changed nothing (an unmatched `/forget`, an unknown `/inbox done` id) SHALL NOT write one — receipts record mutations, and none occurred.

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

#### Scenario: Runbook receipt carries the request id before the request leaves
- **WHEN** a standing runbook invocation is authorized and the process is killed before the request is published
- **THEN** its `authorization` record exists with the target and a `request_id`, and no `action` record exists for that id

## ADDED Requirements

### Requirement: Runbook action results are durable at result time
Every runbook invocation that the gate permitted SHALL produce an `action` record when its outcome becomes known: on a valid executor result, on timeout, on a publish failure (`not-sent`), and on a late result after a timeout. Each verification of a standing fix SHALL produce another `action` record. Each record SHALL carry `request_id`, `tool`, `target`, `phase` (`result`, `verification`, or `drop`), `result` (`ok`, `failed`, `refused`, `expired`, `timeout`, `not-sent`, or null for a verification or drop), `reason`, `exit_code`, `duration_ms`, `verified` (`resolved`, `not-resolved`, `relapsed`, or null for a result or drop), `turn_type`, `initiated_by` (`model` for results, `application` for verifications), and a timestamp. It SHALL be appended without waiting for turn or session completion. An `action` record SHALL NOT contain executor output, event text, or any free text. A permitted invocation with an `authorization` record and no `action` record is itself evidence: the process stopped between decision and outcome.

#### Scenario: Result receipted at result time
- **WHEN** a runbook request returns `ok` and the process is killed non-gracefully immediately afterwards
- **THEN** an `action` record with `phase` `result`, the request id, the target, and result `ok` is already present in the log after restart

#### Scenario: Timeout receipted
- **WHEN** a runbook invocation times out
- **THEN** an `action` record with result `timeout` and the request id exists

#### Scenario: Records carry no output text
- **WHEN** any `action` record is inspected
- **THEN** every string field holds an enumerated value, an identifier, or a target name, and none holds job output or event text
