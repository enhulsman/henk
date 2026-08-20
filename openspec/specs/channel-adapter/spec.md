# channel-adapter Specification

## Purpose
The owner's way in and Henk's way out, and the first place the security posture bites: only the
configured owner identity is ever processed, nothing else gets a reply, a read receipt, or even
a typing indicator. The contract is channel-neutral by construction — receive, reply, send
proactively, route approval keywords — so Signal lives behind one adapter and can be swapped
without touching agent logic. Outbound messages are split at natural boundaries rather than
truncated, and a proactive send cannot name a recipient at all.
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
The Signal adapter SHALL send and receive messages exclusively through a containerized signal-cli-rest-api instance using Henk's dedicated Signal identity. The adapter SHALL NOT embed Signal protocol logic or credentials beyond the bridge's API endpoint and its account identifier. Every bridge **HTTP** request SHALL carry an explicitly configured timeout on **every** transport phase — connect, read, write and pool — and the receive path's connection attempt SHALL carry an explicitly configured timeout: no request and no phase SHALL rely on an HTTP client library's default, since a default shorter than the bridge's own send latency turns an accepted message into a reported failure and a retried duplicate. The configured value SHALL apply to each phase **in full** rather than as a share of a budget divided across them, because the phase that carries the bridge's own processing time is `read`, and a fraction of the configured value there raises the ceiling the motivating defect lives under only marginally. A **total** request budget is deliberately NOT specified: the HTTP client applies the read and write timeouts per socket *operation* rather than per phase, so no allocation across phases bounds a whole request, and the only mechanism that does is cancelling a request in flight — which manufactures the "may already have been delivered" ambiguity the delivery outcome exists to describe rather than to create.

#### Scenario: Inbound message received
- **WHEN** signal-cli-rest-api reports a new incoming message for Henk's account
- **THEN** the adapter converts it to a channel-neutral inbound message (sender identity, text, timestamp) and hands it to the allowlist check

#### Scenario: Outbound reply sent
- **WHEN** the agent core produces a reply for a conversation
- **THEN** the adapter delivers it to the owner via signal-cli-rest-api and reports the outcome

#### Scenario: Bridge unreachable
- **WHEN** signal-cli-rest-api is unreachable or returns an error
- **THEN** the adapter logs the failure and retries with backoff, and the agent process does not crash

#### Scenario: Send timeout is chosen, not inherited
- **WHEN** the Signal bridge's HTTP client is inspected
- **THEN** its request timeout comes from configuration, no bridge code path constructs a client without one, and every transport phase the client can spend time in is bounded

#### Scenario: Every phase carries the configured value in full
- **WHEN** the Signal bridge's HTTP client's timeout is inspected
- **THEN** connect, read, write and pool each carry the configured value, none falls back to the client library's default, and none carries a fraction of it

#### Scenario: The bridge's own send latency is bounded by the read phase
- **WHEN** the bridge waits on signal-cli's processing of a send
- **THEN** the wait is bounded by the configured value in full, so raising that value raises the ceiling the false-failure-and-duplicate defect lives under

#### Scenario: Receive connection timeout is configured
- **WHEN** the receive path's websocket connection is inspected
- **THEN** its connection timeout comes from configuration rather than a constructor default the wiring never passes

### Requirement: Swappable channel-adapter contract
The agent core SHALL interact with messaging only through a channel-neutral adapter interface: receive inbound messages, **send a reply** (`send`), **send a proactive owner-directed message** (`send_proactive`), send approval prompts, and receive approval responses. Reply and proactive sends SHALL be **separate operations** rather than one operation with a flag: a reply carries the adapter's own standing failure notice, while a proactive send's notice is supplied by its caller or omitted (see "Proactive owner-directed sends" for the notice contract), and neither operation SHALL accept a recipient parameter or any parameter naming a recipient. Sending SHALL return a delivery outcome distinguishing `delivered` (at least one chunk was sent and every chunk was acknowledged), `partial` (at least one chunk delivered and the rest abandoned) and `failed` (no chunk delivered, including a send that produced no chunks). **`failed` means delivery was not confirmed, never that nothing arrived**: a transport fault can follow a message the bridge already accepted and sent, so a caller that retries on `failed` MAY duplicate a delivered message. A `partial` SHALL NOT be reported as success. The outcome SHALL be **additive**: a caller that ignores it behaves exactly as it did before this change. While an approval is pending, inbound owner messages SHALL be routed to the approval gate for keyword classification before normal message queueing. No Signal-specific types, identifiers, or API details SHALL appear outside the Signal adapter implementation.

#### Scenario: Adding a second channel
- **WHEN** a new channel adapter (e.g., Telegram) implements the adapter interface
- **THEN** it can be wired in through configuration without modifying agent-core, tool, or approval-gate code

#### Scenario: Signal specifics stay encapsulated
- **WHEN** the codebase outside the Signal adapter module is inspected
- **THEN** it contains no references to signal-cli-rest-api endpoints, Signal numbers, or Signal message formats

#### Scenario: Outcome is additive
- **WHEN** a caller that ignores the send outcome runs against the new contract
- **THEN** its behaviour is unchanged from before this change

#### Scenario: No recipient reachable through any send operation
- **WHEN** every send operation on the contract and on the Signal adapter is inspected
- **THEN** each exposes exactly its declared parameters (`send`: the text; `send_proactive`: the text and an optional failure notice) and no parameter of any of them names a recipient

### Requirement: Long replies are delivered intact
Outbound messages exceeding the channel's safe length limit SHALL be split into sequential messages at paragraph or line boundaries and delivered in order. Replies SHALL NOT be silently truncated. The safe length limit SHALL be measured in **UTF-8 encoded bytes**, not characters: the configured limit is a deliberately conservative value rather than a measured channel maximum, and bytes bound both the wire size and any client-side character limit, whereas a character count bounds neither. A split SHALL NOT divide a Unicode code point, and concatenating the chunks SHALL reproduce the original text exactly. The guarantee is over code points, not grapheme clusters: a multi-code-point emoji sequence may still be divided. The configured limit SHALL be at least the maximum UTF-8 encoding length of a single code point, validated at configuration load, since a smaller limit admits no valid chunk and would make the two guarantees jointly unsatisfiable.

#### Scenario: Long reply split
- **WHEN** the agent produces a reply longer than the channel's safe message length
- **THEN** the adapter sends it as multiple sequential messages, split at natural boundaries, and all content reaches the owner in order

#### Scenario: Multi-byte text respects the byte limit
- **WHEN** a reply consists of characters that encode to multiple bytes each (accented text, emoji) and is near the safe length
- **THEN** every chunk's UTF-8 encoded length is within the limit, no chunk splits a code point, and the concatenation equals the original text

#### Scenario: A limit too small to hold one code point is refused at load
- **WHEN** the configured safe length is smaller than the longest single code point's UTF-8 encoding
- **THEN** configuration loading fails with an explicit error rather than the splitter making no progress at send time

### Requirement: Proactive owner-directed sends
The channel-adapter contract SHALL support sending an agent-initiated message to the owner that is not a reply to any inbound message. Proactive sends SHALL be deliverable only to the configured owner identity — the interface SHALL NOT accept an arbitrary recipient — and SHALL remain channel-neutral (no Signal specifics outside the Signal adapter). Existing allowlist, DM-only, and long-message-splitting rules apply to proactive sends unchanged. A proactive send SHALL report its delivery outcome to its caller.

A proactive send SHALL accept an **optional caller-supplied failure notice**. On any outcome other than `delivered` **where at least one chunk was attempted**, the adapter SHALL emit one failure notice within the same serialized sequence, immediately after the delivered chunks, as a single attempt that SHALL NOT alter the reported outcome. For a reply the notice is the adapter's standing text; for a proactive send it is the caller-supplied notice, and the adapter SHALL emit none of its own when the caller supplied none. The caller owns proactive failure messaging because an adapter-authored notice cannot know what was being sent.

#### Scenario: Triage message delivered proactively
- **WHEN** the agent core produces a triage message with no pending inbound message
- **THEN** the adapter delivers it to the owner over Signal and reports `delivered`

#### Scenario: No arbitrary recipient possible
- **WHEN** the proactive send interface is inspected
- **THEN** it exposes no recipient parameter beyond the configured owner identity

#### Scenario: Long triage message split intact
- **WHEN** a proactive message exceeds the channel's safe length
- **THEN** it is split at natural boundaries and delivered in order, per the existing long-reply rule

#### Scenario: Failed proactive send with no caller notice is silent to the owner
- **WHEN** a proactive send fails permanently and its caller supplied no failure notice
- **THEN** the caller receives a `failed` outcome and the adapter sends no notice of its own to the owner

#### Scenario: Caller-supplied notice follows the delivered chunks
- **WHEN** a proactive send with a caller-supplied notice delivers some chunks and abandons the rest
- **THEN** the notice is sent immediately after the delivered chunks as a single attempt, and the reported outcome is `partial` regardless of whether the notice itself was delivered

#### Scenario: The reply notice does not alter the outcome
- **WHEN** a reply is truncated and the adapter posts its standing failure notice
- **THEN** the reported outcome reflects only the reply's own chunks

#### Scenario: An empty send is not reported as delivered and raises no notice
- **WHEN** a send is called with text that splits into no chunks
- **THEN** the outcome is not `delivered`, nothing is sent, and no failure notice is emitted, since no chunk was attempted

