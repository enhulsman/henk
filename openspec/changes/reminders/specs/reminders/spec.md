# reminders Specification (delta)

## ADDED Requirements

### Requirement: Durable one-shot reminder store
The system SHALL persist reminders as one row per reminder in the SQLite store that already holds memories and the capture inbox, so reminders survive process restarts and container recreation. Each reminder SHALL carry a unique id, its text, an absolute due time, a creation time, a source, a status, and delivery bookkeeping (delivered time, surfaced time, attempt count). Status SHALL be one of `pending`, `delivered`, `delivered-late`, `missed`, `cancelled`, or `abandoned`. Reminder rows SHALL NEVER be deleted and reminder text SHALL NEVER be edited by any code path in this capability: reaching a terminal status is a state change, not a removal. The number of `pending` reminders SHALL be bounded by a configured cap (default 100); a schedule attempt that would exceed it SHALL be rejected with an explicit error naming the cap, and nothing stored.

#### Scenario: Reminder survives restart
- **WHEN** a reminder is scheduled and the process is killed non-gracefully (SIGKILL) before it is due
- **THEN** the reminder is present with status `pending` and its original due time after restart, and is still delivered when due

#### Scenario: Terminal status is not deletion
- **WHEN** a reminder has been delivered or cancelled
- **THEN** its row is still present with the terminal status and its original text and due time

#### Scenario: Pending cap rejects honestly
- **WHEN** the pending cap is already reached and another reminder is scheduled
- **THEN** nothing is stored and the error names the cap

### Requirement: remind tool schedules an owner reminder
The system SHALL provide a `remind` tool (class: mutating, authorization tier: standing, turn scope: owner-only) taking the reminder `text` and an absolute ISO-8601 `when`. The reminder SHALL be durable before the tool result reports success, and the result SHALL confirm it with the reminder's id and the **resolved due time rendered in the owner's configured timezone including the weekday**, so a mis-resolved time is visible in the same reply. Empty or whitespace-only text SHALL be rejected, as SHALL text exceeding the configured length limit — with an explicit error result naming the limit and nothing stored. Turn-scope and taint enforcement are governed by the approval-gate spec.

#### Scenario: Reminder scheduled and echoed
- **WHEN** the agent invokes `remind` with non-empty text and a valid future timestamp in an untainted owner session
- **THEN** a `pending` reminder exists and the tool result names its id and the resolved due time in the owner's timezone, with the weekday

#### Scenario: Scheduling never prompts
- **WHEN** the agent invokes `remind` during an untainted owner conversation
- **THEN** the reminder is stored without any approval prompt being sent

#### Scenario: Empty reminder fails safe
- **WHEN** the agent invokes `remind` with whitespace-only text
- **THEN** nothing is stored and the tool returns an explicit error result

### Requirement: Time resolution is validated and fails closed
A submitted due time SHALL be parsed as an ISO-8601 timestamp; a value carrying no UTC offset SHALL be interpreted in the owner's configured timezone, never as UTC. The system SHALL reject, with an explicit error result and nothing stored: an unparseable value, a due time in the past beyond a small clock-skew tolerance, and a due time beyond a configured horizon (default 365 days). The owner's timezone SHALL be validated at configuration load; an unknown zone SHALL fail startup rather than silently falling back to UTC.

#### Scenario: Naive timestamp uses the owner's timezone
- **WHEN** a reminder is scheduled with a timestamp carrying no UTC offset
- **THEN** the due time is interpreted in the owner's configured timezone and the echoed confirmation shows that local time

#### Scenario: Past time rejected
- **WHEN** a reminder is scheduled for a time already past
- **THEN** nothing is stored and the error states the time is in the past

#### Scenario: Beyond-horizon time rejected
- **WHEN** a reminder is scheduled beyond the configured horizon
- **THEN** nothing is stored and the error names the horizon

#### Scenario: Unparseable time rejected
- **WHEN** a reminder is scheduled with a value that is not an ISO-8601 timestamp
- **THEN** nothing is stored and the error says the time could not be understood

#### Scenario: Unknown timezone fails startup
- **WHEN** the configured owner timezone is not a known zone
- **THEN** startup fails with an error naming the value, and no reminder is ever scheduled against a fallback zone

### Requirement: Owner reminder commands
The system SHALL provide owner commands handled without an agent turn. `/remind <when> <text>` SHALL schedule a reminder, accepting explicit time forms only — `HH:MM` (the next occurrence of that clock time), `YYYY-MM-DD HH:MM`, and a relative offset (`+90m`, `+2h`, `+3d`) — and SHALL reply with the reminder's id and resolved local due time; an unrecognized time form SHALL schedule nothing and reply naming the accepted forms. `/reminders` SHALL list pending reminders oldest-due first with their ids, due times and text. `/reminders cancel <id>` SHALL cancel that pending reminder and confirm, echoing its text so a mistaken cancel is recoverable by re-scheduling; an unknown or already-terminal id SHALL change nothing and reply that no pending reminder has that id. Receipts are governed by the audit-log spec.

#### Scenario: Owner schedules directly
- **WHEN** the owner sends `/remind +2h call the plumber`
- **THEN** a pending reminder exists two hours out, the reply names its id and resolved local time, and no agent session or model tokens are used

#### Scenario: Clock-time form resolves to the next occurrence
- **WHEN** the owner sends `/remind 07:30 leave for the train` at 21:00 local
- **THEN** the reminder is scheduled for 07:30 the following day and the reply states that date

#### Scenario: Unrecognized time form is honest
- **WHEN** the owner sends `/remind sometime next week water the plants`
- **THEN** nothing is scheduled and the reply names the accepted time forms

#### Scenario: Pending reminders are listable
- **WHEN** pending reminders exist and the owner sends `/reminders`
- **THEN** the reply lists them oldest-due first with id, due time and text

#### Scenario: Owner cancels a reminder
- **WHEN** the owner sends `/reminders cancel <id>` for a pending reminder
- **THEN** its status becomes `cancelled`, it is not delivered, and the reply echoes its text

#### Scenario: Unknown cancel id fails honestly
- **WHEN** the owner sends `/reminders cancel 9999` and no pending reminder has that id
- **THEN** nothing changes and the reply states no pending reminder has that id

### Requirement: Cancellation is owner-authored only
The system SHALL NOT provide any tool that cancels, edits, or deletes a reminder: the model may schedule (additive, receipted, echoed) but SHALL NOT un-schedule. Removal SHALL be reachable only through the `/reminders cancel <id>` owner command, whose text never passes through the model.

#### Scenario: No cancel tool exists
- **WHEN** the registered toolset is inspected
- **THEN** it contains no tool that cancels, edits, or deletes a reminder

#### Scenario: Agent asked to cancel points at the command
- **WHEN** the owner asks the agent to cancel a reminder in conversation
- **THEN** the agent states it cannot cancel one itself and names the `/reminders cancel <id>` command, and no reminder changes status

### Requirement: Due reminders are delivered verbatim without an agent turn
A scheduler SHALL deliver each `pending` reminder whose due time has passed, oldest-due first, by sending its **stored text unchanged** through the channel adapter's proactive owner-directed send. Delivery SHALL create no agent session, run no model turn, and consume no tokens. The delivered message SHALL carry a fixed marker distinguishing a reminder from a triage message. Delivery SHALL be independent of the serial turn queue and SHALL NOT be suppressed by a pending approval. After a successful send the reminder's status SHALL become `delivered` (or `delivered-late`, see the catch-up requirement) with its delivery time recorded.

#### Scenario: Reminder delivered on time
- **WHEN** a pending reminder's due time passes
- **THEN** the owner receives its text unchanged, marked as a reminder, no agent session is created, and its status becomes `delivered`

#### Scenario: Text is not rewritten
- **WHEN** a reminder whose text would invite rephrasing is delivered
- **THEN** the delivered message contains the stored text exactly as scheduled

#### Scenario: Cancelled reminders never deliver
- **WHEN** a reminder is cancelled before its due time and that time passes
- **THEN** nothing is sent

#### Scenario: Delivery does not wait on a turn
- **WHEN** a reminder comes due while an agent turn is running
- **THEN** the reminder is delivered without waiting for the turn to complete, and the turn is unaffected

### Requirement: Delivery failures are bounded and never silent
Each delivery attempt SHALL increment a durable attempt counter before the send, so an attempt that crashes mid-send is counted. A send that fails SHALL leave the reminder `pending` for the next tick. A reminder reaching the configured maximum attempts (default 3) without a recorded delivery SHALL move to `abandoned`, SHALL NOT be attempted again, and SHALL be surfaced to the owner as a failure — never dropped quietly. A crash between a successful send and the status write SHALL redeliver at most within the attempt bound: duplicate delivery is the accepted failure mode, silent loss is not.

#### Scenario: Transient send failure retries
- **WHEN** the channel send fails once and succeeds on the next tick
- **THEN** the reminder is delivered and its status becomes `delivered`

#### Scenario: Repeated failure is abandoned loudly
- **WHEN** delivery fails on every attempt up to the maximum
- **THEN** the reminder's status becomes `abandoned`, no further attempts are made, and the owner is told the reminder could not be delivered

#### Scenario: Crash after send does not lose the reminder
- **WHEN** the process is killed after a reminder's message is sent but before its status is written
- **THEN** after restart the reminder is either delivered again (within the attempt bound) or already marked delivered — in no case is it silently discarded

### Requirement: Reminders due during downtime are caught up, never dropped
On startup the system SHALL evaluate every `pending` reminder whose due time has already passed. Those within a configured grace window (default 24 hours) SHALL be delivered immediately, each message stating its **original due time**, and their status SHALL become `delivered-late`. Those older than the grace window SHALL NOT be delivered as reminders; they SHALL be reported to the owner once as a single missed-reminder summary naming each reminder's due time and text, and their status SHALL become `missed`. No overdue reminder SHALL reach a terminal status without either being delivered or appearing in that summary.

#### Scenario: Reminder due during downtime is delivered late
- **WHEN** a reminder comes due while the process is stopped and it is restarted within the grace window
- **THEN** the reminder is delivered on startup, the message states its original due time, and its status becomes `delivered-late`

#### Scenario: Long-stale reminder is summarised, not replayed
- **WHEN** a reminder came due longer ago than the grace window and the process restarts
- **THEN** it is not delivered as a reminder, it appears in a single missed-reminder summary with its due time and text, and its status becomes `missed`

#### Scenario: Nothing overdue means silence
- **WHEN** the process restarts with no overdue reminders
- **THEN** no startup message of any kind is sent

### Requirement: Henk knows about a reminder he just sent
A delivered reminder that has not yet been surfaced in conversation SHALL be injected into the **next owner turn** as a clearly delimited data block listing what was sent and when, framed as messages Henk sent — never as instructions. Injection SHALL be independent of whether the memory recall block was already given in that session, SHALL be bounded in count, and SHALL cover only deliveries within a configured window (default 12 hours). A delivery SHALL be surfaced at most once, tracked durably so a restart between delivery and the owner's reply does not lose it. The block SHALL NOT be injected into event turns, and receiving it SHALL NOT taint the session — reminder text is owner-authored or owner-echoed by construction, since `remind` is owner-turn-only and denied in tainted sessions.

#### Scenario: Follow-up resolves against the delivered reminder
- **WHEN** a reminder is delivered and the owner then replies "why did you ping me?"
- **THEN** that turn's content carries the delimited delivered-reminder block naming the reminder text and delivery time, and the agent can answer without a tool call

#### Scenario: Surfaced exactly once
- **WHEN** a delivery has been surfaced in one owner turn and another owner turn follows
- **THEN** the second turn carries no block for that delivery

#### Scenario: Surfacing survives a restart
- **WHEN** a reminder is delivered, the process restarts, and the owner then sends a message within the window
- **THEN** that turn still carries the delivered-reminder block

#### Scenario: Stale delivery does not resurface
- **WHEN** a delivery is older than the configured window and the owner sends a message
- **THEN** the turn carries no block for it

#### Scenario: Event turns never carry it
- **WHEN** an event-triage turn is processed while an unsurfaced delivery exists
- **THEN** the content passed to the agent session contains no delivered-reminder block

#### Scenario: The note does not taint the session
- **WHEN** an owner turn carries the delivered-reminder block and the agent then invokes `capture` or `store_memory`
- **THEN** the invocation executes normally — the block is not event-derived input and does not taint the session

### Requirement: reminders_read tool
The system SHALL provide a `reminders_read` tool (class: read-only) returning pending reminders oldest-due first with their ids, due times (rendered in the owner's timezone) and text, plus recent deliveries within a bounded window. The number of returned items SHALL be clamped to a bounded maximum.

#### Scenario: Agent reads the schedule
- **WHEN** pending reminders exist and the agent invokes `reminders_read`
- **THEN** the result lists them oldest-due first with id, local due time and text

#### Scenario: Nothing scheduled reads as nothing scheduled
- **WHEN** no pending reminders exist and the agent invokes `reminders_read`
- **THEN** the result states there are no pending reminders

### Requirement: Reminder store failures are loud but honest
A store write failure (`remind`, `/remind`, `/reminders cancel`) SHALL surface as an explicit error and SHALL NOT be reported as success — an owner who believes a reminder is set and it is not is the capability's worst failure. A store read failure (`reminders_read`, `/reminders`) SHALL surface as an explicit error and SHALL NOT be presented as an empty schedule. A store failure inside the scheduler SHALL be logged at error level and SHALL NOT stop the scheduler: the next tick retries.

#### Scenario: Failed schedule is never reported as success
- **WHEN** a `remind` write fails
- **THEN** the tool returns an explicit error result and no confirmation with a due time is produced

#### Scenario: Unreadable schedule is not "nothing scheduled"
- **WHEN** the store cannot be read and the owner sends `/reminders`
- **THEN** the reply states the failure, not an empty schedule

#### Scenario: Scheduler survives a store error
- **WHEN** a scheduler tick raises a store error and a later tick runs with the store healthy
- **THEN** the scheduler is still running and due reminders are delivered

### Requirement: Reminders can be disabled without removing data
A configuration flag SHALL disable the capability: both tools SHALL be absent from the registered toolset, the three owner commands SHALL reply that reminders are not configured, and the scheduler SHALL NOT run. Stored reminders SHALL remain untouched and SHALL be evaluated under the catch-up rules if the capability is re-enabled. No configuration flag SHALL widen the capability — there SHALL be no setting that promotes `remind` beyond standing tier, widens its turn scope, or enables delivery to any identity but the configured owner.

#### Scenario: Disabled means inert, not destructive
- **WHEN** reminders are disabled and the process runs with pending reminders in the store
- **THEN** nothing is delivered, no reminder tool is registered, the commands reply honestly, and every stored reminder is unchanged

#### Scenario: Re-enabling catches up
- **WHEN** reminders are re-enabled after a period disabled
- **THEN** overdue reminders are handled by the late/missed catch-up rules
