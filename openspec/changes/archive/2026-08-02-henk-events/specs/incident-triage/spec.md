# incident-triage

## ADDED Requirements

### Requirement: Every triageable event becomes a triage session
An event that survives debounce and cooldown is a **triageable event**. Every triageable event SHALL start an agent triage session — including evidence gathering via registered read-only tools and handoff publication — regardless of the cadence cap. A triageable incident that is also within the daily cadence cap is an **announceable incident** and SHALL additionally deliver a proactive Signal message to the owner; cap-overflow incidents run their full triage session with Signal delivery suppressed.

#### Scenario: Curated alert triaged end to end
- **WHEN** a `HealthEtl*` event arrives, survives debounce and cooldown, and the cap is not reached
- **THEN** a triage session runs and the owner receives an unprompted Signal message naming the alert with Henk's initial assessment

#### Scenario: Cap-exceeded incident still triaged and handed off
- **WHEN** the daily cap is already reached and a triageable event arrives
- **THEN** a triage session still runs, its handoff document is published, its audit record exists, and no Signal message is sent

#### Scenario: Recurrence of a recently triaged incident
- **WHEN** a triageable event's alert identity was already triaged within the configured recurrence window
- **THEN** the triage session is framed as a recurrence: the resulting message notes it is a recurrence and references the earlier handoff instead of re-running full evidence gathering

### Requirement: Every incident message ends with the triage arc
Every unprompted incident message SHALL end with (a) a diagnosis with an explicit confidence level, (b) a suggested fix, and (c) a pickup path telling the owner where to resume work (referencing the published handoff). AI labeling per the inherited posture applies. Arc compliance SHALL be checked by the application layer after each triage turn and recorded in the audit record (`triage_arc_complete`); a missing component SHALL NOT block delivery.

#### Scenario: Triage arc present
- **WHEN** any unprompted incident message is delivered
- **THEN** it contains a diagnosis with confidence, a suggested fix, and a pickup path

#### Scenario: Arc component missing
- **WHEN** a triage turn produces a message lacking one of the three arc components
- **THEN** the message is still delivered and the session's audit record carries `triage_arc_complete: false`

### Requirement: Triage stays inside the read-only toolset
Event-triggered sessions SHALL have exactly the same registered toolset and structural boundaries as owner-triggered sessions. No mutation capability SHALL be introduced by this change; the approval gate remains wired but unused.

#### Scenario: Triage tool calls audited
- **WHEN** a triage session completes and its audit record is inspected
- **THEN** every tool call is a registered read-only or notify-class tool

### Requirement: Cadence is condition-triggered with a hard cap on announcements
Unprompted Signal messages SHALL be sent only for announceable incidents — never on a timer, and no scheduled digest or "all is well" message SHALL exist. Announceable incidents SHALL be limited by a configured hard cap per 24 hours; triageable incidents beyond the cap are suppressed from Signal only (their triage session, audit record, and handoff still occur), and the next announceable message SHALL note how many incidents were suppressed. The cap bounds unprompted-message volume, not token spend — token spend is bounded upstream by the curated source list, debounce, and cooldown.

#### Scenario: Quiet homelab means silence
- **WHEN** no triageable event occurs for a week
- **THEN** Henk sends zero unprompted messages

#### Scenario: Suppressed count surfaces later
- **WHEN** incidents were cap-suppressed and a new announceable incident occurs after the cap window allows
- **THEN** its Signal message mentions how many incidents were suppressed in the interim

### Requirement: Owner replies interrogate the triage session
An owner reply following a triage message SHALL continue the same agent session, so follow-up questions resolve against the incident context under the existing continuity, `/new`, and idle-expiry rules.

#### Scenario: Follow-up question about the incident
- **WHEN** the owner replies "what does the backup log say?" shortly after a triage message
- **THEN** the reply runs in the triage session and answers with that incident's context
