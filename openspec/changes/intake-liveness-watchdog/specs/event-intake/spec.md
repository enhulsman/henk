## ADDED Requirements

### Requirement: A subscription delivering no proof-of-life frame is treated as failed
Intake SHALL treat the absence of a **proof-of-life frame** as evidence that the subscription
is dead rather than idle, and SHALL key all four of its liveness consumers on that single
unit: the deadline, the backoff-penalty reset, the last-proof-of-life timestamp, and the
termination rule.

A **proof-of-life frame** is any frame whose `event` is not `open`. An `open` frame proves a
connection was accepted; it does not prove the stream is delivering. The definition is keyed on
the frame's type, not on its position relative to an `open` frame.

The deadline SHALL be measured as the budget remaining since the last proof-of-life frame, so
that frames which are not proof of life cannot extend it. The budget SHALL be re-established
immediately before each subscribe call, after any backoff delay, so that a reconnect begins
with a full window rather than inheriting an expired one. Time spent by the consumer of a
delivered event SHALL NOT be charged against the budget: liveness measures whether the stream
is delivering, not how fast Henk processes what it delivers.

When the budget is exhausted before a proof-of-life frame arrives, intake SHALL abandon the
connection and reconnect through the existing backoff and `since` resume path. A liveness trip
SHALL be handled by the same control flow as any other transport failure, so it cannot lose
events, and SHALL NOT be mistaken for a rejected resume point.

A connection that ends **without** having delivered a proof-of-life frame SHALL be treated as
a failure whether it ended cleanly or with an error, and SHALL therefore take the escalating
backoff path. A proof-of-life frame SHALL reset that penalty. This single rule requires no
per-connection state: a healthy stream's keepalives zero the penalty, so a clean end after a
healthy period still costs only the base delay, while a connection that opens and then delivers
nothing escalates and becomes visible instead of retrying at a fixed interval forever.

Control frames SHALL NOT advance the resume cursor. Liveness accounting SHALL read a frame's
`event` field only, so that an `id` carried on an `open` or `keepalive` frame cannot become a
resume point.

#### Scenario: Silent stream is abandoned
- **WHEN** a subscription delivers no proof-of-life frame before the budget is exhausted
- **THEN** intake abandons that connection and reconnects, resuming from the last-seen id

#### Scenario: Keepalive frames alone keep a quiet subscription healthy
- **WHEN** a subscription delivers only `keepalive` frames and no events across several
  consecutive budget windows
- **THEN** intake treats the subscription as healthy, does not reconnect, and does not
  accumulate a backoff penalty

#### Scenario: A stream delivering only open frames still trips
- **WHEN** a subscription delivers `open` frames repeatedly and no other frame, more often than
  the deadline
- **THEN** the budget is still exhausted and intake abandons the connection, because `open`
  frames do not extend it

#### Scenario: A connection that opens and then ends without delivering escalates
- **WHEN** consecutive connections each deliver an `open` frame and then end, cleanly or
  otherwise, without delivering any proof-of-life frame
- **THEN** the reconnect delay escalates across those attempts rather than repeating a fixed
  delay, and the last-proof-of-life timestamp goes stale

#### Scenario: A clean end after a healthy period costs only the base delay
- **WHEN** a subscription delivers proof-of-life frames and then ends cleanly
- **THEN** the reconnect waits the base backoff delay, and the penalty counter advances by one
  until the next proof-of-life frame zeroes it again

#### Scenario: A liveness trip does not kill intake
- **WHEN** a liveness trip occurs
- **THEN** intake reconnects and continues delivering subsequent events, rather than terminating

#### Scenario: A control frame's id is never used as a resume point
- **WHEN** a connection delivers `open` and `keepalive` frames that carry `id` values, and then
  drops before any message frame
- **THEN** the reconnect resumes from the last message id, or cold if there has been none

#### Scenario: Consumer latency does not trip the watchdog
- **WHEN** a subscription delivers proof-of-life frames on a healthy cadence but the consumer of
  each delivered event takes longer than the deadline to return
- **THEN** no liveness trip occurs

### Requirement: The liveness deadline is ordered against the server keepalive interval
The configured liveness deadline SHALL be at least a stated whole multiple, greater than one, of
the ntfy server's recorded `keepalive-interval`, so that a healthy but event-free subscription
cannot trip it and so that a single late keepalive cannot either. Configuration violating that
ordering SHALL be rejected at load time with an error naming both values.

The recorded server interval and the deadline SHALL live in configuration as distinct values —
the interval describing the server, the deadline describing Henk's policy — so the ordering is
checkable rather than implicit. Because they therefore occupy different configuration sections,
validation SHALL run after configuration assembly rather than inside either section's builder.

#### Scenario: Deadline is a permitted multiple of the server keepalive interval
- **WHEN** the configured liveness deadline is compared against the recorded `keepalive-interval`
- **THEN** the deadline is at least the required multiple of that interval

#### Scenario: A deadline below the required multiple is refused
- **WHEN** configuration sets a liveness deadline that is greater than the recorded interval but
  below the required multiple of it
- **THEN** loading that configuration fails with an error naming both values

### Requirement: Intake liveness is observable from outside the process
Intake SHALL make the state a liveness conclusion rests on readable from outside the process, so
that a quiet period can be verified as genuine silence rather than assumed to be.

Because a state-change-only log leaves a healthy stream emitting nothing to read, intake SHALL
emit a one-shot line when the first proof-of-life frame of a process arrives, and a periodic line
thereafter at a configured interval coarse enough that a healthy day produces a handful of lines
rather than one per frame. Each line SHALL carry the last-proof-of-life and last-reconnect
timestamps and the current backoff penalty.

A liveness trip SHALL emit a line that is stably identifiable, so that trip counts and inter-trip
intervals can be extracted later without depending on incidental message wording. Behavioural
indistinguishability from other transport failures is required; log indistinguishability is not.

Intake SHALL additionally expose the same state through a named accessor for use by tests and by
any future in-process reader. The accessor is not the owner-facing surface — the emissions above
are.

#### Scenario: A healthy stream is readable at deploy time
- **WHEN** intake starts, receives its first proof-of-life frame, and continues healthily
- **THEN** a startup line records that first frame and periodic lines confirm continued delivery,
  without one line per frame

#### Scenario: A quiet period is verifiable after the fact
- **WHEN** no events have been received for an extended period
- **THEN** the emitted lines show whether the subscription was continuously alive throughout that
  period, without inspecting the subscription by hand

#### Scenario: Trips are countable
- **WHEN** liveness trips have occurred over some window
- **THEN** their number and spacing can be recovered from the emitted lines by matching a stable
  identifier
