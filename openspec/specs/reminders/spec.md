# reminders Specification

## Purpose
Owner-scheduled one-shot reminders: a short text plus one absolute instant, durable in the same
SQLite store that already holds memories and the capture inbox. Wall-clock input is resolved in the
owner's configured timezone — honest about nonexistent and ambiguous local times — and every surface
renders a due time identically. Nothing is ever edited or deleted, so a reminder's history is a chain
of receipted status changes. This capability owns the scheduling half; the clock that delivers is
specified separately, and reminders ship disabled until it exists.
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
Any code path that puts a reminder row into status `pending` — scheduling by tool, scheduling by command, or reinstating — SHALL write `next_attempt_at` explicitly as part of the same write. A `pending` row SHALL NEVER exist with a null `next_attempt_at`, because the delivery selector is a query and a null value would make the row permanently unselectable while still reporting itself as pending.

#### Scenario: Scheduling sets the selector column
- **WHEN** a reminder is scheduled by either the tool or the command path
- **THEN** the stored row's `next_attempt_at` is non-null

#### Scenario: Reinstating sets the selector column
- **WHEN** a cancelled reminder is reinstated to `pending`
- **THEN** the stored row's `next_attempt_at` is non-null

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
A configuration flag SHALL disable the capability, defaulting to disabled: all reminder tools SHALL be absent from the registered toolset, every reminder owner command SHALL reply that reminders are not configured, and no reminder SHALL be scheduled by any path. Stored reminders SHALL remain untouched and SHALL become operable again if the capability is re-enabled. No configuration flag SHALL widen the capability — there SHALL be no setting that promotes `remind` or `cancel_reminder` beyond standing tier, widens their turn scope, or directs a reminder to any identity but the configured owner.

#### Scenario: Disabled means inert, not destructive
- **WHEN** reminders are disabled and the process runs with pending reminders in the store
- **THEN** no reminder tool is registered, the commands reply honestly, and every stored reminder is unchanged

#### Scenario: Disabled by default
- **WHEN** the configuration carries no reminders section
- **THEN** the capability is disabled

#### Scenario: Re-enabling restores access to stored reminders
- **WHEN** reminders are re-enabled after a period disabled
- **THEN** previously stored pending reminders are listed by `/reminders` with their original text and due times
