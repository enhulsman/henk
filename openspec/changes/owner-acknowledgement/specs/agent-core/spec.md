# agent-core Specification (delta)

## ADDED Requirements

### Requirement: Owner agent turns are bracketed by the working indicator
The agent core SHALL start the channel adapter's working indicator before running an owner **agent turn** and SHALL stop it on every exit path — a delivered reply, an agent error, or cancellation — using the same try/finally discipline the gate-framing context manager already establishes, so an indicator can never outlive the turn that started it. Owner **command** turns SHALL NOT be bracketed: they are deterministic and instant, and an indicator that appears and clears within one round trip is noise. **Event turns** SHALL NOT be bracketed: no owner is waiting on a conversation, and their output is a proactive send. A failure to start or stop the indicator SHALL be logged and SHALL NOT affect the turn (channel-adapter spec).

#### Scenario: Indicator brackets a normal owner turn
- **WHEN** an owner message runs as an agent turn and the reply is delivered
- **THEN** the working indicator was started before the turn and stopped after it

#### Scenario: Indicator clears on an errored turn
- **WHEN** the agent turn raises (API error, timeout, credit exhaustion) and the owner receives the error reply
- **THEN** the working indicator is stopped, not left running

#### Scenario: Commands are not bracketed
- **WHEN** the owner sends `/memories` and it is handled app-side without an agent turn
- **THEN** no working indicator is started

#### Scenario: Event turns are not bracketed
- **WHEN** an event-triage turn is processed
- **THEN** no working indicator is started, and the triage output is delivered by the proactive send path unchanged
