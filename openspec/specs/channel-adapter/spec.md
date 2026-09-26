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
The channel layer SHALL process messages only from the configured owner identity (the owner's Signal number/UUID). Messages from any other sender SHALL be dropped without any reply, read receipt, or typing indicator **originated by Henk**, and the drop SHALL be logged with the sender identity for audit. An acknowledgement of any kind (a read receipt or a working indicator) SHALL be sent only for a message that has passed this check, and only to the configured owner identity. The scope "originated by Henk" is deliberate and narrow: signal-cli emits transport-level **delivery** receipts beneath the application for any message it receives. These reveal only that the registered number exists, which is a property of registering a Signal number at all, and they are an accepted residual. It does not extend to **read** receipts: the signal-cli daemon SHALL NOT be configured to send read receipts automatically, because that would acknowledge strangers beneath the application, where no application-level check can observe it. This SHALL be verified on the deployed instance.

#### Scenario: Owner sends a DM
- **WHEN** a direct message arrives from the configured owner identity
- **THEN** the message is passed to the agent core as an inbound turn

#### Scenario: Unknown sender sends a DM
- **WHEN** a direct message arrives from any sender not on the allowlist
- **THEN** no response of any kind originated by Henk is sent (no reply, no read receipt, no typing indicator), the message is not passed to the agent core, and a log entry records the dropped sender

#### Scenario: Group message received
- **WHEN** a message arrives via a group conversation, even one containing the owner
- **THEN** the message is ignored (v1 is DM-only) and logged as dropped, and no acknowledgement of any kind is sent for it

#### Scenario: The daemon does not acknowledge on Henk's behalf
- **WHEN** the deployed signal-cli daemon's configuration is inspected
- **THEN** it is not configured to send read receipts automatically, so every read receipt the owner or anyone else can receive is one Henk's application code chose to send

### Requirement: Signal transport via signal-cli-rest-api
The Signal adapter SHALL send and receive messages exclusively through a containerized signal-cli-rest-api instance using Henk's dedicated Signal identity. The adapter SHALL NOT embed Signal protocol logic or credentials beyond the bridge's API endpoint and its account identifier. Every bridge **HTTP** request SHALL carry an explicitly configured timeout on **every** transport phase — connect, read, write and pool — and the receive path's connection attempt SHALL carry an explicitly configured timeout: no request and no phase SHALL rely on an HTTP client library's default, since a default shorter than the bridge's own send latency turns an accepted message into a reported failure and a retried duplicate. The configured value SHALL apply to each phase **in full** rather than as a share of a budget divided across them, because the phase that carries the bridge's own processing time is `read`, and a fraction of the configured value there raises the ceiling the motivating defect lives under only marginally. A **total** request budget is deliberately NOT specified **for message sends**: the HTTP client applies the read and write timeouts per socket *operation* rather than per phase, so no allocation across phases bounds a whole request, and the only mechanism that does is cancelling a request in flight — which manufactures the "may already have been delivered" ambiguity the delivery outcome exists to describe rather than to create.

The adapter SHALL send read receipts through the bridge's receipt endpoint with receipt type `read` (never `viewed`), and SHALL start and stop the typing indicator through the bridge's typing-indicator endpoint. Every such request SHALL be addressed to the configured owner identity the adapter was constructed with, and never to an identity taken from an inbound message. Each SHALL be a single attempt with no retry, SHALL be built by the same explicitly-timed client construction as sends, and SHALL NOT raise on a transport error: it reports whether the bridge accepted it. Acknowledgement requests are **not send sequences**. They SHALL NOT wait on or take the outbound send serialization, and the adapter SHALL NOT bound them itself. The whole-request bound that sends deliberately lack is applied to them by cancellation, outside the adapter (see "Owner-only acknowledgement of inbound messages"). That is safe for them, where it is not for sends, because they carry no content and are idempotent.

The inbound conversion SHALL mint the message's opaque channel reference from the message's Signal timestamp, and SHALL leave the reference absent when the envelope carries no timestamp. `acknowledge` SHALL refuse, without a bridge request, a reference it cannot interpret.

#### Scenario: Inbound message received
- **WHEN** signal-cli-rest-api reports a new incoming message for Henk's account
- **THEN** the adapter converts it to a channel-neutral inbound message (sender identity, text, timestamp, and an opaque channel reference) and hands it to the allowlist check

#### Scenario: Timestamp-less envelope carries no channel reference
- **WHEN** an incoming message's envelope carries no timestamp
- **THEN** the converted inbound message has no channel reference, rather than one that the receipt endpoint is certain to reject

#### Scenario: Outbound reply sent
- **WHEN** the agent core produces a reply for a conversation
- **THEN** the adapter delivers it to the owner via signal-cli-rest-api and reports the outcome

#### Scenario: Bridge unreachable
- **WHEN** signal-cli-rest-api is unreachable or returns an error while the adapter receives or sends messages
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

#### Scenario: A read receipt names the owner and the message
- **WHEN** the adapter acknowledges an inbound message whose channel reference it minted
- **THEN** exactly one receipt request reaches the bridge, with receipt type `read`, the configured owner identity as recipient, and the message's timestamp as an integer

#### Scenario: Typing indicator start and stop reach the owner
- **WHEN** the adapter starts and then stops the working indicator
- **THEN** one typing-indicator start and one stop request reach the bridge, each addressed to the configured owner identity

#### Scenario: A failed acknowledgement request is reported, not raised
- **WHEN** the bridge rejects or errors on a receipt or typing-indicator request
- **THEN** the operation reports that it was not accepted, does not raise, and makes no second attempt

#### Scenario: An acknowledgement does not wait behind a send in flight
- **WHEN** a multi-chunk send holds the outbound serialization and an acknowledgement is requested
- **THEN** the acknowledgement reaches the bridge without waiting for the send to finish, and the send's chunks remain contiguous and in order

#### Scenario: An uninterpretable reference makes no request
- **WHEN** `acknowledge` is given a channel reference that is not one the adapter mints
- **THEN** no bridge request is made and the operation reports that it was not accepted

### Requirement: Swappable channel-adapter contract
The agent core SHALL interact with messaging only through a channel-neutral adapter interface: receive inbound messages, **send a reply** (`send`), **send a proactive owner-directed message** (`send_proactive`), send approval prompts, receive approval responses, **acknowledge an inbound message** (`acknowledge`), and **start and stop the working indicator** (`start_working`, `stop_working`). Reply and proactive sends SHALL be **separate operations** rather than one operation with a flag: a reply carries the adapter's own standing failure notice, while a proactive send's notice is supplied by its caller or omitted (see "Proactive owner-directed sends" for the notice contract), and neither operation SHALL accept a recipient parameter or any parameter naming a recipient. Sending SHALL return a delivery outcome distinguishing `delivered` (at least one chunk was sent and every chunk was acknowledged), `partial` (at least one chunk delivered and the rest abandoned) and `failed` (no chunk delivered, including a send that produced no chunks). **`failed` means delivery was not confirmed, never that nothing arrived**: a transport fault can follow a message the bridge already accepted and sent, so a caller that retries on `failed` MAY duplicate a delivered message. A `partial` SHALL NOT be reported as success. The outcome SHALL be **additive**: a caller that ignores it behaves exactly as it did before this change. While an approval is pending, inbound owner messages SHALL be routed to the approval gate for keyword classification before normal message queueing.

The acknowledgement operations SHALL NOT accept a recipient, a sender, or any parameter naming an identity: `acknowledge` SHALL take only an inbound message's channel reference, the working-indicator operations SHALL take no parameters, and each SHALL direct its signal to the configured owner identity. Each SHALL report whether the channel accepted it and SHALL NOT raise on a transport error.

No Signal-specific types, identifiers, or API details SHALL appear outside the Signal adapter implementation, with **one bounded exception**. An inbound message MAY carry an **opaque channel reference**, minted by the adapter that received it, whose only purpose is to let that adapter acknowledge the message later. Channel-neutral code SHALL carry the reference from the inbound message to that adapter's `acknowledge`, and SHALL NOT persist it, write it to an audit record, or pass it into an agent turn, so that channel specifics stay out of what the model reads and out of the durable record. The agent core SHALL receive an owner message as its text alone. An adapter whose channel needs no reference SHALL leave it absent.

#### Scenario: Adding a second channel
- **WHEN** a new channel adapter (e.g., Telegram) implements the adapter interface
- **THEN** it can be wired in through configuration without modifying agent-core, tool, or approval-gate code

#### Scenario: Signal specifics stay encapsulated
- **WHEN** the codebase outside the Signal adapter module is inspected
- **THEN** it contains no references to signal-cli-rest-api endpoints (the receipt and typing-indicator endpoints included), Signal numbers, or Signal message formats (the receipt request's fields included)

#### Scenario: Outcome is additive
- **WHEN** a caller that ignores the send outcome runs against the new contract
- **THEN** its behaviour is unchanged from before this change

#### Scenario: No recipient reachable through any send operation
- **WHEN** every send operation on the contract and on the Signal adapter is inspected
- **THEN** each exposes exactly its declared parameters (`send`: the text; `send_proactive`: the text and an optional failure notice) and no parameter of any of them names a recipient

#### Scenario: No recipient reachable through any acknowledgement operation
- **WHEN** every acknowledgement operation on the contract and on the Signal adapter is inspected
- **THEN** each exposes exactly its declared parameters (`acknowledge`: the channel reference; `start_working` and `stop_working`: none) and no parameter of any of them names a recipient or a sender

#### Scenario: The channel reference never reaches the agent core
- **WHEN** the owner-turn type and the core's submit operation are inspected
- **THEN** the owner turn carries exactly the message text, and the submit operation accepts exactly the text, so no channel reference can be queued into a turn

#### Scenario: The channel reference is never audited, persisted, sent or put in a turn
- **WHEN** an owner message carrying a distinctive channel reference is processed end to end with acknowledgement enabled
- **THEN** the reference reaches the adapter's receipt request and appears in no audit record, no persisted store file, no agent-turn content and no outbound message

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

### Requirement: Outbound sends are serialized
The adapter SHALL serialize outbound sends: at most one send sequence — reply or proactive — SHALL be in flight at a time, and a send that begins while another is in flight SHALL wait for it to complete rather than interleave with it. The chunks of one message SHALL therefore reach the owner contiguous and in order regardless of how many tasks send concurrently, and the failure notice SHALL fire inside the same serialized sequence as the chunks it describes, as the notice contract already states. Serialization SHALL NOT drop, reorder within, or truncate any waiting send: every accepted send runs to its own outcome.

The wait is deliberately unbounded by any hold timer or chunk cap — both were designed and rejected (channel-integrity design D5) — and is bounded in fact by the in-flight message's chunk count times the per-chunk retry ceiling (`max_send_attempts × send_timeout` plus backoff). The healthy-path cost is priced by measurement, not estimate: `reminder-delivery`'s send-latency measurement on the deployed host puts the worst observed per-chunk send at ~1.1 seconds.

#### Scenario: Concurrent sends do not interleave
- **WHEN** two tasks send multi-chunk messages concurrently
- **THEN** every chunk of one message is delivered before any chunk of the other, and both report their own outcomes

#### Scenario: A waiting send is delivered, not dropped
- **WHEN** a send begins while another send is in flight
- **THEN** it waits for the in-flight sequence (including any failure notice) to finish, then runs to completion and reports its outcome

#### Scenario: The notice cannot be separated from its chunks
- **WHEN** a send fails partway while another sender is waiting
- **THEN** the failure notice is emitted before the waiting sender's first chunk

#### Scenario: Serialization is enforced by the adapter, not by caller convention
- **WHEN** the serialization guarantee is exercised in the contract tests
- **THEN** it is exercised against the adapter's real lock with a slow transport double, not against a cooperative channel double that never yields mid-send

### Requirement: Owner-only acknowledgement of inbound messages
When owner acknowledgement is enabled (`signal.acknowledge_owner`, default **true**; an absent key SHALL mean enabled), Henk SHALL send the owner a **read receipt** for every inbound message that passes the owner-only allowlist, whether it is queued as a turn or routed to a pending approval, and SHALL show a **working indicator** for the duration of every owner agent turn (agent-core spec). Neither SHALL be sent for any message that has not passed the allowlist.

The receipt SHALL be sent by the dispatch layer, only after the allowlist check has passed and after the message has been routed. It SHALL be directed to the configured owner identity, never to the message's sender field. When the message carries no channel reference, `acknowledge` SHALL be a no-op: no bridge request, and no failure logged.

The working indicator SHALL be refreshed at an interval below the channel's client-side expiry (Signal clients expire it about 15 seconds after the last start; the refresh interval is 7 seconds, measured from each start's issue time, and an overdue refresh SHALL be issued as soon as the previous request returns or times out, so that after one lost refresh the next start is issued no later than twice the interval after the last successful one, under the expiry) for as long as the turn runs. It SHALL be **suspended** while an approval prompt is pending, because the owner is then the one being waited on, and SHALL resume when the approval resolves. Every typing request of a turn — the first start, each refresh, the pause stop and resume start, and the final stop — SHALL be issued by **one task, sequentially**, so that a start can never land after the stop. Closing the indicator SHALL wake that task at once rather than wait out a poll or refresh interval, so on a healthy bridge the close costs no more than the stop's own round trip. The task SHALL be finished, or cancelled and awaited, before the turn's exit completes, so it never outlives the turn. Awaiting it SHALL NOT absorb a cancellation of the turn itself.

Neither acknowledgement is a message: they carry no content, raise no notification, and SHALL NOT change which messages the owner receives.

Every acknowledgement operation — a receipt, an indicator start or refresh, and the indicator's close as a whole (waking the task, letting any in-flight request finish, and sending the stop) — SHALL be **best-effort and bounded**. Each SHALL be bounded as a whole by its own configured timeout (`signal.acknowledge_timeout_seconds`, default 5 seconds), distinct from the send timeout, and the bound SHALL be enforced by cancelling the operation. A failure or timeout SHALL be logged, SHALL NOT fail the turn, SHALL NOT propagate to its caller, and SHALL delay message handling by no more than that timeout. Refresh failures SHALL be logged at most once per turn. When the close's bound is exhausted before the stop has been sent, the task SHALL be cancelled, no further stop SHALL be attempted, the client-side expiry SHALL be relied on to clear the indicator, and one line SHALL be logged. Cancellation of the surrounding task, as at shutdown, SHALL propagate rather than be absorbed. On that path no stop SHALL be attempted over the network: the indicator task is cancelled and awaited, and the client-side expiry clears the indicator. The acknowledge timeout SHALL NOT exceed the typing refresh interval, and SHALL be refused at load when it is not a positive number, is a boolean, or exceeds that interval.

When acknowledgement is disabled, no read receipt and no working indicator SHALL be sent, and message and turn handling SHALL be unchanged from before this change.

#### Scenario: Owner message receives a read receipt
- **WHEN** acknowledgement is enabled and the owner sends a DM
- **THEN** the message is queued as a turn and a read receipt for that message is sent to the configured owner identity

#### Scenario: Stranger gets nothing with acknowledgement enabled
- **WHEN** acknowledgement is enabled and a DM arrives from a sender not on the allowlist, or a group message arrives, even one from the owner
- **THEN** no read receipt, no typing indicator and no reply is requested from the bridge, no session is created, and the drop is logged exactly as with acknowledgement disabled

#### Scenario: The receipt cannot be addressed to the sender
- **WHEN** a read receipt is sent
- **THEN** its recipient is the configured owner identity the adapter was constructed with, and the acknowledge operation accepts no sender or recipient input from which another address could be taken

#### Scenario: An approval reply is acknowledged
- **WHEN** an approval prompt is pending and the owner replies with an approval keyword
- **THEN** the reply is routed to the approval gate and a read receipt is sent for it

#### Scenario: A message without a channel reference is not acknowledged and logs nothing
- **WHEN** an owner message carrying no channel reference passes the allowlist
- **THEN** it is handled normally, no receipt request is made, and no acknowledgement failure is logged

#### Scenario: The indicator stays up through a long turn
- **WHEN** acknowledgement is enabled and an owner agent turn runs longer than the client-side expiry
- **THEN** the indicator is re-asserted every 7 seconds, never with a gap as long as the expiry, until the turn ends, and it is then stopped

#### Scenario: One hung refresh does not let the indicator expire
- **WHEN** one refresh hangs until the acknowledge timeout cuts it off
- **THEN** the next start is issued as soon as that refresh times out, no later than twice the refresh interval after the last successful start

#### Scenario: The indicator is suspended during an approval wait
- **WHEN** an owner agent turn sends an approval prompt and waits on the owner
- **THEN** the indicator is stopped within about one poll of the prompt, no new refresh is started while the approval is pending, and it resumes within about one poll once the approval resolves while the turn continues

#### Scenario: A start in flight cannot land after the stop
- **WHEN** an owner turn ends while a refresh request is in flight
- **THEN** the stop is sent only after the refresh has completed or been cut off by its own bound, and the turn's exit completes within one acknowledge timeout of the close beginning; a refresh that hangs until its own bound may leave the stop unsent, and the client-side expiry then clears the indicator

#### Scenario: A healthy close does not wait out a poll
- **WHEN** an owner turn ends while no typing request is in flight
- **THEN** the stop is sent at once, and the turn's exit is not delayed by the indicator's poll or refresh interval

#### Scenario: A hung stop is bounded
- **WHEN** the bridge never answers the stop at the end of a turn
- **THEN** the turn's exit completes within the acknowledge timeout, the reply has been delivered, one line is logged, the indicator is left to the client-side expiry, and the next queued turn starts no later than that timeout after the close began

#### Scenario: An acknowledgement failure never fails or delays the turn beyond the bound
- **WHEN** the bridge rejects, errors on, or never answers a receipt or typing-indicator request
- **THEN** the owner's turn runs and its reply is delivered as without acknowledgement, the failure is logged, the error does not propagate, and each operation delays message handling by no more than the acknowledge timeout

#### Scenario: A hung receipt does not hold the message it acknowledges
- **WHEN** a receipt request hangs
- **THEN** the message it acknowledges has already been routed, and the next inbound message is handled no later than the acknowledge timeout after the hang began

#### Scenario: Refresh failures are logged once per turn
- **WHEN** every refresh of the working indicator fails during one long turn
- **THEN** at most one refresh-failure line is logged for that turn, and the refresh keeps being attempted

#### Scenario: Shutdown mid-turn sends no stop
- **WHEN** the application is shut down while an owner turn is running with the indicator up
- **THEN** the cancellation propagates out of the turn rather than being absorbed, the indicator task is cancelled and awaited, and no stop request is attempted

#### Scenario: Acknowledgements are not messages
- **WHEN** acknowledgement is enabled and an owner turn runs to its reply
- **THEN** the message sends that reach the bridge are exactly the reply's chunks, the same as with acknowledgement disabled

#### Scenario: Disabled means nothing is sent
- **WHEN** `signal.acknowledge_owner` is `false` and the owner sends a DM that runs as an agent turn
- **THEN** no read receipt and no working indicator request reaches the bridge, and the turn and reply are unchanged

#### Scenario: Enabled by default when the keys are absent
- **WHEN** the configuration's `signal` section names neither `acknowledge_owner` nor `acknowledge_timeout_seconds`
- **THEN** acknowledgement is enabled with a 5-second acknowledge timeout

#### Scenario: An explicit false is honoured
- **WHEN** the configuration sets `signal.acknowledge_owner: false`
- **THEN** acknowledgement is disabled, proving the loader reads the key rather than relying on a default

#### Scenario: A non-boolean flag is refused
- **WHEN** `signal.acknowledge_owner` is set to a string such as `"false"`, a number, or left blank
- **THEN** configuration loading fails with an explicit error rather than reading the value as true or false

#### Scenario: An out-of-range acknowledge timeout is refused
- **WHEN** `signal.acknowledge_timeout_seconds` is zero, negative, a boolean, or greater than the typing refresh interval
- **THEN** configuration loading fails with an explicit error

