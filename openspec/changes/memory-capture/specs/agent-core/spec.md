# agent-core Specification (delta)

## MODIFIED Requirements

### Requirement: Turns are typed and event turns carry triage framing
The serial queue SHALL carry typed turns distinguishing owner messages from event turns; event turns SHALL carry the event metadata (source, alert identity, payload, firing/resolved state, announceable flag). When processing an event turn, the agent core SHALL compose the turn content as a clearly delimited untrusted-data block containing the event payload, plus triage-mode framing (the triage-arc mandate, the recurrence note where applicable, and the instruction to publish a handoff). Owner turns SHALL NOT receive triage framing. The first owner turn of a session that has not yet received it SHALL be prefixed with the memory recall block per the memory-store spec; event turns SHALL never carry it. Event-turn output SHALL be routed through the channel adapter's proactive owner-directed send (suppressed for non-announceable incidents); owner-turn output keeps the reply path.

#### Scenario: Event turn framed for triage
- **WHEN** an event turn is processed
- **THEN** the text passed to the agent session contains the event payload inside a delimited untrusted-data block and the triage-mode framing, and no recall block

#### Scenario: Owner turn unaffected
- **WHEN** an owner message turn is processed
- **THEN** its content contains no triage framing and its output is delivered as a normal reply

#### Scenario: First owner turn carries recall
- **WHEN** the first owner turn of a session runs while memories exist
- **THEN** its content is prefixed with the recall block (composition details per the memory-store spec)

#### Scenario: Non-announceable event turn output suppressed
- **WHEN** an event turn for a cap-suppressed incident completes
- **THEN** no Signal message is sent for it

## ADDED Requirements

### Requirement: Owner commands are dispatched app-side
The agent core SHALL recognize the owner command set — `/new`, `/remember`, `/forget`, `/memories`, `/capture`, `/inbox`, `/inbox all`, and `/inbox done <id>` — at the start of owner-turn processing and handle each without starting an agent turn, replying immediately over the channel (command effects are specified in the memory-store and capture-inbox capabilities; `/new` keeps its existing behavior). Owner text matching no recognized command SHALL be processed as a normal agent turn exactly as before. Commands arriving while an approval is pending SHALL be classified by the gate first, per the existing approval-gate rules: as unrelated messages they fail the pending action closed and are then handled as commands, not swallowed.

#### Scenario: Command needs no agent session
- **WHEN** the owner sends `/memories` while no agent session is active
- **THEN** the reply is sent without any agent session being created or model tokens spent

#### Scenario: Non-command text unaffected
- **WHEN** the owner sends "what's in my inbox?"
- **THEN** it runs as a normal agent turn

#### Scenario: Command during pending approval fails the action closed
- **WHEN** the owner sends `/inbox` while an approval prompt is pending
- **THEN** the pending action resolves as cancelled per the approval-gate rules, and the `/inbox` command is then handled normally
