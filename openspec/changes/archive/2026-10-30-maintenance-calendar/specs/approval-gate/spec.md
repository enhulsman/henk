## MODIFIED Requirements

### Requirement: Mutating tools declare a turn scope, enforced per session
Every mutating tool SHALL declare the turn types in which it may execute (`owner`, `event`), defaulting to owner-only; the declaration lives in code alongside the tool, like its tier. The agent core SHALL supply the gate the current turn's context (turn type, announceability, whether the session is tainted, and the taint's source when a tool raised it), scoped strictly to the turn. A session becomes tainted when it processes an event turn, or when a taint-source tool is authorized in one of its turns (requirement *Tools may declare themselves taint sources*), and SHALL remain tainted for its lifetime. An invocation of a tool whose scope excludes event turns SHALL be denied — without any channel send, fail closed, outcome `out-of-scope` in its receipt — during any event turn AND during any turn of a tainted session, including the remainder of the turn in which a taint source raised the taint. The denial's tool result SHALL name the reason and the remedy: that the turn is an event turn, that the session is handling an incident, or that a tool earlier in the session returned content from outside the owner's control; and that the owner-command path (`/remember`, `/capture`, `/remind`) or a fresh session (`/new`) is the way to persist something. Every mutating tool registered to date — `store_memory`, `capture`, `remind`, `cancel_reminder`, `calendar_create_event` — is owner-turn-only. Owner commands are not model-initiated tool calls and are outside this requirement's scope.

#### Scenario: Mutating tool denied during an event turn
- **WHEN** the agent invokes `store_memory` (or `capture`) during an event-triage turn
- **THEN** the invocation is denied with outcome `out-of-scope`, no channel message is sent, and the store is unchanged

#### Scenario: Tainted session denies mutations even on owner turns
- **WHEN** the owner follows up on a triage message in the session the event turn started, and the agent then invokes `store_memory`
- **THEN** the invocation is denied with outcome `out-of-scope`, the store is unchanged, and the tool result names the incident taint and the `/remember` / `/new` remedy

#### Scenario: Untainted owner session executes normally
- **WHEN** the agent invokes `store_memory` or `capture` in an owner session no event turn has touched and no taint source has been called in
- **THEN** the tool executes

#### Scenario: Gate state does not outlive the turn
- **WHEN** a non-announceable event turn completes (including by error) and the owner then requests a per-instance action in a fresh owner session
- **THEN** a normal approval prompt is sent


## ADDED Requirements

### Requirement: Calendar tools carry a declared tier and scope
`calendar_create_event` SHALL be registered as mutating, authorization tier **per-instance**, turn scope **owner-only**: unlike the standing writes into Henk-local stores, it changes a shared external calendar that another agent also reads, so every write is shown to the owner first. It SHALL therefore send an approval prompt in the existing format, rendering `title`, `start` and `end` inside the argument delimiters, and SHALL execute only on an approval keyword; it SHALL be denied with outcome `out-of-scope`, without a prompt, during any event turn and during any turn of a tainted session. `calendar_upcoming` SHALL be registered read-only and SHALL bypass the gate. The standing-tier kill switch has no effect on either tool.

#### Scenario: Approval prompt shows the resolved event
- **WHEN** the agent invokes `calendar_create_event` with `title` `Homelab: patch round`, `start` `Sat 2026-11-07 20:00`, `end` `Sat 2026-11-07 22:00` in an untainted owner session
- **THEN** the owner receives a prompt containing `tool: \`calendar_create_event\``, `title: <<<Homelab: patch round>>>`, `start: <<<Sat 2026-11-07 20:00>>>` and `end: <<<Sat 2026-11-07 22:00>>>`, and no insert is made before an approval keyword

#### Scenario: Approval receipted as per-instance
- **WHEN** the owner replies `yes` to that prompt
- **THEN** the authorization record carries tool `calendar_create_event`, tier `per-instance` and outcome `approved`

#### Scenario: Tainted session cannot write the calendar
- **WHEN** the owner follows up on a triage message in the session the event turn started and the agent invokes `calendar_create_event`
- **THEN** the invocation is denied with outcome `out-of-scope`, no prompt is sent, and no insert is made

#### Scenario: Reading needs no approval
- **WHEN** the agent invokes `calendar_upcoming`
- **THEN** it executes without an approval prompt and without an authorization record
