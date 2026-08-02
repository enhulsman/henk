# incident-triage (delta)

## MODIFIED Requirements

### Requirement: Cadence is condition-triggered with a hard cap on announcements
Unprompted Signal messages SHALL be sent only for announceable incidents — never on a timer, and no scheduled digest or "all is well" message SHALL exist. Announceable incidents SHALL be limited by a configured hard cap per 24 hours; triageable incidents beyond the cap are suppressed from Signal only (their triage session, audit record, and handoff still occur), and the next announceable message SHALL note how many incidents were suppressed. The cap bounds unprompted-message volume, not token spend — token spend is bounded upstream by the curated source list, debounce, and cooldown. **The cadence cap window SHALL survive a process restart**: on startup the count of announceable incidents within the current cap window SHALL be reconstructed from the persisted audit log, so a restart does not reset the cap and allow the owner's cadence constraint to be exceeded.

#### Scenario: Quiet homelab means silence
- **WHEN** no triageable event occurs for a week
- **THEN** Henk sends zero unprompted messages

#### Scenario: Suppressed count surfaces later
- **WHEN** incidents were cap-suppressed and a new announceable incident occurs after the cap window allows
- **THEN** its Signal message mentions how many incidents were suppressed in the interim

#### Scenario: Cap holds across a restart
- **WHEN** the daily cap has already been reached, the process restarts, and a new triageable event arrives while still inside the cap window
- **THEN** the incident is triaged and handed off but no Signal message is sent, exactly as before the restart

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
