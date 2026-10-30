## ADDED Requirements

### Requirement: Events published inside an open maintenance window are held
When the maintenance-window provider reports hold windows (maintenance-calendar spec), every event in a debounced batch SHALL be classified, in this order, before the cooldown, recurrence and cap policy:

1. An event whose id is already stored as held SHALL be dropped as a duplicate: no record and no triage.
2. The event SHALL be tested at its **publish time**: the ntfy frame's `time` field, or the event's arrival time when the frame has none.
3. A window active at that instant is **open** unless it has already been released or its release is in flight. An event with at least one open active window SHALL be held against the open window with the latest end. All other events are processed exactly as before.

Holding an event SHALL store its id, title, message, raw payload, identity key, state, publish time and arrival time, together with the window's id, title, start and end as known when the window's first event was held. It SHALL then write a `hold` audit record. If the held row cannot be stored, the event SHALL instead be processed as if no window existed (fail open). If only the `hold` record fails, the failure is logged and the hold stands. A held event SHALL NOT start a triage session, SHALL NOT cause a channel send, and SHALL NOT arm cooldown or count against the cap.

The classification SHALL always pass the remaining events, possibly none, to the pipeline. A batch whose events are all duplicates, held or suppressed SHALL therefore advance the intake checkpoint through the existing checkpoint marker, once every held row of the batch is stored.

#### Scenario: Alert during a window is held silently
- **WHEN** a Gatus `firing` event is published at 20:40 inside an open 20:00–22:00 window
- **THEN** no triage session starts, no Signal message is sent, a held row and a `hold` record exist for it, and no cooldown is armed for its identity

#### Scenario: Publish time decides after a reboot
- **WHEN** an event published at 21:50 inside a 20:00–22:00 window is first delivered to Henk at 22:45, from the ntfy backlog after a restart, before that window was released
- **THEN** it is held, not triaged

#### Scenario: Mixed batch splits
- **WHEN** one debounced batch contains an event published inside an open window and one published before the window started
- **THEN** the first is held, and the second goes through cooldown, recurrence and the cap as before

#### Scenario: Held-only batch advances the checkpoint
- **WHEN** every event of a batch is held and its held rows are stored
- **THEN** the intake checkpoint advances past the batch's last event

#### Scenario: A hold that cannot be stored fails open
- **WHEN** storing the held row for an event inside an open window fails
- **THEN** the event is processed by cooldown, recurrence and the cap as if no window existed

#### Scenario: Duplicate replay is dropped and still advances
- **WHEN** an already-held event is delivered again in a batch of its own, whether or not its window has since been released
- **THEN** no second row, `hold` record or triage results, and the intake checkpoint advances past it

#### Scenario: Late event for a released window is triaged live
- **WHEN** an event published inside W arrives after W was released
- **THEN** it goes through the normal pipeline, W is not released again, and no second `hold-release` record is written

#### Scenario: Event during an in-flight release is triaged live
- **WHEN** an event published inside W is dispatched while W's release turn is submitted but not yet durable
- **THEN** it goes through the normal pipeline, and W is released exactly once

### Requirement: Held events are released after the window, the grace, and intake catch-up
Release SHALL be evaluated at startup and at least every 60 seconds, whenever held events are unreleased, whatever `calendar.enabled` says.

**Caught up.** Intake is caught up when all of these hold:
- a `keepalive` frame has arrived since the latest subscribe (tracked separately from proof of life, which also counts message frames);
- the queue between intake and the debouncer is empty;
- no debounce batch is open;
- no batch is being dispatched, counting from the moment a batch leaves the debouncer until its dispatch returns or raises.

**Eligible.** A window is eligible for release when all of these hold:
- it has unreleased held events and is not in flight;
- its stored end plus `calendar.grace_minutes` has passed;
- intake is caught up, or 15 minutes have passed since the later of that instant and this process's first subscribe attempt.

**Extension.** While the calendar is enabled, a window whose id is in the current snapshot, is active now, and started at or before its earliest held publish time SHALL have its stored end updated and its release deferred. A window moved elsewhere SHALL be released at its stored end.

**Release.** The held events are grouped by identity. An identity whose latest held event is `resolved` counts as recovered. Every other identity SHALL be forwarded with its latest non-resolved event, rebuilt from the stored fields. Forwarded events SHALL be evaluated as one batch by the normal pipeline without advancing the intake checkpoint. A forwarded identity the pipeline cools down SHALL get a `suppression` record exactly as in live dispatch. A resulting event turn SHALL carry the window's id, title, start, end plus grace, the number of distinct held identities, the number recovered, and the hold's age, and the window SHALL be in flight from submission until it is marked released.

**Marking released.** The window SHALL be marked released, and its single `hold-release` record written, only after that turn's triage audit record was written successfully, or at once when audit is not configured. A release producing no turn SHALL mark itself immediately. A triage whose audit write fails, or whose session cannot start, SHALL leave the window unreleased; it is released again after the next restart. When every held identity recovered, no turn SHALL be submitted and no channel message SHALL be sent.

#### Scenario: Recovered alerts send nothing
- **WHEN** a window held a `firing` and then a `resolved` event for the same identity, and it becomes eligible
- **THEN** no event turn is submitted, no Signal message is sent, and a `hold-release` record shows 1 held, 1 recovered, 0 forwarded

#### Scenario: Still-firing alert is triaged after the window
- **WHEN** a window held a `firing` event for identity A, and `firing` then `resolved` events for identity B, and it becomes eligible
- **THEN** exactly one event turn is submitted, containing only identity A and carrying the window's title, 2 held and 1 recovered

#### Scenario: Release waits for the backlog after a long outage
- **WHEN** Henk is down from 21:50 to 23:45 across a window whose end plus grace is 22:30, and the ntfy backlog holds identity A's `resolved` published at 22:10
- **THEN** exactly one release happens, after the backlog has been dispatched, and it counts A as recovered and triages nothing

#### Scenario: Message frames alone do not mean caught up
- **WHEN** intake has delivered cached message frames after a subscribe but no keepalive yet
- **THEN** intake is not caught up and no window is released

#### Scenario: Dispatch in progress blocks release
- **WHEN** a release tick runs while a batch's dispatch is suspended on its submission to the core
- **THEN** intake is not caught up and no window is released on that tick

#### Scenario: Unreachable ntfy cannot hold alerts forever
- **WHEN** a window's end plus grace has passed, Henk has been subscribing for 15 minutes, and no keepalive has arrived
- **THEN** the window is released

#### Scenario: Crash before the triage is durable releases again
- **WHEN** a release turn is submitted and the process is killed before that triage's audit record is written
- **THEN** after restart the window is still unreleased and is released again, and the intake checkpoint is the same as before the first release

#### Scenario: Release survives a restart
- **WHEN** Henk is down when a window's end plus grace passes and starts again later
- **THEN** the window is released once intake has caught up

#### Scenario: Extended window defers release
- **WHEN** the owner extends a running window's end from 22:00 to 23:00, the next refresh sees it, and the time is 22:30
- **THEN** nothing is released until 23:30 at the earliest

#### Scenario: Moved window is not an extension
- **WHEN** the owner moves a running 20:00–22:00 window to next week before 22:30
- **THEN** its held events are released at 22:30 plus catch-up, not next week

#### Scenario: Release obeys cooldown and the daily cap
- **WHEN** a release forwards one identity that is cooling down and one that survives while the daily cap is spent
- **THEN** the first gets a `suppression` record and the second is triaged as non-announceable, exactly like live events

#### Scenario: Release runs with the calendar disabled
- **WHEN** events are held, the owner sets `calendar.enabled` false and restarts, and the stored end plus grace passes
- **THEN** the window is released as usual
