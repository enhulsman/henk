## ADDED Requirements

### Requirement: A subscription delivering no proof-of-life frame is treated as failed
Intake SHALL treat the absence of a **proof-of-life frame** as evidence that the subscription
is dead rather than idle, and SHALL key all four of its liveness consumers on that single
unit: the deadline, the backoff-penalty reset, the last-proof-of-life timestamp, and the
termination rule.

A **proof-of-life frame** is any frame whose `event` is not `open`. An `open` frame proves a
connection was accepted; it does not prove the stream is delivering.

When no proof-of-life frame arrives within the configured deadline, intake SHALL abandon the
connection and reconnect through the existing backoff and `since` resume path, so a liveness
trip is indistinguishable downstream from any other transport failure and cannot lose events.

A connection that ends **without** having delivered a proof-of-life frame SHALL be treated as
a failure whether it ended cleanly or with an error, and SHALL therefore take the escalating
backoff path. A proof-of-life frame SHALL reset that penalty. This single rule requires no
per-connection state: a healthy stream's keepalives zero the penalty, so a clean end after a
healthy period still costs only the base delay, while a connection that opens and then
delivers nothing escalates and becomes visible instead of retrying at a fixed interval
forever.

#### Scenario: Silent stream is abandoned
- **WHEN** a subscription delivers no proof-of-life frame for longer than the liveness deadline
- **THEN** intake abandons that connection and reconnects, resuming from the last-seen id

#### Scenario: Keepalive frames alone keep a quiet subscription healthy
- **WHEN** a subscription delivers only `keepalive` frames and no events for a period longer
  than the liveness deadline
- **THEN** intake treats the subscription as healthy, does not reconnect, and does not
  accumulate a backoff penalty

#### Scenario: A connection that opens and then ends without delivering escalates
- **WHEN** consecutive connections each deliver an `open` frame and then end, cleanly or
  otherwise, without delivering any proof-of-life frame
- **THEN** the reconnect delay escalates across those attempts rather than repeating a fixed
  delay, and the last-proof-of-life timestamp goes stale

#### Scenario: A clean end after a healthy period costs only the base delay
- **WHEN** a subscription delivers proof-of-life frames and then ends cleanly
- **THEN** the reconnect waits the base backoff delay, unchanged from the behaviour before
  this requirement existed

### Requirement: The liveness deadline is ordered against the server keepalive interval
The configured liveness deadline SHALL be a multiple greater than one of the ntfy server's
recorded `keepalive-interval`, so that a healthy but event-free subscription cannot trip it.
Configuration that violates this ordering SHALL be rejected at load time rather than
producing a watchdog that reconnects on a perfectly healthy stream. The recorded server
interval SHALL be carried in configuration alongside the deadline that derives from it, so
the ordering is checkable rather than implicit, and SHALL be validated after configuration
assembly because the two values do not necessarily live in the same section.

#### Scenario: Deadline exceeds the server keepalive interval
- **WHEN** the configured liveness deadline is compared against the recorded
  `keepalive-interval`
- **THEN** the deadline is a multiple greater than one of that interval

#### Scenario: A deadline below the keepalive interval is refused
- **WHEN** configuration sets a liveness deadline at or below the recorded server
  `keepalive-interval`
- **THEN** loading that configuration fails with an error naming both values

### Requirement: Intake liveness state is observable
Intake SHALL expose, through a named accessor rather than log archaeology, when it last
received a proof-of-life frame, when it last reconnected, and its current backoff penalty, so
that a quiet period can be verified as genuine silence rather than assumed to be. A conclusion
drawn from an absence of events SHALL be supportable from this state without manual inspection
of the subscription.

Because a state-change-only log leaves a healthy stream emitting nothing to read, intake SHALL
additionally emit a one-shot line when the first proof-of-life frame of a process arrives, and
a periodic line thereafter at an interval coarse enough that a healthy day produces a handful
of lines rather than one per frame.

#### Scenario: Quiet period is verifiable
- **WHEN** no events have been received for an extended period and the liveness accessor is
  read
- **THEN** the last-proof-of-life and last-reconnect timestamps show whether the subscription
  was continuously alive throughout that period

#### Scenario: A healthy stream is readable at deploy time
- **WHEN** intake starts and receives its first proof-of-life frame, then continues healthily
- **THEN** a startup line records that first frame and periodic lines confirm continued
  delivery, without one line per frame
