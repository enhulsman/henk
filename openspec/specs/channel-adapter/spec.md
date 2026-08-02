# channel-adapter Specification

## Purpose
TBD - created by archiving change henk-v1. Update Purpose after archive.
## Requirements
### Requirement: Owner-only allowlist
The channel layer SHALL process messages only from the configured owner identity (the owner's Signal number/UUID). Messages from any other sender SHALL be dropped without any reply, read receipt, or typing indicator, and the drop SHALL be logged with the sender identity for audit.

#### Scenario: Owner sends a DM
- **WHEN** a direct message arrives from the configured owner identity
- **THEN** the message is passed to the agent core as an inbound turn

#### Scenario: Unknown sender sends a DM
- **WHEN** a direct message arrives from any sender not on the allowlist
- **THEN** no response of any kind is sent, the message is not passed to the agent core, and a log entry records the dropped sender

#### Scenario: Group message received
- **WHEN** a message arrives via a group conversation, even one containing the owner
- **THEN** the message is ignored (v1 is DM-only) and logged as dropped

### Requirement: Signal transport via signal-cli-rest-api
The Signal adapter SHALL send and receive messages exclusively through a containerized signal-cli-rest-api instance using Henk's dedicated Signal identity. The adapter SHALL NOT embed Signal protocol logic or credentials beyond the bridge's API endpoint and its account identifier.

#### Scenario: Inbound message received
- **WHEN** signal-cli-rest-api reports a new incoming message for Henk's account
- **THEN** the adapter converts it to a channel-neutral inbound message (sender identity, text, timestamp) and hands it to the allowlist check

#### Scenario: Outbound reply sent
- **WHEN** the agent core produces a reply for a conversation
- **THEN** the adapter delivers it to the owner via signal-cli-rest-api

#### Scenario: Bridge unreachable
- **WHEN** signal-cli-rest-api is unreachable or returns an error
- **THEN** the adapter logs the failure and retries with backoff, and the agent process does not crash

### Requirement: Swappable channel-adapter contract
The agent core SHALL interact with messaging only through a channel-neutral adapter interface (receive inbound messages, send text replies, send approval prompts, receive approval responses). While an approval is pending, inbound owner messages SHALL be routed to the approval gate for keyword classification before normal message queueing. No Signal-specific types, identifiers, or API details SHALL appear outside the Signal adapter implementation.

#### Scenario: Adding a second channel
- **WHEN** a new channel adapter (e.g., Telegram) implements the adapter interface
- **THEN** it can be wired in through configuration without modifying agent-core, tool, or approval-gate code

#### Scenario: Signal specifics stay encapsulated
- **WHEN** the codebase outside the Signal adapter module is inspected
- **THEN** it contains no references to signal-cli-rest-api endpoints, Signal numbers, or Signal message formats

### Requirement: Long replies are delivered intact
Outbound messages exceeding the channel's safe length limit SHALL be split into sequential messages at paragraph or line boundaries and delivered in order. Replies SHALL NOT be silently truncated.

#### Scenario: Long reply split
- **WHEN** the agent produces a reply longer than the channel's safe message length
- **THEN** the adapter sends it as multiple sequential messages, split at natural boundaries, and all content reaches the owner in order

### Requirement: Proactive owner-directed sends
The channel-adapter contract SHALL support sending an agent-initiated message to the owner that is not a reply to any inbound message. Proactive sends SHALL be deliverable only to the configured owner identity — the interface SHALL NOT accept an arbitrary recipient — and SHALL remain channel-neutral (no Signal specifics outside the Signal adapter). Existing allowlist, DM-only, and long-message-splitting rules apply to proactive sends unchanged.

#### Scenario: Triage message delivered proactively
- **WHEN** the agent core produces a triage message with no pending inbound message
- **THEN** the adapter delivers it to the owner over Signal

#### Scenario: No arbitrary recipient possible
- **WHEN** the proactive send interface is inspected
- **THEN** it exposes no recipient parameter beyond the configured owner identity

#### Scenario: Long triage message split intact
- **WHEN** a proactive message exceeds the channel's safe length
- **THEN** it is split at natural boundaries and delivered in order, per the existing long-reply rule

