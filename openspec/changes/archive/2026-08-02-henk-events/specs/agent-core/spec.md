# agent-core (delta)

## ADDED Requirements

### Requirement: Event-triggered turns share the conversation lane
A triageable event SHALL be enqueued as an event turn in the same serial per-owner queue as inbound messages — event and owner turns SHALL never run concurrently. If no session is active or the idle window has expired, the event turn SHALL start a fresh session; if a session is active, the event turn SHALL run within it. Sessions started or continued by an event turn follow the existing continuity, `/new`, and idle-expiry rules, so owner replies after a triage message resolve against the incident context.

#### Scenario: Event arrives while an owner turn is running
- **WHEN** an event turn is enqueued while an owner message is mid-turn
- **THEN** the event turn runs after the owner turn completes, and neither is dropped

#### Scenario: Event arrives when idle
- **WHEN** an event turn runs and no active session exists (or the idle window has expired)
- **THEN** it starts a fresh session with no stale conversation context

#### Scenario: Owner resets after a triage message
- **WHEN** the owner sends `/new` after receiving a triage message
- **THEN** the triage session is discarded and the next message starts fresh, exactly as for owner-initiated sessions

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
