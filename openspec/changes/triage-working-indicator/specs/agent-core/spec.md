# agent-core Specification (delta)

## MODIFIED Requirements

### Requirement: Owner agent turns are bracketed by the working indicator
When owner acknowledgement is enabled, the agent core SHALL start the channel adapter's working indicator for every owner **agent turn** and SHALL stop it when the turn ends. It SHALL do so through one asynchronous bracket with the same try/finally discipline the gate-framing context manager already establishes, so that no exit path escapes it and the indicator's refresh can never outlive the turn that started it.

The bracket SHALL open after `/new` and app-side command dispatch and before the session is ensured. It SHALL enclose session setup, turn composition, the gate-framed agent turn, and the send of the reply or error reply. It SHALL close only after that send has been attempted, because the owner's wait ends when the reply lands, not when the model finishes. Opening the bracket SHALL issue the indicator's start before the session receives the turn's content, and SHALL NOT wait on the start's response.

On every normal exit path — a delivered reply, an empty reply, an agent error with its error reply, or an exception from session setup — the indicator SHALL be stopped. On **cancellation** (shutdown), including a cancellation delivered while the bracket is being entered, the indicator task SHALL be cancelled and awaited, no stop SHALL be attempted over the network, and the cancellation SHALL propagate out of the turn rather than being absorbed; the channel's client-side expiry clears the indicator (channel-adapter spec). While an approval prompt raised inside the turn is pending, the indicator SHALL be suspended and SHALL resume when the approval resolves (channel-adapter spec).

Owner **command** turns SHALL NOT be bracketed: they are deterministic and instant, and an indicator that appears and clears within one round trip is noise. **Event turns** are bracketed only as "Sent event triages are bracketed by the working indicator" specifies. A failure to start, refresh or stop the indicator SHALL be logged and SHALL NOT affect the turn (channel-adapter spec). When acknowledgement is disabled, owner-turn processing SHALL be unchanged from before this change.

#### Scenario: Indicator brackets a normal owner turn
- **WHEN** an owner message runs as an agent turn and the reply is delivered
- **THEN** the working indicator was started before the session received the turn's content and stopped after the reply was sent

#### Scenario: Indicator clears on an errored turn
- **WHEN** the agent turn raises (API error, timeout, credit exhaustion) and the owner receives the error reply
- **THEN** the working indicator is stopped after the error reply is sent, not left running

#### Scenario: Indicator clears when session setup fails
- **WHEN** ensuring the session raises before the agent turn runs
- **THEN** the working indicator is stopped and its refresh does not outlive the failed turn

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

#### Scenario: Disabled acknowledgement leaves owner turns unchanged
- **WHEN** acknowledgement is disabled and an owner message runs as an agent turn
- **THEN** no working indicator is started, and the content sent to the session and the reply sent to the owner are identical to before this change

## ADDED Requirements

### Requirement: Sent event triages are bracketed by the working indicator
When owner acknowledgement is enabled, the agent core SHALL show the channel adapter's working indicator for every **event turn whose output will be sent to the owner** — an announceable turn, as decided by the alert cap before the turn is queued — and SHALL NOT show it for a turn the cap has made non-announceable. The owner is waiting on such a triage: the external alert has already said one is coming. A cap-suppressed triage produces no Signal message, so an indicator there would promise a message that never comes.

The bracket SHALL be the same asynchronous bracket owner agent turns use, with the same discipline, bounds, cancellation and approval-pause behaviour (see "Owner agent turns are bracketed by the working indicator" and the channel-adapter spec). It SHALL open before the incident session is started and SHALL close only after the proactive send has been attempted: the triage message for a completed turn, or the incomplete-triage notice for an errored one, since either is the message the owner is waiting on. Opening it SHALL issue the indicator's start before the session receives the turn's content, and SHALL NOT wait on the start's response.

On every normal exit path — a completed triage and its send, an errored triage and its notice, or an exception from starting the incident session — the indicator SHALL be stopped. On cancellation, the indicator task SHALL be cancelled and awaited, no stop SHALL be attempted over the network, and the cancellation SHALL propagate out of the turn. A cap-suppressed event turn, and every event turn when acknowledgement is disabled, SHALL be processed exactly as before this change.

#### Scenario: A sent triage is bracketed
- **WHEN** an announceable event turn completes and its triage message is sent
- **THEN** the working indicator was started before the session received the turn's content and stopped after the triage message was sent

#### Scenario: An errored sent triage clears after its notice
- **WHEN** an announceable event turn raises and the incomplete-triage notice is sent
- **THEN** the working indicator is stopped after the notice was sent, not left running

#### Scenario: A cap-suppressed triage shows no indicator
- **WHEN** a non-announceable event turn is processed
- **THEN** no working indicator is started, the triage and its audit record are produced as before, and nothing is sent to the owner

#### Scenario: Cancellation during a sent triage sends no stop
- **WHEN** the core worker is cancelled while an announceable event turn is running with the indicator up
- **THEN** the cancellation propagates out of the core worker, the indicator task has finished, and no stop request was attempted

#### Scenario: A hung stop after a triage does not hold the next turn beyond the bound
- **WHEN** the channel never answers the indicator's stop at the end of a sent triage and another turn is queued
- **THEN** the triage message has already been sent, and the queued turn starts no later than the acknowledge timeout after the bracket began to close

#### Scenario: Disabled acknowledgement leaves event turns unchanged
- **WHEN** acknowledgement is disabled and an announceable event turn runs
- **THEN** no working indicator is started, and the content sent to the session and the message sent to the owner are identical to before this change
