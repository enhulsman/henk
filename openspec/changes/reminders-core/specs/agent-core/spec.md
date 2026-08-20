# agent-core Specification (delta)

## MODIFIED Requirements

### Requirement: Turns are typed and event turns carry triage framing
The serial queue SHALL carry typed turns distinguishing owner messages from event turns; event turns SHALL carry the event metadata (source, alert identity, payload, firing/resolved state, announceable flag). When processing an event turn, the agent core SHALL compose the turn content as a clearly delimited untrusted-data block containing the event payload, plus triage-mode framing (the triage-arc mandate, the recurrence note where applicable, and the instruction to publish a handoff). Owner turns SHALL NOT receive triage framing. **When reminders are enabled, every owner turn SHALL carry a one-line current-time header** stating the current time in the owner's configured timezone, so a relative time is resolved against the moment of the turn and not against the session's start; the header SHALL be composed per turn rather than per session, SHALL be delimited as data, and event turns SHALL NEVER carry it. The header SHALL be produced by the same renderer as every reminder due time (reminders spec), so the time the model reasons from and the time the owner is told read identically. The first owner turn of a session that has not yet received it SHALL be prefixed with the memory recall block per the memory-store spec; event turns SHALL never carry it. Event-turn output SHALL be routed through the channel adapter's proactive owner-directed send (suppressed for non-announceable incidents); owner-turn output keeps the reply path.

#### Scenario: Event turn framed for triage
- **WHEN** an event turn is processed
- **THEN** the text passed to the agent session contains the event payload inside a delimited untrusted-data block and the triage-mode framing, and no recall block and no time header

#### Scenario: Owner turn unaffected
- **WHEN** an owner message turn is processed
- **THEN** its content contains no triage framing and its output is delivered as a normal reply

#### Scenario: Every owner turn knows the time
- **WHEN** reminders are enabled and two owner turns run an hour apart in the same session
- **THEN** each turn's content carries a current-time header reflecting the time of that turn, rendered in the owner's configured timezone with its weekday and zone marker

#### Scenario: No header when reminders are disabled
- **WHEN** reminders are disabled and an owner turn is processed
- **THEN** its content carries no current-time header

#### Scenario: First owner turn carries recall
- **WHEN** the first owner turn of a session runs while memories exist
- **THEN** its content is prefixed with the recall block (composition details per the memory-store spec)

#### Scenario: Non-announceable event turn output suppressed
- **WHEN** an event turn for a cap-suppressed incident completes
- **THEN** no Signal message is sent for it

### Requirement: Owner commands are dispatched app-side
The agent core SHALL recognize the owner command set — `/new`, `/remember`, `/forget`, `/memories`, `/capture`, `/inbox`, `/inbox all`, `/inbox done <id>`, `/remind <when> <text>`, `/reminders`, `/reminders cancel <id>`, and `/reminders reinstate <id>` — at the start of owner-turn processing and handle each without starting an agent turn, replying immediately over the channel (command effects are specified in the memory-store, capture-inbox, and reminders capabilities; `/new` keeps its existing behavior). Owner text matching no recognized command SHALL be processed as a normal agent turn exactly as before. Commands arriving while an approval is pending SHALL be classified by the gate first, per the existing approval-gate rules: as unrelated messages they fail the pending action closed and are then handled as commands, not swallowed.

#### Scenario: Command needs no agent session
- **WHEN** the owner sends `/memories` while no agent session is active
- **THEN** the reply is sent without any agent session being created or model tokens spent

#### Scenario: Reminder command needs no agent session
- **WHEN** the owner sends `/remind +2h call the plumber`
- **THEN** the reminder is scheduled and confirmed without any agent session being created or model tokens spent

#### Scenario: Non-command text unaffected
- **WHEN** the owner sends "what's in my inbox?"
- **THEN** it runs as a normal agent turn

#### Scenario: Command during pending approval fails the action closed
- **WHEN** the owner sends `/inbox` while an approval prompt is pending
- **THEN** the pending action resolves as cancelled per the approval-gate rules, and the `/inbox` command is then handled normally

### Requirement: System prompt enumerates the full registered toolset
The session system prompt SHALL enumerate all registered tools, including `publish_handoff` and — when reminders are enabled — `remind`, `cancel_reminder` and `reminders_read`. The enumeration SHALL match the registry: a tool that is not registered SHALL NOT be advertised, and no registered tool SHALL be omitted. The prompt SHALL state that the agent can schedule and cancel a reminder but cannot reinstate a cancelled one, naming the owner command that can. Triage-mode instructions SHALL NOT live in the base system prompt — they arrive with event turns — so owner conversations are unaffected by triage machinery.

#### Scenario: System prompt lists publish_handoff
- **WHEN** the session system prompt is inspected
- **THEN** `publish_handoff` appears in the tool enumeration and no triage-arc instructions are present

#### Scenario: System prompt matches the registry
- **WHEN** reminders are enabled and the system prompt is inspected
- **THEN** `remind`, `cancel_reminder` and `reminders_read` appear in the tool enumeration, the enumerated names equal the registered names, and the prompt states that reinstating a cancelled reminder is an owner command

#### Scenario: Disabled reminders are not advertised
- **WHEN** reminders are disabled and the system prompt is inspected
- **THEN** no reminder tool appears in the enumeration
