# agent-core Specification (delta)

## MODIFIED Requirements

### Requirement: Inbound message becomes an agent turn
The agent core SHALL run each inbound owner message as a turn of a Claude Agent SDK session and deliver the agent's final text response back through the channel adapter. Intermediate tool activity SHALL NOT be sent as separate chat messages in v1.

For owner agent turns, the application SHALL use the ending precedence in the incident-triage requirement "An incomplete triage is reported, never silent". When an error signal outranks a refusal, the application SHALL discard the agent's reply text, send the generic error reply, and record the outcome as `error`. A refusal without an error SHALL retain the model's reply text; an empty reply without an error or refusal SHALL send no message.

#### Scenario: Simple question answered
- **WHEN** the owner sends "is everything up?"
- **THEN** the agent runs a turn (invoking read-only tools as needed) and the owner receives a single reply message with the answer

#### Scenario: Agent turn fails
- **WHEN** the Agent SDK call fails (API error, credit pool exhausted, timeout)
- **THEN** the owner receives a short error message stating the failure honestly, and the process remains alive for the next message

#### Scenario: An owner-turn error outranks a refusal
- **WHEN** an owner agent turn reports both a refusal and a result-level error
- **THEN** the owner receives the generic error reply instead of the model's text, and the recorded outcome is `error`

### Requirement: Owner agent turns are bracketed by the working indicator
When owner acknowledgement is enabled, the agent core SHALL start the channel adapter's working indicator for every owner **agent turn** and SHALL stop it when the turn ends. It SHALL do so through one asynchronous bracket with the same try/finally discipline the gate-framing context manager already establishes, so that no exit path escapes it and the indicator's refresh can never outlive the turn that started it.

The bracket SHALL open after `/new` and app-side command dispatch and before the session is ensured. It SHALL enclose session setup, turn composition, the gate-framed agent turn, and the send of the reply or error reply. It SHALL close only after that send has been attempted, because the owner's wait ends when the reply lands, not when the model finishes. Opening the bracket SHALL issue the indicator's start before the session receives the turn's content, and SHALL NOT wait on the start's response.

On every normal exit path — a delivered reply, an empty reply, an agent error with its error reply, or an exception from session setup — the indicator SHALL be stopped. If session setup raises, the agent core SHALL send the generic error reply to the owner before stopping the indicator and SHALL NOT send the exception text. On **cancellation** (shutdown), including a cancellation delivered while the bracket is being entered, the indicator task SHALL be cancelled and awaited, no stop SHALL be attempted over the network, and the cancellation SHALL propagate out of the turn rather than being absorbed; the channel's client-side expiry clears the indicator (channel-adapter spec). While an approval prompt raised inside the turn is pending, the indicator SHALL be suspended and SHALL resume when the approval resolves (channel-adapter spec).

Owner **command** turns SHALL NOT be bracketed: they are deterministic and instant, and an indicator that appears and clears within one round trip is noise. **Event turns** are bracketed only as "Sent event triages are bracketed by the working indicator" specifies. A failure to start, refresh or stop the indicator SHALL be logged and SHALL NOT affect the turn (channel-adapter spec). When acknowledgement is disabled, no working indicator SHALL be started; owner-turn error replies and ending classification SHALL apply in either mode.

#### Scenario: Indicator brackets a normal owner turn
- **WHEN** an owner message runs as an agent turn and the reply is delivered
- **THEN** the working indicator was started before the session received the turn's content and stopped after the reply was sent

#### Scenario: Indicator clears on an errored turn
- **WHEN** the agent turn raises (API error, timeout, credit exhaustion) and the owner receives the error reply
- **THEN** the working indicator is stopped after the error reply is sent, not left running

#### Scenario: Indicator clears when session setup fails
- **WHEN** ensuring the session raises before the agent turn runs
- **THEN** the owner receives the generic error reply rather than exception text, and the working indicator is stopped after the reply is sent so its refresh does not outlive the failed turn

#### Scenario: Indicator clears on an empty reply
- **WHEN** the agent turn completes with no reply text
- **THEN** the working indicator is stopped and no message is sent

#### Scenario: Cancellation leaves no refresh running and sends no stop
- **WHEN** the core worker is cancelled while an owner turn is running with the indicator up
- **THEN** the cancellation propagates out of the core worker, the indicator task has finished, and no stop request was attempted

#### Scenario: A hung stop does not hold the next turn beyond the bound
- **WHEN** the channel never answers the indicator's stop at the end of an owner turn and another owner message is queued
- **THEN** the reply has already been delivered, and the queued turn starts no later than the acknowledge timeout after the first turn's bracket began to close

#### Scenario: Commands are not bracketed
- **WHEN** the owner sends `/memories` and it is handled app-side without an agent turn
- **THEN** no working indicator is started

#### Scenario: Reset is not bracketed
- **WHEN** the owner sends `/new`
- **THEN** no working indicator is started and the reset confirmation is sent as before

#### Scenario: Disabled acknowledgement changes only the indicator
- **WHEN** acknowledgement is disabled and an owner message runs as an agent turn
- **THEN** no working indicator is started, and the content sent to the session and the reply sent to the owner follow the same rules as when acknowledgement is enabled
