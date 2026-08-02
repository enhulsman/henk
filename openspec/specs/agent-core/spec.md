# agent-core Specification

## Purpose
TBD - created by archiving change henk-v1. Update Purpose after archive.
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
The serial queue SHALL carry typed turns distinguishing owner messages from event turns; event turns SHALL carry the event metadata (source, alert identity, payload, firing/resolved state, announceable flag). When processing an event turn, the agent core SHALL compose the turn content as a clearly delimited untrusted-data block containing the event payload, plus triage-mode framing (the triage-arc mandate, the recurrence note where applicable, and the instruction to publish a handoff). Owner turns SHALL NOT receive triage framing. Event-turn output SHALL be routed through the channel adapter's proactive owner-directed send (suppressed for non-announceable incidents); owner-turn output keeps the reply path.

#### Scenario: Event turn framed for triage
- **WHEN** an event turn is processed
- **THEN** the text passed to the agent session contains the event payload inside a delimited untrusted-data block and the triage-mode framing

#### Scenario: Owner turn unaffected
- **WHEN** an owner message turn is processed
- **THEN** its content contains no triage framing and its output is delivered as a normal reply

#### Scenario: Non-announceable event turn output suppressed
- **WHEN** an event turn for a cap-suppressed incident completes
- **THEN** no Signal message is sent for it

### Requirement: System prompt enumerates the full registered toolset
The session system prompt SHALL enumerate all registered tools, including `publish_handoff`. Triage-mode instructions SHALL NOT live in the base system prompt — they arrive with event turns — so owner conversations are unaffected by triage machinery.

#### Scenario: System prompt lists publish_handoff
- **WHEN** the session system prompt is inspected
- **THEN** `publish_handoff` appears in the tool enumeration and no triage-arc instructions are present

### Requirement: Event-triage audit record is written at triage completion
On completion of an event turn, the agent core SHALL write that triage's audit record immediately, without waiting for session close. Keeping the session open for owner interrogation SHALL NOT delay or suppress the record.

#### Scenario: Record written before session close
- **WHEN** an event triage turn completes and its session remains open awaiting owner follow-up
- **THEN** the triage's audit record has already been written to the log

