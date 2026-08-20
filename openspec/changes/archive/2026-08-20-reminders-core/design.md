# Design — Reminders Core

## Context

This change lands the half of reminders that has no clock in it. The superseded single change was
reviewed to destruction — eleven rounds, six consecutive criticals, every one of them inside the
delivery machinery — and the verdict was that the store, the time resolution, the tools, the
commands and the audit records were sound while delivery was over-engineered by a third. Splitting
along that line lets the sound half ship and the other half be rewritten smaller.

What already exists and is reused unchanged:

- the SQLite store on the backed-up audit volume (`henk/store/`), lazily opened, WAL, shared by
  memories and the capture inbox;
- the two-axis permission model — an authorization tier per named action, plus a turn scope
  enforced with session taint — and the `capture` / `store_memory` precedent for a standing-tier
  mutating tool that writes into a Henk-local store;
- the app-side owner-command dispatcher, which never passes text through the model, writes its own
  receipt at execution time, and keeps working during an incident interrogation;
- the append-only audit log with decision-time receipts and a committed, versioned JSON Schema;
- `SendOutcome` and `send_proactive` from `channel-integrity` — used by `reminder-delivery`, not
  by anything here.

Two facts about the current code shape the whole design:

1. **There is no transaction boundary.** `Store.connection()` returns a raw `sqlite3.Connection`
   built with pysqlite's default `isolation_level=""`, and each repository method issues its own
   `conn.commit()`. Any repository call made inside an enclosing transaction therefore commits it.
   Nothing today notices, because no code path composes two writes; `reminder-delivery`'s
   pre-work / post-send transactions are built entirely out of that composition.
2. **There is no migration mechanism.** Every `CREATE TABLE` is `IF NOT EXISTS`, executed on first
   connect. Once the table exists on rp5, a later change that adds a column produces code that
   reads a column the database does not have — on the deployed host only, where no test runs.

Constraints inherited and not up for renegotiation: no new volume, port, socket, ACL grant or
secret; no inbound listener; no client data; every mutation receipted; fail closed on ambiguity.

## Goals / Non-Goals

**Goals:**

- A durable reminder row that survives restarts and container recreation, is never deleted, and
  never has its text rewritten by any code path.
- A store that can express "these three writes happen together or not at all", available to every
  repository sharing the file, with repository methods that work identically inside and outside a
  caller's transaction.
- Time resolution that is correct across both DST transitions, and *visibly* correct: the failure
  mode to design against is a silent one-hour error the owner discovers a week later.
- A column set complete enough that `reminder-delivery` adds no DDL.
- Deploying this change changes nothing observable until someone edits rp5's config.

**Non-Goals:**

- The scheduler, delivery, the grace/late/missed catch-up, the delivered-reminder note's
  injection, retry and crash bounds, the missed-reminder report, and the `incident-triage` cadence
  amendment. All `reminder-delivery`. The columns those need ship here; the behaviour does not.
- Recurring reminders. Recurrence needs a per-schedule cap, a missed-occurrence policy, its own
  DST rules (a daily 09:00 across a transition is a *wall-clock* schedule, which is a different
  model from this change's resolved instant) and an end condition. Own change — and one that will
  need either a second table or the migration mechanism this change deliberately does not build.
- Natural-language date parsing in the application. The model resolves; the app validates and
  echoes; the command path takes explicit forms only.
- Snooze, edit-in-place, priorities, reminders about anything Henk observes on his own, delivery
  to any identity but the configured owner.
- A general migration engine. This change adds a drift *check*, not a migration path — see D1.

## Decisions

### D1 — One table in the existing file, with its complete final column set

`reminders` joins `memories` and `inbox` in the same SQLite file, same lazy connection, same
volume, same backup allowlist entry. A separate database file would mean a second connection and a
second thing to back up; reusing the `inbox` table with a due date would conflate two capabilities
whose eviction, listing and lifecycle rules disagree (the inbox never evicts and drains
oldest-first; a reminder has a due order and terminal states). Both rejected.

The columns, all created at once:

| column | type | written by |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | core |
| `text` | TEXT NOT NULL | core |
| `due_at` | REAL NOT NULL | core — the resolved instant, epoch seconds |
| `due_tz` | TEXT NOT NULL | core — the owner zone configured at scheduling time |
| `input_spec` | TEXT NOT NULL DEFAULT '' | core — the submitted time string, silently truncated |
| `created_at` | REAL NOT NULL | core |
| `source` | TEXT NOT NULL | core — `tool` or `command` |
| `status` | TEXT NOT NULL | core + delivery |
| `next_attempt_at` | REAL NOT NULL DEFAULT 0 | core initializes, delivery advances |
| `send_attempts` | INTEGER NOT NULL DEFAULT 0 | delivery |
| `delivered_at` | REAL | delivery |
| `surfaced_at` | REAL | delivery |
| `reported_at` | REAL | delivery |

Plus `idx_reminders_status_due ON reminders(status, due_at, id)`, which serves both the pending
listing and delivery's selector.

Statuses are `pending`, `delivered`, `delivered-late`, `missed`, `cancelled`, `abandoned`. Nothing
is ever deleted — same principle as the inbox: a terminal status is a state change, not a removal,
so what the owner asked for survives being cancelled.

Three column choices carry an argument:

- **`next_attempt_at` is `NOT NULL DEFAULT 0`, not nullable.** Delivery's selector is a query, and
  `NULL <= now` is not true, so a null makes a row permanently unselectable — a reminder that
  exists, says `pending`, and can never fire. Defaulting to `0` means a forgotten initialization
  fails toward *delivered immediately*, which is loud and observable, instead of *never*, which is
  silent. The spec still requires every path into `pending` to write it explicitly; the default is
  the second line of defence, not the mechanism.
- **`due_tz` and `input_spec` are forensic, and that is enough to justify them.** With no
  migration path, a column that turns out to be wanted is unobtainable and a column that turns out
  to be unwanted is permanent — an asymmetry that argues for conservatism about *speculative*
  needs and generosity about *known* ones. Both of these are known: the review record names time
  resolution as the least-reviewed, most bug-dense part of the design, and tool-result text is
  deliberately no longer recorded in the audit log, so without `input_spec` the exact string that
  produced a mis-resolution is unrecoverable from anywhere. `due_tz` answers the other half — if
  `owner.timezone` is ever changed, a pending reminder's instant is fixed but its wall clock is
  not, and `due_tz` is what lets `/reminders` say "scheduled as 07:30 CET" instead of silently
  listing 01:30. Rendering always uses the *current* configured zone; `due_tz` is a disclosure
  and a diagnostic, never the rendering source.

  Both need a value on **every** path, not only where a wall clock was resolved, or they become
  worse than absent — a forensic column whose meaning silently varies per row cannot be read at
  all. `due_tz` is therefore **the owner zone configured at scheduling time**, on the wall-clock
  path and the duration path alike; `input_spec` is the tool's `when` argument or the command's
  `<when>` token (not the whole command line), **silently** truncated at 64 characters, because a
  diagnostic column must never be the reason a valid schedule fails. `input_spec` already records
  which family the input was, so no discriminator column is needed.
- **No `terminal_at`.** It was dead state once delivery's report time budget was cut, and a
  permanent column in a store with no migration path is exactly the wrong place to keep dead
  state.

**The drift check.** On first connect, after the DDL, the store reads
`PRAGMA table_info(reminders)` and compares the column set against the expected one, raising
`StoreError` naming the missing or unexpected columns. This is not a migration path and must not
be mistaken for one: it is the enforcement of "there is no migration path", converting the
failure mode from *code reads a column rp5 does not have, in production, silently* into *the
process refuses to start and says which column*. It costs about fifteen lines and it is the only
mechanism in the repo that would catch a future change forgetting this rule.

### D2 — `Store.transaction()`: autocommit plus explicit `BEGIN IMMEDIATE`

The connection moves to `isolation_level=None` (autocommit), and `Store.transaction()` is a
context manager that issues `BEGIN IMMEDIATE` on entry, `COMMIT` on clean exit and `ROLLBACK` on
exception. `IMMEDIATE` rather than deferred because a deferred transaction takes its write lock on
first write, which is the classic upgrade-deadlock shape; taking it up front makes contention fail
fast instead of half-way through.

Autocommit is the part that actually fixes the defect. Leaving `isolation_level=""` and issuing
`BEGIN` by hand leaves pysqlite's implicit transaction management in play alongside it, which is
the ambiguity being removed. With autocommit, no transaction exists unless this code opened one.

Three properties make repository methods **transaction-agnostic**:

1. Every write method wraps its work in `with self._store.transaction():` and calls no `commit()`
   of its own. Standalone, that is one atomic write. Inside a caller's transaction, it joins.
2. `transaction()` is **reentrant by depth**: only the outermost `__enter__` issues `BEGIN` and
   only the outermost `__exit__` commits. Nested blocks are bookkeeping.
3. A nested block that raises **poisons** the transaction: the flag is set on the way out, and the
   outermost exit rolls back rather than commits even if the exception was caught in between. Half
   of a multi-write guarantee committed because an inner failure was swallowed is precisely the
   class of bug this API exists to prevent, and "the caller must re-raise" is a convention no test
   enforces.

*Alternatives considered.* SAVEPOINTs for nested blocks, giving true partial rollback — rejected
as unneeded: no caller wants to continue a transaction after part of it failed, and poisoning
expresses that directly. A `commit=False` keyword on every repository method — rejected: it puts
the transaction decision at every call site and the wrong default is invisible. `sqlite3`'s
`autocommit` attribute with `False` — rejected: it gives implicit `BEGIN DEFERRED`, which is the
lock mode we do not want.

The store is used from one thread (the event loop) and no store call awaits, so a single shared
connection cannot interleave two transactions; `check_same_thread=False` overstates the intent.
That assumption is written down and tested, because a future `asyncio.to_thread` around a store
call would break it silently.

**Porting memory and inbox is required, not optional cleanup.** `MemoryStore.store` performs cap
eviction and an insert as several statements followed by one `commit()`; under the implicit `BEGIN`
those were one transaction. Under autocommit, unported, they become several independent commits —
a crash mid-eviction would leave a memory evicted and its replacement unwritten. The port wraps
those paths in `transaction()` and deletes the self-commits. The existing cap-eviction and
mark-done tests are the regression net and must pass untouched.

### D3 — Two input families, two arithmetic rules

This is the core of the time model, and the source of most of the errors it prevents.

- **Wall-clock inputs** — `HH:MM`, `YYYY-MM-DD HH:MM`, and an ISO-8601 value carrying no UTC
  offset — name a reading on the owner's clock. They are resolved *through* the owner's zone and
  are therefore subject to its transitions: nonexistent and ambiguous readings are possible, and
  D4/D5 say what happens.
- **Duration inputs** — `+90m`, `+2h`, `+3d` — name an elapsed interval. They are added to the
  *instant*, never to the wall clock. `+3d` is 72 hours, so a `+3d` across a fall-back lands one
  hour earlier on the clock, and that is correct: the owner asked for three days from now, and
  three days from now is when it fires. The echo shows the resulting local time, so the shift is
  disclosed rather than hidden.
- **An ISO value carrying an offset** — including a `Z` suffix — is **rejected**. It does denote an
  instant, and that is exactly the problem: the offset is a conversion *the model performed*, from a
  reading it inferred to a zone it guessed. `2026-08-25T07:30:00Z` becomes 09:30 CEST, which is the
  silent two-hour error this whole decision exists to prevent, arriving through the one door where
  the app has nothing left to validate. The app knows the owner's zone; the model has no
  information the app lacks, so an offset can only ever be a guess wearing a precise notation.
  Cross-zone intent ("remind me at 9am Tokyo time") stays expressible — the model converts to the
  owner's local reading, which is the reading the owner will actually be looking at when it fires,
  and which the echo then shows.

Both families are available on **both** paths — the `remind` tool and the `/remind` command. The
principle behind that, stated so the next narrowing can be checked against something rather than
argued from scratch: **the app owns every arithmetic a DST transition can perturb; the model owns
only naming the target — a wall clock or a duration — and never converting between them.**
"No offsets" follows from it. "No durations on the tool path" would contradict it: it would force
the model to turn "in three days" into a wall clock, which is wall-clock addition, which is 71 or
73 hours across a transition — the precise error the duration family exists to prevent, relocated
to the one place this project's tests cannot reach. It would also create an unsatisfiable rejection
loop, since the time header is composed once per turn: a model asked for "+1 minute" late in a slow
turn emits a value already past, is refused, and has no fresher clock to retry with.

**The accepted grammar is an explicit whitelist, validated before parsing.** `fromisoformat`'s
accept-set is wider than it looks — it takes `20260825`, `2026-W35` and `2026-W35-1`, and it makes
`2026-08-25` indistinguishable from `2026-08-25T00:00` after parsing, so "reject a date with no
time of day" is not detectable downstream of the parse. Accepted shapes:

| path | wall clock | duration |
|---|---|---|
| `remind` tool | `YYYY-MM-DD[T ]HH:MM[:SS[.ffffff]]` | `+<N><m\|h\|d>`, `N` of 1–6 digits |
| `/remind` command | the same, plus `HH:MM` (next occurrence, D10) | the same |

Rejected on both paths, each with its own error: any offset or `Z`; week dates; basic format;
date-only; a bare `HH:MM` on the tool path (the next-occurrence search stays on the deterministic
command path, where the owner reads the echo immediately). Surrounding whitespace is stripped
before matching. `N` is bounded in the *grammar* rather than by catching an exception, because the
arithmetic runs before the horizon check and would otherwise raise first: `+999999999d` is
`ValueError: year 2739933 is out of range` and `+99999999999999d` is an `OSError` from the platform
clock, so the horizon step never gets a turn on an input the grammar could have refused. A duration
must also be strictly positive, so `+0m` is refused rather than resolving to now.

**The validation ladder, per family** — one sequence per input shape, because a single ladder
hides two mistakes (the DST check appearing on the duration path, where it can only produce a
false rejection, and appearing *before* candidate selection on the `HH:MM` path, where it produces
a wrong rejection — see D10):

| step | dated wall clock | `HH:MM` | duration |
|---|---|---|---|
| shape match | yes | yes | yes (incl. the magnitude bound) |
| parse | yes | yes | yes |
| candidate selection | n/a — the value is the candidate | **first**, per D10 | n/a — the instant is computed |
| imaginary / ambiguous | on the value | on the **selected** candidate | skipped — a duration is never a wall clock |
| past + skew tolerance | yes | guaranteed by selection; the check is unreachable here, stated rather than relied on | strictly-positive magnitude |
| horizon | yes | yes | yes |
| text limit → pending cap | yes | yes | yes |

**The current instant is captured exactly once** per resolution, and every comparison in the ladder
uses that value. Two reads of the clock make the "unreachable" cell above false in a sub-second
window — a candidate selected as future at 07:29:59.9 is past when the ladder re-reads at
07:30:00.1, producing a spurious "that time is in the past" on a valid schedule. One read is also
one place for a process-zone leak to hide (D8a).

*Alternative considered.* Calendar-day arithmetic for `+Nd` (same wall clock, N days later), which
preserves "07:30 stays 07:30". Rejected: it silently turns a duration into a wall clock, which
means `+1d` acquires the nonexistent-and-ambiguous problem for a form that has no business having
it, and it makes `+24h` and `+1d` mean different things. One rule per family, stated, is worth
more than a locally friendlier surprise.

*Alternative considered.* Accepting an offset and cross-checking it against the owner's zone.
Rejected because the check cannot work: it cannot distinguish "the model correctly means Tokyo
9am, expressed as +09:00" from "the model wrongly `Z`-suffixed a local reading" — both disagree
with the owner's zone. A rejecting cross-check is therefore just this decision by a longer route,
and a warning cross-check is just the echo.

### D4 — A nonexistent local time is rejected, everywhere, naming the gap

On the spring-forward night, `02:30` does not exist in `Europe/Amsterdam`. Constructing it with
`zoneinfo` does not fail: PEP 495 gives an imaginary time the offset from *before* the transition,
so `02:30 CET` normalises to `01:30 UTC`, which renders back as `03:30 CEST`. Today's code would
accept it, store an instant, and echo a time the owner never typed — and one hour off is exactly
the error a reminder must not make quietly.

Detection uses the stdlib only, in two steps:

1. **Two offsets?** `dt.utcoffset() != dt.replace(fold=1).utcoffset()` is true for both imaginary
   and ambiguous local times and false otherwise. Everything else needs no further checking.
2. **Which kind?** Round-trip the value through UTC and back into the zone. If the wall clock
   comes back changed, the reading is imaginary (D4). If it comes back identical, the reading
   exists twice (D5). This is why the nonexistent check cannot find ambiguous times and vice
   versa: both folds of an ambiguous time round-trip cleanly.

Note for the implementer, because the natural generalisation is false: `fold=0` is the earlier
instant for an **ambiguous** reading only. For an imaginary one it is the *later* one
(`fold=0` → epoch 1774747800, `fold=1` → 1774744200). Anything that leans on "fold=0 is earlier"
outside D5's scope is wrong.

Imaginary readings are rejected with an error that names the gap and both valid neighbours — "02:30
does not exist on 2026-03-29 in Europe/Amsterdam; the clocks go forward at 02:00 — try 01:30 or
03:30". The neighbours are **derived from the transition boundaries** (the last valid instant
before, the first valid instant after), never computed as ±1 hour: not every gap is an hour wide,
and the "actionable in one message" defence collapses if the named neighbour is itself invalid.
On `Australia/Lord_Howe` the gap is 30 minutes; on `Antarctica/Troll` it is two hours, so the +1h
neighbour is *also* imaginary; `Pacific/Apia` once skipped a whole calendar day.

Rejection rather than a shift because there is no correct answer to pick and every choice is a
guess about intent; naming the neighbours makes the rejection actionable in one message. This
holds on **every** path: the tool, `YYYY-MM-DD HH:MM`, and bare `HH:MM`. In particular, bare
`HH:MM` does **not** skip to the following day to find a valid reading — a reminder silently
scheduled 24 hours late is a broken promise, and "next occurrence" is not licence to move a day.

On the tool path the rejection carries one more instruction: the error tells the agent to **ask the
owner which neighbour they meant**, and is not phrased so as to invite a retry with a substituted
time. Without that, a rejection aimed at the model does not avoid inventing intent — it delegates
the invention to the least accountable actor in the loop, with no echo of its reasoning.

*Alternative considered.* Shift forward to the first valid instant (03:30) and disclose it in the
echo, since the echo already exists and the owner reads it. Genuinely defensible, and it never
refuses a schedule. Rejected because it invents intent on a path where the project's posture is to
fail closed, and because the once-a-year cost of a rejection is one retyped message.

*Why this rejects while D9 accepts, since both are "visible in the same reply and reversible in one
step".* That pairing is not what separates them — **determinacy of the target** is.
`cancel_reminder(12)` names exactly what it affects, so standing tier risks a wrong-but-identified
action the owner can name back. An imaginary reading names a target that does not exist, and
shifting picks between two defensible substitutes (the first valid instant, or the requested minute
past the gap). Choosing there is inventing intent, which is the documented fail-closed trigger.

### D5 — An ambiguous local time resolves to the earlier instant, and the echo says so

On the fall-back night, `02:30` happens twice. Unlike D4 there *is* a defensible answer: `fold=0`
is by definition the first of the two occurrences, it is the first time the owner's clock reads
02:30, and it is the choice that is never *late*. So an ambiguous reading resolves to `fold=0` and
the echo discloses it — "Sunday 25 October at 02:30 CEST (the first of two — the clocks go back
that night)". Rejecting would be worse than in D4: the time exists, the owner's phrasing was not
wrong, and there is a safe reading.

The asymmetry between D4 and D5 is the point and should read as deliberate: *no* valid reading →
refuse; *two* valid readings → take the earlier one and say which.

**Scope of the `fold=0` policy.** It is the answer for a **fully specified** wall clock, where the
earlier reading is never late. The next-occurrence search (D10) is a different question: there the
rule is the earliest occurrence *strictly after now*, which is `fold=1` when `fold=0` has already
passed. That is a refinement of this decision's own "never late" argument once "future" is a
constraint, not a reversal of it — and without it, a `/remind 02:30` sent at 02:45 during the first
pass of the repeated hour skips a valid occurrence 45 minutes away and lands 24.75 hours out, which
is exactly what D4 forbids.

### D6 — Resolve to an instant; the instant is the promise

A reminder stores `due_at` as an absolute instant. Resolution happens once, at scheduling, with
the target date's own offset — `zoneinfo` gets per-date offsets right, so a reminder set in August
for 25 December at 14:00 resolves against CET, not CEST. Nothing re-resolves later, so a reminder
cannot drift because the zone database was updated or the config was edited.

What this forecloses, stated so the next change does not discover it: a *wall-clock* schedule
("every day at 09:00, whatever the offset") is a different model and needs the wall clock and zone
stored as the schedule rather than as forensics. That is recurrence, it is out of scope, and it
will need its own columns — which, with no migration path, means its own table or a migration
mechanism.

**The residual, named rather than left as implied upside.** Resolve-once means a timezone-database
*rule* change inside the 365-day horizon leaves already-scheduled reminders an hour wrong, with no
re-resolution path and no detection. That is not hypothetical at a one-year horizon: Iran, Mexico,
Jordan, Lebanon and Greenland have all changed future rules within a year recently, sometimes with
days of notice, and the EU's abolition proposal is still standing. Accepted, with the mitigation
written down: the horizon bounds the exposure, `due_tz` and `input_spec` make every affected row
forensically recoverable, and re-resolution is a named future change rather than an oversight.

### D7 — One renderer, weekday and zone abbreviation, used by every surface

A single function renders an instant for a human, and the tool results, all four command replies,
`reminders_read` **and the per-turn time header** (agent-core delta) all call it. A due time that reads
differently in two places is a bug the owner has to adjudicate — and that argument applies at least
as strongly to *now* versus *due* as it does between two due times, which is why the header is on
the list rather than being a second rendering surface beside it. The format carries the **weekday**,
the date, the local time and the **zone abbreviation or numeric offset** — the weekday because that
is the field a human actually checks when the model resolved "next Tuesday", and the zone marker
because it makes a DST boundary visible for a few characters ("Wednesday 20 August at 07:30 CEST").
Not every zone has an alphabetic abbreviation: `tzname()` yields `+0545` for Kathmandu, `+1030` for
Lord Howe, `-03` for São Paulo. The disclosure property is unaffected; the wording must not promise
letters.

The weekday and month names come from a **fixed table, not `strftime`**. `%A` and `%B` follow
`LC_TIME`, so the echo would turn Dutch the day someone adds locales to the image for an unrelated
reason. `%Z` is safe — it comes from `tzname()` — but there is no reason to keep one locale
dependency for the sake of two format codes.

The echo is the safety mechanism for the whole capability, not a nicety: a mis-resolved time
becomes a wrong-but-visible confirmation in the same reply, correctable with one
`cancel_reminder`, instead of a silent surprise a week later.

### D8 — `owner.timezone`: no default, and required only when reminders are enabled

The owner's timezone is an owner-level fact — the per-turn time header needs it too — so it goes
in the `owner` section, not under `reminders`. It has **no default**: a hardcoded
`Europe/Amsterdam` bakes a personal fact into a repo that is publication-bound, and a UTC fallback
turns a missing config key into every reminder firing one or two hours off. Validation is at
config load against `zoneinfo`, and an unknown zone fails startup naming the value.

Making it *unconditionally* required would break rp5's next deploy, since that host's
`config.yaml` is locally modified by design and will not carry the key. So it is required
**exactly when `reminders.enabled` is true**, and that combination is checked at load: enabled
with no timezone fails startup naming both keys. Since this change ships with `enabled: false`,
the deploy is a no-op and enabling is a deliberate two-key edit.

The configured value must be a **Region/Location key** — it contains a `/` and is not `localtime`.
`ZoneInfo("localtime")` otherwise validates cleanly and resolves against the host clock, which is
precisely the fallback zone this decision forbids, and it appears in `available_timezones()`, so
validating against that set does not catch it.

`tzdata` becomes a dependency for the same fail-loud reason inverted: `zoneinfo` reads the OS zone
database and `python:3.12-slim` is not guaranteed to ship one. It is pure data, no code, no
transitive dependencies.

But the wheel alone does **not** make the container independent of the base image, and it is worth
being precise because the weaker claim is the tempting one to write. `zoneinfo` searches `TZPATH`
first and consults the wheel only on a miss, so a base image carrying a *stale or trimmed* zone
tree wins silently — and silence is the failure mode, because absence is loud while staleness is
not. This is not theoretical: the development host's tree is already trimmed to 498 zones with
`US/Eastern` and `Europe/Kiev` absent.

So the image sets **`PYTHONTZPATH=""`** alongside the dependency, which empties `TZPATH` and makes
the pinned wheel the only source. Verified: with the wheel, 598 zones resolve including ones the
host tree lacks; without it, `ZoneInfoNotFoundError` at the first resolution rather than a quiet
fall back to something older. The zone database then moves when a dependency is bumped and reviewed,
not when a base image is rebuilt. It also removes `localtime` from `available_timezones()`
entirely, so the key-shape rule above becomes belt-and-braces in production and the real guard on a
developer's machine.

### D8a — Nothing reads the process timezone

The zone database was one mechanism; the process's *default zone* is a separate one, and it is the
larger hazard because it fails asymmetrically. A single `datetime.now()`, a bare `.astimezone()`,
a zone-less `fromtimestamp(t)`, or `.timestamp()` on a **naive** value (which is the natural
two-line shape of a bug here, since `fromisoformat` returns naive) all silently read the process
zone. The development host resolves as `Europe/Amsterdam`; a slim container with no `TZ` is UTC.
So the likeliest slip in this module is green locally and two hours wrong on rp5 only — the exact
dev/prod asymmetry D8 exists to close, arriving through a door D8 did not look at.

Resolution and rendering therefore depend on the configured owner zone and nothing else, and the
suite proves it rather than the code promising it: every clock-touching test runs under a hostile
`TZ`. `Pacific/Kiritimati` (+14) is the right hostile value because a leak changes the *date*, not
merely the hour. The image also sets `TZ=UTC`, so production is at least deterministic if the guard
is ever removed — but the test is the guard and the environment variable is the floor.

### D9 — `cancel_reminder` is a tool at standing tier; reinstating is command-only

The superseded draft had no cancel tool, reasoning that the model may add but not un-schedule
because "an extra reminder is noise, a missing reminder is a broken promise". That reasoning was
reversed in review, and the reversal is right for a reason the original argument did not have
available: **cancellation here is not removal.** The row survives with its text and due time,
`cancel_reminder`'s result echoes both, and `/reminders reinstate <id>` puts it back. A mis-cancel
is therefore visible in the same reply and undone with one command — the same containment shape
that justifies standing tier for `capture` and `store_memory`, plus a working undo. Against that
sits the real cost of the draft's position: "cancel that one" is the natural next sentence after
scheduling, and a Henk who must refuse it and recite a command is slower than not using Henk.

So `remind` and `cancel_reminder` are both mutating, **standing**, owner-turn-only, both denied in
event turns and in any tainted session, both receipted every time, and both demoted to inline
approval by the existing standing-tier kill switch.

Reinstating stays **command-only** — no tool. It re-arms a message the owner deliberately killed,
and as a tool it would need a pending-cap bypass and a counter reset. As a command it needs
neither. It also refuses when the reminder's due time has already passed, naming `/remind` as the
way to set a new one; that refusal is what keeps the entire late/missed question inside
`reminder-delivery` instead of leaking into a core command.

There is no `reschedule_reminder`: it is `cancel_reminder` + `remind`, two calls with two echoes,
and the echoes are the safety mechanism.

### D10 — `/remind` takes explicit forms only, matched longest-first

`/remind <when> <text>`, where `<when>` is `+90m` / `+2h` / `+3d`, `HH:MM`, or
`YYYY-MM-DD HH:MM`. The parser tries the two-token date-and-time form first, then the single-token
forms, so `/remind 2026-08-25 07:30 buy bread` splits correctly without a heuristic. An
unrecognized form schedules nothing and replies naming the accepted forms; a recognized time with
no text left over replies that the text is required. A command must be deterministic, and a
command is not the place to guess — `/remind sometime next week …` is a sentence for the agent,
not for the dispatcher.

`HH:MM` means the next occurrence of that clock reading, and the rule is a **selection** that runs
*before* any DST evaluation:

1. Build the reading on today's local date. If it is ambiguous, consider `fold=0` then `fold=1`;
   take the earliest whose **instant** is strictly after now.
2. Only if neither is future, take the same reading on the next calendar date — at most one date
   forward — and evaluate that candidate instead.
3. Apply D4 and D5 to the **selected** candidate only, in full: reject if imaginary, and if
   ambiguous take `fold=0` *with the disclosure*. Not rejection alone — the advanced candidate can
   be ambiguous rather than imaginary, and that case must still schedule.

Calendar-date arithmetic then re-resolution, never "add 24 hours", which is wrong by an hour twice a
year. Note that `aware_dt + timedelta(days=1)` is wall-clock arithmetic and therefore equals the
rebuild — so it is not itself the bug, but it produces an imaginary `fold=0` datetime when the next
date is the transition date, which is why step 3 must re-evaluate rather than trust the advance.

**Selection before evaluation is the whole point of the ordering** and it is load-bearing. Evaluated
the other way round, `/remind 02:30 …` sent at 20:00 on the spring-forward day is refused with
"02:30 does not exist on 2026-03-29" — while the reading the owner actually meant, 02:30 the
following night, exists and is perfectly schedulable. The same command at 00:30 that same night
*is* correctly refused, because there the owner is asking for tonight. Both cases must be specified
together; either alone permits the wrong ordering.

A zone with a **fully skipped calendar date** is out of scope, named rather than silently
mishandled: `Pacific/Apia` skipped 2011-12-30 entirely, so a one-date advance from 12-29 rejects
while 12-31 would have been valid. Unreachable in the owner's zone, and bounding the search is worth
more than covering a zone nobody is configured for.

### D11 — Audit v4 is the complete reminder schema, and two records answer two questions

`schema_version` bumps to 4, adding the `reminder` record type and the `scheduler` value for
`initiated_by`. v4 ships the **complete** enumeration — including `delivered`, `delivered-late`,
`missed`, `abandoned` and the `scheduler` initiator, none of which this change writes — so
`reminder-delivery` needs no second bump. A schema document is a validation contract, not an
inventory of what the current build emits; forcing v5 for a status value would mean two committed
documents and a version step for no structural change.

A `reminder` record carries the reminder id, due time, status, `initiated_by`
(`model` / `owner-command` / `scheduler`) and a timestamp — and **not the reminder's text**. The
store holds the content; the log holds the evidence, and the log gets read and pasted around.

Two records, two questions, not collapsed: a `remind` tool call writes an `authorization` record
(*was the agent allowed to do this?*, at the gate's decision) **and** a `reminder` record (*what
changed?*, at the transition). Owner commands write both too, with `initiated_by: owner-command`
and no tier. A schedule rejected by validation or the cap writes **neither** — receipts record
state changes, and none occurred.

Ordering: the lifecycle record is appended **after** the store transaction commits. A crash
between them costs a receipt for a real transition; the reverse ordering would leave a receipt for
a transition that never happened, and a log that claims state the store does not have is worse
than a log with a gap. The authorization receipt keeps its existing decision-time ordering, which
is upstream of both.

### D12 — Shipping inert is the feature

`reminders.enabled: false` unregisters all three tools, makes all four commands reply that
reminders are not configured, and leaves every stored row untouched. The table is still created by
the lazy schema path, which is harmless and keeps the DDL on one code path.

Inert is not a compromise here. A build that accepts "remind me at six", echoes a confident
"Reminder #3 set for Wednesday at 18:00", and then never says anything at six has spent the
owner's trust on a promise it structurally cannot keep. Off is the honest state until
`reminder-delivery` exists. No configuration flag may widen the capability — nothing promotes
`remind` beyond standing, widens its turn scope, or points delivery at another identity.

## Risks / Trade-offs

- **The autocommit switch touches every existing store write.** → It is a two-file port with an
  existing test suite over both paths; the cap-eviction and mark-done tests are the atomicity net
  and must pass untouched. The alternative — a transaction API layered over pysqlite's implicit
  `BEGIN` — is the defect, not a safer option.
- **The complete column set means shipping five columns nothing writes yet.** → Named in the
  table above with their owner, so a reader does not mistake them for dead code, and justified by
  the absence of a migration path. `terminal_at` was cut precisely because it would have been the
  sixth and had no future writer.
- **A permanent column that turns out to be wrong is unremovable.** → Accepted, and the reason
  `due_tz` and `input_spec` needed an argument rather than a preference. The same asymmetry is why
  a drift check ships: the next change to want a column will be told, at startup, that it needs a
  migration mechanism.
- **Rejecting nonexistent times refuses a schedule the owner meant.** → Once a year, in a
  one-hour window, with an error naming both valid neighbours. The failure it prevents is a silent
  one-hour error on any of the other 8,759 hours.
- **Ambiguous times resolve without asking.** → To the earlier instant, disclosed in the echo.
  The residual is a reminder up to an hour earlier than one possible reading, on one night a year.
- **The model can schedule or cancel reminders the owner did not clearly ask for.** → Both echo in
  the same reply; both are receipted; `max_pending` bounds accumulation; a cancel is reversible by
  command; nothing is deleted. And both are owner-turn-only, so no event payload can reach either.
- **Timezone misconfiguration on rp5 shifts every reminder.** → Startup fails on an unknown zone
  and on enabled-without-timezone; the echo's zone abbreviation reveals a *valid but wrong* zone
  on the first reminder.
- **`tzdata` as a dependency in a container that may already have the zone database.** →
  Duplicated data measured in kilobytes, against a startup failure that would otherwise depend on
  the base image's packaging choices.
- **A reminder scheduled here and never delivered.** → That is the intended state until
  `reminder-delivery` ships, and the reason `enabled` defaults to false. The only way to schedule
  one on rp5 is to edit the host config to enable a capability whose delivery half does not exist.

## Migration Plan

1. Ship with `reminders.enabled: false`. The new table is created by the existing lazy schema
   path on first connection; no data migration, no downtime, no observable change.
2. Verify the autocommit port on the existing suite before anything reminder-specific is trusted:
   memory cap eviction and inbox mark-done are the two multi-statement paths, and they must pass
   without test edits.
3. Confirm `zoneinfo` resolves a real zone **inside the built image**, not only on the dev host —
   that is what the `tzdata` dependency is for and the only place the assumption can be checked.
4. Leave rp5 disabled. Enabling is a host-side edit adding the `reminders` section **and**
   `owner.timezone`, and it is not worth doing until `reminder-delivery` lands; a deploy of this
   change alone is expected to be behaviourally invisible.
5. When enabling for a local end-to-end check: schedule via tool and via `/remind +2m`, confirm
   both echoes name the weekday, local time and zone; confirm `/reminders` lists them,
   `/reminders cancel` echoes the text, `/reminders reinstate` restores it, and reinstating a
   past-due reminder is refused; confirm the audit log has an `authorization` **and** a `reminder`
   record per write, with no reminder text in the latter.
6. **Rollback:** `reminders.enabled: false` and restart. Nothing is deleted; v4 records stay valid
   against their committed document; an unused table is inert. The autocommit change is not
   behind a flag and rolls back only by reverting the image — which is the same exposure as any
   other store change, and why step 2 is not optional.

## Open Questions

- **Does the drift check belong in `Store` or in a startup self-check?** Specced in the store,
  where the DDL is. A separate startup check would read better but would need the connection
  opened eagerly, which the lazy-open contract forbids.
- **Should the per-turn time header exist when reminders are disabled?** Specced as coupled to
  `reminders.enabled`, since its only purpose is resolving relative times for `remind` and the
  narrower prefix is the cheaper default. It is plausibly useful on its own ("what time is it?"),
  and decoupling it later is a one-line change plus a scenario.
- **Grammar variances to confirm at apply time, both deliberate.** The tool accepts optional
  fractional seconds (`…T07:30:00.000`, a common JSON-serializer output) because refusing them
  costs a valid schedule and has no DST implication; and the command accepts `YYYY-MM-DD HH:MM`
  with either a space or a `T`, since the longest-first parser handles both and refusing one is a
  surprise with no upside. Neither widens the DST surface.
- **Should a rejected schedule be visible anywhere?** It writes no audit record by design
  (receipts record state changes), nothing is stored, and tool-result text is no longer logged — so
  a model repeatedly submitting a bad form is invisible except in the token bill. Specced as one
  INFO log line naming the rejected shape and the reason, which leaves the receipt rule intact.
