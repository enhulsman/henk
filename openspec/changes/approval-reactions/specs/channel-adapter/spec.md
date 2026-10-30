## MODIFIED Requirements

### Requirement: Owner-only allowlist
The channel layer SHALL process messages and reactions only from the configured owner identity (the owner's Signal number/UUID). Messages and reactions from any other sender SHALL be dropped without any reply, read receipt, or typing indicator **originated by Henk**, and the drop SHALL be logged with the sender identity for audit. Group messages and group reactions SHALL be dropped the same way. An acknowledgement of any kind (a read receipt or a working indicator) SHALL be sent only for a message that has passed this check, and only to the configured owner identity; a reaction SHALL receive no acknowledgement. The scope "originated by Henk" is deliberate and narrow: signal-cli emits transport-level **delivery** receipts beneath the application for any message it receives. These reveal only that the registered number exists, which is a property of registering a Signal number at all, and they are an accepted residual. It does not extend to **read** receipts: the signal-cli daemon SHALL NOT be configured to send read receipts automatically, because that would acknowledge strangers beneath the application, where no application-level check can observe it. This SHALL be verified on the deployed instance.

#### Scenario: Owner sends a DM
- **WHEN** a direct message arrives from the configured owner identity
- **THEN** the message is passed to the agent core as an inbound turn

#### Scenario: Unknown sender sends a DM
- **WHEN** a direct message arrives from any sender not on the allowlist
- **THEN** no response of any kind originated by Henk is sent (no reply, no read receipt, no typing indicator), the message is not passed to the agent core, and a log entry records the dropped sender

#### Scenario: Unknown sender reacts
- **WHEN** a reaction arrives from any sender not on the allowlist, including one targeting a pending approval prompt
- **THEN** it is dropped and logged, it reaches neither the approval gate nor the agent core, and nothing is sent

#### Scenario: Group message received
- **WHEN** a message arrives via a group conversation, even one containing the owner
- **THEN** the message is ignored (v1 is DM-only) and logged as dropped, and no acknowledgement of any kind is sent for it

#### Scenario: The daemon does not acknowledge on Henk's behalf
- **WHEN** the deployed signal-cli daemon's configuration is inspected
- **THEN** it is not configured to send read receipts automatically, so every read receipt the owner or anyone else can receive is one Henk's application code chose to send

### Requirement: Swappable channel-adapter contract
The agent core SHALL interact with messaging only through a channel-neutral adapter interface: receive inbound messages and reactions, **send a reply** (`send`), **send a proactive owner-directed message** (`send_proactive`), **send an approval prompt** (`send_prompt`), **edit a sent approval prompt into its outcome** (`edit_prompt`), **acknowledge an inbound message** (`acknowledge`), and **start and stop the working indicator** (`start_working`, `stop_working`). Reply and proactive sends SHALL be **separate operations** rather than one operation with a flag: a reply carries the adapter's own standing failure notice, while a proactive send's notice is supplied by its caller or omitted (see "Proactive owner-directed sends" for the notice contract), and no send, prompt or edit operation SHALL accept a recipient parameter or any parameter naming a recipient. Sending SHALL return a delivery outcome distinguishing `delivered` (at least one chunk was sent and every chunk was acknowledged), `partial` (at least one chunk delivered and the rest abandoned) and `failed` (no chunk delivered, including a send that produced no chunks). **`failed` means delivery was not confirmed, never that nothing arrived**: a transport fault can follow a message the bridge already accepted and sent, so a caller that retries on `failed` MAY duplicate a delivered message. A `partial` SHALL NOT be reported as success. The outcome SHALL be **additive**: a caller that ignores it behaves exactly as it did before this change. While an approval is pending, inbound owner messages SHALL be routed to the approval gate for keyword classification before normal message queueing, and inbound owner reactions SHALL be routed only to the approval gate, never to the agent core.

`send_prompt` SHALL take a structured approval prompt (label, tool name, and ordered argument fields) and render it in the adapter's own format, SHALL never split it across messages (a prompt whose form as sent exceeds one message SHALL not be sent and SHALL report `failed`), is at-least-once like every send, and SHALL return its delivery outcome together with an **opaque prompt reference** naming the sent message, or no reference when the channel did not report one. A prompt reference and a reaction's target reference SHALL be minted by one function from the same integer form of the sent timestamp, whatever JSON type the channel reports it in, so that equality cannot fail on form. `edit_prompt` SHALL take only a prompt reference and a structured outcome, SHALL render the outcome in the same mode the prompt was actually sent in, SHALL make a single attempt, SHALL report whether the channel accepted it, and SHALL NOT raise on a transport error.

The acknowledgement operations SHALL NOT accept a recipient, a sender, or any parameter naming an identity: `acknowledge` SHALL take only an inbound message's channel reference, the working-indicator operations SHALL take no parameters, and each SHALL direct its signal to the configured owner identity. Each SHALL report whether the channel accepted it and SHALL NOT raise on a transport error.

No Signal-specific types, identifiers, markup or API details SHALL appear outside the Signal adapter implementation, with **two bounded exceptions**, both opaque references minted by the adapter and interpreted only by it. An inbound message MAY carry a **channel reference** whose only purpose is to let that adapter acknowledge the message later; and a sent approval prompt's **prompt reference** (and an inbound reaction's target reference, which names a prompt the same way) exists only so the gate can match a reaction to the pending prompt and hand the reference back to `edit_prompt`. Channel-neutral code SHALL compare these references only for equality, and SHALL NOT parse them, persist them, write them to an audit record, or pass them into an agent turn, so that channel specifics stay out of what the model reads and out of the durable record. The agent core SHALL receive an owner message as its text alone. An adapter whose channel needs no reference SHALL leave it absent.

#### Scenario: Adding a second channel
- **WHEN** a new channel adapter (e.g., Telegram) implements the adapter interface
- **THEN** it can be wired in through configuration without modifying agent-core, tool, or approval-gate code

#### Scenario: Signal specifics stay encapsulated
- **WHEN** the codebase outside the Signal adapter module is inspected
- **THEN** it contains no references to signal-cli-rest-api endpoints (the receipt, typing-indicator, send-edit and reaction fields included), Signal numbers, Signal message formats, or Signal styling markup

#### Scenario: Outcome is additive
- **WHEN** a caller that ignores the send outcome runs against the new contract
- **THEN** its behaviour is unchanged from before this change

#### Scenario: No recipient reachable through any send operation
- **WHEN** every send operation on the contract and on the Signal adapter is inspected
- **THEN** each exposes exactly its declared parameters (`send`: the text; `send_proactive`: the text and an optional failure notice; `send_prompt`: the structured prompt; `edit_prompt`: the prompt reference and the structured outcome) and no parameter of any of them names a recipient

#### Scenario: No recipient reachable through any acknowledgement operation
- **WHEN** every acknowledgement operation on the contract and on the Signal adapter is inspected
- **THEN** each exposes exactly its declared parameters (`acknowledge`: the channel reference; `start_working` and `stop_working`: none) and no parameter of any of them names a recipient or a sender

#### Scenario: The channel reference never reaches the agent core
- **WHEN** the owner-turn type and the core's submit operation are inspected
- **THEN** the owner turn carries exactly the message text, and the submit operation accepts exactly the text, so no channel reference can be queued into a turn

#### Scenario: The channel reference is never audited, persisted, sent or put in a turn
- **WHEN** an owner message carrying a distinctive channel reference is processed end to end with acknowledgement enabled
- **THEN** the reference reaches the adapter's receipt request and appears in no audit record, no persisted store file, no agent-turn content and no outbound message

#### Scenario: The prompt reference is never audited or put in a turn
- **WHEN** an approval prompt is sent with a distinctive prompt reference and the owner approves it by reaction
- **THEN** the reference reaches the adapter's edit request and appears in no audit record, no persisted store file and no agent-turn content

### Requirement: Outbound sends are serialized
The adapter SHALL serialize outbound sends: at most one send sequence — reply, proactive, approval prompt or prompt edit — SHALL be in flight at a time, and a send that begins while another is in flight SHALL wait for it to complete rather than interleave with it. The chunks of one message SHALL therefore reach the owner contiguous and in order regardless of how many tasks send concurrently, and the failure notice SHALL fire inside the same serialized sequence as the chunks it describes, as the notice contract already states. Serialization SHALL NOT drop, reorder within, or truncate any waiting send: every accepted send runs to its own outcome.

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

## ADDED Requirements

### Requirement: Approval prompts are one styled message, unstyled by configuration
The Signal adapter SHALL render an approval prompt as one styled message: a bold `Approval needed` header line; a line with the label followed by the tool name in brackets; one line per argument as `<key>: <value>` with the value in monospace; and the footer line `React 👍 to approve or 👎 to deny, or reply yes / no. Any other message cancels it.` It SHALL render a prompt outcome as one line: the bold outcome word, a colon, the label, and each argument value in monospace, separated by commas. Every styling character in a value, key, label or tool name SHALL be escaped so no content can change the message's styling; a backslash, which the bridge's styled mode cannot render literally before a styling character, SHALL be replaced in styled text by the visible stand-in `⧵` (U+29F5); and the delimiter sequences `<<<` and `>>>` SHALL be stripped from values in every mode. When configuration disables styled prompts, the adapter SHALL send the same prompt and its outcome edit unstyled, every value wrapped in `<<<` `>>>`, with the same header, label line and footer. A styled send SHALL follow the same retry rules as every other send; there is no automatic fallback, because the bridge's style parser cannot reject a message and the bridge reports every send failure with the same status, so no response can single out styling as the cause. Styled text SHALL be used for approval prompts and their outcome edits only.

#### Scenario: Styled prompt
- **WHEN** styled prompts are enabled and the gate sends a prompt for `calendar_create_event` with title `Homelab: patch round`
- **THEN** the bridge receives one styled send whose text has the bold header, the line `Add a calendar event (calendar_create_event)`, the title value in monospace, and the footer

#### Scenario: A value cannot restyle the prompt
- **WHEN** a prompt value contains `**bold**`, `` `tick` ``, `~strike~`, `||spoiler||` or a backslash
- **THEN** every styling character in it is escaped in the sent text and the value renders literally inside its monospace span

#### Scenario: A trailing backslash cannot open the span
- **WHEN** a prompt value ends in a backslash
- **THEN** the styled text carries the stand-in `⧵` in its place and the value's monospace span closes where it should

#### Scenario: A failed styled send is retried like any send
- **WHEN** the bridge answers a styled prompt send with an error
- **THEN** the adapter retries it within the reply retry budget, still styled, and a prompt that is never acknowledged resolves the approval as `cancelled` exactly as today

#### Scenario: Styling disabled by configuration
- **WHEN** styled prompts are disabled in configuration
- **THEN** prompts and their outcome edits are sent unstyled with delimiters, and no send uses styled text mode

#### Scenario: Other messages stay unstyled
- **WHEN** Henk sends a reply, a triage message, a reminder or a notice
- **THEN** the send does not use styled text mode

### Requirement: Inbound reactions are normalised and bound to Henk's own messages
The Signal adapter SHALL convert a reaction envelope into a channel-neutral reaction carrying the sender, the emoji, whether it is a removal, whether it came from a group, and a target reference. The target reference SHALL be minted only when the reaction's target author is the configured Henk account, from the target message's sent timestamp, in the same form `send_prompt` returns; otherwise it SHALL be absent. Reaction envelopes SHALL never be converted into inbound messages or reach the agent core.

#### Scenario: Owner reacts to Henk's prompt
- **WHEN** the owner reacts 👍 to a message Henk sent with sent timestamp T
- **THEN** the adapter yields a reaction whose target reference equals the prompt reference `send_prompt` returned for T

#### Scenario: Reaction on the owner's own message
- **WHEN** the owner reacts to a message the owner sent
- **THEN** the yielded reaction carries no target reference, and the gate ignores it

#### Scenario: Prompt edit addresses the prompt
- **WHEN** the gate edits the prompt with reference for sent timestamp T
- **THEN** the bridge receives one send to the owner with the edit timestamp T and the outcome text
