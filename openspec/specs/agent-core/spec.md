# agent-core Specification

## Purpose
Turns owner messages and triageable events into serial, typed agent turns with the right context
composed in (triage framing and untrusted-data delimiting for events; memory recall for owner
turns) and the right boundaries enforced (closed toolset, session isolation per incident,
continuity by rebuild). Owner commands are dispatched app-side so deterministic actions never
cost a model turn.
## Requirements
### Requirement: Inbound message becomes an agent turn
The agent core SHALL run each inbound owner message as a turn of a Claude Agent SDK session and deliver the agent's final text response back through the channel adapter. Intermediate tool activity SHALL NOT be sent as separate chat messages in v1.

#### Scenario: Simple question answered
- **WHEN** the owner sends "is everything up?"
- **THEN** the agent runs a turn (invoking read-only tools as needed) and the owner receives a single reply message with the answer

#### Scenario: Agent turn fails
- **WHEN** the Agent SDK call fails (API error, credit pool exhausted, timeout)
- **THEN** the owner receives a short error message stating the failure honestly, and the process remains alive for the next message

### Requirement: Conversation continuity and reset
The agent core SHALL maintain conversation context across consecutive messages so follow-ups resolve naturally, and SHALL start a fresh session when the owner sends a reset command (`/new`) or when the conversation has been idle beyond a configured window (default 60 minutes).

#### Scenario: Follow-up uses context
- **WHEN** the owner asks "what's on my board?" and then "and which of those are overdue?"
- **THEN** the second turn runs in the same session and resolves "those" to the previously listed items

#### Scenario: Owner resets the conversation
- **WHEN** the owner sends `/new`
- **THEN** the agent immediately replies with a short confirmation (e.g., "Session reset."), and the next message starts a fresh session with no prior conversation context

#### Scenario: Idle expiry
- **WHEN** a message arrives after the idle window has elapsed since the last turn
- **THEN** it starts a fresh session

### Requirement: Closed, explicit toolset
The agent session SHALL expose only the explicitly registered Henk tools. Built-in SDK capabilities that touch the host (shell execution, file read/write, web access) SHALL be disabled so the agent cannot act outside its registered toolset.

#### Scenario: Only registered tools available
- **WHEN** the agent session is constructed
- **THEN** its tool list contains exactly the registered Henk tools and no built-in shell, filesystem, or network tools

#### Scenario: Agent is asked to do something outside its tools
- **WHEN** the owner asks for an action no registered tool supports (e.g., "restart the container")
- **THEN** the agent replies that it cannot do that, and no out-of-toolset action occurs

### Requirement: Serial processing per conversation
The agent core SHALL process messages from the same conversation one at a time, in arrival order. Messages arriving while a turn is running SHALL be queued, not dropped and not run concurrently. While an approval gate is pending, inbound messages SHALL be classified by the gate (approval/denial keyword or unrelated) before normal queueing, per the approval-gate spec.

#### Scenario: Rapid consecutive messages
- **WHEN** the owner sends a second message while the first turn is still running
- **THEN** the second message runs as the next turn after the first completes, and both receive replies in order

### Requirement: Event-triggered turns share the conversation lane
A triageable event SHALL be enqueued as an event turn in the same serial per-owner queue as inbound messages — event and owner turns SHALL never run concurrently. A **new incident** (a fresh debounced event turn) SHALL start its own session rather than inheriting the context of an unrelated prior incident or an owner conversation, so no incident's context bleeds into another's triage. An owner reply following a triage message SHALL continue that incident's session, so follow-up questions resolve against the incident context under the existing continuity, `/new`, and idle-expiry rules. Sessions started or continued by an event turn otherwise follow the existing continuity, `/new`, and idle-expiry rules.

#### Scenario: Event arrives while an owner turn is running
- **WHEN** an event turn is enqueued while an owner message is mid-turn
- **THEN** the event turn runs after the owner turn completes, and neither is dropped

#### Scenario: New incident does not inherit prior context
- **WHEN** an event turn for one incident is processed while a session started by a different incident (or by an owner conversation) is still active
- **THEN** the new incident's triage runs with no context from the prior session

#### Scenario: Owner resets after a triage message
- **WHEN** the owner sends `/new` after receiving a triage message
- **THEN** the triage session is discarded and the next message starts fresh, exactly as for owner-initiated sessions

#### Scenario: Owner interrogates the incident
- **WHEN** the owner replies with a follow-up shortly after a triage message
- **THEN** the reply resolves against that incident's session context

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

### Requirement: Event-triage audit record is written at triage completion
On completion of an event turn, the agent core SHALL write that triage's audit record immediately, without waiting for session close. Keeping the session open for owner interrogation SHALL NOT delay or suppress the record.

#### Scenario: Record written before session close
- **WHEN** an event triage turn completes and its session remains open awaiting owner follow-up
- **THEN** the triage's audit record has already been written to the log

