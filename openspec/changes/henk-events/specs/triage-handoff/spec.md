# triage-handoff

## ADDED Requirements

### Requirement: Handoff doc published per triaged incident
For every triaged incident (including cap-suppressed ones), Henk SHALL publish a handoff document to the dedicated deny-all handoffs topic via a registered notify-class `publish_handoff` tool. The document SHALL contain: the trigger event(s), the evidence gathered (tool findings), the diagnosis with confidence, the suggested fix, and pickup instructions for resuming the investigation.

#### Scenario: Handoff available after triage
- **WHEN** a triage session completes
- **THEN** the handoffs topic holds a document with trigger, evidence, diagnosis + confidence, fix, and pickup instructions, and the Signal message's pickup path references it

#### Scenario: Suppressed incident still hands off
- **WHEN** an incident is suppressed by the cadence cap
- **THEN** its handoff document is still published

### Requirement: Handoff destination is fixed
The `publish_handoff` tool SHALL NOT accept a topic, server, or recipient parameter; it publishes only to the configured handoffs topic. Published content SHALL carry the `[AI]` label per the inherited posture.

#### Scenario: Alternate destination impossible
- **WHEN** the agent produces arguments attempting to target a different topic or server
- **THEN** the tool interface has no such parameter and the document can only go to the configured handoffs topic

### Requirement: henk-pickup retrieves handoffs on demand
A `henk-pickup` CLI in `~/.claude-config/bin` SHALL retrieve handoffs from the handoffs topic pull-based (ntfy poll endpoint, owner read credential), printing the latest handoff by default and supporting listing those within the retention window. It SHALL run without any daemon or new service, from any tailnet host.

#### Scenario: Latest handoff retrieved
- **WHEN** `henk-pickup` is run on any tailnet host after an incident was triaged
- **THEN** it prints the most recent handoff document

#### Scenario: Nothing to pick up
- **WHEN** `henk-pickup` is run and no handoff exists within the retention window
- **THEN** it states that honestly and exits cleanly
