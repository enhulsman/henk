# reminders Specification

## Purpose
Owner-scheduled one-shot reminders: a short text plus one absolute instant, durable in the same
SQLite store that already holds memories and the capture inbox. Wall-clock input is resolved in the
owner's configured timezone — honest about nonexistent and ambiguous local times — and every surface
renders a due time identically. Nothing is ever edited or deleted, so a reminder's history is a chain
of receipted status changes.

This capability owns **both halves**: the scheduling side, and the clock that delivers it. A polling
scheduler ticks on a configured interval, delivers each due reminder verbatim through the channel
adapter with no session, no model turn and no tokens, and records every outcome durably — because the
selector is a query, so an exit held only in memory reverts on restart. Downtime is handled rather
than lost: overdue-within-grace is delivered late stating its original due time, overdue-beyond-grace
becomes `missed` and is named in a catch-up summary. Duplicate delivery is the accepted failure mode;
silent loss is not. The capability still ships **disabled** by default, because a build that accepts a
promise it cannot keep is worse than one that declines it.
## Requirements
### Requirement: Durable one-shot reminder store with its complete final column set
The system SHALL persist reminders as one row per reminder in the SQLite store that already holds memories and the capture inbox, so reminders survive process restarts and container recreation. Each reminder SHALL carry a unique id, its text, an absolute due instant, the timezone its wall clock was resolved in, the raw time string as submitted (bounded, diagnostic only), a creation time, a source (`tool` or `command`), a status, and the delivery bookkeeping columns `next_attempt_at`, `send_attempts`, `delivered_at`, `surfaced_at` and `reported_at`. Status SHALL be one of `pending`, `delivered`, `delivered-late`, `missed`, `cancelled`, or `abandoned`.

Because all schema DDL is `CREATE TABLE IF NOT EXISTS` and the system has no migration mechanism, the table SHALL be created with **every** column the capability will ever need, including those only the delivery half writes; `next_attempt_at` SHALL be non-nullable with a default that makes a row eligible rather than permanently unselectable. The store SHALL verify the live table's column set against the expected set when it opens and SHALL fail with an explicit error naming the discrepancy rather than running against a table it does not recognize.

The number of `pending` reminders SHALL be bounded by a configured cap (default 100); a schedule attempt that would exceed it SHALL be rejected with an explicit error naming the cap, and nothing stored. The cap check and the insert SHALL happen in one transaction.

#### Scenario: Reminder survives restart
- **WHEN** a reminder is scheduled and the process is killed non-gracefully (SIGKILL) before it is due
- **THEN** the reminder is present with status `pending`, its original text and its original due instant after restart

#### Scenario: The table carries the delivery columns from creation
- **WHEN** the reminders table is created and its columns are inspected
- **THEN** `next_attempt_at`, `send_attempts`, `delivered_at`, `surfaced_at` and `reported_at` are all present, and `next_attempt_at` is non-nullable

#### Scenario: A drifted table fails loudly
- **WHEN** the store opens against a pre-existing reminders table missing an expected column
- **THEN** an explicit error names the missing column and no reminder operation is attempted

#### Scenario: Pending cap rejects honestly
- **WHEN** the pending cap is already reached and another reminder is scheduled
- **THEN** nothing is stored and the error names the cap

### Requirement: Every path into pending initializes the delivery selector column
Any code path that puts a reminder row into status `pending` — scheduling by tool, scheduling by command, or reinstating — SHALL write `next_attempt_at` explicitly **to the reminder's due instant** as part of the same write. A `pending` row SHALL NEVER exist with a null `next_attempt_at`, because the delivery selector is a query and a null value would make the row permanently unselectable while still reporting itself as pending — and SHALL NEVER exist with `next_attempt_at` earlier than its due instant, because the schema default (`0`) means *eligible now* and the selector's due-instant conjunct is defence in depth, not the only guard. A floor-scheduled retry, which keeps a row `pending`, sets `next_attempt_at` later than the due instant; nothing may ever set it earlier.

#### Scenario: Scheduling sets the selector column to the due instant
- **WHEN** a reminder is scheduled by either the tool or the command path
- **THEN** the stored row's `next_attempt_at` equals its due instant

#### Scenario: Reinstating sets the selector column to the due instant
- **WHEN** a cancelled reminder is reinstated to `pending`
- **THEN** the stored row's `next_attempt_at` equals its due instant

### Requirement: The store exposes an explicit transaction boundary
The store SHALL expose a transaction context manager that opens an immediate write transaction on entry, commits on clean exit, and rolls back on exception. The connection SHALL NOT rely on the SQLite driver's implicit transaction handling, so that no transaction exists unless this API opened one. Nested use SHALL join the enclosing transaction rather than open a second one: only the outermost scope commits. If an exception escapes any nested scope, the whole transaction SHALL roll back even when that exception is caught before reaching the outermost scope.

Every reminder repository method SHALL be transaction-agnostic: it SHALL perform its own writes inside this boundary and SHALL NOT commit independently, so that calling it standalone is atomic and calling it inside a caller's transaction joins that transaction. No repository method sharing this store SHALL commit a transaction it did not open.

#### Scenario: Multi-write rollback leaves nothing behind
- **WHEN** two writes are performed inside one transaction and the second raises
- **THEN** neither write is present after the exception

#### Scenario: A repository call cannot commit its caller's transaction
- **WHEN** a caller opens a transaction, calls a reminder repository write, and then raises before the transaction ends
- **THEN** the repository's write is absent afterwards

#### Scenario: A swallowed inner failure still rolls back
- **WHEN** an inner transaction scope raises, the caller catches the exception, and the outer scope exits normally
- **THEN** nothing written in that transaction is committed

#### Scenario: A repository write standalone is durable
- **WHEN** a reminder repository write is called with no enclosing transaction
- **THEN** the write is committed and visible after the call returns

### Requirement: Wall-clock input is resolved in the owner's timezone
A submitted wall-clock time — `HH:MM` (command path only), `YYYY-MM-DD HH:MM`, or an ISO-8601 value carrying no UTC offset — SHALL be interpreted in the owner's configured timezone, never as UTC, and resolved to an absolute instant using the offset in effect on the target date. The resolved instant SHALL be what is stored; nothing SHALL re-resolve a stored reminder later.

The accepted forms SHALL be an explicit whitelist, matched **before** parsing rather than inferred from what the parser happens to accept. The system SHALL reject, with an explicit error and nothing stored: a value not matching the whitelist (including a date with no explicit time of day, an ISO week date, a basic-format date, and a bare clock reading on the tool path); a value carrying a UTC offset or `Z` suffix; an instant already in the past beyond a small configured clock-skew tolerance; and an instant beyond a configured horizon (default 365 days).

All past, future, and horizon comparisons SHALL be performed on absolute instants (epoch seconds or UTC-converted values) and SHALL NEVER be performed on aware date-times in the owner's timezone, whose ordering inside a repeated hour is a wall-clock comparison rather than an instant comparison. The current instant SHALL be captured exactly once per resolution and every comparison SHALL use that captured value.

#### Scenario: Naive timestamp uses the owner's timezone
- **WHEN** a reminder is scheduled with a timestamp carrying no UTC offset
- **THEN** the due instant corresponds to that wall clock in the owner's configured timezone and the echoed confirmation shows that local time

#### Scenario: An accepted wall clock round-trips to the wall clock submitted
- **WHEN** a wall-clock submission is accepted and its stored instant is rendered back in the owner's timezone
- **THEN** its wall clock equals the wall clock submitted, whether the reading was unambiguous or ambiguous

#### Scenario: Offset-carrying timestamp is rejected
- **WHEN** a reminder is scheduled with a timestamp carrying a UTC offset or a `Z` suffix
- **THEN** nothing is stored and the error states that the time must be the owner's local time with no offset, naming an accepted form

#### Scenario: Target-date offset is used, not today's
- **WHEN** a reminder is scheduled during summer time for a wall clock on a date after the autumn transition
- **THEN** the resolved instant uses the target date's offset, so the echoed local time equals the requested wall clock

#### Scenario: Comparisons inside the repeated hour use instants
- **WHEN** a candidate reading's earlier occurrence precedes the current instant while comparing as later in wall-clock terms
- **THEN** it is not treated as being in the future

#### Scenario: Past time rejected, and the error dates itself
- **WHEN** a reminder is scheduled for an instant already past beyond the skew tolerance
- **THEN** nothing is stored and the error states the time is in the past and names the current local time

#### Scenario: Beyond-horizon time rejected
- **WHEN** a reminder is scheduled beyond the configured horizon
- **THEN** nothing is stored and the error names the horizon

#### Scenario: Unwhitelisted shapes rejected
- **WHEN** a reminder is scheduled with a date and no time of day, an ISO week date, a basic-format date, or a bare clock reading on the tool path
- **THEN** nothing is stored in each case and the error names an accepted form

### Requirement: A nonexistent local time is rejected on every path
A wall-clock reading that does not exist in the owner's timezone because the clocks moved forward SHALL be rejected with an explicit error, on every input path, with nothing stored. The error SHALL name the date, the transition, and both valid neighbouring readings, and those neighbours SHALL be **derived from the transition boundaries** — the last valid instant before and the first valid instant after — never computed as a fixed one-hour offset, since a gap is not always an hour wide and a named neighbour that is itself invalid makes the error useless.

The system SHALL NOT accept such a reading and silently store the instant the timezone library assigns it — which renders back as a different wall clock than the one submitted — and SHALL NOT advance to a later date or a later day to find a valid reading, because a reminder silently scheduled a day late is a broken promise. On the tool path the error SHALL instruct the agent to ask the owner which neighbouring reading was meant, and SHALL NOT be phrased so as to invite a retry with a substituted time: a rejection aimed at the model otherwise delegates the invention of intent rather than preventing it.

#### Scenario: Nonexistent ISO timestamp rejected
- **WHEN** the agent schedules a reminder for a wall clock inside the spring-forward gap
- **THEN** nothing is stored, the error names the gap and both valid neighbouring readings, and it tells the agent to ask the owner which was meant

#### Scenario: Nonexistent command time rejected
- **WHEN** the owner sends `/remind <HH:MM> …` for a reading inside the spring-forward gap on the next occurrence of that clock reading
- **THEN** nothing is scheduled, the error names the gap, and no reminder is scheduled a day later instead

#### Scenario: Neighbours come from the transition, not from a fixed hour
- **WHEN** a nonexistent reading is rejected in a timezone whose transition is not one hour wide
- **THEN** the neighbouring readings named in the error are both valid readings in that timezone

#### Scenario: An ordinary reading is not rejected
- **WHEN** a reminder is scheduled for a wall clock on a date with no timezone transition
- **THEN** it is accepted and no nonexistent-time error is produced

### Requirement: An ambiguous local time resolves to the earlier instant and says so
A **fully specified** wall-clock reading that occurs twice in the owner's timezone because the clocks moved back SHALL resolve to the **earlier** of the two instants, and the confirmation SHALL disclose that the reading occurred twice and which one was chosen. Detection SHALL NOT rely on the nonexistent-time check: both occurrences of an ambiguous reading render back to the wall clock that was submitted, so that check cannot see them.

Where the reading is not fully specified — the next-occurrence search for a bare clock reading — the rule is the earliest occurrence whose instant is strictly after the current instant, which is the **later** occurrence when the earlier one has already passed. That is a refinement of the earlier-is-never-late rule once "future" is a constraint, not an exception to it.

#### Scenario: Ambiguous reading takes the earlier instant
- **WHEN** a reminder is scheduled for a fully specified wall clock that occurs twice on the autumn transition night
- **THEN** the stored instant is the earlier of the two

#### Scenario: The ambiguity is disclosed
- **WHEN** an ambiguous reading is accepted
- **THEN** the confirmation states that the reading occurs twice that night and which occurrence was scheduled

#### Scenario: An ordinary reading carries no ambiguity disclosure
- **WHEN** a reminder is scheduled for a wall clock on a date with no timezone transition
- **THEN** the confirmation contains no statement that the reading occurs twice

#### Scenario: The next occurrence may be the later of the two
- **WHEN** the owner sends `/remind <HH:MM> …` for the repeated clock reading, during the first pass of that repeated hour and after that reading has passed
- **THEN** the reminder is scheduled for the second occurrence that night, disclosed as such, and not for the following day

### Requirement: Duration input is added to the instant, never to the wall clock
A relative offset (`+90m`, `+2h`, `+3d`) SHALL be added to the current instant, so it denotes elapsed time and is unaffected by any timezone transition it spans. It SHALL NOT be resolved as a wall clock, SHALL therefore never produce a nonexistent or ambiguous reading, and the nonexistent/ambiguous evaluation SHALL be skipped entirely on this path — a check there can only produce a false rejection. The resulting local time SHALL be shown in the confirmation, so a transition-crossing offset is disclosed rather than hidden.

The duration form SHALL be available on **both** the tool and the command path. Restricting it to the command path would force the model to express an elapsed interval as a wall clock, which is wall-clock arithmetic and therefore wrong by an hour across a transition, in the one place the test suite cannot reach; it would also make a short interval unsatisfiable, since the per-turn time header gives a retry no fresher clock.

The magnitude SHALL be bounded by the accepted grammar and SHALL be strictly positive: an unbounded magnitude raises from the arithmetic before the horizon check can reject it, and a zero magnitude resolves to the current instant rather than to a future one.

#### Scenario: Offset across a transition is elapsed time
- **WHEN** either path schedules `+3d` from an instant three days before a timezone transition
- **THEN** the due instant is exactly 72 hours later and the confirmation shows the resulting local time, which differs from the starting wall clock by the transition's offset change

#### Scenario: Offsets never fail on a transition
- **WHEN** a relative offset lands inside a spring-forward gap in wall-clock terms
- **THEN** the reminder is scheduled without error

#### Scenario: A duration submission has no wall-clock invariant
- **WHEN** a relative offset is accepted
- **THEN** the rendered local time is that of the resulting instant, and no wall-clock round-trip invariant is asserted for it

#### Scenario: Absurd and zero magnitudes are refused by the grammar
- **WHEN** a relative offset whose magnitude exceeds the accepted digit bound, or whose magnitude is zero, is submitted
- **THEN** nothing is stored and the error names an accepted form, with no arithmetic error surfacing

### Requirement: Resolved due times render identically on every surface
One shared renderer SHALL produce every human-facing time — tool results, all command replies, the read tool, **and the per-turn current-time header** — so a time never reads differently in two places. The rendering SHALL include the weekday, the date, the local time in the owner's currently configured timezone, and the timezone's abbreviation or numeric offset as that zone reports it (not every zone has an alphabetic abbreviation). It SHALL be produced from an instant, not from a submitted string.

Weekday and month names SHALL NOT depend on the process locale: they SHALL come from a fixed table rather than from locale-sensitive formatting, so adding locales to the image cannot change the language of a reminder confirmation.

#### Scenario: Confirmation carries weekday and zone
- **WHEN** a reminder is scheduled successfully
- **THEN** the confirmation names the reminder's id and renders the due time with its weekday, local time and the zone's abbreviation or numeric offset

#### Scenario: The same reminder reads the same everywhere
- **WHEN** the same reminder appears in a scheduling confirmation, in the pending listing, and in the read tool's result
- **THEN** its due time is rendered identically in all three

#### Scenario: Now and due read the same way
- **WHEN** the current-time header and a due time are rendered for the same instant
- **THEN** the two strings are identical

#### Scenario: Locale does not reach the rendering
- **WHEN** the same instant is rendered under differing process locale settings
- **THEN** the weekday and month names are identical in every case

### Requirement: remind tool schedules an owner reminder
The system SHALL provide a `remind` tool (class: mutating, authorization tier: standing, turn scope: owner-only) taking the reminder `text` and a `when` value. The accepted `when` forms SHALL be enumerated in the tool's own declaration and SHALL be exactly: a local date-time with an explicit time of day and **no** UTC offset, and a relative offset (`+90m`, `+2h`, `+3d`). A bare clock reading SHALL NOT be accepted on this path — the next-occurrence search belongs to the command path, where the owner reads the confirmation immediately. The reminder SHALL be durable before the tool result reports success, and the result SHALL confirm it with the reminder's id and the resolved due time, so a mis-resolved time is visible in the same reply. Empty or whitespace-only text SHALL be rejected, as SHALL text exceeding the configured length limit — with an explicit error result naming the limit and nothing stored. Turn-scope and taint enforcement are governed by the approval-gate spec.

#### Scenario: Reminder scheduled and echoed
- **WHEN** the agent invokes `remind` with non-empty text and a valid future timestamp in an untainted owner session
- **THEN** a `pending` reminder exists and the tool result names its id and the rendered due time

#### Scenario: Scheduling never prompts
- **WHEN** the agent invokes `remind` during an untainted owner conversation
- **THEN** the reminder is stored without any approval prompt being sent

#### Scenario: Relative offsets work on the tool path
- **WHEN** the agent invokes `remind` with a relative offset
- **THEN** the reminder is scheduled at that elapsed interval from the current instant, with no wall-clock arithmetic involved

#### Scenario: Empty reminder fails safe
- **WHEN** the agent invokes `remind` with whitespace-only text
- **THEN** nothing is stored and the tool returns an explicit error result

#### Scenario: Over-limit text fails safe
- **WHEN** the agent invokes `remind` with text longer than the configured limit
- **THEN** nothing is stored and the error result names the limit; no truncated variant is stored

### Requirement: cancel_reminder tool cancels without deleting
The system SHALL provide a `cancel_reminder` tool (class: mutating, authorization tier: standing, turn scope: owner-only) taking a reminder id. Cancelling SHALL set the reminder's status to `cancelled` and SHALL NOT delete the row or alter its text or due instant. The tool result SHALL echo the cancelled reminder's text and rendered due time, so a wrong cancellation is visible in the same reply, and SHALL name the owner command that restores it. An id that is unknown or not `pending` SHALL change nothing and SHALL return an explicit result saying no pending reminder has that id.

#### Scenario: Cancellation is a status change
- **WHEN** the agent invokes `cancel_reminder` for a pending reminder
- **THEN** the row is still present with status `cancelled`, its original text and due instant, and the result echoes both

#### Scenario: Cancellation names its undo
- **WHEN** a cancellation succeeds
- **THEN** the result names the `/reminders reinstate <id>` command

#### Scenario: Unknown id changes nothing
- **WHEN** the agent invokes `cancel_reminder` with an id no pending reminder has
- **THEN** no reminder changes status and the result says so

### Requirement: Reinstatement is owner-authored only, and refuses a past due time
The system SHALL NOT provide any tool that reinstates a cancelled reminder: reinstatement SHALL be reachable only through the `/reminders reinstate <id>` owner command, whose text never passes through the model. Reinstating SHALL return the reminder to `pending` with its original text and due instant, SHALL be subject to the pending cap, and SHALL be refused — changing nothing — when the reminder's due instant has already passed, naming `/remind` as the way to set a new time. Reinstating anything other than a `cancelled` reminder SHALL change nothing and say so.

#### Scenario: No reinstate tool exists
- **WHEN** the registered toolset is inspected
- **THEN** it contains no tool that reinstates a reminder

#### Scenario: Owner reinstates a cancelled reminder
- **WHEN** the owner sends `/reminders reinstate <id>` for a cancelled reminder whose due instant is still in the future
- **THEN** its status becomes `pending` with its original text and due instant, and the reply renders that due time

#### Scenario: Past-due reinstatement refused
- **WHEN** the owner reinstates a cancelled reminder whose due instant has passed
- **THEN** nothing changes and the reply states the time has passed and names `/remind`

#### Scenario: Reinstatement respects the pending cap
- **WHEN** the pending cap is already reached and the owner reinstates a cancelled reminder
- **THEN** nothing changes and the reply names the cap

### Requirement: No code path edits or deletes a reminder
No tool, command, or repository method in this capability SHALL delete a reminder row or modify a stored reminder's text or due instant. Reaching a terminal status SHALL be a state change, not a removal, so the record of what the owner asked for survives. Changing when a reminder fires SHALL be done by cancelling it and scheduling a new one, each with its own confirmation.

#### Scenario: Terminal status is not deletion
- **WHEN** a reminder has been cancelled
- **THEN** its row is still present with the terminal status and its original text and due instant

#### Scenario: No reschedule or edit path exists
- **WHEN** the registered toolset and the owner command set are inspected
- **THEN** neither contains an operation that rewrites a reminder's text or moves its due time

### Requirement: Owner reminder commands
The system SHALL provide owner commands handled without an agent turn. `/remind <when> <text>` SHALL schedule a reminder, accepting explicit time forms only — `HH:MM` (the next occurrence of that clock reading), a dated local date-time, and a relative offset (`+90m`, `+2h`, `+3d`) — and SHALL reply with the reminder's id and rendered due time. The two-token dated form SHALL be recognized before the single-token forms so a dated time is not mistaken for a clock reading followed by text. An unrecognized time form SHALL schedule nothing and reply naming the accepted forms; a recognized time with no remaining text SHALL schedule nothing and reply that the reminder text is required.

The next occurrence of a bare clock reading SHALL be **selected before** any nonexistent/ambiguous evaluation: the earliest occurrence on today's local date whose instant is strictly after the current instant — considering both occurrences where that reading is repeated — and only if neither is future, the same reading on the next local date, advancing at most one date. The nonexistent and ambiguous rules SHALL then apply to the selected candidate in full, including the ambiguous rule's disclosure, since an advanced candidate may be ambiguous rather than nonexistent. Evaluating before selecting SHALL NOT be done: it refuses a schedule the owner can have, because the reading they meant is on the next date and exists.

`/reminders` SHALL list pending reminders oldest-due first with their ids, rendered due times and text, bounded by a configured page size. `/reminders cancel <id>` SHALL cancel that pending reminder and reply echoing its text, and `/reminders reinstate <id>` SHALL restore it per the reinstatement requirement; an unknown id SHALL change nothing and reply that no such reminder exists. An unrecognized `/reminders` subcommand SHALL change nothing and reply naming the accepted subcommands. Receipts are governed by the audit-log spec.

#### Scenario: Owner schedules directly
- **WHEN** the owner sends `/remind +2h call the plumber`
- **THEN** a pending reminder exists two hours out, the reply names its id and rendered due time, and no agent session or model tokens are used

#### Scenario: Clock-reading form resolves to the next occurrence
- **WHEN** the owner sends `/remind 07:30 leave for the train` at 21:00 local
- **THEN** the reminder is scheduled for 07:30 the following local date and the reply renders that date

#### Scenario: Next occurrence is a calendar day, not 24 hours
- **WHEN** the owner sends `/remind <HH:MM> …` for a reading already past today, on the day before a timezone transition
- **THEN** the reminder resolves to that same clock reading on the next local date, not to the instant 24 hours later

#### Scenario: Dated form is not mistaken for a clock time
- **WHEN** the owner sends `/remind 2026-08-25 07:30 buy bread`
- **THEN** the reminder is scheduled for 07:30 on 2026-08-25 and its text is `buy bread`

#### Scenario: On the transition evening, the next occurrence is the next date
- **WHEN** the owner sends `/remind <HH:MM> …` on the evening of the spring-forward date, for a reading that fell inside that date's gap and has already passed
- **THEN** the reminder is scheduled for that reading on the following date, which exists, rather than being refused as nonexistent

#### Scenario: Before the gap on the same night, the reading is refused
- **WHEN** the owner sends the same `/remind <HH:MM> …` earlier that night, while the reading is still ahead on that date
- **THEN** nothing is scheduled and the reply names the gap

#### Scenario: Unrecognized time form is honest
- **WHEN** the owner sends `/remind sometime next week water the plants`
- **THEN** nothing is scheduled and the reply names the accepted time forms

#### Scenario: Missing text is refused
- **WHEN** the owner sends `/remind +2h` with no text
- **THEN** nothing is scheduled and the reply states that the reminder text is required

#### Scenario: Pending reminders are listable
- **WHEN** pending reminders exist and the owner sends `/reminders`
- **THEN** the reply lists them oldest-due first with id, rendered due time and text

#### Scenario: Owner cancels a reminder
- **WHEN** the owner sends `/reminders cancel <id>` for a pending reminder
- **THEN** its status becomes `cancelled`, the row is retained, and the reply echoes its text

#### Scenario: Unknown cancel id fails honestly
- **WHEN** the owner sends `/reminders cancel 9999` and no pending reminder has that id
- **THEN** nothing changes and the reply states no pending reminder has that id

#### Scenario: Unknown subcommand is honest
- **WHEN** the owner sends `/reminders frobnicate 3`
- **THEN** nothing changes and the reply names the accepted subcommands

### Requirement: reminders_read tool
The system SHALL provide a `reminders_read` tool (class: read-only) returning pending reminders oldest-due first with their ids, rendered due times and text. The number of returned items SHALL be clamped to a bounded maximum. An empty schedule SHALL be reported as an empty schedule, never as an error.

#### Scenario: Agent reads the schedule
- **WHEN** pending reminders exist and the agent invokes `reminders_read`
- **THEN** the result lists them oldest-due first with id, rendered due time and text

#### Scenario: Nothing scheduled reads as nothing scheduled
- **WHEN** no pending reminders exist and the agent invokes `reminders_read`
- **THEN** the result states there are no pending reminders

#### Scenario: Result size is bounded
- **WHEN** more pending reminders exist than the bounded maximum
- **THEN** the result contains at most that maximum and says how many were not shown

### Requirement: The owner timezone is configured and fails closed
The owner's timezone SHALL be a configuration value with no default, validated at configuration load. An unknown zone SHALL fail startup with an error naming the value, and no reminder SHALL ever be resolved against a fallback zone. The configured value SHALL be a Region/Location zone key; a value that resolves against the host's local time rather than a named zone SHALL be refused, since validating only that a key resolves would admit it. Enabling reminders without a configured owner timezone SHALL fail startup with an error naming both settings.

The timezone database SHALL be a declared, version-pinned dependency of the deployed image and SHALL be the **only** source consulted at runtime: declaring the dependency alone guarantees availability but not precedence, because a zone database present in the base image is searched first and a stale one would win silently. A missing database SHALL fail loudly at first resolution rather than falling back to any other source.

#### Scenario: Unknown timezone fails startup
- **WHEN** the configured owner timezone is not a known zone
- **THEN** startup fails with an error naming the value

#### Scenario: A host-local zone value is refused
- **WHEN** the configured owner timezone names the host's local time rather than a Region/Location zone
- **THEN** startup fails, even though the value would otherwise resolve

#### Scenario: Enabled without a timezone fails startup
- **WHEN** reminders are enabled and no owner timezone is configured
- **THEN** startup fails with an error naming both the enable flag and the timezone setting

#### Scenario: Disabled without a timezone starts normally
- **WHEN** reminders are disabled and no owner timezone is configured
- **THEN** startup succeeds and behaviour is unchanged from before this change

#### Scenario: The image resolves zones from the pinned database
- **WHEN** a zone is resolved inside the built image
- **THEN** it resolves from the declared dependency, and the source is recorded by the verification rather than inferred from the resolution succeeding

### Requirement: Time resolution does not depend on the process timezone
Resolution and rendering SHALL depend only on the configured owner timezone. No code path SHALL consult the process or host timezone: no naive current-time read, no zone-less conversion between an instant and a wall clock in either direction. Identical inputs SHALL produce identical stored instants and identical rendered strings regardless of the process timezone, and this SHALL be enforced by the test suite rather than by convention, because a leak is correct-looking on any host whose zone happens to match the owner's and wrong only where it is deployed.

#### Scenario: A hostile process timezone changes nothing
- **WHEN** the same schedule is resolved and rendered under process timezones differing from the owner's by a whole calendar date
- **THEN** the stored instant and the rendered string are identical in every case

#### Scenario: The guard covers every clock-touching surface
- **WHEN** the resolver, the renderer, the reminder owner commands, and the current-time header are exercised under a hostile process timezone
- **THEN** each produces the same result as under the owner's own timezone

### Requirement: Reminder store failures are loud but honest
A store write failure on any scheduling, cancellation, or reinstatement path SHALL surface as an explicit error and SHALL NOT be reported as success — an owner who believes a reminder is set when it is not is the capability's worst failure. A store read failure SHALL surface as an explicit error and SHALL NOT be presented as an empty schedule.

#### Scenario: Failed schedule is never reported as success
- **WHEN** a `remind` write fails
- **THEN** the tool returns an explicit error result and no confirmation naming a due time is produced

#### Scenario: Unreadable schedule is not "nothing scheduled"
- **WHEN** the store cannot be read and the owner sends `/reminders`
- **THEN** the reply states the failure, not an empty schedule

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
A `pending` reminder whose due instant lies more than the configured grace window (default 24 hours) before the captured instant SHALL move to `missed` in the pre-work transaction — counter cleared, `next_attempt_at` set, `reported_at` left null, `missed` audit record written — and SHALL NOT be delivered as a reminder: a day-old instruction delivered as if current is worse than useless. A reminder overdue within the grace window is delivered under the late-delivery rule. No overdue reminder SHALL reach a terminal status without either being delivered or being named in an attempted catch-up summary — with exactly one exception, which the crash-attempt bound's pre-work placement makes unavoidable: a report row retired by the **crash-attempt** give-up after repeated process deaths between the pre-work commit and the summary's dispatch was never named, because no summary for it was ever attempted. That exit SHALL be error-logged and is bounded by the crash-attempt limit, so it is loud rather than silent, and it SHALL NOT be read as an assertion that the owner was told. The **report horizon** carries no equivalent residual, and that is why it is evaluated post-send instead.

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

The horizon SHALL be evaluated **only in the post-send write of an attempted summary**, never in the pre-work transaction: a give-up there could retire a row that arrived already stale (a restart after long downtime) before any summary ever named it. Because the check runs post-send, every row that reaches the give-up exit on the horizon was named in at least one attempted summary. How many namings it gets is the span from the moment it became reportable to `due_at + grace + horizon`, divided by the retry floor, **plus the one final attempt in whose post-send write the give-up is written** — so the count is three-valued rather than one number: a row that went `missed` on time becomes reportable at `due_at + grace` and gets on the order of horizon-over-floor attempts; a row that exited to `abandoned` becomes reportable within a few ticks of its due instant, since nothing waits a grace window to abandon, and therefore gets on the order of **(grace + horizon) over floor** — roughly twice as many, and the largest of the three; a row that arrived already stale gets exactly one. All three are bounded, which is the property; none of them is the single figure "horizon over floor". The crash-attempt give-up stays in the pre-work transaction, unchanged and for the opposite reason: a crash is what prevents the post-send write, so its bound is never evaluable there, while a channel outcome exists only after the send returns. The horizon is the report path's only channel-outcome bound — the crash-attempt limit cannot fire on channel outcomes, since the counter clears on every return — and without it a summary whose send deterministically returns partial would re-deliver its head chunks every floor interval, forever.

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
- **THEN** each named row's summary sends are bounded by the span from its reportability to `due_at + grace + horizon` over the retry floor, plus the one final attempt whose post-send write performs the give-up, and once a named row's due instant is older than the grace window plus the horizon its `reported_at` is written as the give-up exit — in the post-send write of an attempted summary, with an error log — and it is never named again

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
