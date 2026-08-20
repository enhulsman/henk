# agent-core Specification (delta)

## MODIFIED Requirements

### Requirement: Turns are typed and event turns carry triage framing
The serial queue SHALL carry typed turns distinguishing owner messages from event turns; event turns SHALL carry the event metadata (source, alert identity, payload, firing/resolved state, announceable flag). When processing an event turn, the agent core SHALL compose the turn content as a clearly delimited untrusted-data block containing the event payload, plus triage-mode framing (the triage-arc mandate, the recurrence note where applicable, and the instruction to publish a handoff). Owner turns SHALL NOT receive triage framing. **Every owner turn SHALL carry a one-line current-time header** stating the current time in the owner's configured timezone, so relative times are resolved against now and not against the session's start; event turns SHALL NOT carry it. The first owner turn of a session that has not yet received it SHALL be prefixed with the memory recall block per the memory-store spec; event turns SHALL never carry it. **An owner turn SHALL additionally carry the delivered-reminder block when an unsurfaced delivery exists** (reminders spec), independently of whether the recall block was already given in that session; event turns SHALL never carry it, and it SHALL NOT taint the session. Event-turn output SHALL be routed through the channel adapter's proactive owner-directed send (suppressed for non-announceable incidents); owner-turn output keeps the reply path.

#### Scenario: Event turn framed for triage
- **WHEN** an event turn is processed
- **THEN** the text passed to the agent session contains the event payload inside a delimited untrusted-data block and the triage-mode framing, and no recall block, no time header, and no delivered-reminder block

#### Scenario: Owner turn unaffected
- **WHEN** an owner message turn is processed
- **THEN** its content contains no triage framing and its output is delivered as a normal reply

#### Scenario: Every owner turn knows the time
- **WHEN** two owner turns run an hour apart in the same session
- **THEN** each turn's content carries a current-time header reflecting the time of that turn

#### Scenario: First owner turn carries recall
- **WHEN** the first owner turn of a session runs while memories exist
- **THEN** its content is prefixed with the recall block (composition details per the memory-store spec)

#### Scenario: Delivered reminder reaches a mid-session turn
- **WHEN** a reminder is delivered while a session is already open and the owner then sends a message
- **THEN** that turn's content carries the delivered-reminder block even though the recall block was given earlier in the session

#### Scenario: Non-announceable event turn output suppressed
- **WHEN** an event turn for a cap-suppressed incident completes
- **THEN** no Signal message is sent for it

### Requirement: Owner commands are dispatched app-side
The agent core SHALL recognize the owner command set — `/new`, `/remember`, `/forget`, `/memories`, `/capture`, `/inbox`, `/inbox all`, `/inbox done <id>`, `/remind <when> <text>`, `/reminders`, and `/reminders cancel <id>` — at the start of owner-turn processing and handle each without starting an agent turn, replying immediately over the channel (command effects are specified in the memory-store, capture-inbox, and reminders capabilities; `/new` keeps its existing behavior). Owner text matching no recognized command SHALL be processed as a normal agent turn exactly as before. Commands arriving while an approval is pending SHALL be classified by the gate first, per the existing approval-gate rules: as unrelated messages they fail the pending action closed and are then handled as commands, not swallowed.

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
The session system prompt SHALL enumerate all registered tools, including `publish_handoff` and — when reminders are enabled — `remind` and `reminders_read`. It SHALL state that the agent can schedule a reminder but cannot cancel one, naming the owner command that can. Triage-mode instructions SHALL NOT live in the base system prompt — they arrive with event turns — so owner conversations are unaffected by triage machinery.

#### Scenario: System prompt lists publish_handoff
- **WHEN** the session system prompt is inspected
- **THEN** `publish_handoff` appears in the tool enumeration and no triage-arc instructions are present

#### Scenario: System prompt matches the registry
- **WHEN** reminders are enabled and the system prompt is inspected
- **THEN** `remind` and `reminders_read` appear in the tool enumeration, and the prompt states that cancelling a reminder is an owner command

#### Scenario: Disabled reminders are not advertised
- **WHEN** reminders are disabled and the system prompt is inspected
- **THEN** neither reminder tool appears in the enumeration

## ADDED Requirements

### Requirement: The reminder scheduler runs alongside the core worker
When reminders are enabled, the application SHALL run the reminder scheduler as a task alongside the core queue worker and (where enabled) the event coordinator, started with them and cancelled with them on shutdown. The scheduler SHALL NOT enqueue turns and SHALL NOT block the queue worker: a due reminder is delivered directly through the channel adapter (reminders spec). A scheduler failure SHALL NOT stop message handling, triage, or replies.

#### Scenario: Scheduler starts and stops with the app
- **WHEN** the application starts with reminders enabled and is then shut down
- **THEN** the scheduler task runs for the application's lifetime and is cancelled cleanly on shutdown, with no pending task left running

#### Scenario: Scheduler crash does not take the app down
- **WHEN** the scheduler task raises an unexpected error
- **THEN** the error is logged, the owner can still send messages and receive replies, and event triage still runs
