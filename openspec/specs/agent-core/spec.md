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

