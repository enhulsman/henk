# incident-triage Specification (delta)

## MODIFIED Requirements

### Requirement: An incomplete triage is reported, never silent
After each triage turn, the application SHALL classify how the turn ended from the agent
SDK's structured signals and the CLI-rendered API error text heuristic. The first matching
check, in this order, decides the ending:
1. an assistant-message error in the turn: `error`;
2. a result message marked as an error, carrying an API error status, or reporting a terminal reason other than `completed`: `error`;
3. the turn raised: `error`;
4. reply text beginning with the CLI-rendered `API Error` prefix: `error`;
5. a refusal stop reason on an assistant or result message: `refused`;
6. empty reply text: `no-reply`;
7. otherwise: `completed`.

The ending SHALL be recorded as the triage record's outcome. For any ending other than
`completed`, the reply text SHALL be discarded and SHALL NOT be delivered. Text that the
agent SDK rendered for an error or a refusal is not a triage message.

For an **announceable** incident whose ending is not `completed`, the application SHALL
send a short application-authored notice on the proactive path, in place of the model's
message. The notice SHALL carry the suppressed-incident count exactly as a model-written
triage message does (cadence requirement). The notice:
- names the incident, bounded in length;
- states how the triage ended, including the SDK's closed assistant-error class and the
  HTTP status where the SDK reported them;
- states that no diagnosis was produced;
- ends with a `Pickup:` line naming `henk-pickup` if a handoff was published, and the
  audit record otherwise.

It SHALL NOT state a refusal category, because the agent SDK does not report one. The
notice is the honest form of the message the announceable incident was going to
produce; it is not an additional message class. A cap-suppressed incident SHALL remain
silent. Every ending other than `completed` SHALL set `triage_arc_complete: false`.

#### Scenario: An API error rendered as text is not delivered as the triage
- **WHEN** an announceable triage turn ends with an assistant-message error and reply text beginning "API Error"
- **THEN** the owner receives the incomplete-triage notice and not the API-error text, and the record's outcome is `error`

#### Scenario: A refusal produces an honest notice
- **WHEN** an announceable triage turn ends with a refusal stop reason
- **THEN** the owner receives one notice stating the model declined and that no diagnosis was produced, with no refusal category, and the record's outcome is `refused` with `triage_arc_complete: false`

#### Scenario: A result-level API error is reported with its status
- **WHEN** a triage turn's result message is marked as an error with an API error status of 529
- **THEN** the ending is `error`, and the announceable notice states the triage failed with HTTP 529

#### Scenario: A result-level error outranks a refusal
- **WHEN** a triage turn carries both a refusal stop reason and a result-level error
- **THEN** the ending is `error`, the reply text is discarded, and an announceable incident receives the incomplete-triage error notice

#### Scenario: An empty reply is no longer silent
- **WHEN** an announceable triage turn completes without any error signal and produces no reply text
- **THEN** the owner receives the incomplete-triage notice, and the record's outcome is `no-reply`

#### Scenario: An errored triage is reported
- **WHEN** an announceable triage turn raises an error
- **THEN** the owner receives the incomplete-triage notice stating the triage failed, and the record's outcome is `error`

#### Scenario: The notice carries the suppressed count
- **WHEN** the incomplete-triage notice is sent for an announceable incident while earlier incidents were cap-suppressed
- **THEN** the notice states how many incidents were suppressed, as a triage message would

#### Scenario: A suppressed incomplete triage stays silent
- **WHEN** a cap-suppressed triage turn ends without completing
- **THEN** no Signal message is sent, and the record carries the ending's outcome
