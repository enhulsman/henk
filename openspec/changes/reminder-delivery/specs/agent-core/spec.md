# agent-core Specification (delta)

## MODIFIED Requirements

### Requirement: Turns are typed and event turns carry triage framing
The serial queue SHALL carry typed turns distinguishing owner messages from event turns; event turns SHALL carry the event metadata (source, alert identity, payload, firing/resolved state, announceable flag). When processing an event turn, the agent core SHALL compose the turn content as a clearly delimited untrusted-data block containing the event payload, plus triage-mode framing (the triage-arc mandate, the recurrence note where applicable, and the instruction to publish a handoff). Owner turns SHALL NOT receive triage framing. **When reminders are enabled, every owner turn SHALL carry a one-line current-time header** stating the current time in the owner's configured timezone, so a relative time is resolved against the moment of the turn and not against the session's start; the header SHALL be composed per turn rather than per session, SHALL be delimited as data, and event turns SHALL NEVER carry it. The header SHALL be produced by the same renderer as every reminder due time (reminders spec), so the time the model reasons from and the time the owner is told read identically. The first owner turn of a session that has not yet received it SHALL be prefixed with the memory recall block per the memory-store spec; event turns SHALL never carry it. **An owner turn SHALL additionally carry the delivered-reminder block when an unsurfaced delivery exists** (reminders spec), independently of whether the recall block was already given in that session; event turns SHALL never carry it, and it SHALL NOT taint the session. Event-turn output SHALL be routed through the channel adapter's proactive owner-directed send (suppressed for non-announceable incidents); owner-turn output keeps the reply path.

#### Scenario: Event turn framed for triage
- **WHEN** an event turn is processed
- **THEN** the text passed to the agent session contains the event payload inside a delimited untrusted-data block and the triage-mode framing, and no recall block, no time header, and no delivered-reminder block

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

#### Scenario: Delivered reminder reaches a mid-session turn
- **WHEN** a reminder is delivered while a session is already open and the owner then sends a message
- **THEN** that turn's content carries the delivered-reminder block even though the recall block was given earlier in the session

#### Scenario: Non-announceable event turn output suppressed
- **WHEN** an event turn for a cap-suppressed incident completes
- **THEN** no Signal message is sent for it

## ADDED Requirements

### Requirement: The reminder scheduler runs alongside the core worker
When reminders are enabled, the application SHALL run the reminder scheduler as a task alongside the core queue worker and (where enabled) the event coordinator, started with them and cancelled with them on shutdown. The scheduler SHALL NOT enqueue turns and SHALL NOT block the queue worker: a due reminder is delivered directly through the channel adapter (reminders spec). A scheduler failure SHALL NOT stop message handling, triage, or replies, and a failure in any of those SHALL NOT stop the scheduler.

#### Scenario: Scheduler starts and stops with the app
- **WHEN** the application starts with reminders enabled and is then shut down
- **THEN** the scheduler task runs for the application's lifetime and is cancelled cleanly on shutdown, with no pending task left running

#### Scenario: Scheduler failure does not take the app down
- **WHEN** a scheduler tick raises an unexpected error
- **THEN** the error is logged, the owner can still send messages and receive replies, and event triage still runs

#### Scenario: No scheduler task when disabled
- **WHEN** the application starts with reminders disabled
- **THEN** no scheduler task is created
