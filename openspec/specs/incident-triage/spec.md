# incident-triage Specification

## Purpose
Defines what happens when the homelab breaks: every triageable event gets a full investigation
and a durable handoff, the owner's attention is spent only within the cadence contract (hard cap,
suppression to the record — never to the inbox), and triage runs with read-only hands unless a
verb's declared scope says otherwise.
## Requirements
### Requirement: Every triageable event becomes a triage session
Every triageable event SHALL start an agent triage session, regardless of the cadence cap.
An event that survives debounce and cooldown is a **triageable event**. The session
includes evidence gathering via registered read-only tools and handoff publication. A
triageable incident that is also within the daily cadence cap is an **announceable
incident**, and SHALL additionally deliver a proactive Signal message to the owner.
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

When the application appends the suppressed-incident note (cadence requirement) to a
triage message or to the notice, that note SHALL come after the pickup line. A message
whose arc is followed only by that application note still ends with the triage arc for
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

### Requirement: Triage stays inside the read-only toolset
Event-triggered sessions SHALL have exactly the same registered toolset and structural boundaries as owner-triggered sessions. A mutating tool executes during a triage only if its declared turn scope includes event turns (approval-gate spec, "Mutating tools declare a turn scope, enforced per session"); no tool in this change declares event scope, so triage tool *executions* remain read-only or notify-class. A mutating invocation attempted during an event turn or in a tainted session is denied by the gate's turn-scope enforcement, silently and fail-closed, with an `out-of-scope` receipt.

#### Scenario: Triage tool calls audited
- **WHEN** a triage session completes and its audit record is inspected
- **THEN** every tool call that executed is a registered read-only or notify-class tool

#### Scenario: Mutating attempt during triage denied with a receipt
- **WHEN** the agent attempts a mutating tool during an event-triage turn
- **THEN** the invocation is denied without any channel message, the audit record shows only read-only/notify executions, and an `out-of-scope` authorization record exists for the attempt

### Requirement: Cadence is condition-triggered with a hard cap on announcements
Unprompted Signal messages SHALL be sent only for announceable incidents — never on a timer, and no system-scheduled digest, heartbeat, or "all is well" message SHALL exist. **Owner-scheduled reminder delivery is the one and only exception, and it is not a timer in this sense**: a reminder message exists because the owner asked for that message, at that time, in their own words (reminders spec), so it is owner-initiated content whose delivery moment happens to be deferred. Reminder deliveries and their catch-up summaries SHALL NOT consume the announceable-incident cap, SHALL NOT be generated by Henk on his own initiative, and are bounded instead by the reminders capability's own pending cap. The unprompted-message classes are therefore exactly two — announceable incidents and owner-scheduled reminder deliveries — and nothing else. Announceable incidents SHALL be limited by a configured hard cap per 24 hours; triageable incidents beyond the cap are suppressed from Signal only (their triage session, audit record, and handoff still occur), and the next announceable message SHALL note how many incidents were suppressed. Mutating invocations are the one exception to "Signal only": during a suppressed triage they fail closed silently per the approval-gate spec — a suppressed incident can never place an approval prompt (a context-free owner interruption) on the channel. The cap bounds unprompted-message volume, not token spend — token spend is bounded upstream by the curated source list, debounce, and cooldown. **The cadence cap window SHALL survive a process restart**: on startup the count of announceable incidents within the current cap window SHALL be reconstructed from the persisted audit log, so a restart does not reset the cap and allow the owner's cadence constraint to be exceeded.

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

### Requirement: Owner replies interrogate the triage session
An owner reply following a triage message SHALL continue the same agent session, so follow-up questions resolve against the incident context under the existing continuity, `/new`, and idle-expiry rules.

#### Scenario: Follow-up question about the incident
- **WHEN** the owner replies "what does the backup log say?" shortly after a triage message
- **THEN** the reply runs in the triage session and answers with that incident's context

### Requirement: Triage framing directs the method, not only the arc
The triage-mode framing SHALL direct the investigation's method as well as its output.
It SHALL instruct the agent to:
- determine **which condition of the rule fired, and when**, from the alert's values and
  its notification time, bearing in mind that the notification can come later than the
  condition's onset. When a rule has more than one branch, **check every branch's own
  measurement**, because an alert's value does not always identify the branch that
  fired;
- use, for each measurement, **the shortest available window that reaches back past the
  alert's notification time minus the rule's `for` window**, and read longer windows as
  context rather than as the incident. Every window ends at the time of the query, so a
  window matching the `for` alone would miss the alert's onset;
- name the responsible process, container, host systemd unit or node where a tool can
  show it;
- write **"evidence not available: <what>"** where the evidence is not available from any
  tool, instead of inferring it;
- treat remembered facts, when the turn carries them, as **context and not an override of
  what the evidence shows**;
- consult `homelab_docs` before diagnosing, **only when that tool is registered** in the
  session.

The framing SHALL name no tool the session does not have. It SHALL keep the existing arc
mandate: the `Diagnosis:` line with confidence, `Fix:`, and `Pickup:`, each on its own
line, checked by the application after the turn. It SHALL keep the handoff instruction,
which SHALL ask for the time each evidence figure refers to. The framing SHALL sit after
the untrusted-data block. It is defence-in-depth only: the closed-toolset hook and the
approval gate remain the boundary, whatever the framing says.

#### Scenario: Framing directs branches, window, and missing evidence
- **WHEN** an event turn is composed
- **THEN** the framing instructs the agent to check every branch's measurement, to use the shortest window reaching back past the notification time minus the rule's `for`, and to write "evidence not available: <what>" rather than infer missing evidence

#### Scenario: The handoff instruction asks for times
- **WHEN** an event turn is composed
- **THEN** the handoff instruction asks for the time each evidence figure refers to

#### Scenario: Memory is framed as context only when present
- **WHEN** an event turn is composed with a recall block, and another without one
- **THEN** the first framing states that remembered facts are context and not an override of the evidence, and the second carries no such line

#### Scenario: Docs are named only when registered
- **WHEN** an event turn is composed for a session whose registry includes `homelab_docs`, and another for a session whose registry does not
- **THEN** the first framing instructs the agent to consult `homelab_docs`, and the second framing does not mention `homelab_docs`

#### Scenario: The arc and its check are unchanged
- **WHEN** a triage reply carries the `Diagnosis:` (with confidence), `Fix:` and `Pickup:` lines under the new framing
- **THEN** the application's arc check reports the arc complete exactly as before

#### Scenario: Framing follows the untrusted block
- **WHEN** an event turn is composed
- **THEN** the method framing appears after the end marker of the untrusted-data block, and no framing text appears inside it

### Requirement: Nothing inside the untrusted block can forge a block marker
Every string the application places inside an event turn's untrusted-data block SHALL be
neutralised against every block marker the application uses, before composition. That
covers:
- the event title and message;
- identity fields;
- the text of every related-handoff digest entry.

The markers are the untrusted block's begin and end markers, the recall block's markers,
and the prior-handoffs header. The same neutralisation SHALL apply to every stored memory
rendered into the recall block (memory-store spec). Neutralised text SHALL stay readable. It SHALL never be
byte-equal to a marker, and it SHALL NOT be able to open or close a block. After
composition, the content SHALL contain exactly one untrusted-block begin marker and
exactly one end marker, at the positions the composer placed them.

#### Scenario: A payload cannot close the block early
- **WHEN** an event's message contains the untrusted block's end marker followed by text phrased as instructions
- **THEN** the composed content contains exactly one end marker, the composer's own, and the payload's copy appears inside the block in neutralised form

#### Scenario: A memory cannot open or close a block
- **WHEN** a stored memory contains the recall block's end marker, or the untrusted block's begin marker, and an event turn is composed
- **THEN** the composed content contains exactly one recall-block end marker and exactly one untrusted-block begin marker, both the composer's own, and the memory's copies appear in neutralised form

#### Scenario: A retained handoff cannot close the block early
- **WHEN** a retained handoff shown in the digest contains the untrusted block's end marker or the recall block's begin marker
- **THEN** the composed content contains exactly one untrusted-block end marker and no recall-block marker inside the untrusted block, and the handoff's copies appear in neutralised form

### Requirement: Each incident states when it was notified and when it was received
Each incident in an event turn's untrusted-data block SHALL carry two times, both
rendered in UTC in the same format as range-query summaries (homelab-tools spec):
- the **notification time**, taken from the transport's own publish time on the received
  frame;
- the time Henk **received** it.

The notification time SHALL be labelled as the notification time and not as the
condition's onset, because a repeat notification for a long-firing alert can be sent
hours after the condition began. When the frame carries no usable notification time, the
block SHALL say that it is unknown, and SHALL NOT substitute the receive time for it. Both
times SHALL be inside the untrusted-data block.

#### Scenario: Both times are present
- **WHEN** an event whose frame carries a notification time is triaged
- **THEN** its incident header in the untrusted-data block states the notification time, labelled as such, and the receive time in UTC

#### Scenario: The notification time is not presented as onset
- **WHEN** the incident header and the framing are inspected
- **THEN** the time is called the notification time, and the framing states that it can be later than when the condition began

#### Scenario: A missing notification time is not invented
- **WHEN** an event's frame carries no usable notification time
- **THEN** its incident header states that the notification time is unknown and still states the receive time

### Requirement: An incomplete triage is reported, never silent
After each triage turn, the application SHALL classify how the turn ended from the agent
SDK's **structured signals, before considering any reply text**. The first matching
check, in this order, decides the ending:
1. an assistant-message error in the turn: `error`;
2. a refusal stop reason on an assistant or result message: `refused`;
3. a result message marked as an error, or carrying an API error status: `error`;
4. the turn raised: `error`;
5. empty reply text: `no-reply`;
6. otherwise: `completed`.

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

