# approval-gate Specification (delta)

## MODIFIED Requirements

### Requirement: Mutating tools declare a turn scope, enforced per session
Every mutating tool SHALL declare the turn types in which it may execute (`owner`, `event`), defaulting to owner-only; the declaration lives in code alongside the tool, like its tier. The agent core SHALL supply the gate the current turn's context (turn type, announceability, and whether the session is tainted), scoped strictly to the turn. A session becomes tainted when it processes an event turn and SHALL remain tainted for its lifetime. An invocation of a tool whose scope excludes event turns SHALL be denied — without any channel send, fail closed, outcome `out-of-scope` in its receipt — during any event turn AND during any turn of a tainted session. The denial's tool result SHALL name the reason and the remedy: that the session is handling an incident (or the turn is an event turn), and that the owner-command path (`/remember`, `/capture`, `/remind`) or a fresh session (`/new`) is the way to persist something. Every mutating tool registered to date — `store_memory`, `capture`, `remind`, `cancel_reminder` — is owner-turn-only. Owner commands are not model-initiated tool calls and are outside this requirement's scope.

#### Scenario: Mutating tool denied during an event turn
- **WHEN** the agent invokes `store_memory` (or `capture`) during an event-triage turn
- **THEN** the invocation is denied with outcome `out-of-scope`, no channel message is sent, and the store is unchanged

#### Scenario: Tainted session denies mutations even on owner turns
- **WHEN** the owner follows up on a triage message in the session the event turn started, and the agent then invokes `store_memory`
- **THEN** the invocation is denied with outcome `out-of-scope`, the store is unchanged, and the tool result names the incident taint and the `/remember` / `/new` remedy

#### Scenario: Untainted owner session executes normally
- **WHEN** the agent invokes `store_memory` or `capture` in an owner session no event turn has touched
- **THEN** the tool executes

#### Scenario: Gate state does not outlive the turn
- **WHEN** a non-announceable event turn completes (including by error) and the owner then requests a per-instance action in a fresh owner session
- **THEN** a normal approval prompt is sent

## ADDED Requirements

### Requirement: Reminder tools carry a declared tier and scope
`remind` and `cancel_reminder` SHALL both be registered as mutating, authorization tier **standing**, turn scope **owner-only** — the same containment argument as `capture` and `store_memory`: a write into a Henk-local store whose only external effect is a message to the configured owner, receipted every time, and in the cancellation case reversible by an owner command with the row and its text retained. They SHALL therefore execute without an approval prompt in an untainted owner session, and SHALL be denied with outcome `out-of-scope` during any event turn and during any turn of a tainted session. `reminders_read` SHALL be registered read-only and SHALL bypass the gate. The standing-tier kill switch SHALL apply to both mutating reminder tools unchanged: with demotion enabled, scheduling or cancelling a reminder requires inline approval.

#### Scenario: Scheduling executes without a prompt
- **WHEN** the agent invokes `remind` in an untainted owner session
- **THEN** the reminder is stored, no approval prompt is sent, and the authorization is reported for the audit record with tier `standing` and outcome `authorized`

#### Scenario: Untrusted event input cannot schedule a message
- **WHEN** an event payload instructs Henk to set a reminder and the event turn is processed
- **THEN** the invocation is denied with outcome `out-of-scope`, no channel message is sent, and no reminder is stored

#### Scenario: Untrusted event input cannot cancel a reminder
- **WHEN** an event payload instructs Henk to cancel a reminder and the event turn is processed
- **THEN** the invocation is denied with outcome `out-of-scope` and no reminder changes status

#### Scenario: Tainted session cannot schedule
- **WHEN** the owner follows up on a triage message in the session the event turn started and the agent then invokes `remind`
- **THEN** the invocation is denied with outcome `out-of-scope` and the tool result names the incident taint and the `/remind` command as the remedy

#### Scenario: Kill-switch demotes both reminder tools
- **WHEN** the demotion flag is enabled and the agent invokes `remind` or `cancel_reminder`
- **THEN** an approval prompt is sent and the tool's effect occurs only on an approval keyword
