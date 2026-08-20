# Reminders Core

## Why

Henk can remember a fact and catch a thought, but he cannot bring either back at the moment it
matters — "remind me at six to move the bins" has nowhere to land, so it becomes a capture the
owner has to go looking for. NORTH-STAR names reminders as roadmap change 2, and the attention
contract already draws the line this capability needs: *owner-scheduled sends are fine;
system-scheduled digests, heartbeats and "all is well" messages stay banned.*

This change builds everything a reminder needs **except the clock**: the durable table, the time
resolution that turns "tomorrow at 07:30" into a specific instant without lying about DST, the
tools and commands that write and read it, and the receipts. It deliberately does **not** deliver
anything. That split is not tidiness — a version that confirms reminders it cannot deliver is
the capability's worst possible failure, so this change ships with `reminders.enabled: false` and
`reminder-delivery` is what turns it on.

Two pieces of unfinished business from the superseded draft's review are in scope here because
nothing downstream works without them:

- **The store has no transaction API.** `henk/store/db.py` hands out a raw connection and every
  repository method calls `conn.commit()` itself, so under pysqlite's implicit `BEGIN` any
  repository call made inside an open transaction commits it. Every multi-write guarantee in the
  delivery design is unimplementable until this is fixed.
- **All DDL is `CREATE TABLE IF NOT EXISTS` with no migration mechanism**, so a column added
  after the table exists on rp5 is never created. The reminders table must therefore ship its
  **complete final column set** in this change, including the columns only `reminder-delivery`
  will write.

## What Changes

- **A reminder is a row: short text plus one absolute instant.** The `reminders` table joins
  `memories` and `inbox` in the same SQLite file on the same backed-up volume — no new deploy
  surface. Statuses are `pending`, `delivered`, `delivered-late`, `missed`, `cancelled`,
  `abandoned`; nothing is ever deleted and reminder text is never edited, so a terminal status is
  a state change and the record of what the owner asked for survives. Recurring schedules are
  explicitly out of scope.
- **The complete final column set ships now.** `next_attempt_at`, `send_attempts`, `reported_at`,
  `surfaced_at` and `delivered_at` are created with the table even though only
  `reminder-delivery` writes them, because there is no migration path to add them later. Every
  path in this change that puts a row into `pending` initializes `next_attempt_at`, since a null
  there makes a row permanently unselectable (`NULL <= now` is not true). A startup check
  compares the live table's columns against the expected set and fails loud on drift — that is
  the enforcement of "no migrations", not an invitation to skip them.
- **`Store.transaction()`: an explicit `BEGIN IMMEDIATE` context manager**, with the connection
  moved to autocommit so pysqlite stops opening transactions behind the code's back. Every
  reminder repository method is **transaction-agnostic**: it wraps its own work in
  `transaction()`, which is correct standalone and a no-op join when a caller already opened one.
  A nested block that raises poisons the outer transaction, so a swallowed inner failure cannot
  be committed by the outer scope. The existing memory and inbox repositories are ported to the
  same discipline — their multi-statement paths (cap eviction, mark-done) keep the atomicity the
  implicit `BEGIN` was giving them for free.
- **Time is resolved by the model, validated by the app, and echoed back.** `remind(text, when)`
  takes an ISO-8601 timestamp; the app resolves it to an instant in the owner's timezone, rejects
  the past and anything past a horizon, and **echoes the resolved local time with the weekday and
  zone abbreviation** so a mis-resolution is visible in the same reply. Because a model cannot
  resolve "tomorrow at 8" without knowing now, **every owner turn carries a one-line current-time
  header** — a session an hour old must not schedule an hour late.
- **DST is handled explicitly, in both directions, with a stated rule per input family.**
  Wall-clock inputs (`HH:MM`, dated local date-times, naive ISO) are resolved through the owner's
  zone and are subject to its transitions; duration inputs (`+90m`, `+2h`, `+3d`) are added to the
  instant and are not. A **nonexistent** local time (spring forward — `02:30` on the transition
  night, which today would silently round-trip to `03:30`) is **rejected** naming the gap and the
  two valid neighbours, computed from the transition rather than assumed to be an hour away, on
  every path including bare `HH:MM`, which never advances a day to dodge it. An **ambiguous** local
  time (fall back — `02:30` happens twice, and both folds round-trip cleanly, so the nonexistent
  check cannot see them) resolves to the **earlier** instant and says so in the echo — except in the
  next-occurrence search, where the rule is the earliest occurrence still ahead, which may be the
  second. Every past/future comparison is made on instants, never on aware date-times, whose
  ordering inside a repeated hour is a wall-clock comparison and reports a past instant as future.
- **An offset is a conversion the model performed, so `when` refuses one.** `remind` takes a naive
  local date-time or a duration; an offset or `Z` suffix is rejected naming the accepted form.
  `2026-08-25T07:30:00Z` would otherwise become 09:30 CEST — a silent two-hour error on the one
  path where the app has nothing left to validate. The principle, stated so the next narrowing can
  be checked against it: the app owns every arithmetic a DST transition can perturb; the model
  names a target and never converts between forms. Durations are therefore on **both** paths — a
  model forced to express "in three days" as a wall clock produces 71 or 73 hours across a
  transition, in the one place the test suite cannot reach.
- **Nothing reads the process timezone, and the suite proves it.** A single naive `now`, bare
  `astimezone()`, or `.timestamp()` on a naive value (which is what `fromisoformat` returns) reads
  the host zone: correct on a dev host that happens to match the owner's, two hours wrong on rp5.
  Every clock-touching test therefore runs under a hostile `TZ` — `Pacific/Kiritimati`, so a leak
  changes the date and not merely the hour. The owner's timezone is validated at config load; an
  unknown zone, or one naming host-local time, fails startup rather than falling back.
- **Three tools: `remind`, `cancel_reminder`, `reminders_read`.** `remind` and `cancel_reminder`
  are mutating, **standing**, owner-turn-only; `reminders_read` is read-only. Cancelling is a tool
  here — reversing the draft's command-only stance — because "cancel that one" is the natural next
  sentence after scheduling, and the reversal is safe for a specific reason: a cancel is a status
  change, never a deletion; it echoes the reminder's text and due time; and `/reminders reinstate`
  puts it back. There is no `reschedule_reminder` tool (it is cancel + remind, two calls with two
  echoes) and no `reinstate_reminder` tool (it would bypass the pending cap and re-arm a message
  the owner deliberately killed).
- **Four owner commands, costing no model turn.** `/remind <when> <text>` accepts explicit forms
  only — `HH:MM` (next occurrence of that clock time), `YYYY-MM-DD HH:MM`, and `+90m` / `+2h` /
  `+3d`; `/reminders` lists pending oldest-due first with ids; `/reminders cancel <id>` and
  `/reminders reinstate <id>` are the owner's undo and its undo. Reinstating a reminder whose due
  time has passed is refused, naming `/remind` as the way to set a new one — which keeps the
  late/missed question entirely inside `reminder-delivery`.
- **Receipts, two records for two questions.** A `remind` tool call writes both an
  `authorization` receipt (was it allowed?) and a `reminder` lifecycle record (what changed?);
  they answer different questions and are not collapsed. `reminder` records carry the reminder's
  id, due time, status, initiator and timestamp — and **not its text**: the store holds the
  content, the audit log holds the evidence. `schema_version` bumps to **4**, and v4 is the
  *complete* reminder schema — including the `scheduler` initiator and the `delivered` /
  `delivered-late` / `missed` / `abandoned` statuses that only `reminder-delivery` writes — so
  that change needs no second bump.
- **Ships inert.** `reminders.enabled: false` unregisters all three tools, makes the commands
  reply honestly that reminders are not configured, and leaves stored rows untouched. Enabling
  additionally requires `owner.timezone`; with reminders enabled and no timezone set, startup
  fails naming both keys. No configuration may widen the capability.

Out of scope, by design and named so no reader mistakes it for an omission: the scheduler,
delivery, the grace/late/missed catch-up, the delivered-reminder note (its `surfaced_at` column
ships, its injection does not), retry and crash bounds, and the `incident-triage` cadence
amendment. All of that is `reminder-delivery`. Read receipts and the typing indicator are
`owner-acknowledgement`.

## Capabilities

### New Capabilities

- `reminders`: the durable reminder store and its final column set; the explicit store
  transaction boundary the store never had; DST-correct resolution of owner and model time input
  to an absolute instant, with one shared renderer for every echo; the `remind`,
  `cancel_reminder` and `reminders_read` tools; the `/remind` and `/reminders` command family;
  the pending cap, text limit and horizon; and the kill switch. Delivery is added to this same
  capability by `reminder-delivery`.

### Modified Capabilities

- `agent-core`: the owner command set gains `/remind <when> <text>`, `/reminders`,
  `/reminders cancel <id>` and `/reminders reinstate <id>`; every owner turn carries a
  current-time header in the owner's timezone (event turns never do); the system prompt
  enumerates the three reminder tools when enabled and states that the agent can schedule and
  cancel but cannot reinstate, naming the command that can.
- `audit-log`: `schema_version` 4 adds the `reminder` record type and the `scheduler` value for
  `initiated_by`; reminder records are durable at the moment of each transition and carry no
  reminder text; a rejected schedule writes no record.
- `approval-gate`: names the reminder tools' tier and scope — `remind` and `cancel_reminder`
  mutating/standing/owner-turn-only, `reminders_read` read-only — and states that both mutating
  tools are covered by the standing-tier demotion kill switch.
- `secure-deployment`: the reminders table shares the existing memory/inbox SQLite store on the
  backed-up audit volume; this change adds no volume, port, socket, ACL grant or secret.

## Impact

- **Code:** new `henk/store/reminders.py` (repository, transaction-agnostic) and
  `henk/reminders/timeparse.py` (resolution, validation, the single shared renderer);
  `henk/store/db.py` (reminders DDL and index, `Store.transaction()`, autocommit connection,
  column-drift check); `henk/store/memory.py` + `henk/store/inbox.py` (ported off self-commits —
  required by the isolation change, not optional cleanup); `henk/store/factory.py` +
  `henk/store/__init__.py` (the third repository and its exports); new `henk/tools/reminders.py`;
  `henk/tools/__init__.py` (registration behind `reminders.enabled`); `henk/agent/commands.py`
  (the `/remind` and `/reminders` family); `henk/agent/core.py` (the per-owner-turn time header);
  `henk/config.py` + `config.yaml` (`RemindersConfig`, `owner.timezone`, and the
  **`AgentConfig.system_prompt` enumeration — which currently hardcodes "exactly these seven"**,
  a count this change changes); `henk/audit/logger.py` + a new
  `henk/audit/schema/audit-record.v4.schema.json` (`reminder_record`, `SCHEMA_VERSION = 4`);
  `henk/runtime.py` (wiring the repository, clock and renderer into commands and the registry);
  `Dockerfile` (`PYTHONTZPATH=""`, `TZ=UTC`) and `pyproject.toml` (`tzdata`).
- **Known test changes**, enumerated rather than promised. Any test that asserts the system
  prompt's tool count or its "exactly these seven" phrasing must be updated (`grep -rn "seven"
  tests/`). Tests that construct `Config`/`OwnerConfig` positionally break if `owner.timezone` is
  added as a positional field — it is added with a default of `None` for that reason, with the
  enabled-without-timezone case enforced at load. Store tests that assert `conn.commit()`
  behaviour or in-flight transaction state need review against autocommit; the memory
  cap-eviction and inbox mark-done tests are the atomicity regression net for that port and must
  pass untouched.
- **Dependencies:** one added — `tzdata` (pure data, no code, no transitive dependencies).
  `zoneinfo` reads the operating system's zone database and `python:3.12-slim` is not guaranteed to
  carry one. The wheel alone guarantees *availability* but not *precedence* — `TZPATH` is searched
  first, so a trimmed or stale tree in the base image wins silently, and this dev host's tree is
  already trimmed (498 zones, `US/Eastern` absent). The image therefore also sets
  `PYTHONTZPATH=""`, making the pinned wheel the only source: the zone database then moves on a
  reviewed dependency bump rather than on a base-image rebuild, and a missing wheel fails loudly
  at first resolution instead of falling back to something older. Nothing removed.
- **Deployment:** no new volume, published port, listening socket, ACL grant or secret. Two
  `Dockerfile` environment lines (`PYTHONTZPATH=""`, `TZ=UTC`). The existing store file gains a
  table and the audit volume's backup allowlist entry already covers it. The new config keys must be
  confirmed against rp5's live `config.yaml`, which is locally modified and will not carry them:
  `reminders.enabled` defaults to `false`, so the deployed behaviour is unchanged until the host
  config is edited to add the section **and** `owner.timezone`.
- **Rollback:** `reminders.enabled: false` and restart. Nothing is deleted; audit records already
  written under v4 stay valid against their committed schema document, and an unused table is
  inert.
- **Blocked work unblocked:** `reminder-delivery` — which needs the table, the transaction
  boundary, the resolved instants and the v4 records, and supplies the only thing that makes any
  of it visible to the owner.
