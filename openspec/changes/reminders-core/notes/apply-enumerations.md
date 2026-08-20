# Apply-time enumerations, the ladder matrix walk, and the verification record

> ## State, and what is left — read this first
>
> **`reminders-core` is IMPLEMENTED and green as of 2026-08-20.** Suite: `1269 passed,
> 12 deselected` (the 12 are the opt-in `dst_sweep` zones; `pytest -m dst_sweep` runs them
> and they pass). Pre-change baseline was **553**, so this adds 716 tests.
> `openspec validate --changes reminders-core --strict` passes.
>
> **Every task is done.** 3.5 was verified in the built image on 2026-08-20 — the output and
> what each line settles are recorded under "3.5 — DONE" below, negative control included.
> 9.5 (the deploy) is the owner's call and carries the runbook note below.
>
> **Read this before calling the deploy a no-op:** it is a no-op for *reminders* — the flag
> defaults to false, rp5's locally-modified `config.yaml` carries no `reminders` section and
> no `owner.timezone`, so no tool registers, no command works and no header is composed. It
> is **not** a no-op for the store: the autocommit port is **not behind a flag** and changes
> how every memory and inbox write commits. That is why the pre-existing suite passing
> **untouched** was the acceptance condition for group 1, and why the deploy-verify list
> below exercises `/remember`, `/capture` and `/inbox done` rather than anything reminder
> shaped. Rollback for that half is reverting the image, the same exposure as any other store
> change.
>
> **Known follow-up, deliberately not done here:** `README.md`'s `## Tools` table and
> its `henk/tools/` row still describe the seven-tool set, and its owner-command list
> omits `/remind` / `/reminders`. Correct *today*, because the capability ships inert
> and those tables describe what a running Henk actually has. They want a pass at the
> same time `reminders.enabled` is flipped on rp5 — not before, or the README would
> promise a capability the deployed build does not register. (`README.md` also carried
> unrelated uncommitted edits during the apply session, which is the second reason it
> was left alone.)


Produced at `/opsx:apply` time (2026-08-20) by re-running the greps the task list
requires, because the suite moves. A later reviewer should be able to check each list
against the diff.

## Task 1.1 — every `commit()` / `isolation_level` / `in_transaction` site

`grep -rn "commit()\|isolation_level\|in_transaction" henk/ tests/`

| site | what it is | disposition in this change |
|---|---|---|
| `henk/store/db.py:80` | `conn.commit()` after the first-connect DDL | deleted — autocommit commits DDL statement-by-statement |
| `henk/store/memory.py:107` | `conn.commit()` in `MemoryStore.add` (insert + cap trim) | replaced by `with store.transaction()` |
| `henk/store/memory.py:140` | `conn.commit()` in `delete_containing` (bulk delete) | replaced by `with store.transaction()` |
| `henk/store/inbox.py:97` | `conn.commit()` in `append` | replaced by `with store.transaction()` |
| `henk/store/inbox.py:140` | `conn.commit()` in `mark_done` (update + read-back) | replaced by `with store.transaction()` |

Companion `rollback()` sites, all removed (the context manager owns rollback):
`henk/store/memory.py:109`, `henk/store/inbox.py:99`, `henk/store/inbox.py:148`.

`isolation_level` and `in_transaction`: **no hits anywhere**, `henk/` or `tests/`.
Consequence that matters for the standing rule "the existing suite is the regression
net": **no existing test asserts driver-level commit behaviour**, so no test in
`tests/test_store*.py`, the memory tests or the inbox tests needed editing for the
port. They pass untouched, which is the port's own evidence.

## Task 1.5 — the single-connection assumption

`grep -rn "to_thread\|run_in_executor" henk/ tests/` → **empty**. No store call is
reached through a thread or an executor, so the shared connection cannot interleave
two transactions. Guarded by a test rather than a comment
(`tests/test_store_transaction.py::test_no_store_call_is_dispatched_to_a_thread`).

## Task 5.3 — readers of `SCHEMA_VERSION` / `AUDIT_SCHEMA_PATH`

- `henk/audit/logger.py` — definitions; three record builders stamp `SCHEMA_VERSION`
  (`suppression_record`, `authorization_record`, `session_record`); `reminder_record`
  is the fourth added here.
- `henk/audit/__init__.py` — re-exports both plus `AUDIT_SCHEMA_V1..V3_PATH`.
- `tests/test_audit_log.py:18-26` — validates current records against
  `AUDIT_SCHEMA_PATH` (now v4) and asserts `schema_version == SCHEMA_VERSION`.
- `tests/test_audit_receipts.py:22-33,57` — same, plus
  `tests/test_audit_receipts.py:195` `assert SCHEMA_VERSION == 3`, which is the
  version pin this change moves to 4 (see the edited-tests note in the report).
- `tests/test_audit_receipts.py:218-242` — asserts v1/v2 documents stay committed and
  still validate; v3 joins that list here.

Nothing outside `henk/audit/` and the two audit test modules reads either symbol, so
the bump has no other call sites.

## Task 8.3 — "seven"

`grep -rn "seven\|these seven" henk/ tests/`

- `henk/config.py:44` — "exactly these seven" in `AgentConfig.system_prompt`
- `henk/config.py:70` — "falls outside these seven" in the same prompt
- `tests/test_production_registry.py:90` — `assert "seven" in prompt and len(registry.names()) == 7`
- `henk/events/identity.py:31`, `tests/test_event_identity.py:174` — unrelated
  ("seven scrape targets"); untouched.

Fixed by deriving the count and the enumeration from one source: the prompt is built
by `AgentConfig.build_system_prompt(...)`, which spells the count from the tool list
it enumerates, so the two can no longer drift. The registry test is repointed at the
same helper.

## Task 4 — the ladder matrix walk (design D3, every family x every step)

Walked before writing any group-4 code. Families: **A** dated wall clock
(`YYYY-MM-DD[T ]HH:MM[:SS[.ffffff]]`), **B** bare `HH:MM` (command path only),
**C** duration (`+N{m|h|d}`).

| step | A — dated wall clock | B — `HH:MM` | C — duration |
|---|---|---|---|
| shape match | rejects an offset or `Z`/`z` suffix, ISO week dates (`2026-W35`, `2026-W35-1`), basic format (`20260825`), date-only (`2026-08-25`), `T24:00`, and anything `fromisoformat` would widen the accept-set with. Surrounding whitespace stripped first | rejects everything that is not exactly two 2-digit fields with `:`; rejects **outright on the tool path** (the next-occurrence search is command-only) | rejects a magnitude of 7+ digits, a zero magnitude, a missing or unknown unit, and a leading `-`. This is where the bound lives, because `+999999999d` raises `ValueError` from the arithmetic and `+99999999999999d` an `OSError` from the parse — both *before* the horizon step could refuse them |
| parse | builds a naive `datetime`; a shape-valid but calendar-invalid value (`2026-02-30`) is refused here | builds `(hour, minute)`; the date arrives from selection | `int(N)` x unit → `timedelta`. Cannot fail once the magnitude bound held |
| candidate selection | **n/a** — the value is the candidate: date and time are both given, so there is nothing to choose | **first**, per D10: the earliest *distinct instant* of that reading on today's local date that is strictly after `now` (both folds considered where the reading repeats); only if none is future, the same reading on the next local date, advancing **at most one** date | **n/a** — the instant is computed (`now + delta`), never selected |
| imaginary / ambiguous | on the value. Imaginary → reject, naming the date, the jump and both transition-boundary neighbours. Ambiguous → the earlier instant plus the disclosure | on the **selected** candidate, in full — including the ambiguous branch, because an advanced candidate can be ambiguous rather than imaginary (verified: `now 2026-10-24 20:00` → advanced candidate is ambiguous and must still schedule) | **skipped entirely.** A duration never names a wall clock, so a check here can only false-reject: `+3d` may land inside the spring gap in wall-clock terms while denoting a perfectly real instant |
| past + skew tolerance | `instant < now - tolerance` → reject, naming the current local time | **unreachable by construction** — selection only ever returns an instant strictly after the single captured `now`. Stated, not relied on; the single clock capture is what makes it true (two reads would reopen a sub-second window) | **unreachable** — the magnitude is strictly positive at the shape step, so `now + delta > now` always |
| horizon | `instant > now + horizon` → reject naming the horizon | same check; reachable only through the one-date advance, so never near 365 days. Kept for uniformity rather than because it bites | same check, and here it genuinely bites (`+999999d`) |
| text limit → pending cap | yes (repository: empty → over-limit → cap, cap and insert in one transaction) | yes | yes |

**Empty cells: none.** Three cells are `n/a` or `skipped` for a structural reason
stated above, two are unreachable-by-construction and say so; every other cell names
what it rejects. The three cells that review rounds 2 and 3 found missing — B's
ordering, C's DST skip, C's magnitude bound — are each their own row entry now.

**Which ordering each bare-`HH:MM` case asserts** (task 4.2's requirement, since
"rejected" and "scheduled on the next date" are both right for different `now`):

| `now` (Europe/Amsterdam) | ask | today's reading | selected | outcome asserted |
|---|---|---|---|---|
| 2026-03-29 20:00 CEST | 02:30 | imaginary, both instants past | next date, normal | **schedule** 2026-03-30 02:30 — pins select-before-evaluate |
| 2026-03-29 00:30 CET | 02:30 | imaginary, instants still future | today | **reject** — pins that selection does not skip an imaginary-but-future candidate |
| 2026-03-28 20:00 CET | 02:30 | past | next date (03-29), imaginary | **reject** — pins "no skipping a day" (D4) |
| 2026-10-24 20:00 CEST | 02:30 | past | next date (10-25), ambiguous | **schedule** fold=0 **with disclosure** — pins that the re-run is the full treatment, not rejection-only |
| 2026-10-25 02:45 fold=0 | 02:30 | ambiguous, fold=0 past / fold=1 future | today, fold=1 | **schedule** +45 min — pins instant comparison over wall-clock comparison |

---

# Verification record (group 9), from the apply session

## 9.1 — suite state

`1269 passed, 12 deselected` (the deselected 12 are the opt-in `dst_sweep` zones;
`pytest -m dst_sweep` runs them and they pass, zero mismatches). **The pre-change baseline
was 553**, so this change adds 716 tests.

(An earlier draft of this note said 569. That figure was measured *after* group 1 had landed
and therefore already included its 16 tests — 553 + 16. Caught while verifying the commit
split below, and corrected here rather than left as a number a later reader would have to
re-derive.)

New test modules and their counts:

| module | tests | covers |
|---|---|---|
| `tests/test_store_transaction.py` | 16 | group 1 — the transaction boundary and the autocommit port |
| `tests/test_reminders_store.py` | 30 | group 2 — schema, drift check, repository, durability |
| `tests/test_config_reminders.py` | 16 | group 3 — `RemindersConfig`, `owner.timezone` |
| `tests/test_reminders_timeparse.py` | 373 | group 4 — the DST core (x3 process zones) |
| `tests/test_reminders_oracle.py` | 5 (+12 opt-in) | task 4.9 — the ground-truth oracle |
| `tests/test_audit_v4.py` | 19 | group 5 — schema v4 and the reminder record |
| `tests/test_tools_reminders.py` | 49 | group 6 — the three tools, tier and scope |
| `tests/test_reminders_commands.py` | 151 | group 7 — the four commands (x3 process zones) |
| `tests/test_agent_core_reminders.py` | 41 | group 8 — the time header and the prompt |
| `tests/test_reminders_inert.py` | 16 | tasks 9.3 / 9.4 — the kill switch and the split |

**Commit split, each commit verified green in isolation** — checked by exporting `HEAD` to a
scratch tree and overlaying only that commit's file set, not by reasoning about the import
graph. That is how the 553-vs-569 slip above was caught, which is the argument for doing it
this way:

| commit | scope | suite |
|---|---|---|
| `docs(readme)` | the rp5 redeploy runbook (pre-existing, from an earlier session) | no code |
| `feat(store)` | `henk/store/**`, `henk/config.py`, `config.yaml`, `pyproject.toml`, `uv.lock` + their three test files | **615 passed** |
| `feat(reminders)` | `henk/reminders/`, audit v4, tools, commands, agent core, runtime, image + their test files | **1269 passed** |
| `docs(openspec)` | the change set and this record | no code |

The store commit is split out because it is independently *reviewable* — a transaction boundary
and a table are one coherent thing. It is deliberately **not** claimed to be independently
*revertable*: `henk/store/reminders.py` calls `Store.transaction()`, so reverting the store
commit alone would break the capability commit. Reviewability was the goal; saying so now is
cheaper than someone discovering it mid-incident.

**Existing tests edited — two, both reported with the reason:**

1. `tests/test_audit_receipts.py::test_schema_version_is_three` → `..._is_four`
   (`assert SCHEMA_VERSION == 3` → `== 4`). This test **is** the version pin, and this
   change bumps the version by design; the assertion did exactly the job it exists for.
   Same file: `test_prior_schema_documents_remain_committed_and_valid` gained v3 in its
   loop plus a v3 record validated against v3's own document, because v3 is now a
   historical version.
2. `tests/test_production_registry.py::test_default_system_prompt_enumerates_every_registered_tool`
   — `assert "seven" in prompt and len(registry.names()) == 7` replaced by a derivation
   from `BASE_TOOL_SUMMARIES` / `COUNT_WORDS`. Task 8.3 requires the count and the
   enumeration to come from one source; the literal in this test was the second place
   to forget.

**No test in `tests/test_store*.py`, the memory tests or the inbox tests was edited.**
That is the autocommit port's own evidence, per the standing rule: a test changed to
make the port pass would be the port failing.

`tests/conftest.py` gained the `process_tz` fixture (an addition; no existing assertion
changed).

## 9.2 — the "Settled — do not re-litigate" list, checked against the implementation

From `openspec/changes/reminders/notes/README.md`:

| settled item | status |
|---|---|
| two-budget separation (`send_attempts` cleared on any return) | delivery's; column exists, **nothing writes it** (asserted) |
| crash maximum evaluated in the **pre-work** transaction | delivery's; `Store.transaction()` is what makes it implementable, and it is reentrant and poisoning so a pre-work transaction can compose |
| `next_attempt_at` initialized on **every** path into `pending` | honoured: `NOT NULL DEFAULT 0`, written explicitly by the INSERT and by the reinstate UPDATE, asserted **on the stored row** for both paths and after zeroing the column behind the repository's back |
| exits must **write** state the selector tests | honoured: `cancel` writes `status`, which is the selector's predicate — no in-memory exit |
| the cadence amendment's two-class enumeration | delivery's; untouched, and asserted untouched (`PipelineConfig` has no reminder field) |
| the audit log's two-records-for-two-questions rule | honoured: `authorization` at gate-decision time **and** `reminder` after the commit, both asserted at the tool AND the command layer, including the failed-write asymmetry |
| the resolved-time echo **with the weekday** | honoured: `render_instant` carries weekday + date + local time + zone marker, and all five surfaces call it |
| B must ship the **complete final column set** | honoured: 13 columns created at once, plus a `PRAGMA table_info` drift check that names missing *and* unexpected columns |
| `terminal_at` cut | honoured: absent, and the drift check flags a table that has it |
| no `reschedule_reminder` | honoured: no such tool, asserted against the registry |
| `reinstate_reminder` **not** a tool; `/reminders reinstate` a command, subject to the cap | honoured, both halves asserted |

Nothing was quietly reversed. One wording note: that file's older "What each change
contains" paragraph still lists `reschedule_reminder` and `reinstate_reminder` as
`reminders-core` tools; its own "Cut this scope" list (#5, #6) and this change's design
D9 supersede it, and the design is what was built.

## 9.3 — the deployed-behaviour claim, asserted rather than inspected

`tests/test_reminders_inert.py`. With no `reminders` section in config:

- **registry** — `names()` equals the pre-change list exactly, in order; mutating tools
  are still `capture` + `store_memory`.
- **system prompt** — sha256 `21113cef7389e049533e03a2904ac5f8235d641f2c12f730f6ef49e4d30ce2bd`,
  which is the value at `51972fd`, asserted for all four routes that produce it
  (`AgentConfig()`, `build_system_prompt()`, a minimal `from_dict`, and the committed
  `config.yaml`). A hash rather than an 1,836-character literal — same strength, one line.
- **owner command set** — read off the dispatch table, so a later addition is caught.
  `/remind` and `/reminders` are recognized but **inert**: they reply honestly and change
  nothing. That is deliberate and is the honest reading of "the commands reply that
  reminders are not configured" — recognizing-and-refusing beats falling through to the
  model, which would silently answer as if it could schedule.
- **owner-turn composition** — the session receives exactly the owner's text, no header.
- **the table is still created**, which keeps the DDL on one code path (the thing that
  matters most where there is no migration mechanism), and **stored rows survive a
  disabled run** unchanged.

## 9.4 — what this change does NOT do

- `grep -rniE "(UPDATE|INSERT).*(surfaced_at|send_attempts|delivered_at|reported_at)" henk/`
  → **empty**. Every hit for those four names is the `CREATE TABLE` DDL, the expected-column
  tuple, the `SELECT` list, or the dataclass/row-reader — all reads. Asserted as a test,
  scanning only non-docstring string literals (this change's prose names those columns
  constantly while explaining that it does not write them).
- `henk/reminders/` contains exactly `__init__.py` and `timeparse.py`; no `scheduler.py`,
  no `delivery.py`. Neither `timeparse.py` nor `tools/reminders.py` calls `send` or
  `send_proactive`.
- `PipelineConfig` has no reminder field: no cadence amendment rode along.
- (`henk/channel/signal.py`'s `max_send_attempts` is the bridge's pre-existing HTTP retry
  budget and unrelated.)

## Task 4.0b re-run after group 8

Scope `henk/reminders/ henk/agent/`. Every code hit reviewed:

- `timeparse.py:185` `datetime.fromtimestamp(float(epoch), zone)` — zone supplied; the
  grep matched on the inner `float(...)` parenthesis.
- `timeparse.py:270,271,469,491` `.timestamp()` — all on **aware** values built with
  `tzinfo=self._zone`, which is correct and zone-independent. This is the legitimate-hit
  case that makes "empty" the wrong bar.
- Remaining hits are docstring/comment prose describing the forbidden shapes.
- `henk/agent/` has **no** hits: the time header goes through `resolver.time_header()`.

Encoded as a suite guard rather than left as a note
(`test_no_module_in_scope_reads_the_process_timezone`), AST-based so it fails only on the
shapes that are actually wrong, with two tests validating the guard both ways. It has
already earned its keep: it caught `TimeResolver.now()`, which was a false positive but
also a genuinely bad name — sharing the word with the thing it must never be. Renamed to
`current_instant()`.

## Group 4's "confirm red" requirement

A mutation harness ran 14 deliberate breakages against
`tests/test_reminders_timeparse.py`. **All 14 go red.** Two survived the first pass and
each exposed a real test gap, now closed:

1. **weekday/month from `strftime`** survived because the locale sweep is vacuous on a
   host with only `C`, `C.utf8`, `en_US.utf8`, `POSIX` — none of which changes a month
   name. Replaced with a structural assertion (`render_instant`'s source contains no
   `%A`/`%B`/`%a`/`%b`, and does read `WEEKDAYS`/`MONTHS`), keeping the behavioural sweep
   beside it for images that have more locales.
2. **the past check performed on aware date-times** survived because no case exercised it
   inside the repeated hour. Added: at 02:15 during the **second** pass of 2026-10-25, a
   dated `02:30` resolves to the first occurrence, 45 minutes in the **past** — the
   wall-clock comparison says `02:30 >= 02:15` and accepts it. Now `shape == "past"`.

The three zone-leak mutations were run separately to validate the **TZ parametrisation
itself** (task 4.0): a zone-less `fromtimestamp(now).date()` on the `HH:MM` path fails
**only** under `Pacific/Kiritimati` (1 failure; 0 under UTC and 0 under
Europe/Amsterdam) — which is precisely why +14 is the hostile value and why "a leak
changes the *date*, not only the hour" is the criterion. That leak initially went
**green**, because every row in the next-occurrence table happened to agree across the
three zones; the discriminating case (`now` 2026-08-20 23:00 CEST = 2026-08-21 11:00 in
Kiritimati, asking `23:30`) was added and it now fails under Kiritimati alone.

## Publication safety

`.githooks/pre-commit`'s pattern layer run over every added line (tracked diff +
untracked files, with `pipefail` as the hook has it): no tailnet IPs, no
non-allowlisted `.dev` domains, no token-shaped strings, no non-placeholder phone
numbers. `gitleaks` over the working tree reports one finding, `.env` — gitignored and
untracked, so the hook's `--staged` scan never sees it. `owner.timezone` is left
**commented out** in `config.yaml` rather than filled with a plausible placeholder, so
no personal fact ships and enabling fails loudly instead of resolving in the wrong zone.

## 3.5 — DONE, verified in the built image 2026-08-20

Run on the WSL dev workstation with `sudo docker` (the socket needs it — see the "Blocked"
note below, kept for the record). Image `henk:reminders-core-verify`, built from this working
tree at `sha256:08a81d5bed3c05`, base `python:3.12-slim@sha256:2c941e860699`.

**Positive check — output matched the prediction exactly, field for field:**

```
TZ env             : 'UTC'
PYTHONTZPATH env   : ''
process zone       : ('UTC', 'UTC')
zoneinfo.TZPATH    : ()
tzdata wheel       : 2026.3 | IANA: 2026c
zones available    : 598
'localtime' present: False
resolves Europe/Amsterdam    : Europe/Amsterdam
resolves US/Eastern          : US/Eastern
resolves Australia/Lord_Howe : Australia/Lord_Howe
from wheel Europe/Amsterdam  : True
from wheel US/Eastern        : True
VERDICT: wheel is the only source
```

What each line settles, since "it resolved" was never the bar:

- `TZPATH ()` — the search path is **empty**, so no system tree can win silently. This is the
  provenance record: with nothing to search, the wheel is the only place a zone can come from.
- `from wheel … True` — positive identification rather than elimination.
- `598 zones` and `'localtime' present: False` — the wheel's count, and `localtime` is gone,
  which makes `owner.timezone`'s Region/Location key rule belt-and-braces in production and a
  real guard only on a developer's machine.
- `US/Eastern` resolves — the zone a trimmed system tree was measured to be **missing**. Under
  the default `TZPATH` on the dev host this same key came from the system tree; here it cannot
  have.
- `TZ=UTC` and `time.tzname == ('UTC', 'UTC')` — the floor under D8a is in place. The suite's
  three-zone parametrisation is still the real guard; this only means production is
  deterministic if that guard is ever removed.

**Negative control — failed loud, which is the half that matters:**

```
ModuleNotFoundError: No module named 'tzdata'
  ...
zoneinfo._common.ZoneInfoNotFoundError: 'No time zone found with key Europe/Amsterdam'
```

No `resolved anyway:` line. So with the wheel removed the very first resolution raises rather
than quietly reaching for an older tree — absence is loud, and staleness (the failure mode this
decision exists to close) has nowhere to hide. `PYTHONTZPATH=""` is doing real work: without
it this command would have printed a zone from `/usr/share/zoneinfo`.

Both halves together are what the task asked for. A check that only recorded "resolution
succeeded" would have passed on the exact configuration D8 exists to rule out — the base image
carrying its own tree — because that configuration also resolves.

## Blocked — historical, resolved above

**Task 3.5 (verify the timezone database inside the BUILT image) could not be done in
this session:** `docker` requires password sudo here (`/var/run/docker.sock` is
`root:root` mode 660 and the user is not in a docker group), and the sandbox has no
interactive tty. Everything else in group 3 is done: `tzdata>=2025.1` is a declared
dependency (locked at 2026.3, IANA 2026c), and `PYTHONTZPATH=""` + `TZ=UTC` are in the
Dockerfile as their own `ENV` instructions.

Verified on the dev host as a floor, not a substitute — the point of 3.5 is precisely
that the dev host is not the deployed image:

```
default TZPATH  -> ('/usr/share/zoneinfo', ...), 599 zones, 'localtime' PRESENT
PYTHONTZPATH="" -> TZPATH=(), 598 zones, 'localtime' ABSENT,
                   US/Eastern and Europe/Amsterdam both resolve from the wheel
```

The in-image check still to run. It records **which source** provided the zone rather than
only that resolution succeeded — a check that logs "resolved OK" passes on the very
configuration it exists to rule out. Run these in a real terminal; in Claude Code, prefix each
with `!` so the output lands in the conversation.

```bash
docker build -t henk:reminders-core-verify .
```

```bash
docker run --rm -i henk:reminders-core-verify python - <<'PY'
import os, time, zoneinfo, tzdata
import importlib.resources as res

print("TZ env             :", repr(os.environ.get("TZ")))
print("PYTHONTZPATH env   :", repr(os.environ.get("PYTHONTZPATH")))
print("process zone       :", time.tzname)
print("zoneinfo.TZPATH    :", zoneinfo.TZPATH)
print("tzdata wheel       :", tzdata.__version__, "| IANA:", tzdata.IANA_VERSION)
zones = zoneinfo.available_timezones()
print("zones available    :", len(zones))
print("'localtime' present:", "localtime" in zones)
for key in ("Europe/Amsterdam", "US/Eastern", "Australia/Lord_Howe"):
    print(f"resolves {key:20}:", zoneinfo.ZoneInfo(key))
# THE SOURCE, positively identified — not inferred from "resolution succeeded".
for key in ("Europe/Amsterdam", "US/Eastern"):
    print(f"from wheel {key:18}:",
          res.files("tzdata").joinpath("zoneinfo/" + key).is_file())
print("VERDICT: wheel is the only source" if zoneinfo.TZPATH == ()
      else "VERDICT: FAIL - TZPATH is not empty, a system tree can win silently")
PY
```

**Expected**, matching the dev-host floor above:

```
TZ env             : 'UTC'
PYTHONTZPATH env   : ''
process zone       : ('UTC', 'UTC')
zoneinfo.TZPATH    : ()
tzdata wheel       : 2026.3 | IANA: 2026c
zones available    : 598
'localtime' present: False
resolves Europe/Amsterdam    : Europe/Amsterdam
resolves US/Eastern          : US/Eastern            <- absent from a trimmed system tree
resolves Australia/Lord_Howe : Australia/Lord_Howe
from wheel Europe/Amsterdam  : True
from wheel US/Eastern        : True
VERDICT: wheel is the only source
```

`TZPATH == ()` is itself the record of provenance — with no search path there is nothing else
the zone could have come from — and the `from wheel` lines confirm it positively rather than
by elimination.

**Negative control, and it is the half that matters.** Without the wheel the first resolution
must raise, not quietly fall back to something older: staleness is the silent failure mode this
whole decision exists to close, and absence is the loud one.

```bash
docker run --rm -i --user root henk:reminders-core-verify sh -s <<'SH'
pip uninstall -y -q tzdata
python - <<'PY'
import zoneinfo
print("resolved anyway:", zoneinfo.ZoneInfo("Europe/Amsterdam"))
PY
SH
```

**Expected:** `zoneinfo._common.ZoneInfoNotFoundError: 'No time zone found with key
Europe/Amsterdam'`, and no `resolved anyway:` line. If it prints a zone instead, the base image
is carrying a tree that `PYTHONTZPATH=""` did **not** exclude, D8's precedence claim is false,
and that is the finding.

(`--user root` because the image drops to the non-root `henk` user, which cannot write
site-packages. `--rm` so the mutilated container is gone either way. `-i` so the container's
shell can read the heredoc from stdin.)

Record the actual output in this file when it runs, then tick task 3.5.

## 9.5 — hard stop

**No deploy to rp5.** Owner go required. When it comes, the deploy is expected to be a
behavioural no-op (`reminders.enabled` defaults to false and rp5's locally-modified
config carries no `reminders` section and no `owner.timezone`), so pair it with the
pending rp5 rebuild for `51972fd` — the per-phase timeout fix, committed and not
deployed — rather than rebuilding twice.
