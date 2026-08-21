# Tasks — Reminders Core

TDD throughout: each group starts by writing tests derived from the delta spec scenarios (each
Given/When/Then → at least one test; each SHALL → at least one assertion), then implements to
green. Implementation happens in a fresh session via `/opsx:apply`. **Hard stop before any deploy
to rp5 — explicit owner go required**, and this change is expected to be behaviourally invisible
when deployed (`reminders.enabled` defaults to false).

**Read before starting, in this order.** Each exists because a review round found something that
reading the code would not have:

1. `notes/dst-verified-facts.md` (this change) — every DST claim in the design, with the executed
   output that established it. **Required before touching `henk/reminders/timeparse.py`**; it is
   also the source for most of group 4's test tables.
2. `openspec/changes/archive/2026-08-21-reminders-superseded/notes/README.md` — the superseded draft's continuation notes. The
   "Settled — do not re-litigate" list is binding on this change (task 9.2 checks it).
3. `design.md` D1–D3 and D8a — the column set, the transaction contract, the two input families
   and the per-family validation ladder. The ladder table is the thing to walk as a matrix before
   writing group-4 code.

Four standing rules for this change:

- **Exercise the real store, not a cooperative double.** The transaction defect this change fixes
  is invisible to any fake that has no connection and no implicit `BEGIN`. Every transaction and
  repository test runs against a real `sqlite3` file (`tmp_path`), and the crash-durability
  assertions use a real process kill or a real connection close, not a mocked one.
- **Pin real transition dates, never a synthesized offset.** The DST tests use
  `Europe/Amsterdam`, forward transition **2026-03-29 02:00 → 03:00** and back transition
  **2026-10-25 03:00 → 02:00**. A test that fabricates a `timedelta` offset proves nothing about
  `zoneinfo`, which is the thing under test.
- **Before repointing any call site, grep for what observes it.** Not just the symbol —
  monkeypatches, test-double recording attributes, `caplog` assertions. A test that patches an old
  symbol goes silently dead rather than failing. The enumerations below were produced this way and
  must be re-run when the task is picked up, since the suite moves.
- **The existing suite is the regression net for the autocommit port.** No test in
  `tests/test_store*.py`, the memory tests, or the inbox tests may be edited to accommodate the
  port unless it asserts driver-level commit behaviour (group 1 enumerates those). A test changed
  to make the port pass is the port failing.

## 1. The store transaction boundary and the autocommit port

- [x] 1.1 Enumerate first, before touching anything: `grep -rn "commit()\|isolation_level\|in_transaction" henk/ tests/`
      and record every site. The port is only safe once the list is complete — write the list into
      the task notes so a later reviewer can check it against the diff
- [x] 1.2 Tests from the "store exposes an explicit transaction boundary" scenarios, against a
      real file: two writes in one transaction with the second raising leaves neither; a
      repository write inside a caller's transaction is absent when the caller raises; a nested
      scope that raises and is **caught** still rolls the outer transaction back (the poisoning
      rule); a standalone repository write is committed and visible after the call returns; nested
      entry does not issue a second `BEGIN` (assert via `sqlite3.Connection.in_transaction` or a
      statement trace, not by inspecting the manager's own counter)
- [x] 1.3 Test that the connection is in autocommit mode and that no transaction is open outside a
      `transaction()` scope — this is the assertion that pins the actual fix rather than the new
      API's surface
- [x] 1.4 Implement `Store.transaction()` in `henk/store/db.py`: `isolation_level=None`,
      `BEGIN IMMEDIATE` on outermost entry, `COMMIT` / `ROLLBACK` on outermost exit, depth
      counting, and the poison flag. Document the single-thread/single-connection assumption in
      the docstring beside `check_same_thread=False`
- [x] 1.5 Test that the assumption holds: no store call in `henk/` is reached through
      `asyncio.to_thread` or an executor (`grep -rn "to_thread\|run_in_executor" henk/`), so a
      shared connection cannot interleave two transactions. This is a guard against a future
      change, and it belongs in the suite rather than in a comment
- [x] 1.6 Port `henk/store/memory.py` and `henk/store/inbox.py` off self-commits and onto
      `transaction()`, wrapping the multi-statement paths — memory's cap eviction plus insert, and
      inbox's mark-done update plus read-back. The existing cap-eviction and mark-done tests must
      pass **untouched**; if either needed editing, the port is wrong
- [x] 1.7 Test the atomicity that used to come free from the implicit `BEGIN`: an eviction that
      raises before the insert leaves the evicted memory in place

## 2. The reminders table and repository

- [x] 2.1 Tests for the schema: every column in the design's table exists after first connect;
      `next_attempt_at` is non-nullable; the index exists; opening against a pre-existing
      reminders table with a column removed fails with a `StoreError` naming that column
- [x] 2.2 Implement the DDL and index in `henk/store/db.py` plus the `PRAGMA table_info` drift
      check. The check runs after the DDL on first connect and names both missing and unexpected
      columns
- [x] 2.3 Tests for the repository: schedule; list pending oldest-due first with a page bound;
      get by id including terminal rows; cancel (status change, row and text retained); reinstate
      (back to `pending`, `next_attempt_at` written); the pending-cap rejection naming the cap
      with nothing stored; cap check and insert in one transaction (a failed insert leaves the
      count unchanged); no method deletes a row or rewrites text or `due_at`
- [x] 2.4 Test that every path into `pending` writes `next_attempt_at` — scheduling by either
      source and reinstating — asserted on the stored row, not on the call
- [x] 2.5 Test durability: a reminder scheduled, the connection closed hard, and the file reopened
      still shows the row with its original text, status and due instant
- [x] 2.6 Implement `henk/store/reminders.py` — every write method transaction-agnostic per
      design D2, no `commit()` of its own — and add the repository to `henk/store/factory.py`
      and the `henk/store/__init__.py` exports

## 3. Configuration, the timezone, and the image

- [x] 3.1 Tests for `RemindersConfig`: defaults with the section absent entirely (disabled); the
      `enabled` flag; `max_pending`, `text_length_limit`, `horizon_days`,
      `clock_skew_tolerance_seconds` and the read/page bound; and that no key can widen the
      capability (there is no tier, scope or recipient key to set)
- [x] 3.2 Tests for `owner.timezone`: absent and reminders disabled → loads fine; absent and
      reminders enabled → `ConfigError` naming both keys; present but not a known zone →
      `ConfigError` naming the value; **`localtime` refused** (it resolves cleanly and appears in
      `available_timezones()`, so the rule is a Region/Location key shape — a `/`, and not
      `localtime` — not "the key resolves"); present and valid → available as a resolved zone. Add
      `owner.timezone` with a `None` default so no existing positional construction of
      `OwnerConfig` breaks
- [x] 3.3 Implement `RemindersConfig` and the `owner.timezone` validation in `henk/config.py`, and
      add the `reminders` section to `config.yaml` with commented rationale in the existing
      sections' documentation style — including a placeholder timezone and a comment that rp5's
      locally-modified config must carry the real value
- [x] 3.4 Add `tzdata` to `pyproject.toml` dependencies **and `PYTHONTZPATH=""` to the Dockerfile**,
      with the reason: declaring the dependency guarantees availability but not precedence —
      `zoneinfo` searches `TZPATH` first, so a stale or trimmed tree in the base image wins
      silently, and staleness is silent where absence is loud. Emptying `TZPATH` makes the pinned
      wheel the only source, so the zone database moves on a reviewed dependency bump rather than
      on a base-image rebuild. Verified shape: `PYTHONTZPATH=""` + the wheel gives 598 zones
      including ones this dev host's tree lacks; without the wheel it raises
      `ZoneInfoNotFoundError` at first resolution. Also set `TZ=UTC` in the image (task 4.0 is the
      real guard; this is the floor)
- [x] 3.5 **DONE — verified in the built image 2026-08-20** (`sudo docker` on the dev
      workstation): `TZPATH ()`, 598 zones, `localtime` absent, `US/Eastern` resolving
      **from the wheel** (`importlib.resources` confirms it positively, not by
      elimination), `TZ=UTC`, tzdata 2026.3 / IANA 2026c. Negative control: with the
      wheel removed the first resolution raises `ZoneInfoNotFoundError` — loud, not a
      quiet fall back. Full output and what each line settles:
      `notes/apply-enumerations.md`, section "3.5 — DONE". Original blocking note: `docker` requires password
      sudo on this host (`/var/run/docker.sock` is root:root 660, the user is not in a
      docker group) and the apply session has no tty. Everything the check depends on is
      in place (`tzdata>=2025.1` declared and locked at 2026.3 / IANA 2026c;
      `PYTHONTZPATH=""` and `TZ=UTC` as their own Dockerfile `ENV` instructions), and the
      dev-host floor was verified — see `notes/apply-enumerations.md` for the exact
      `docker build` + `docker run` commands, including the negative control that must
      raise `ZoneInfoNotFoundError` without the wheel. Original task text follows.
      Verify inside the **built image**, not on the dev host: `docker build`, then resolve a
      real zone in the container and record **which source provided it** (`zoneinfo.TZPATH`, the
      `tzdata` version). A check that only records "resolution succeeded" passes on the very
      configuration it exists to rule out

## 4. Time resolution (the DST core)

Two rules for this group specifically, both earned in review rather than assumed:

- **Write the wrong implementation and confirm red.** Two draft assertions in this group passed
  against a plausibly broken implementation before they were rewritten. Every test below gets this
  check, and the ones marked *(discriminating)* are the ones that carry the weight.
- **Walk the ladder as a matrix before writing code.** Design D3's per-family ladder is the right
  abstraction and it was still completed by enumeration: review round 2 found the missing ordering
  cell, round 3 found the duration DST-skip cell and the magnitude-bound cell. Walk every family ×
  every step once and state, per cell, either what it rejects or why it does not apply. Empty cells
  are where the next defect lives; this takes fifteen minutes.

- [x] 4.0 **Process-timezone guard, first and before anything else in this group.** Parametrise
      every clock-touching test over `TZ ∈ {UTC, Pacific/Kiritimati, Europe/Amsterdam}` with
      `time.tzset()` in a fixture, asserting identical stored instants **and** identical rendered
      strings. Scope is the resolver, the renderer, the group-7 command tests and the group-8 time
      header — not the resolver module alone; the command dispatcher is where `datetime.now()` is
      most idiomatic to write. Kiritimati (+14) is the right hostile value because a leak changes
      the *date*, not only the hour. **Validate the guard itself**: introduce one `datetime.now()`
      deliberately and confirm the suite goes red under at least one TZ. A guard never seen to fail
      is not a guard
- [x] 4.0b Grep, with the expected result "every hit reviewed and justified" — **not** empty:
      `grep -rnE "datetime\.now\(\)|utcnow|fromtimestamp\([^,)]*\)|\.astimezone\(\)|\.timestamp\(\)|date\.today\(\)|datetime\.today\(\)|time\.localtime|time\.mktime" henk/reminders/ henk/agent/`
      `.timestamp()` on an *aware* value is correct and will hit, which is why justification rather
      than emptiness is the bar — a grep that must come back empty on a pattern with legitimate
      hits gets deleted by the next person. The additions past the obvious ones matter: `fromisoformat`
      returns a **naive** datetime, so `fromisoformat(s).timestamp()` silently uses the process zone
      and is the natural two-line shape of a bug here (verified: 1792888200 / 1792895400 / 1792845000
      under Amsterdam / UTC / Kiritimati), and `time.mktime` is the classic naive-local-to-epoch
      converter with the same property
- [x] 4.1 Tests for the wall-clock family: naive ISO interpreted in the owner zone and **not** as
      UTC *(discriminating — assert the resolved instant, not the rendered string)*; a target date
      on the far side of a transition resolved with the *target date's* offset, so the round-trip
      wall clock equals the request; the round-trip invariant asserted for an ambiguous reading on
      **both** folds (it holds for both — that is why the draft's "except where ambiguous" carve-out
      was wrong); past rejected beyond the skew tolerance and accepted inside it, with the error
      naming the current local time; beyond-horizon rejected naming the horizon. Plus the whitelist
      rejections, each with its own case: date-only, `20260825`, `2026-W35`, `2026-W35-1`, a bare
      `07:30` on the tool path, and an offset-carrying or `Z`-suffixed value. **Negative case:** an
      ordinary time is neither rejected nor annotated
- [x] 4.2 Tests for the nonexistent-time rule at the real forward transition: `02:30` on
      2026-03-29 is rejected on the tool path and on the dated path; the error names the date, the
      transition and both neighbouring readings, and the tool-path error tells the agent to ask the
      owner which was meant; **no** path stores an instant that renders back as `03:30`. State
      explicitly which ordering each bare-`HH:MM` case asserts — see 4.5, where "rejected" and
      "scheduled on the next date" are both correct answers for different `now` values, so an
      unqualified assertion here would pin the wrong branch. One case in a zone whose transition is
      **not** one hour wide (`Australia/Lord_Howe`, 30 minutes; or `Antarctica/Troll`, two hours,
      where the +1h neighbour is itself imaginary), asserting the named neighbours are valid
      readings *(discriminating — an implementation hardcoding ±1h passes every Amsterdam test)*
- [x] 4.3 Tests for the ambiguous-time rule at the real back transition: `02:30` on 2026-10-25
      resolves to the **earlier** of the two instants *(discriminating — assert the epoch value and
      the offset, never the wall clock, since both folds render the same)*; the confirmation
      discloses that the reading occurs twice and which was chosen; the nonexistent check does not
      fire on it. **Negative case:** an ordinary date produces no disclosure — assert the absence of
      the substring. Without it, an implementation whose detection step is inverted or stubbed
      `True` annotates every reminder and passes every other test in this group
- [x] 4.4 Tests for the duration family, run against **both** the tool and the command path: `+3d`
      across each transition is exactly 72 hours and the resulting local time differs from the
      starting wall clock by the offset change *(discriminating — `aware + timedelta` yields 71h and
      73h across the two transitions; this is the assertion that pins the rule, and it now covers
      the tool path, where a 71-hour result would otherwise be invisible)*; `+90m`, `+2h`, `+3d`
      parse; an offset landing inside the forward gap in wall-clock terms still succeeds; the
      nonexistent/ambiguous evaluation is **skipped** on this path; `+0m` and an over-long magnitude
      are refused by the grammar with no arithmetic error surfacing (verified: `+999999999d` raises
      `ValueError: year … out of range` and `+99999999999999d` an `OSError` *before* the horizon
      check can reject them, so the bound belongs in the shape step). Keep `+24h` == `+1d` only as a
      guard on the rejected calendar-day alternative, and label it as such — on its own it is a
      tautology, since `timedelta(days=1) == timedelta(hours=24)`
- [x] 4.5 Tests for the `HH:MM` next-occurrence rule, as a table over `now`: a reading later today
      resolves today; a reading already past resolves to the same reading on the next **local
      date**; at 02:15 / 02:30 / 02:45 in **both** folds of the repeated hour, every accepted result
      is within one hour of `now` and never 25 hours out *(discriminating — the fold=0-only rule
      skips a valid occurrence 45 minutes away and lands 24.75h out)*; and the ordering pair —
      20:00 on the spring-forward date schedules the reading on the following date, while 00:30 that
      same night is refused. Both rows are required: either alone permits the wrong ordering. Set
      `now` so one case advances **into** the gap and assert detection re-runs on the advanced
      candidate; assert too that an advanced candidate which is *ambiguous* still schedules with its
      disclosure, rather than only the imaginary case being handled
- [x] 4.6 Tests for the renderer: weekday, date, local time and the zone's abbreviation **or
      numeric offset** present (`tzname()` yields `+0545`, `+1030`, `-03` for some zones — the
      wording must not promise letters); the same instant renders identically for a tool result, a
      command reply, the read tool **and the time header**; weekday and month names are unchanged
      across differing process locales *(discriminating — `%A`/`%B` follow `LC_TIME`, `%Z` does
      not)*; rendering uses the current configured zone, not the row's `due_tz`. **Negative case:**
      no DST annotation on an ordinary instant
- [x] 4.7 Implement `henk/reminders/timeparse.py`: the whitelist-then-parse grammar, the two input
      families, the two-step imaginary/ambiguous detection from design D4 (`fold` offset comparison,
      then the UTC round-trip), the per-family ladder with selection before evaluation, the single
      clock capture, and the single renderer. Comment the detection with *why* each step exists —
      the next reader will assume the round-trip alone is sufficient, and it is not: both folds of an
      ambiguous reading round-trip cleanly. Comment two more traps in the same block: `fold=0` is the
      earlier instant for an **ambiguous** reading only (for an imaginary one it is the later), and
      two aware date-times in the same zone compare by wall clock, so `fold=0` and `fold=1` of one
      reading compare **equal** while denoting instants an hour apart
- [x] 4.7b Log one INFO line on a rejected schedule, naming the rejected shape and the reason. A
      rejection writes no audit record by design (receipts record state changes, and none occurred),
      nothing is stored, and tool-result text is no longer logged — so without this a model
      repeatedly submitting a bad form is invisible except in the token bill. Test that a rejection
      logs it and that an accepted schedule does not
- [x] 4.8 Store `input_spec` — the tool's `when` argument or the command's `<when>` token, not the
      whole command line — silently truncated at its bound, and `due_tz` as the owner zone
      configured at scheduling time. Assert both on a duration row as well as a wall-clock row: a
      forensic column whose meaning varies per row cannot be read at all
- [x] 4.9 **Commit the ground-truth oracle as a test**, the transferable artifact of this work and
      the same role `verify_selector_invariants.py` played for delivery, in about 30 lines:
      enumerate every UTC minute of a year, map each to its local wall clock, count occurrences
      (0 = imaginary, 1 = normal, 2 = ambiguous), and assert the implementation's classifier agrees
      on every local minute. This is what proved the algorithm sound across ~6.2M classifications
      and 12 zones with zero mismatches, and it is the only test here that cannot be satisfied by
      an implementation that happens to handle the hand-written Amsterdam cases. Default to a
      reduced zone set (Europe/Amsterdam plus one non-1h-transition zone) with the full sweep behind
      a marker, and state its non-coverage at the top of the file. Validate it: stub the detection's
      first step to `True` and confirm red

## 5. Audit schema v4

- [x] 5.1 Tests: a `reminder` record carries id, due time, transition, `initiated_by` and
      timestamp and **no** reminder text; `SCHEMA_VERSION` is 4 and new records declare it; the v4
      document validates a record for every transition in the complete enumeration, including the
      delivery-half ones; v1–v3 records still validate against their own committed documents;
      rejected attempts (past time, cap, unknown id) write no record
- [x] 5.2 Tests for the two-records rule: a successful `remind` tool call produces an
      `authorization` record **and** a `reminder` record; an authorized call whose store write
      fails produces the `authorization` record and no `reminder` record; the lifecycle record is
      appended only after the commit (drive it with a transaction whose commit raises — the
      assertion is the record's absence, which is what pins the ordering)
- [x] 5.3 Implement `reminder_record()` and `SCHEMA_VERSION = 4` in `henk/audit/logger.py`, commit
      `henk/audit/schema/audit-record.v4.schema.json` with the complete transition and
      `initiated_by` enumerations, and keep `AUDIT_SCHEMA_V1..V3_PATH` exports intact. Grep for
      readers of `SCHEMA_VERSION` and `AUDIT_SCHEMA_PATH` before changing either

## 6. Tools

- [x] 6.1 Tests from the `remind` scenarios: stored and echoed with id and rendered due time;
      whitespace-only text rejected with nothing stored; over-limit text rejected naming the limit
      with no truncated variant stored; no approval prompt in an untainted owner session; a store
      failure returns an explicit error and never a confirmation naming a due time
- [x] 6.2 Tests from the `cancel_reminder` scenarios: status becomes `cancelled` with the row,
      text and due instant retained; the result echoes text and rendered due time and names
      `/reminders reinstate <id>`; an unknown or non-pending id changes nothing and says so
- [x] 6.3 Tests from the `reminders_read` scenarios: oldest-due first with id, rendered due time
      and text; an empty schedule reads as empty and not as an error; the result is clamped to the
      bounded maximum and says how many were not shown
- [x] 6.4 Tests for the declared tier and scope (approval-gate delta): both mutating tools
      register as mutating/standing/owner-only; both are denied `out-of-scope` on an event turn
      and in a tainted session, with the tainted-session result naming `/remind`; the demotion
      kill switch prompts for both; `reminders_read` is read-only and bypasses the gate
- [x] 6.5 Test that the toolset contains **no** reinstate, reschedule, edit or delete operation for
      reminders — asserted against the registry, so adding one later fails a test rather than a
      review
- [x] 6.6 Implement `henk/tools/reminders.py` (`remind`, `cancel_reminder`, `reminders_read`) and
      register all three in `henk/tools/__init__.py` **behind `reminders.enabled`**, with tests
      that the registry contains none of them when disabled

## 7. Owner commands

- [x] 7.1 Tests from the command scenarios: `/remind +2h call the plumber` schedules and confirms
      with no agent session and no tokens; `/remind 07:30 …` at 21:00 local resolves to the next
      local date; `/remind 2026-08-25 07:30 buy bread` splits into the dated time and `buy bread`
      (the two-token form matched before the one-token form); an unrecognized form names the
      accepted forms; a recognized time with no text says the text is required
- [x] 7.2 Tests for `/reminders`: pending listed oldest-due first with ids, rendered due times and
      text, page-bounded; an unreadable store replies with the failure and never as an empty
      schedule
- [x] 7.3 Tests for `/reminders cancel <id>` and `/reminders reinstate <id>`: cancel echoes the
      text and retains the row; reinstate returns it to `pending` with `next_attempt_at` written;
      reinstating a past-due reminder changes nothing and names `/remind`; reinstating at the
      pending cap changes nothing and names the cap; reinstating a non-cancelled reminder changes
      nothing; an unknown id changes nothing; an unrecognized subcommand names the accepted ones
- [x] 7.4 Tests for the disabled path: all four commands reply that reminders are not configured,
      nothing is stored, and stored rows are unchanged
- [x] 7.5 Tests for the receipts: each mutating command writes its `authorization` receipt (as the
      existing commands do) **and** its `reminder` lifecycle record; `/reminders` writes neither
- [x] 7.6 Implement the `/remind` and `/reminders` family in `henk/agent/commands.py`, reusing the
      shared renderer and the same repository instance the tools use, and extend the agent-core
      command recognition set

## 8. Agent-core wiring

- [x] 8.1 Tests from the modified turn-composition requirement: with reminders enabled, two owner
      turns an hour apart each carry a current-time header for their own turn, rendered in the
      owner zone; event turns carry no header; with reminders disabled no owner turn carries one
- [x] 8.2 Tests for the system prompt: with reminders enabled the three tool names appear, the
      enumerated names equal the registered names, and the prompt states that reinstating is an
      owner command; with reminders disabled none of the three appears
- [x] 8.3 Update `AgentConfig.system_prompt` — it currently hardcodes "exactly these seven",
      a count this change changes. Grep first: `grep -rn "seven\|these seven" henk/ tests/`, and
      make the enumeration and the count derive from one source if the fix is otherwise a second
      place to forget
- [x] 8.4 Implement the per-turn time header in `henk/agent/core.py` (delimited as data,
      owner-turns-only, conditional on `reminders.enabled`) and wire the repository, clock,
      zone and renderer through `henk/runtime.py` into both the registry and the command
      dispatcher — the same instances, as memory already does

## 9. Verification and close-out

- [x] 9.1 Full suite green, including every test the enumerations in 1.1, 5.3 and 8.3 turned up.
      Report the count and any test that was edited, with the reason
- [x] 9.2 Re-read the "Settled — do not re-litigate" list in
      `openspec/changes/archive/2026-08-21-reminders-superseded/notes/README.md` against the implementation, and confirm this
      change did not quietly reverse any of it
- [x] 9.3 Confirm the deployed-behaviour claim by inspection: with no `reminders` section in
      config, the registry, the command set, the system prompt and the turn composition are
      byte-identical to before the change. If any of them is not, the kill switch is incomplete
- [x] 9.4 Confirm what this change does **not** do, so the handoff to `reminder-delivery` is
      honest: no scheduler task, no send, no `surfaced_at` / `send_attempts` / `delivered_at` /
      `reported_at` writer, no cadence amendment. A grep for writers of those four columns must
      come back empty
- [x] 9.5 **DONE — deployed to rp5 2026-08-20** (image `ba638f0466ea`, `0bfcc5b` → `bccc642`,
      so it carried `51972fd` and the channel-integrity archive too — the paired rebuild the
      reminder-delivery notes were holding out for). Startup clean, no ConfigError on a config
      carrying neither new key; the store half verified over Signal because it opens LAZILY and
      startup proves nothing about it; four v4 receipts on the volume; 11 log lines, zero
      ERROR/WARNING/StoreError. Full record and the standing watch:
      `notes/apply-enumerations.md`, section "9.5 — DEPLOYED". Original task text:
      **Hard stop.** No deploy to rp5 without explicit owner go. When it comes, the deploy is
      expected to be a no-op — pair it with the pending rp5 rebuild for `51972fd` (the per-phase
      timeout fix, committed and not deployed) rather than rebuilding twice
