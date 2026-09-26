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
The serial queue SHALL carry typed turns distinguishing owner messages from event turns.
Event turns SHALL carry the event metadata: source, alert identity, payload,
firing/resolved state, announceable flag, and the event's notification and receive times.

When processing an event turn, the agent core SHALL compose the turn content in this
order:
1. the memory recall block (memory-store spec), when the store is non-empty and readable;
2. a clearly delimited untrusted-data block, holding each incident's payload and times
   followed by the related-handoff digest (triage-handoff spec) with any recurrence
   reference;
3. the triage-mode framing: the method and arc mandate, the handoff instruction, and the
   recurrence note where applicable.

The framing SHALL come after the untrusted-data block and SHALL name only tools registered
in the session. An event turn that injects the recall block SHALL mark the session as
having received it, so the owner follow-up in that session does not receive it again. The
event turn SHALL remain an event turn for the gate, and its session SHALL remain tainted.

Owner turns SHALL NOT receive triage framing, the untrusted-data block, or the digest.

**When reminders are enabled, every owner turn SHALL carry a one-line current-time
header** stating the current time in the owner's configured timezone, so a relative time
is resolved against the moment of the turn and not against the session's start. The
header:
- SHALL be composed per turn rather than per session;
- SHALL be delimited as data;
- SHALL NEVER be carried by an event turn;
- SHALL be produced by the same renderer as every reminder due time (reminders spec), so
  the time the model reasons from and the time the owner is told read identically.

The first owner turn of a session that has not yet received the recall block SHALL be
prefixed with it, per the memory-store spec.

**An owner turn SHALL additionally carry the delivered-reminder block when an unsurfaced
delivery exists** (reminders spec), independently of whether the recall block was already
given in that session. Event turns SHALL never carry it, and it SHALL NOT taint the
session.

Event-turn output SHALL be routed through the channel adapter's proactive owner-directed
send, suppressed for non-announceable incidents. Owner-turn output keeps the reply path.

#### Scenario: Event turn framed for triage
- **WHEN** an event turn is processed while memories exist and a related handoff is retained
- **THEN** the text passed to the agent session is, in order, the recall block, the delimited untrusted-data block containing the event payload with its times followed by the digest, and the triage-mode framing — and it contains no time header and no delivered-reminder block

#### Scenario: Event turn with an empty store carries no recall block
- **WHEN** an event turn is processed while no memories exist
- **THEN** its content begins with the untrusted-data block and contains no recall block

#### Scenario: Recall given at the event turn is not repeated
- **WHEN** an event turn injected the recall block and the owner then sends a follow-up in that session
- **THEN** the follow-up's content carries no second recall block

#### Scenario: Event turns stay tainted with recall
- **WHEN** an event turn carrying the recall block attempts a mutating tool
- **THEN** the gate denies it as out of scope for an event turn, exactly as without recall

#### Scenario: Owner turn unaffected
- **WHEN** an owner message turn is processed
- **THEN** its content contains no triage framing, no untrusted-data block and no digest, and its output is delivered as a normal reply

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

### Requirement: Event sessions run on the triage profile
The agent core SHALL create every session that an event turn starts from a **triage
profile**: a session model, an effort and a thinking mode configured separately from the
chat profile. Sessions started by an owner turn SHALL use the chat profile. A session
keeps the profile it was created with for its lifetime, so an owner follow-up inside a
triage session SHALL run on the triage profile. After `/new` or idle expiry, the next
owner session SHALL use the chat profile.

Each triage-profile setting that is **absent** from configuration SHALL equal the
corresponding resolved chat-profile setting, so a configuration that names none of them
behaves exactly as before this change. An explicit null effort or thinking SHALL keep its
existing meaning of deferring to the SDK's own default. The two profiles SHALL share one
tool registry and one approval gate, and SHALL differ only in model, effort and thinking.
Effort SHALL be passed to the SDK explicitly whenever it is configured, so a model's own
default effort never applies silently. Every session audit record SHALL name the profile
and the effort of **the factory that created the session**, never a profile inferred from
the record's trigger. An owner follow-up recorded from inside a triage session therefore
names the triage profile.

#### Scenario: Defaults change nothing
- **WHEN** the configuration names no triage-profile setting
- **THEN** event sessions are created with exactly the chat profile's model, effort and thinking

#### Scenario: A configured triage profile applies to event sessions only
- **WHEN** the triage profile names a different model and effort, and an event turn and then a fresh owner session run
- **THEN** the event session is created with the triage model and effort, and the owner session with the chat model and effort

#### Scenario: Follow-ups stay on the triage profile
- **WHEN** the owner replies to a triage message within the idle window
- **THEN** the reply runs in the triage session on the triage profile

#### Scenario: Reset returns to the chat profile
- **WHEN** the owner sends `/new` after a triage and then a message
- **THEN** the new session uses the chat profile

#### Scenario: Profiles share the boundary
- **WHEN** the chat and triage session factories are inspected
- **THEN** they hold the same tool registry and the same approval gate, and their configurations differ at most in model, effort and thinking

#### Scenario: The record names the profile
- **WHEN** an event triage's audit record is written
- **THEN** it carries profile `event` and the effort the session ran with

#### Scenario: A follow-up record names the factory's profile, not its trigger
- **WHEN** the owner's follow-up inside a triage session is recorded, with trigger `owner-message`
- **THEN** its record carries profile `event` and the triage effort

#### Scenario: A plain owner session records the chat profile
- **WHEN** an owner session that no event started is recorded
- **THEN** its record carries profile `chat` and the chat effort

