## ADDED Requirements

### Requirement: A subscription delivering no frames is treated as failed
Intake SHALL treat the absence of *any* frame, including ntfy `keepalive` control frames, as
evidence that the subscription is dead rather than idle. A liveness deadline SHALL be
configured as a multiple of the ntfy server's `keepalive-interval` so that a healthy but
event-free subscription cannot trip it, and the configured deadline SHALL be recorded
alongside the measured server interval it derives from. When no frame arrives within the
deadline, intake SHALL abandon the connection and reconnect through the existing backoff and
`since` resume path, so a liveness trip is indistinguishable downstream from any other
transport failure and cannot lose events.

#### Scenario: Silent stream is abandoned
- **WHEN** a subscription delivers no frame of any kind for longer than the liveness deadline
- **THEN** intake abandons that connection and reconnects, resuming from the last-seen id

#### Scenario: Keepalive frames alone keep a quiet subscription healthy
- **WHEN** a subscription delivers only `keepalive` control frames and no events for a period
  longer than the liveness deadline
- **THEN** intake treats the subscription as healthy and does not reconnect

#### Scenario: Deadline exceeds the server keepalive interval
- **WHEN** the configured liveness deadline is compared against the ntfy server's
  `keepalive-interval`
- **THEN** the deadline is a multiple greater than one of that interval

### Requirement: Persistent liveness failure notifies the owner exactly once
A single liveness trip SHALL recover silently, logged but not announced, because an isolated
trip is an expected consequence of network re-keying or a sensor-side restart and announcing
each one would convert a self-healing event into channel noise. When consecutive trips
without an intervening healthy interval exceed a bounded threshold, intake SHALL send exactly
one owner notice and SHALL NOT repeat it while the condition persists, mirroring the bounded
one-shot notice already used for checkpoint rejection.

#### Scenario: Single trip recovers quietly
- **WHEN** one liveness trip occurs and the following reconnect receives frames normally
- **THEN** the trip is logged, no owner notice is sent, and intake continues

#### Scenario: Repeated trips notify once
- **WHEN** consecutive liveness trips without an intervening healthy interval exceed the
  bounded threshold
- **THEN** exactly one owner notice is sent, and further trips while the condition persists
  send none

### Requirement: Intake liveness state is observable
Intake SHALL expose when it last received a frame and when it last reconnected, so that a
quiet period can be verified as genuine silence rather than assumed to be. A conclusion drawn
from an absence of events SHALL be supportable from this state without manual inspection of
the subscription.

#### Scenario: Quiet period is verifiable
- **WHEN** no events have been received for an extended period and intake state is inspected
- **THEN** the last-frame and last-reconnect timestamps show whether the subscription was
  continuously alive throughout that period
