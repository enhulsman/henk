# channel-adapter (delta)

## ADDED Requirements

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
