# Reminders — revision record and continuation notes

**Read this file, not `revision-record.md`.** The record is 1,522 lines of eleven review
rounds; this is what you need to act. Dated 2026-08-19.

## Status

| Change | State |
|---|---|
| `channel-integrity` | **DONE** — implemented, deployed to rp5 and archived 2026-08-20 at `openspec/changes/archive/2026-08-20-channel-integrity/`. Its design D5/D6 and its As-built deploy record are required reading for `reminder-delivery`. The prerequisite is satisfied: `SendOutcome` and `send_proactive` exist. |
| `owner-acknowledgement` | Not written; `proposal.md` + the moved `agent-core` delta exist at `openspec/changes/owner-acknowledgement/`, with thirteen source-verified findings to fold in. Split out of `channel-integrity` — it carries the only new endpoints, the only encapsulation exception and the only rollback flag, and **nothing below depends on it**. |
| `reminders-core` | **IMPLEMENTED 2026-08-20**, green, **not yet committed or archived** — store with the complete column set, `Store.transaction()`, DST-correct time resolution, `remind` / `cancel_reminder` / `reminders_read`, the `/remind` and `/reminders` commands, audit v4, shipping inert. Two tasks remain open and neither is code: **3.5** (verify the zone database inside the built image — needs a real terminal for `docker`) and **9.5** (deploy, hard stop pending owner go). **Read `openspec/changes/reminders-core/notes/apply-enumerations.md` first** — its header block is the state-of-play, and it carries the apply-time enumerations, the ladder matrix walk, the verification record for group 9, and the exact commands for 3.5. The verified DST facts behind the requirement wording are in that change's `notes/dst-verified-facts.md`, still required reading before touching `timeparse.py`. |
| `reminder-delivery` | Not written. Scheduler, delivery, catch-up, the delivered-reminder note, the cadence amendment. **Also inherits outbound send serialization** — see the note below. |
| `openspec/changes/reminders/` (the original draft) | **Superseded, do not implement.** ~20 sites stale; several decisions reversed. |

The original single change was reviewed to destruction: eleven rounds, then one clean pass
from a fresh reviewer. It must be rewritten as the changes above.

## What each change contains

**`channel-integrity`** — `send()` reports a delivery outcome (`delivered`/`partial`/`failed`,
where `failed` means *not confirmed*, never *nothing arrived*); `send`/`send_proactive` split with
a caller-supplied failure notice; splitting measures UTF-8 bytes; explicit **total** bridge
timeouts. Fixes bugs live today, valuable even if reminders never ship. Owner read receipts and
the typing indicator moved to `owner-acknowledgement`; send serialization moved to
`reminder-delivery`.

**Two things `reminder-delivery` inherits, decided in `channel-integrity`'s design D5/D6 — read
them before designing:**

1. **Outbound send serialization is yours.** Chunks of concurrent sends can interleave today. It
   was deliberately not fixed: the only cross-task sender today emits a 212-character
   single-chunk notice, which cannot be interleaved, so a mutex buys a cosmetic fix at the cost of
   real head-of-line blocking (`N × per-chunk latency`, `N` unbounded; ~90–144s for an ~18-chunk
   `/memories` reply on a *working* bridge). Your scheduler is the first real second sender.
   **Before designing the bound: log signal-cli send latency on rp5 for a day.** Both the
   serialization residual and `send_timeout_seconds = 10.0` are provisional on that number.
   *Actionable since 2026-08-20* — `channel-integrity` is deployed, so the measurement can
   start whenever. **Take the rp5 rebuild with it:** a post-archive fix (`51972fd`) raised the
   effective per-operation `read` ceiling from 6.0s to the full configured 10.0s, and it is
   committed but NOT deployed — rp5 still runs `0bfcc5b`, i.e. 6.0s. Deliberate: redeploying
   for a ceiling nobody has observed being hit is churn when your measurement may change the
   number anyway. So measure against 6.0s, decide the value, then rebuild once. Two things to fold in when you take it: multi-chunk delivery is confirmed
   working on the real bridge (the As-built record has the numbers), and that change's
   `partial`/`failed` log watch was archived **open**, so its grep is where any real send
   failure will first show up. Run both from the same logs. Two
   mechanisms were already designed and rejected — a bounded hold (its `failed` outcome is
   unreachable) and a chunk cap (it discards owner-requested content on the healthy path). The
   preferred mechanism is **delivery-path priority**; if you need interleaving fixed without a
   bound, **paginate rather than discard**.
2. **`Delivery does not wait on a turn` needs amending or priority.** That scenario is scoped by a
   requirement about the *serial turn queue* and stays true of the queue. Once a mutex exists it
   stops being true of the *send path* — a reminder due mid-multi-chunk-reply waits. Amend the
   scenario or implement delivery-path priority. A test that appears to prove otherwise is testing
   a cooperative double (`conftest.FakeChannel` has no lock), which this project's task lists
   forbid.

**`reminders-core`** — the reminders table (with the complete final column set, see below);
resolve-to-instant time handling with the DST rules; **three** tools —
`remind` / `cancel_reminder` / `reminders_read`; `/remind`, `/reminders`,
`/reminders cancel`, `/reminders reinstate`; audit `schema_version` 4. **Deploys inert** —
`reminders.enabled: false` — because a version that confirms reminders it cannot deliver is
the capability's worst failure.

*(This paragraph originally listed `reschedule_reminder` and `reinstate_reminder` as tools.
Cuts #5 and #6 below removed both, and `reminders-core`'s design D9 is what was built:
rescheduling is `cancel_reminder` + `remind` — two calls with two echoes, and the echoes are
the safety mechanism — and reinstating is `/reminders reinstate <id>` only, which is what
keeps it subject to the pending cap instead of bypassing it. Corrected here so the table
above and this list agree.)*

**`reminder-delivery`** — polling scheduler, delivery, grace/late/missed handling, the
delivered-reminder note, and the `incident-triage` cadence amendment.

## Cut this scope when writing `reminder-delivery`

The fresh review's verdict was that delivery was over-engineered by about a third, and that
the excess was *generating* the defects: rounds 6–11 produced six consecutive criticals, all
inside the retry/report machinery, none in the store, time resolution, tools, commands, audit
records, or the channel work. Cut, in value order:

1. **The report's item bound and "and N more" pagination** — dissolves an open critical
   (below). The report already batches like a delivery, so 100 missed reminders are a handful
   of messages, which is the right volume for a long outage.
2. **`terminal_at`** — dead state once the report's time budget was deleted. It would be a
   permanent column in a store with no migration path.
3. **The seven-step backoff schedule and `unconfirmed_sends`** → one fixed ~15-minute retry
   floor. The requirement was never "roughly a dozen duplicates", it was "not 2,880".
4. **Chunk-atomic batching** (measure-before-add, per-batch transactions, the straddle rule,
   the largest-framing config validation) → one message per due reminder. The typical tick has
   one reminder; the outage case is already covered by the catch-up summary.
5. **`reschedule_reminder`** — it is cancel + remind, two calls with two echoes.
6. **`reinstate_reminder` as a tool** — keep `/reminders reinstate` as a command. Removes the
   pending-cap bypass, the counter reset, and half the constitutional question about tiers.

What remains: poll, select due, send one message, record the outcome, one retry floor, a
crash-attempt bound, grace → missed, `reported_at`, and the catch-up summary.

## Two open defects — fix these when writing B and C

1. **Report pagination strands rows.** The pre-work transaction charges an attempt to every
   *selected* row; if composition then omits overflow rows behind an item bound, those rows get
   no post-send transaction, so their counter never clears and they are incremented again next
   tick. Rows past position `3 × bound` hit the crash maximum **without ever being named** and
   are marked "gave up telling you". Cut #1 above dissolves this entirely.
2. ~~**The store has no transaction API.**~~ **FIXED by `reminders-core` (2026-08-20).**
   `Store.transaction()` exists: a `BEGIN IMMEDIATE` context manager on an
   `isolation_level=None` (autocommit) connection, reentrant by depth so only the outermost
   scope commits, and **poisoned** by any nested failure so a swallowed inner exception still
   rolls the whole transaction back. Every repository sharing the file — memory, inbox and
   reminders — is transaction-agnostic and commits nothing of its own. `memory.py` and
   `inbox.py` were ported, and the pre-existing suite passes **untouched**, which is the
   port's own evidence. So delivery's pre-work / post-send transactions are now
   implementable; `tests/test_store_transaction.py` is the contract they can rely on, and
   note that a store call dispatched off the event loop would break the single-connection
   assumption (there is a grep-based test that fails if one appears).

## Settled — do not re-litigate

Arrived at expensively and verified: the two-budget separation (`send_attempts` cleared on any
return so it accumulates only across process death); **evaluating the crash maximum in the
pre-work transaction, not post-send** (a crash is what prevents post-send, so a maximum
evaluated there is never evaluated on the path it bounds — measured at 84 attempts against a
maximum of 3); `next_attempt_at` initialized on every path into `pending` (a null value makes a
row permanently unselectable, since `NULL <= now` is not true); exits must **write** state the
selector tests, because a selector is a query; the cadence amendment's two-class enumeration;
the audit log's two-records-for-two-questions rule; and the resolved-time echo with the weekday,
which is the actual product insight — a mis-resolved time becomes visible in the same reply.

Also settled: **B must ship the complete final column set.** All DDL is `CREATE TABLE IF NOT
EXISTS` with no migration mechanism, so a column added after the table exists is never created.

## Warning — DISCHARGED by `reminders-core` (2026-08-20)

The concern below was real and is now addressed. What closed it: a dedicated three-round
scrutiny pass on the time model (recorded in `reminders-core/notes/dst-verified-facts.md`),
then an implementation whose every clock-touching test runs under three process timezones
including `Pacific/Kiritimati` (+14, so a leak changes the *date*), plus a committed
**ground-truth oracle** (`tests/test_reminders_oracle.py`) that walks every UTC minute of a
year and asserts the classifier agrees on every local minute — 12 zones, zero mismatches.

The three specific traps named below are each covered by discriminating tests: the
`02:30 -> 03:30` silent round-trip is rejected naming both transition-boundary neighbours,
ambiguous readings are detected by the offset-comparison step the round-trip cannot see, and
bare `HH:MM` selects a candidate **before** evaluating it.

**The lesson worth carrying forward:** 12 of 14 deliberate mutations of the finished
implementation went red immediately — and **two passed**, both revealing real gaps (a locale
test that was vacuous on a host with only English locales, and a past-check that was never
exercised inside the repeated hour, where aware-datetime comparison silently accepts an
instant 45 minutes in the past). A third mutation, a zone-leaked local *date*, initially went
green because every row of the next-occurrence table happened to agree across all three
process zones. **Green tests are not evidence until you have watched them go red.**

The original warning, kept for the record:

**The DST / time-resolution work has had the least review of anything here and is the most
bug-dense part of the change.** Eleven rounds went into delivery machinery instead. Give it a
scrutiny pass of its own: nonexistent local times (spring forward — `02:30` silently
round-trips to `03:30`), ambiguous times (fall back — both folds round-trip cleanly, so the
nonexistent-time check cannot detect them), and bare `HH:MM` advancing to the next valid
occurrence rather than rejecting.

## The model

`verify_selector_invariants.py` models the delivery state machine as specified and asserts
termination under crash faults, detectability where termination is impossible, quiescence under
channel failure, conservation (success set == acknowledged set), and partial handling. Run it
with `python3 verify_selector_invariants.py`.

It found four defects that eleven rounds of reading did not. **The model is disposable; the
fault-injection matrix is the transferable artifact** — retarget it at the real store and
scheduler in `reminder-delivery`, and note its stated non-coverage at the top of the file. If
the design changes, change the model and the spec together: a defect found in the model is
unfixed until the requirement text changes, and the requirement is what gets built.
