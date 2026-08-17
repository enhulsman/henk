# incident-triage Specification (delta)

## MODIFIED Requirements

### Requirement: Triage stays inside the read-only toolset
Event-triggered sessions SHALL have exactly the same registered toolset and structural boundaries as owner-triggered sessions. A mutating tool executes during a triage only if its declared turn scope includes event turns (approval-gate spec, "Mutating tools declare a turn scope, enforced per session"); no tool in this change declares event scope, so triage tool *executions* remain read-only or notify-class. A mutating invocation attempted during an event turn or in a tainted session is denied by the gate's turn-scope enforcement, silently and fail-closed, with an `out-of-scope` receipt.

#### Scenario: Triage tool calls audited
- **WHEN** a triage session completes and its audit record is inspected
- **THEN** every tool call that executed is a registered read-only or notify-class tool

#### Scenario: Mutating attempt during triage denied with a receipt
- **WHEN** the agent attempts a mutating tool during an event-triage turn
- **THEN** the invocation is denied without any channel message, the audit record shows only read-only/notify executions, and an `out-of-scope` authorization record exists for the attempt

### Requirement: Cadence is condition-triggered with a hard cap on announcements
Unprompted Signal messages SHALL be sent only for announceable incidents — never on a timer, and no scheduled digest or "all is well" message SHALL exist. Announceable incidents SHALL be limited by a configured hard cap per 24 hours; triageable incidents beyond the cap are suppressed from Signal only (their triage session, audit record, and handoff still occur), and the next announceable message SHALL note how many incidents were suppressed. Mutating invocations are the one exception to "Signal only": during a suppressed triage they fail closed silently per the approval-gate spec — a suppressed incident can never place an approval prompt (a context-free owner interruption) on the channel. The cap bounds unprompted-message volume, not token spend — token spend is bounded upstream by the curated source list, debounce, and cooldown. **The cadence cap window SHALL survive a process restart**: on startup the count of announceable incidents within the current cap window SHALL be reconstructed from the persisted audit log, so a restart does not reset the cap and allow the owner's cadence constraint to be exceeded.

#### Scenario: Quiet homelab means silence
- **WHEN** no triageable event occurs for a week
- **THEN** Henk sends zero unprompted messages

#### Scenario: Suppressed count surfaces later
- **WHEN** incidents were cap-suppressed and a new announceable incident occurs after the cap window allows
- **THEN** its Signal message mentions how many incidents were suppressed in the interim

#### Scenario: Cap holds across a restart
- **WHEN** the daily cap has already been reached, the process restarts, and a new triageable event arrives while still inside the cap window
- **THEN** the incident is triaged and handed off but no Signal message is sent, exactly as before the restart

#### Scenario: Suppressed triage cannot prompt
- **WHEN** the agent attempts a per-instance mutating tool during a cap-suppressed triage
- **THEN** no approval prompt or any other Signal message is sent, and the attempt is recorded with a fail-closed outcome
