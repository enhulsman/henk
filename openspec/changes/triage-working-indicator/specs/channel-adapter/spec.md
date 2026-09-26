# channel-adapter Specification (delta)

## MODIFIED Requirements

### Requirement: Owner-only acknowledgement of inbound messages
When owner acknowledgement is enabled (`signal.acknowledge_owner`, default **true**; an absent key SHALL mean enabled), Henk SHALL send the owner a **read receipt** for every inbound message that passes the owner-only allowlist, whether it is queued as a turn or routed to a pending approval, and SHALL show a **working indicator** for the duration of every owner agent turn and of every event turn whose output will be sent to the owner (agent-core spec). Neither SHALL be sent for any message that has not passed the allowlist.

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

#### Scenario: A triage the owner will receive shows the indicator
- **WHEN** acknowledgement is enabled and an event triage whose output will be sent to the owner runs
- **THEN** the working indicator is shown from the start of the turn until after the triage message or incomplete-triage notice is sent, with the same refresh, bounds and cancellation as an owner turn

#### Scenario: A triage the owner will not receive shows nothing
- **WHEN** an event triage has been held back by the alert cap
- **THEN** no working indicator request reaches the bridge for it

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
