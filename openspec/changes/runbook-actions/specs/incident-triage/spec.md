## MODIFIED Requirements

### Requirement: Triage stays inside the read-only toolset
Event-triggered sessions SHALL have exactly the same registered toolset and structural boundaries as owner-triggered sessions. A mutating tool executes during a triage only if its declared turn scope includes event turns (approval-gate spec, "Mutating tools declare a turn scope, enforced per session"). The one such tool is `restart_container`, and only for a standing target bound to the triggering alert (runbook-actions spec); every other triage tool *execution* remains read-only or notify-class. Any other mutating invocation attempted during an event turn or in a tainted session (except a tool that declares `tainted-owner`, in an owner turn), and any runbook invocation for an unbound target, is denied by the gate's turn-scope enforcement, silently and fail-closed, with an `out-of-scope` receipt. The triage arc is composed in full whether or not an action ran, because the message may be sent.

#### Scenario: Triage tool calls audited
- **WHEN** a triage session completes and its audit record is inspected
- **THEN** every tool call that executed is a registered read-only or notify-class tool, or a `restart_container` invocation for a target bound to the triggering alert

#### Scenario: Mutating attempt during triage denied with a receipt
- **WHEN** the agent attempts a mutating tool during an event-triage turn
- **THEN** the invocation is denied without any channel message, the audit record shows only read-only/notify executions, and an `out-of-scope` authorization record exists for the attempt

#### Scenario: Bound standing restart during triage is the only mutation
- **WHEN** a triage bound to `cadvisor` invokes `restart_container` for `cadvisor` and then `store_memory`
- **THEN** the restart is published with an `authorized` receipt, and `store_memory` is denied with outcome `out-of-scope`

### Requirement: Every triageable event becomes a triage session
Every triageable event SHALL start an agent triage session, regardless of the cadence cap.
An event that survives debounce and cooldown is a **triageable event**. The session
includes evidence gathering via registered read-only tools and handoff publication. A
triageable incident that is also within the daily cadence cap is an **announceable
incident**, and SHALL additionally deliver a proactive Signal message to the owner, with one
exception: an announceable triage whose every item was fixed by a bound standing runbook
action and verified by its RESOLVED event is dropped without a send (runbook-actions spec,
"Silence after a standing fix requires a resolved event for every item"); it still runs in
full, publishes its handoff, and writes its audit record.
Cap-overflow incidents run their full triage session with Signal delivery suppressed.

Recurrence detection SHALL be reconstructed from the persisted audit log on startup, so
recurrence framing survives a restart. Recurrence detection means two things: whether an
identity was triaged within the recurrence window, and the prior handoff reference used
for recurrence framing.

**A recurrence SHALL carry the prior handoff's content, not only its reference.** When
the prior handoff is retained locally (triage-handoff spec), its content SHALL be placed
inside the untrusted-data block, marked as the recurrence reference. The recurrence note
in the framing SHALL point at it without reproducing it. When the prior handoff is not
retained, the note SHALL say that its content is not available locally, and SHALL NOT
present the reference as though its content were known.

#### Scenario: Curated alert triaged end to end
- **WHEN** a `HealthEtl*` event arrives, survives debounce and cooldown, and the cap is not reached
- **THEN** a triage session runs and the owner receives an unprompted Signal message naming the alert with Henk's initial assessment

#### Scenario: Cap-exceeded incident still triaged and handed off
- **WHEN** the daily cap is already reached and a triageable event arrives
- **THEN** a triage session still runs, its handoff document is published, its audit record exists, and no Signal message is sent

#### Scenario: Recurrence of a recently triaged incident
- **WHEN** a triageable event's alert identity was already triaged within the configured recurrence window and that triage's handoff is retained locally
- **THEN** the triage session is framed as a recurrence, the earlier handoff's content is inside the untrusted-data block marked as the recurrence reference, and the framing tells the agent to note the recurrence and build on that handoff instead of re-running full evidence gathering

#### Scenario: Recurrence whose prior handoff is not retained
- **WHEN** an identity recurs within the recurrence window but its prior handoff reference does not resolve to a retained handoff
- **THEN** the recurrence note names the reference and states that its content is not available locally, and no handoff content is presented for it

#### Scenario: Recurrence framing survives a restart
- **WHEN** an identity was triaged, the process restarts, and the same identity re-fires within the recurrence window (but past cooldown)
- **THEN** the new triage is framed as a recurrence, references the prior handoff reconstructed from the audit log, and carries that handoff's retained content

#### Scenario: Verified standing fix is triaged and handed off but not sent
- **WHEN** an announceable incident's only item is bound, restarted with result `ok`, and its RESOLVED event arrives within the verify window
- **THEN** the triage session ran, its handoff is published and records the restart, its audit record exists, and no Signal message is sent

### Requirement: Cadence is condition-triggered with a hard cap on announcements
Unprompted Signal messages SHALL be sent only for announceable incidents — never on a timer, and no system-scheduled digest, heartbeat, or "all is well" message SHALL exist. **Owner-scheduled reminder delivery is the one and only exception, and it is not a timer in this sense**: a reminder message exists because the owner asked for that message, at that time, in their own words (reminders spec), so it is owner-initiated content whose delivery moment happens to be deferred. Reminder deliveries and their catch-up summaries SHALL NOT consume the announceable-incident cap, SHALL NOT be generated by Henk on his own initiative, and are bounded instead by the reminders capability's own pending cap. The unprompted-message classes are therefore exactly two — announceable incidents and owner-scheduled reminder deliveries — and nothing else. Announceable incidents SHALL be limited by a configured hard cap per 24 hours; triageable incidents beyond the cap are suppressed from Signal only (their triage session, audit record, and handoff still occur), and the next announceable message that is sent SHALL note how many incidents were suppressed. An announceable triage dropped as a verified standing fix (runbook-actions spec) SHALL give back its cap slot, SHALL return its suppressed count to the pending count so the next sent message reports it, and SHALL leave each of its identities' cooldown as it was before that triage, so a relapse is triaged and not suppressed; the dropped triage still counts for recurrence, so the relapse is framed as one. The RESOLVED event that verified the fix is consumed and starts no triage. A dropped triage is not a new message class: nothing is sent for it. Mutating invocations are the one exception to "Signal only": during a suppressed triage they fail closed silently per the approval-gate spec — a suppressed incident can never place an approval prompt (a context-free owner interruption) on the channel. The cap bounds unprompted-message volume, not token spend — token spend is bounded upstream by the curated source list, debounce, and cooldown. **The cadence cap window SHALL survive a process restart**: on startup the count of announceable incidents within the current cap window SHALL be reconstructed from the persisted audit log, excluding triages with an explicit drop record (runbook-actions), so a restart does not reset the cap and allow the owner's cadence constraint to be exceeded.

#### Scenario: Quiet homelab means silence
- **WHEN** no triageable event occurs for a week and no reminder is scheduled
- **THEN** Henk sends zero unprompted messages

#### Scenario: No system-scheduled message exists
- **WHEN** the process runs for a week with reminders enabled, no reminder scheduled, and no triageable event
- **THEN** no digest, heartbeat, status, or "all is well" message is sent — the scheduler delivers only what the owner scheduled

#### Scenario: Reminder delivery does not consume the incident cap
- **WHEN** reminders are delivered on a day when the announceable-incident cap has not been reached
- **THEN** the number of incidents that may still be announced that day is unchanged

#### Scenario: Suppressed count surfaces later
- **WHEN** incidents were cap-suppressed and a new announceable incident occurs after the cap window allows
- **THEN** its Signal message mentions how many incidents were suppressed in the interim

#### Scenario: Cap holds across a restart
- **WHEN** the daily cap has already been reached, the process restarts, and a new triageable event arrives while still inside the cap window
- **THEN** the incident is triaged and handed off but no Signal message is sent, exactly as before the restart

#### Scenario: Suppressed triage cannot prompt
- **WHEN** the agent attempts a per-instance mutating tool during a cap-suppressed triage
- **THEN** no approval prompt or any other Signal message is sent, and the attempt is recorded with a fail-closed outcome

#### Scenario: A dropped fix gives back its cap slot
- **WHEN** the cap is 3, two incidents were announced today, and a third announceable incident is dropped as a verified standing fix
- **THEN** a fourth announceable incident later that day is still sent

#### Scenario: A relapse after a silent fix is triaged
- **WHEN** an identity's triage was dropped as a verified fix and the same identity fires again 2 hours later
- **THEN** the event is triaged rather than suppressed by cooldown

### Requirement: Every incident message ends with the triage arc
Every unprompted incident message composed by the model SHALL end with:
(a) a diagnosis with an explicit confidence level;
(b) a suggested fix;
(c) a pickup path telling the owner where to resume work, referencing the published
handoff.

AI labeling per the inherited posture applies. Arc compliance SHALL be checked by the
application layer after each triage turn and recorded in the audit record
(`triage_arc_complete`). A missing component SHALL NOT block delivery.

The application-authored incomplete-triage notice ("An incomplete triage is reported,
never silent") SHALL be exempt from the diagnosis and fix components, because no
diagnosis exists and a placeholder one would be false. It SHALL still end with a pickup
path. Its triage's record SHALL carry `triage_arc_complete: false`.

When the application appends notes to a triage message or to the notice, they SHALL come
after the pickup line in this order: the suppressed-incident note (cadence requirement),
then the runbook action lines, then the fixed-count note (runbook-actions spec). A message
whose arc is followed only by these application notes still ends with the triage arc for
the purpose of this requirement.

#### Scenario: Triage arc present
- **WHEN** any unprompted incident message composed by the model is delivered
- **THEN** it contains a diagnosis with confidence, a suggested fix, and a pickup path

#### Scenario: Arc component missing
- **WHEN** a triage turn produces a message lacking one of the three arc components
- **THEN** the message is still delivered and the session's audit record carries `triage_arc_complete: false`

#### Scenario: The suppressed-incident note may follow the arc
- **WHEN** an announceable triage message carries the arc and the application appends the suppressed-incident note
- **THEN** the note follows the `Pickup:` line, and the message still counts as ending with the triage arc

#### Scenario: The incomplete-triage notice carries a pickup path and no invented diagnosis
- **WHEN** the incomplete-triage notice is delivered
- **THEN** it ends with a `Pickup:` line, contains no `Diagnosis:` or `Fix:` line, and the triage's record carries `triage_arc_complete: false`

#### Scenario: Application notes follow the arc in a fixed order
- **WHEN** a triage message carries the suppressed-incident note, a runbook action line, and the fixed-count note
- **THEN** all three follow the `Pickup:` line in that order, and the message still counts as ending with the triage arc
