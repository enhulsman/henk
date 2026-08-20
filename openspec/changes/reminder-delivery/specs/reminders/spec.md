# reminders Specification (delta)

## ADDED Requirements

### Requirement: A polling scheduler owns delivery
When reminders are enabled, the system SHALL run a scheduler that ticks at a configured poll interval (default 30 seconds). Each tick SHALL capture the current instant exactly once and use that captured value for every comparison in the tick. Each tick SHALL select, oldest-due first: `pending` reminders with `next_attempt_at` at or before the captured instant **and `due_at` at or before the captured instant** — a reminder is never delivered before its due instant, whatever `next_attempt_at` holds — and `missed` or `abandoned` reminders with a null `reported_at` and `next_attempt_at` at or before the captured instant. Delivery selection SHALL be bounded per tick: at most the configured per-tick delivery limit (default 10) of `pending` rows are selected, oldest-due first; rows beyond the limit are simply not selected that tick — not charged an attempt, not written — and remain eligible on following ticks. Report selection is not bounded (the summary is one message however many rows it names). There SHALL be no startup-specific catch-up pass: the first tick after a start finds whatever downtime left behind, because eligibility is a property of the selection query, not of boot.

Ordering within the tick SHALL be: grace transitions first (applied to **every** `pending` row past the grace window, selected or not), then selection against the post-grace state, then the attempt increments and bound checks for the selected rows — all inside the one pre-work transaction. A row that exits to `abandoned` in the same tick's pre-work joins that tick's summary composition without a second increment: charged rows are exactly the selected rows.

A failure inside one tick — store or channel — SHALL be logged and SHALL NOT stop the scheduler: the next tick retries. The scheduler SHALL NOT dispatch any store call off the event loop.

#### Scenario: Due reminder is delivered within a tick
- **WHEN** a pending reminder's due instant passes while the scheduler is running, fewer rows are due than the per-tick delivery limit, and no other outbound send is in flight
- **THEN** it is delivered no later than one poll interval after coming due

#### Scenario: A future reminder is never delivered early
- **WHEN** a reminder scheduled for a week from now exists, its `next_attempt_at` is at or before the captured instant, and many ticks run
- **THEN** nothing is sent and its status remains `pending`

#### Scenario: A within-grace backlog is paced, not burst
- **WHEN** one hundred reminders came due within the grace window during downtime and the process restarts
- **THEN** each tick delivers at most the per-tick delivery limit of them, oldest-due first, none is dropped or attempt-charged while waiting, and all are eventually delivered

#### Scenario: Scheduler survives a store error
- **WHEN** a scheduler tick raises a store error and a later tick runs with the store healthy
- **THEN** the scheduler is still running and due reminders are delivered

#### Scenario: Restart needs no special pass
- **WHEN** reminders come due while the process is stopped and it restarts
- **THEN** the first tick selects them under the same rules as any other tick

### Requirement: Due reminders are delivered verbatim without an agent turn
The scheduler SHALL deliver each due reminder as its own message by sending its **stored text unchanged** through the channel adapter's proactive owner-directed send. Delivery SHALL create no agent session, run no model turn, and consume no tokens. The delivered message SHALL carry a fixed marker distinguishing a reminder from a triage message. A delivery occurring more than a configured lateness threshold (default 300 seconds) after the due instant SHALL additionally state the reminder's original due time, rendered by the shared renderer, and SHALL record status `delivered-late`; an on-time delivery records `delivered`. Reminders SHALL be delivered one message per reminder, oldest-due first, before any catch-up summary in the same tick.

The caller-supplied failure notice SHALL state that a reminder could not be fully delivered — truthful for a wholly failed send as well as a partial one — and SHALL name the reminder's due time rendered by the shared renderer, so the owner knows which promise it concerns. The notice SHALL be supplied on a first or crash-retried delivery attempt and SHALL NOT be supplied on a floor-scheduled retry, so a persistently failing reminder produces at most the crash-attempt limit of notices before its grace exit, never one per retry.

Delivery SHALL be independent of the serial turn queue — it creates no turn and never waits for one — and SHALL NOT be suppressed by a pending approval. Delivery MAY wait on an in-flight outbound send, since outbound sends are serialized (channel-adapter spec); that wait is bounded by the in-flight message's chunk count times the send path's per-chunk retry ceiling, and a reminder that waited is delivered, never skipped. Worst-case owner-visible lateness is therefore **additive**: up to one poll interval of selection latency, plus any pacing ticks behind a same-tick backlog, plus the in-flight send wait — the poll interval does not absorb the send wait.

A reminder cancelled before its due instant SHALL never be delivered. Immediately before dispatching each reminder's send, the scheduler SHALL re-read the row's status and SHALL NOT dispatch a row that is no longer `pending` — the pre-work selection is not trusted across the awaits that separate it from the send. A cancellation that commits after dispatch, while the send is waiting on the lock or in flight, MAY still result in delivery; in that case the post-send write SHALL record `delivered` (the message factually reached the owner — recording `cancelled` would be false) and the row's audit trail carries both the `cancelled` and the delivery record.

#### Scenario: Reminder delivered on time
- **WHEN** a pending reminder's due instant passes and the send is confirmed
- **THEN** the owner receives its text unchanged, marked as a reminder, no agent session is created, and its status becomes `delivered`

#### Scenario: Text is not rewritten
- **WHEN** a reminder whose text would invite rephrasing is delivered
- **THEN** the delivered message contains the stored text exactly as scheduled

#### Scenario: Late delivery states its original due time
- **WHEN** a reminder is delivered more than the lateness threshold after its due instant
- **THEN** the message states the original due time rendered by the shared renderer and the status recorded is `delivered-late`

#### Scenario: Cancelled reminders never deliver
- **WHEN** a reminder is cancelled before its due instant and that instant passes
- **THEN** nothing is sent

#### Scenario: Cancellation between selection and dispatch is honoured
- **WHEN** a due reminder is selected by a tick and its cancellation commits before the scheduler dispatches its send (for example while an earlier reminder's send holds the lock)
- **THEN** the pre-send status re-read skips it and nothing is sent for it

#### Scenario: Cancellation that loses the race is recorded honestly
- **WHEN** a reminder's send has been dispatched and is waiting on the send lock or in flight when its cancellation commits, and the send then completes
- **THEN** the row records `delivered`, and its audit trail carries both the `cancelled` record and the delivery record

#### Scenario: Persistent failure does not repeat the notice
- **WHEN** a reminder's send fails on its first attempt and keeps failing on floor-scheduled retries
- **THEN** the owner receives the failure notice (naming the reminder's rendered due time) with the first attempt and no notice with the floor retries

#### Scenario: Delivery does not wait on a turn
- **WHEN** a reminder comes due while an agent turn is running and no outbound send is in flight
- **THEN** the reminder is delivered without waiting for the turn to complete, and the turn is unaffected

#### Scenario: Delivery waits on an in-flight send but is never skipped
- **WHEN** a reminder comes due while a multi-chunk reply is being sent
- **THEN** the reminder's send begins after the in-flight send completes, its chunks are not interleaved with the reply's, and it is delivered

#### Scenario: A pending approval does not suppress delivery
- **WHEN** a reminder comes due while an approval prompt is pending
- **THEN** the reminder is delivered and the pending approval is unaffected

### Requirement: Every delivery attempt is counted before the send, and the counter clears on any return
Each tick SHALL open a pre-work transaction that increments `send_attempts` for every selected row **before any send is attempted**, so an attempt the process does not survive is already counted. Every post-send write — recording `delivered`, `delivered-late`, a failed send's retry, or a report outcome — SHALL clear `send_attempts` to zero. The counter therefore accumulates only when a post-send write does not happen — process death being the case it exists for (a persistently failing post-send store write accumulates it the same way, and exits through the same bound). Channel failures are governed by the retry floor and the grace window, never by this counter.

No transaction scope SHALL span an await: the transaction boundary is connection-scoped and shared by every task on the loop, so a scope held across an await could silently join an unrelated task's transaction and be rolled back with it. The pre-work transaction closes before the first send begins; each post-send write opens its own.

#### Scenario: An attempt that crashes mid-send is counted
- **WHEN** the process is killed after the pre-work transaction commits and before the send completes
- **THEN** after restart the reminder's stored `send_attempts` reflects the crashed attempt

#### Scenario: Channel failures do not accumulate attempts
- **WHEN** a reminder's send fails and returns a failed outcome on many consecutive eligible ticks
- **THEN** its stored `send_attempts` does not grow across those ticks and the reminder is never abandoned for channel failure alone

#### Scenario: Crash between send and mark redelivers, never silently discards
- **WHEN** the process is killed after a reminder's message is sent but before its status is written
- **THEN** after restart the reminder is either delivered again (within the crash-attempt bound) or already marked delivered — in no case is it silently discarded

### Requirement: A crash-attempt bound exits to abandoned, evaluated in the pre-work transaction
A `pending` reminder whose incremented `send_attempts` reaches the configured crash-attempt limit (default 3) SHALL move to `abandoned` **inside the pre-work transaction, beside the increment** — never in the post-send transaction, which a crash is precisely what prevents. The exit SHALL clear the counter, set `next_attempt_at` to the captured instant (the value now determines report eligibility, so it is load-bearing, not cosmetic), leave `reported_at` null, and write the `abandoned` audit record, so the same tick's catch-up summary names the reminder as a delivery that was given up on. An abandoned reminder SHALL never be attempted again.

#### Scenario: A crash loop terminates at the limit
- **WHEN** every delivery attempt for a reminder is interrupted by process death and the process keeps restarting
- **THEN** the reminder moves to `abandoned` after the configured limit of attempts, an `abandoned` audit record exists, and no further delivery attempt is made

#### Scenario: The exit is evaluated on the path it bounds
- **WHEN** a reminder's attempts have all crashed before any post-send write could run
- **THEN** the abandoned exit still fires, because it is evaluated in the pre-work transaction

#### Scenario: Abandonment is surfaced, not silent
- **WHEN** a reminder is abandoned
- **THEN** it appears in the catch-up summary as a reminder that could not be delivered

### Requirement: Failed sends retry on a fixed floor, and a partial reminder counts as delivered
A delivery send returning a failed outcome SHALL leave the reminder `pending` with `next_attempt_at` set to the captured instant plus the configured retry floor (default 900 seconds), and the counter cleared. There SHALL be no escalating backoff schedule. A delivery send returning a partial outcome SHALL be recorded as `delivered` (or `delivered-late`) with an error log and SHALL NOT be re-sent: a reminder is one text whose delivered head is the reminder itself, the adapter's caller-supplied notice has already told the owner it was cut, and a retry would duplicate the whole text.

#### Scenario: Transient failure retries after the floor
- **WHEN** a reminder's send fails and the channel recovers
- **THEN** the reminder is not re-attempted before the retry floor elapses, is delivered on the first eligible tick after it, and its status becomes `delivered` or `delivered-late`

#### Scenario: Partial reminder delivery is not re-sent
- **WHEN** a reminder's send reports a partial outcome
- **THEN** the reminder is recorded as delivered, an error is logged, and no further send of its text occurs

### Requirement: Overdue beyond the grace window becomes missed, inside the pre-work transaction
A `pending` reminder whose due instant lies more than the configured grace window (default 24 hours) before the captured instant SHALL move to `missed` in the pre-work transaction — counter cleared, `next_attempt_at` set, `reported_at` left null, `missed` audit record written — and SHALL NOT be delivered as a reminder: a day-old instruction delivered as if current is worse than useless. A reminder overdue within the grace window is delivered under the late-delivery rule. No overdue reminder SHALL reach a terminal status without either being delivered or being named in an attempted catch-up summary.

#### Scenario: Downtime within grace delivers late
- **WHEN** a reminder comes due while the process is stopped and the process restarts within the grace window
- **THEN** the reminder is delivered on the first tick, states its original due time, and its status becomes `delivered-late`

#### Scenario: Downtime beyond grace is missed, not replayed
- **WHEN** a reminder came due longer ago than the grace window and the process restarts
- **THEN** it is not delivered as a reminder, its status becomes `missed` with an audit record, and it is named in the catch-up summary

#### Scenario: Nothing overdue means silence
- **WHEN** the process restarts with no overdue reminders and nothing unreported
- **THEN** no startup message of any kind is sent

### Requirement: Missed and abandoned reminders are reported once, all of them, with no item bound
Each tick with selected report work SHALL compose **one** catch-up summary naming **every** report row selected that tick (null `reported_at`, `next_attempt_at` at or before the captured instant) together with every row that exited to `abandoned` in the same tick's pre-work — and no others: a row cooling on the retry floor is not renamed early, so the floor governs both whether a summary is sent and what it names. The summary SHALL name its rows in selection order, oldest-due first — which places any horizon-eligible (oldest) rows in the head chunks, exactly the part a partial send did deliver. Each named row carries its text, its original due time rendered by the shared renderer, and for abandoned rows the fact that delivery was given up on. There SHALL be no item bound and no pagination: a long summary splits into sequential chunks like any long message, and composition SHALL never omit a row from its set, so no row can be attempt-charged without a recording write. The summary SHALL be sent after the tick's individual deliveries, through the proactive send **with no failure notice**: the summary is itself the last-resort report, a banner saying it was cut adds nothing a retry does not, and a failed summary must never spawn a second owner-visible message.

`reported_at` SHALL be written for the named rows **only when the summary's outcome is delivered** — the give-up exits below being the sole exception, and they write it to terminate reporting with an error log, never to assert the owner was told. On a partial or failed outcome, no row SHALL be marked reported: the summary carries one promise per row, a lost tail is other reminders entirely, and a duplicated head beats a row that silently vanishes. The two non-delivered outcomes then part ways, because they cost the owner differently:

- A **failed** summary (nothing landed) schedules a floor retry, indefinitely — it is costless to the owner (no chunk, no notice), error-logged per attempt, and it lands on channel recovery. The last-resort promise is never given up on a channel outage.
- A **partial** summary (its head landed) is owner-visible on every retry, so it is bounded: in the post-send write, any named row whose due instant lies more than the grace window **plus the configured report horizon** (default 86400 seconds) before the captured instant takes the give-up exit — `reported_at` written, error logged, no new audit transition — and the remaining named rows take the floor retry.

The horizon SHALL be evaluated **only in the post-send write of an attempted summary**, never in the pre-work transaction: a give-up there could retire a row that arrived already stale (a restart after long downtime) before any summary ever named it. Because the check runs post-send, every row that reaches the give-up exit on the horizon was named in at least one attempted summary — a fresh row gets on the order of horizon-over-floor attempts, a stale row gets exactly one. The crash-attempt give-up stays in the pre-work transaction, unchanged and for the opposite reason: a crash is what prevents the post-send write, so its bound is never evaluable there, while a channel outcome exists only after the send returns. The horizon is the report path's only channel-outcome bound — the crash-attempt limit cannot fire on channel outcomes, since the counter clears on every return — and without it a summary whose send deterministically returns partial would re-deliver its head chunks every floor interval, forever.

#### Scenario: One summary names everything
- **WHEN** two missed reminders and one abandoned reminder are unreported and a tick runs
- **THEN** one summary message names all three with their texts and original due times, identifies the abandoned one as a failed delivery, and all three get `reported_at` set after the send is confirmed

#### Scenario: A failed summary marks nothing
- **WHEN** the summary's send returns a failed outcome
- **THEN** every named row still has a null `reported_at` and the summary is retried after the retry floor

#### Scenario: A partial summary marks nothing
- **WHEN** the summary's send returns a partial outcome and no named row is past the report horizon
- **THEN** no named row is marked reported, and the summary is retried after the retry floor even though its head chunks were delivered

#### Scenario: A large backlog is fully named
- **WHEN** one hundred reminders are missed during a long outage
- **THEN** the summary names all one hundred across its sequential chunks and no selected row is omitted from composition

#### Scenario: Reported rows never resurface
- **WHEN** a missed reminder has been named in a delivered summary and later ticks run
- **THEN** it is never selected or named again

#### Scenario: Report crash loop terminates
- **WHEN** every attempt to send the summary is interrupted by process death, up to the crash-attempt limit
- **THEN** the named rows' `reported_at` is written as the give-up exit and an error is logged

#### Scenario: A persistently partial summary terminates at the horizon
- **WHEN** every summary send returns a partial outcome and eligible ticks keep running
- **THEN** each named row's summary sends are bounded by the report horizon over the retry floor, and once a named row's due instant is older than the grace window plus the horizon its `reported_at` is written as the give-up exit — in the post-send write of an attempted summary, with an error log — and it is never named again

#### Scenario: A channel outage never forfeits the report
- **WHEN** the channel is down when rows become reportable, stays down past the report horizon, and then recovers
- **THEN** the summary attempts while it was down return failed and give up on nothing, and the first eligible tick after recovery delivers a summary naming every unreported row, which is then marked reported

#### Scenario: Stale rows are named before any give-up
- **WHEN** the process restarts (or reminders are re-enabled) after downtime longer than the grace window plus the report horizon, with stored reminders that came due before the downtime ended
- **THEN** every such row is named in an attempted catch-up summary at least once before any give-up exit can fire for it

### Requirement: Every exit writes state the selector tests
Every path that removes a reminder from the scheduler's working set — delivery, late delivery, missed, abandoned, reported, cancellation — SHALL do so by writing the store column the selection query predicates on (`status`, `next_attempt_at`, `reported_at`). No exit SHALL exist only in process memory, because the selector is a query and an in-memory exit reverts on restart.

#### Scenario: Exits survive restart
- **WHEN** a reminder is delivered, missed, abandoned, or reported and the process then restarts
- **THEN** the first tick's selection does not pick it up again for the work it already exited

### Requirement: Henk knows about a reminder he just sent
A delivered reminder not yet surfaced in conversation SHALL be injected into the next **owner** turn as a clearly delimited data block listing what was sent and when, framed as messages Henk sent — never as instructions. The block SHALL cover only deliveries within a configured window (default 12 hours), bounded to a configured maximum count, newest first. Injecting it SHALL durably set `surfaced_at` so each delivery is surfaced at most once, surviving a restart between delivery and the owner's reply. The block SHALL be injected independently of whether the recall block was already given in the session, SHALL never be injected into event turns, and SHALL NOT taint the session — reminder text is owner-authored, owner-echoed, or model-composed inside an untainted owner turn, since `remind` is owner-turn-only and denied in tainted sessions: the same provenance as the recall block, carried with the same data framing.

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

### Requirement: Delivery configuration only narrows, and is validated at load
The delivery settings — poll interval, retry floor, crash-attempt limit, grace window, lateness threshold, report horizon, per-tick delivery limit, note window and note count — SHALL be configuration with safe defaults, validated at load: each strictly positive, the lateness threshold strictly less than the grace window, and the report horizon strictly greater than the retry floor — a horizon at or below the floor would silently convert the post-send bound into a one-attempt drop for every row. No delivery setting SHALL widen the capability: there SHALL be no setting that directs a delivery to any identity but the configured owner, routes it around the channel adapter, or exempts any transition from its audit record.

#### Scenario: Invalid delivery configuration fails startup
- **WHEN** a delivery setting is non-positive or an ordering constraint is violated (lateness threshold not below the grace window, report horizon not above the retry floor)
- **THEN** configuration loading fails with an error naming the setting

#### Scenario: No widening knob exists
- **WHEN** the delivery configuration surface is inspected
- **THEN** it contains no setting naming a recipient, an alternative channel, or an audit exemption

## MODIFIED Requirements

### Requirement: Every path into pending initializes the delivery selector column
Any code path that puts a reminder row into status `pending` — scheduling by tool, scheduling by command, or reinstating — SHALL write `next_attempt_at` explicitly **to the reminder's due instant** as part of the same write. A `pending` row SHALL NEVER exist with a null `next_attempt_at`, because the delivery selector is a query and a null value would make the row permanently unselectable while still reporting itself as pending — and SHALL NEVER exist with `next_attempt_at` earlier than its due instant, because the schema default (`0`) means *eligible now* and the selector's due-instant conjunct is defence in depth, not the only guard. A floor-scheduled retry, which keeps a row `pending`, sets `next_attempt_at` later than the due instant; nothing may ever set it earlier.

#### Scenario: Scheduling sets the selector column to the due instant
- **WHEN** a reminder is scheduled by either the tool or the command path
- **THEN** the stored row's `next_attempt_at` equals its due instant

#### Scenario: Reinstating sets the selector column to the due instant
- **WHEN** a cancelled reminder is reinstated to `pending`
- **THEN** the stored row's `next_attempt_at` equals its due instant

### Requirement: Reminders can be disabled without removing data
A configuration flag SHALL disable the capability, defaulting to disabled: all reminder tools SHALL be absent from the registered toolset, every reminder owner command SHALL reply that reminders are not configured, no reminder SHALL be scheduled by any path, and **the delivery scheduler SHALL NOT run — no tick, no delivery, no catch-up summary, no store write**. Stored reminders SHALL remain untouched and SHALL become operable again if the capability is re-enabled; on re-enablement, overdue reminders SHALL be handled by the ordinary grace rules — delivered late within the grace window, missed and summarised beyond it. No configuration flag SHALL widen the capability — there SHALL be no setting that promotes `remind` or `cancel_reminder` beyond standing tier, widens their turn scope, or directs a reminder to any identity but the configured owner.

#### Scenario: Disabled means inert, not destructive
- **WHEN** reminders are disabled and the process runs with pending reminders in the store
- **THEN** no reminder tool is registered, the commands reply honestly, no scheduler task runs, nothing is delivered, and every stored reminder is unchanged

#### Scenario: Disabled by default
- **WHEN** the configuration carries no reminders section
- **THEN** the capability is disabled

#### Scenario: Re-enabling restores access to stored reminders
- **WHEN** reminders are re-enabled after a period disabled
- **THEN** previously stored pending reminders are listed by `/reminders` with their original text and due times

#### Scenario: Re-enabling catches up under the grace rules
- **WHEN** reminders are re-enabled and stored reminders came due while the capability was disabled
- **THEN** those within the grace window are delivered late stating their original due times, and those beyond it are missed and named in the catch-up summary
