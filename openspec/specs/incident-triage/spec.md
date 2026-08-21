# incident-triage Specification

## Purpose
Defines what happens when the homelab breaks: every triageable event gets a full investigation
and a durable handoff, the owner's attention is spent only within the cadence contract (hard cap,
suppression to the record — never to the inbox), and triage runs with read-only hands unless a
verb's declared scope says otherwise.
## Requirements
### Requirement: Every triageable event becomes a triage session
An event that survives debounce and cooldown is a **triageable event**. Every triageable event SHALL start an agent triage session — including evidence gathering via registered read-only tools and handoff publication — regardless of the cadence cap. A triageable incident that is also within the daily cadence cap is an **announceable incident** and SHALL additionally deliver a proactive Signal message to the owner; cap-overflow incidents run their full triage session with Signal delivery suppressed. Recurrence detection (whether an identity was triaged within the recurrence window, and the prior handoff reference used for recurrence framing) SHALL be reconstructed from the persisted audit log on startup, so recurrence framing survives a restart.

#### Scenario: Curated alert triaged end to end
- **WHEN** a `HealthEtl*` event arrives, survives debounce and cooldown, and the cap is not reached
- **THEN** a triage session runs and the owner receives an unprompted Signal message naming the alert with Henk's initial assessment

#### Scenario: Cap-exceeded incident still triaged and handed off
- **WHEN** the daily cap is already reached and a triageable event arrives
- **THEN** a triage session still runs, its handoff document is published, its audit record exists, and no Signal message is sent

#### Scenario: Recurrence of a recently triaged incident
- **WHEN** a triageable event's alert identity was already triaged within the configured recurrence window
- **THEN** the triage session is framed as a recurrence: the resulting message notes it is a recurrence and references the earlier handoff instead of re-running full evidence gathering

#### Scenario: Recurrence framing survives a restart
- **WHEN** an identity was triaged, the process restarts, and the same identity re-fires within the recurrence window (but past cooldown)
- **THEN** the new triage is framed as a recurrence and references the prior handoff reconstructed from the audit log

### Requirement: Every incident message ends with the triage arc
Every unprompted incident message SHALL end with (a) a diagnosis with an explicit confidence level, (b) a suggested fix, and (c) a pickup path telling the owner where to resume work (referencing the published handoff). AI labeling per the inherited posture applies. Arc compliance SHALL be checked by the application layer after each triage turn and recorded in the audit record (`triage_arc_complete`); a missing component SHALL NOT block delivery.

#### Scenario: Triage arc present
- **WHEN** any unprompted incident message is delivered
- **THEN** it contains a diagnosis with confidence, a suggested fix, and a pickup path

#### Scenario: Arc component missing
- **WHEN** a triage turn produces a message lacking one of the three arc components
- **THEN** the message is still delivered and the session's audit record carries `triage_arc_complete: false`

### Requirement: Triage stays inside the read-only toolset
Event-triggered sessions SHALL have exactly the same registered toolset and structural boundaries as owner-triggered sessions. A mutating tool executes during a triage only if its declared turn scope includes event turns (approval-gate spec, "Mutating tools declare a turn scope, enforced per session"); no tool in this change declares event scope, so triage tool *executions* remain read-only or notify-class. A mutating invocation attempted during an event turn or in a tainted session is denied by the gate's turn-scope enforcement, silently and fail-closed, with an `out-of-scope` receipt.

#### Scenario: Triage tool calls audited
- **WHEN** a triage session completes and its audit record is inspected
- **THEN** every tool call that executed is a registered read-only or notify-class tool

#### Scenario: Mutating attempt during triage denied with a receipt
- **WHEN** the agent attempts a mutating tool during an event-triage turn
- **THEN** the invocation is denied without any channel message, the audit record shows only read-only/notify executions, and an `out-of-scope` authorization record exists for the attempt

### Requirement: Cadence is condition-triggered with a hard cap on announcements
Unprompted Signal messages SHALL be sent only for announceable incidents — never on a timer, and no system-scheduled digest, heartbeat, or "all is well" message SHALL exist. **Owner-scheduled reminder delivery is the one and only exception, and it is not a timer in this sense**: a reminder message exists because the owner asked for that message, at that time, in their own words (reminders spec), so it is owner-initiated content whose delivery moment happens to be deferred. Reminder deliveries and their catch-up summaries SHALL NOT consume the announceable-incident cap, SHALL NOT be generated by Henk on his own initiative, and are bounded instead by the reminders capability's own pending cap. The unprompted-message classes are therefore exactly two — announceable incidents and owner-scheduled reminder deliveries — and nothing else. Announceable incidents SHALL be limited by a configured hard cap per 24 hours; triageable incidents beyond the cap are suppressed from Signal only (their triage session, audit record, and handoff still occur), and the next announceable message SHALL note how many incidents were suppressed. Mutating invocations are the one exception to "Signal only": during a suppressed triage they fail closed silently per the approval-gate spec — a suppressed incident can never place an approval prompt (a context-free owner interruption) on the channel. The cap bounds unprompted-message volume, not token spend — token spend is bounded upstream by the curated source list, debounce, and cooldown. **The cadence cap window SHALL survive a process restart**: on startup the count of announceable incidents within the current cap window SHALL be reconstructed from the persisted audit log, so a restart does not reset the cap and allow the owner's cadence constraint to be exceeded.

#### Scenario: Quiet homelab means silence
- **WHEN** no triageable event occurs for a week and no reminder is scheduled
- **THEN** Henk sends zero unprompted messages

#### Scenario: No system-scheduled message exists
- **WHEN** the process runs for a week with reminders enabled, no reminder scheduled, and no triageable event
- **THEN** no digest, heartbeat, status, or "all is well" message is sent — the scheduler delivers only what the owner scheduled

#### Scenario: Reminder delivery does not consume the incident cap
- **WHEN** reminders are delivered on a day when the announceable-incident cap has not been reached
- **THEN** the number of incidents that may still be announced that day is unchanged

#### Scenario: Suppressed count surfaces later
- **WHEN** incidents were cap-suppressed and a new announceable incident occurs after the cap window allows
- **THEN** its Signal message mentions how many incidents were suppressed in the interim

#### Scenario: Cap holds across a restart
- **WHEN** the daily cap has already been reached, the process restarts, and a new triageable event arrives while still inside the cap window
- **THEN** the incident is triaged and handed off but no Signal message is sent, exactly as before the restart

#### Scenario: Suppressed triage cannot prompt
- **WHEN** the agent attempts a per-instance mutating tool during a cap-suppressed triage
- **THEN** no approval prompt or any other Signal message is sent, and the attempt is recorded with a fail-closed outcome

### Requirement: Owner replies interrogate the triage session
An owner reply following a triage message SHALL continue the same agent session, so follow-up questions resolve against the incident context under the existing continuity, `/new`, and idle-expiry rules.

#### Scenario: Follow-up question about the incident
- **WHEN** the owner replies "what does the backup log say?" shortly after a triage message
- **THEN** the reply runs in the triage session and answers with that incident's context

