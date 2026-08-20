# Fix Plan — reminders change, round 11 (final)

Splitting `openspec/changes/reminders/` into **A** `channel-send-integrity`, **B**
`reminders-core`, **C** `reminder-delivery`. No source edits.

R1 (C1–C5, M1–M14, m1–m17) → R2 (B1–B4) → R3 (D1–D8, N-a–N-d) → R4 (E1–E11) → R5 (F1–F8) → R6 (G1–G11) → R7 (H1–H9) → R8 (J1–J10) → R9 (K1–K6) → R10 (L1–L8) → R11 (N1–N7).

§1 resolves round 4, §1b round 5, §1c round 6, §1d round 7, §1e round 8, §1f round 9, §1g round 10. §2 holds five requirements in full text. §3–§4 carry
forward settled resolutions. §5 is the sweep, now enumerated by `find` across all nine
artifacts.

---

## 0. Process

### The term-of-art list — the fix that would have prevented five findings

Accepted outright; this is the review's most valuable contribution across four rounds. The
pattern, tabulated from my own failures:

| Round | Word | Formal meaning | Provided? |
|---|---|---|---|
| 1 | "bounded by its own pending cap" | rate bound | no — a storage cap |
| 2 | "≤1 per tick" | count bound | no — splitting restores count |
| 3 | "atomic" | all-or-nothing | no — internal retry duplicates |
| 4 | "at-least-once" | never zero | no — `missed` is zero |
| 4 | "distinct" | independent budgets | no — one shared counter |
| 5 | "at most one report" | at-most-once | no — a crash repeats it (F3) |

Every one is a **borrowed term of art**, reached for to stop explaining. So:

> **Closed vocabulary — atomic, exactly-once, at-least-once, idempotent, distinct,
> independent, separate, isolated, bounded, ordered, durable, once, monotonic,
> serializable, guaranteed, never, always.** Before submitting, **grep the new text for
> these words.** Each obliges either satisfying the formal definition or naming the
> deviation in the same sentence.
>
> **Plus the paraphrase family, because the vocabulary is closed and the ways of saying it
> are not** — *at most one, no more than, exactly one, a single, only ever, in every case,
> cannot, all-or-nothing*. F3 proves the need: the grep flagged "never silently dropped" and
> walked past "at most one report" two sentences later.
>
> **The grep is only the cheap first pass. The real trigger is claim *type*:** any claim that
> **counts** (once, at most one, exactly N), **relates two mechanisms** (distinct,
> independent), or **orders** (before, atomically) demands an execution trace — and a trace
> catches longhand a lexical check never will. That answers the reviewer's Q3: the list does
> not need to grow indefinitely, because the trace obligation already covers what the grep
> cannot.
>
> Two different obligations, because two different failures:
> - words asserting a **state or delivery property** (atomic, once, bounded, durable) →
>   **name the enforcement line** (property-anchoring, R3);
> - words asserting a **relationship between mechanisms** (distinct, independent,
>   separate) → **trace one concrete execution and show the paths diverge.** E1 is exactly
>   the case where the first check passes and the second was never run.

**Q1 — why did §0's table miss the two claims in the requirement it was written to
protect?** Because I built it from the reviewer's round-3 findings rather than by grepping
my own new §2 text. The table was a record of answers, not an inspection of the work. The
grep step above is what makes it an inspection.

**Q2 — what would have caught E1?** Not property-anchoring: "distinct" *has* an
enforcement point (the counter), and I would have pointed at it and called it anchored.
What catches it is the three-row execution trace the reviewer wrote — attempt 1, 2, 3 with
the counter column. Hence the second obligation above. R2 separated the budgets
conceptually, R3 wrote a mechanism, and nobody ran a trace through it.

**R5 Q1 — what does it say that I implemented a reviewer's mechanism faithfully and did not
trace it?** That a reviewer's *prescription* arrives with exactly the same status as my own
draft — unverified. I ran the trace obligation on their *diagnosis* (E1's counter table) and
not on their *fix*, which is how F1 got through: a mechanism specified as three writes, with
no statement about their atomicity. **Rule: run the same obligation on the fix as on the
finding, regardless of who authored it.** A suggestion from review is a starting point, not
a specification.

**R5 Q2 — is the reflex "find what fails" rather than "find what misleads"?** Yes, and the
sweep proves it: it covered `specs/` and `proposal.md` — the normative artifacts, the ones
whose staleness a test would eventually catch — and never opened `design.md`, which carries
*rationale*. Stale requirements get caught by tests; **stale rationale gets believed.** A
reader in six months reads D8, concludes cancellation is command-only, and reasons onward
from it. So the sweep rule now covers rationale explicitly and enumerates its targets by
`find`, not by recall (§5).

**Q3 — does re-crossing need to reach every artifact the change touches?** Yes, and it is
a grep, not a judgement — over a `find`-enumerated file list, not a remembered one.
Performed properly in §5: D8 and E1 landed in **~20 sites across all nine artifacts**,
including `tasks.md:61`, a task that tests the inverse of what will ship, and the whole of
`design.md`'s D8 section, which round 4's sweep never opened.

### Tabulate mechanisms; prose hides a missing dimension

**R6 Q1 and Q3, answered together, because they have one answer.** The word-list grep is tuned
for *claims*; "cleared when the send returns" is a **description**, makes no claim, and passed
every check while being wrong (G1). A third grep trigger would not have caught it. What catches
it is form:

> **A mechanism with more than one moving part is specified as a table with mandatory columns,
> not as prose. The empty cell is the finding.**

G1, G2, G4 and G5 are four instances of two columns nobody wrote down — *the window a counter
covers* and *the transaction its writes belong to*. Prose let me describe `report_attempts` for
a whole round without ever stating its window, because prose has no slot demanding it. A grid
does. This is generative rather than detective: it produces the omission instead of hunting it.

So yes to Q3 — **state machines, budgets and transitions are categories where prose is the
wrong form.** Six rounds of prose about counters, and the artifact that would have prevented
most of it is a four-column grid (§2.2).

### When you delete a mechanism, grep for the claims it supported

Three rounds, three variants of one failure, and only two had rules:

| Round | Failure | Rule |
|---|---|---|
| 6 | G1: a mechanism **copied** by shape, not by the property it enforces | trace the relationship |
| 7 | H3: state **introduced** as "derived", so it got no write points | derived is state until proven otherwise |
| 8 | J1: a mechanism **deleted**, and the claims it supported survived it | *this one* |

> **A deletion is traced for what it leaves asserting, not only for what it removes.** Grep for
> the deleted names — and then for the *properties* they carried (bounded, not attempted again,
> gives up, at most one), because those are the survivors a name-grep walks past.

Evidence it is needed: my round-7 self-check caught the scenario naming `report_attempts` and
missed §2.5's cost paragraph asserting a give-up H6 had removed and §2.2's "SHALL NOT be attempted
again" asserting an inertness H6 had removed. It caught the survivor that named the deleted thing
and missed the two that named its consequences — which is J1 and J2.

**Two further corrections to the table rule itself** (R8 Q2 and Q3):

- **`n/a` is a forbidden cell value.** J3 hid behind two of them: "this isn't a counter" masked
  "and the transition it triggers has no transaction". A cell says *why* it is inapplicable, or it
  is a finding.
- **Newly added rows get audited by the rule that prompted them.** J4 is H1's defect inside the
  row added to satisfy H3 — new cells arrived after the split-column rule and were never checked
  against it. The rule is *check every cell, including the ones you just wrote.*

### Every new transition into a state is checked against every invariant on that state

**R6 Q2, accepted as a rule and executed in §2.6.** G3 was invisible to every check so far: no
term of art, no keyword, no reversed decision — a *new path into `pending`* whose invariant
(the cap) lives in a requirement that never mentions the new verb, and never needed to until
the verb existed.

> **For each status, enumerate the transitions into it and the invariants on it. A new verb
> adds rows; every row must satisfy every invariant.**

Enumerable and small: eight statuses, four invariants. §2.6 carries the grid, and it is what
found G3's siblings without a reviewer.

**Q4 — why does the formal-sounding term arrive exactly when a fix is nearly done?**
Because a term of art is a compression: it lets me stop explaining and declare the thing
finished. The reach for it *is* the reach to be done. So the trigger for the word-list grep
is the feeling of having finished — which is precisely when I have been least inclined to
look again. Naming that is more useful to me than the rule is.

---

## 1. Round-4 resolutions

### E1 — the two budgets share one counter; abandonment fires at 1m30s **[CRITICAL]**

Accepted; the execution trace is correct and decisive. A backoff retry *is* a send, so it
increments the counter, so `max=3` terminates the reminder before the 5m step is ever
reached. **C5's original defect — abandoning on a routine bridge restart — is reproduced
inside the requirement written to fix it**, with the fix sitting unreachable two paragraphs
below.

Root cause, stated precisely because it is the reusable part: *incrementing before the send
is what makes a crash indistinguishable from a returned failure* — which is the counter's
purpose on the crash path and its fatal flaw on the send path. One counter cannot carry
both.

**Resolution [C] — the reviewer's two-state model, plus one derived column:**

| Column | Written | Cleared | Bounds |
|---|---|---|---|
| `send_attempts` | before each send | **when the send returns at all**, either outcome | crash loop: max 3 → `abandoned` |
| `unconfirmed_sends` | when a send returns `failed` | **on reinstate** (a confirmed delivery makes the row terminal, so clearing there is unreachable) | backoff schedule position |
| `next_attempt_at` | on each `failed` return, = now + schedule[`unconfirmed_sends`] | — | makes the tick query `WHERE status='pending' AND next_attempt_at <= now` |

`send_attempts` therefore only accumulates across process death: a returned failure clears
it, a crash leaves it set, and the next start increments. On startup, `send_attempts > 0`
identifies a crash victim; `unconfirmed_sends > 0` with `send_attempts == 0` identifies a
cleanly-rejected send. The discriminator is mechanical, and "distinct" now has an execution
trace rather than an assertion (§2.2 carries it).

On the reviewer's own uncertainty (a boolean plus the existing counter): a boolean cannot
bound a crash *loop*, so it needs a companion count anyway — same state, one more moving
part. Two counters is the simpler shape. `next_attempt_at` is derived, not a third fact.

### E2 — "at-least-once" is the fifth borrowed guarantee **[MAJOR]**

Accepted, and it is the most instructive of the five because it reads as a *concession*.
§2.2 permits zero deliveries (that is what `missed` is), so the formal guarantee is exactly
what the design does not provide.

**Resolution [C]** — the reviewer's sentence, which contains no term of art and cannot be
wrong:

> Delivery is neither idempotent nor guaranteed. A confirmed delivery may nonetheless have
> arrived more than once; an unconfirmed delivery may have arrived zero times. Duplicate
> delivery is accepted; non-delivery terminates as `missed` and is reported, never silent.

### E3 — the report is referenced twice, written nowhere, and D7 broke its trigger **[MAJOR]**

Accepted on all three counts. And the resolution goes further than the two options offered,
because both have a flaw: appending to the next delivery can wait a week, and
in-conversation routing leaves "I failed to do something you asked" invisible until the
owner next speaks — which the reviewer flagged as their own residual doubt.

**Resolution [C] — a missed reminder is not a new message class; it is the outcome of
class (b).** The owner asked for a message at time T. Telling them "I could not deliver it"
carries *the same authorization* as delivering it: both trace to one owner-created row, and
neither is triggered by the clock alone — the trigger is a **delivery attempt on a specific
row reaching a terminal outcome**.

So §2.3 collapses from three classes to **two**, and the vague one is the one that
disappears. Mid-run misses are reported when they happen, unprompted, with no fourth class
and no invisibility.

The tightening that keeps this from being a loophole: class (b) requires *a delivery
attempt on a specific row*. A hypothetical "weekly summary of your reminders" has no
delivery attempt behind it, so it is caught by both the enumeration and the category ban —
which a looser "report about owner-created rows" wording would not have been.

The report is written out as the fifth requirement (§2.5): contents, item bound with an
"and N more" remainder, coalescing with any delivery message in the same tick,
chunk-atomic batching, send-then-mark, once-per-item.

### E4 — two incompatible batching rules in adjacent sentences **[MAJOR]**

Accepted; N-a's config-time intent had leaked into the runtime rule, where it packs
needlessly small and contradicts measure-before-add. Split explicitly in §2.1: **runtime**
batching measures the actual rendered batch in its actual framing; **config-load validation
and the N-a test** use the largest framing.

### E5–E8 + the full sweep — D8's blast radius **[MAJOR]**

Accepted, and the sweep found twelve sites rather than four. Full list in §5. Notably
`tasks.md:61` asserts *"no registered tool can cancel, edit, or delete a reminder"* — a
task that tests the inverse of the shipped design. Fixed by rewriting from the sweep list,
not by patching the reported four.

**E8** specifically: `/reminders` gains a bounded tail of **recently cancelled** reminders
with their ids (alongside m1's `source` column), because reinstate-by-id is unusable once
the cancel confirmation has scrolled away — which is exactly the 18:05 "why didn't you
remind me?" case that is §1a's strongest argument. Without the affordance the recovery
story does not close.

### E9 — `partial` on a single-reminder message has no status rule **[MAJOR]**

Accepted; it is the one case the defensive batch clause does not reach, and D6 removed the
banner that would otherwise have told the owner. **Resolution [C]:** a `partial` on a
single-reminder message is handled as `failed` — leave pending, redeliver — consistent
with the batch rule and with duplicate-beats-loss.

### E10 — batch status writes need a transaction rule **[MINOR]**

Accepted; R2's §0 promised one batched transaction and §2.1 dropped it. With
`synchronous=FULL` on one connection, 25 commits is 25 event-loop fsyncs, and a crash at
update 12 splits the batch. §2.1 states: one transaction per batch, so attribution is
all-or-nothing.

### E11 — approval prompts are unclassified against a closed enumeration **[MINOR]**

Accepted as future-proofing. A per-instance tool during an announceable event turn would
place a prompt on the channel with no inbound message behind it and no matching class — and
after D8, per-instance ships unexercised precisely so its first use can be something
consequential, i.e. exactly such a change. §2.3 adds: this requirement governs
Henk-authored **content** messages; approval prompts are governed by the approval-gate spec.

---

## 1b. Round-5 resolutions

### F1 — a crash *between* the three post-return writes bounds nothing **[CRITICAL]**

Accepted; the trace is fatal and it is my own faithful implementation of a mechanism nobody
traced. One `failed` return triggers three writes (clear `send_attempts`, increment
`unconfirmed_sends`, set `next_attempt_at`). A crash between the first and second clears the
crash budget without advancing the backoff, so `send_attempts` never exceeds 1,
`next_attempt_at` never advances, the reminder is due on every tick, and **neither budget
bounds it** — under `restart: unless-stopped`, forever, possibly arriving each time.

Mirror image of E1: E1 was one counter that could not distinguish two facts; F1 is two
counters that can, but only if written together.

**Resolution [C]:** the post-return bookkeeping is a **single transaction** covering all
three writes across **every member of the tick's batch** — so a crash lands wholly before it
(pre-send state, `send_attempts` set, crash budget catches it) or wholly after it (consistent,
backoff advanced). E10 established one-transaction-per-batch for *status*; this extends it to
*retry bookkeeping*, which fell outside that rule. Scenario added: a crash immediately after a
returned failure leaves the backoff advanced, not reset. §2.2 carries it.

### F2 — the report has no attempt counter **[MAJOR]**

Accepted; my own suspicion in the question was right. Send-then-mark means a deterministic
fault in the report path (a render error, an OOM on a 40-item batch) reproduces the report on
every restart, unbounded — the exact shape §2.2 bounds for deliveries and §2.5 did not.

**Resolution [C]** *(superseded by H6, §1d — the report now reuses delivery's counters and needs
none of its own)*: `report_attempts`, same shape as `send_attempts`; at the maximum, stop
attempting and log at error level. The cost is stated in the requirement rather than hidden:
a terminal reminder can end up unreported if the report path is deterministically broken. It
stays discoverable via `reminders_read` and the audit log, so it is not lost — but
"discoverable if you look" is weaker than this capability's standard elsewhere, and the trade
is taken only because the alternative is an unbounded loop.

### F3 — "at most one report" is an at-most-once claim, contradicted two sentences later **[MAJOR]**

Accepted — the sixth borrowed guarantee, and the one my own grep walked past because "at most
one" is a paraphrase rather than a listed word. Restated as the mechanism: reported once per
successful send-and-mark cycle, a crash repeats it, bounded by `report_attempts`, duplicate
reporting accepted and an unreported terminal reminder not. The paraphrase family and the
claim-*type* trigger are now in §0.

### F4 — recurrence would inherit permission from class (b) **[MAJOR]**

Accepted, and it is the one attack that got through §2.3. A recurring row is by construction a
clock-triggered generator of class-(b) messages: owner-created row, genuine delivery attempt,
terminal outcome — and a heartbeat by any ordinary reading. The clause written to forbid
heartbeats would bless one, with a citation.

**Resolution [C]:** one sentence — this requirement governs one-shot rows; a recurring
schedule SHALL NOT be introduced without its own amendment here.

On the reviewer's own doubt that this is overreach: it is the project's **existing** governance
rule applied prospectively. NORTH-STAR.md already says a spec requirement that contradicts it
"gets amended deliberately, never silently", and `design.md`'s Non-Goals already lists four
reasons recurrence needs its own change. This is the fifth, and naming it costs one sentence
while the alternative is a future change inheriting an argument it never made. Flagged at
presentation as a strikeable owner decision.

### F5 — reinstate does not reset the retry counters **[MAJOR]**

Accepted; D8's new verb crossed with E1's new state and neither of us re-crossed the pair. A
reinstated reminder would resume at backoff position 4 and could be abandoned by crashes
charged to its previous life. **Resolution [B/C]:** reinstate resets `send_attempts`,
`unconfirmed_sends` and `next_attempt_at` — it is a fresh owner intent. The decorative
"cleared on confirmed delivery" cell is corrected to "on reinstate", since a confirmed
delivery makes the row terminal and that clear was unreachable.

### F6 — the sweep never opened `design.md` **[MAJOR]**

Accepted, and the systematic point lands: round 4's sweep covered the *normative* artifacts
— the ones whose staleness a test eventually catches — and skipped the one carrying
*rationale*. Re-run by `find` with no filter: **~20 sites across all nine artifacts**,
including the entire D8 section of `design.md` (201–215) and three more `tasks.md` sites.
Complete list in §5. §0's rule now enumerates targets by `find` and names rationale
explicitly.

### F7 / F8 — batch non-durability and the cross-change dependency **[MINOR]**

Both accepted as single clauses in §2.1: a batch is not a durable object across retries (only
"every member stayed pending" survives a failure, never "the batch is redelivered"), and C's
transaction guarantee depends on B's store hardening — declared, because an undeclared
cross-change dependency is exactly what a three-way split makes dangerous.

---

## 1c. Round-6 resolutions

**Four of six findings were one missing table.** Written first, per the recommendation, and it
dictates G1, G2, G4 and G5 rather than each being patched — patching separately is how G1
happened in the first place. The table is in §2.2.

### G1 — `report_attempts` is cleared before the window it bounds **[CRITICAL]** *(fix superseded by H6: the counter is gone and the gap is closed structurally)*

Accepted, and it is the sharpest finding of six rounds: F2's *fix* failed at the same point F1
did, one requirement over, because I copied `send_attempts` **by shape rather than by the
property it enforces**. `send_attempts` works because its window is *the send* and it clears on
return. The report's dangerous window is the **send→mark gap**, so clearing on return sits before
the window opens and the counter never exceeds 1 — the unbounded loop F2 claimed to bound, with a
counter in front of it that cannot see it.

**Resolution [C]:** cleared **on a successful mark**. §2.5 now states the window and why the
clear point differs from delivery's, so the next reader cannot copy the shape again.

### G1b — and then the report needs the two-budget split too **[MAJOR]**

Accepted; you were right to continue past the fix. With the clear at the mark, a channel outage
walks the counter to 3 in 90 seconds and gives up — contradicting §2.5's own promise two
paragraphs above that an unavailable channel makes the report *wait*. **Resolution [C]:**
`report_unconfirmed` on the same backoff schedule, bounded by time. On your judgement call: keep
the promise and pay for the counter. That promise is §2.5's honest half, and dropping it would
re-break the standard the requirement is named for.

### G2, G4, G5 — three transaction boundaries, one rule **[MAJOR]** *(structure superseded twice: by H2's per-message granularity, then by K1's pre-work transaction — see §1f)*

All accepted; all three are the "transaction" column nobody wrote. **A tick makes exactly two
transactions**, each across every batch member: pre-send (`send_attempts` for all members — a
partial increment could `abandon` members with no send ever attempted, contradicting §2.1's
all-or-nothing attribution), and post-send covering **all** post-send writes *including the
status transitions they trigger* — because a crash between bookkeeping and status could leave
`send_attempts` at the maximum with the row still `pending`, and the next start would increment
**past** a maximum the requirement says SHALL NOT be exceeded. This subsumes G4 and collapses
§2.1's and §2.2's two competing clauses into one.

Your Q1 answer is recorded as verified: commit-time and post-commit crashes are clean, WAL
commit is all-or-nothing, and `synchronous=FULL` carries it against power loss — which is now a
**named dependency** (G9) rather than a silent assumption.

### G3 — reinstate bypasses the pending cap **[MAJOR]**

Accepted, and it is the finding no previous check could have produced: no term of art, no
keyword, no reversed decision — a new path into a state whose invariant lives in a requirement
that never mentions the new verb. §2.6 carries both the fix and the **grid** that generalizes it,
per your Q2. The exploit is trivial and repeatable, and §2.3 cites this cap *in place of* a rate
limit, so it has to actually bound.

### G6 — the sweep cannot see silently-wrong scenarios; the pass is run, not promised **[MAJOR]**

Accepted, and I ran the pass rather than recommending it: **five scenarios are mechanism-wrong,
four of which you did not report** (§2.7). The worst asserts that channel failure ends in
`abandoned`, which §2.2 changed to `missed` — a scenario that would have been implemented, passed,
and enshrined the wrong terminal state.

### G7–G11 — five sentences

G7 F5's reset is safe **only because** reinstate is `cancelled`-only; widening it requires
re-examining the crash budget (§2.2) · G8 the demotion kill-switch does demote `cancel_reminder`
to per-instance, which is correct for a kill switch — §1a's argument is against per-instance as
the *default*, not as an emergency posture · G9 durability depends on `synchronous=FULL` (§2.2) ·
G10 startup scans for terminal-but-unreported reminders (§2.5) · G11 the note references the
report rather than re-enumerating it, since the note's bound is 5 and a report may carry forty
(§2.5).

---

## 1d. Round-7 resolutions

### H3 — `next_attempt_at` is never initialized; the tick query cannot match null **[CRITICAL]**

Accepted, and **verified by execution** rather than reasoning, since it is the load-bearing fact
of the only critical and the reviewer flagged asserting it untested:

```
rows: (1,'pending',NULL), (2,'pending',100.0)
SELECT id WHERE status='pending' AND next_attempt_at <= 200  ->  [(2,)]
SELECT NULL <= 200                                           ->  None
```

Row 1 is unselectable. So a freshly scheduled reminder would sit `pending` forever after its owner
was told it was set — this capability's stated worst failure, on its primary path, and reinstate's
explicit "reset" made it reachable a second way. The diagnosis of *why* it survived seven rounds is
the part worth keeping: `next_attempt_at` was introduced as *"derived, not a third fact"*, and a
column framed as derived gets no creation-time write points. **Answering R7 Q3: yes — anything
introduced as "derived" is state until proven otherwise, and the proof is naming its write points
on every path into the state it governs.**

**Resolution [B/C]:** initialized to `due_at` on every path into `pending`; reinstate resets it
**to `due_at`**, never to null; a fourth row in the counter table with its write points; the fifth
column in §2.6's grid ("selectable by the tick query"), which is H3 arrived at from the invariant
side; scenarios for a fresh reminder and a reinstated one being selected.

### H1 — the table contained the flaw it was built to expose **[MAJOR]**

Accepted, and it is the sharpest process point of the round: a single-valued Transaction column for
counters whose write and clear live in *different* transactions, contradicting the surrounding prose
in three cells. **Answering R7 Q1: yes, a table needs the same treatment prose got** — every cell is
checked against the requirement text around it, and a cell that cannot hold the answer is itself the
finding. Split into **Write txn** / **Clear txn**; all three cells corrected; the grace budget added
as a comparison row, since it is a budget that is not a counter and fell outside "every counter".

### H2 — per-tick was the wrong granularity, and it was a widening **[MAJOR]**

Accepted. Per-tick bookkeeping holds two messages' writes until the last send returns, so a crash
after message 1 redelivers all 40 rather than 25. Now **two transactions per outbound message**,
which is the same rule at the right granularity and bounds redelivery to one batch. An appended
report shares its host message's pair; a standalone report has its own. Plus **H8**: strictly
sequential, never nested — SQLite has no nested transactions and the connection is shared.

### H6 — taken, not rejected: the report *is* a delivery **[MAJOR]**

**I disagree with the recommendation to reject this and am taking the restructure.** The reviewer
priced it as "four columns removed, at restructure risk". Tracing it shows it does more:

- **G1 becomes structurally impossible.** G1 existed because §2.5 had a clear-on-return and a
  *separate later* mark, creating a gap. Delivery has no such gap — H2/G4's post-send transaction
  clears the counter and writes the status **together**. Once the report writes `reported_at` in
  that same transaction, the gap it needed does not exist. That is strictly better than fixing G1
  with a special-case clear point, which the next reader can misread exactly as I did.
- **H5 dissolves** — no `next_report_at`, because the report uses `next_attempt_at`.
- **G10 dissolves** — no special startup scan, because the tick selector includes terminal-unreported
  rows every tick.
- **Four new columns become one** (`reported_at`).

So the change **deletes** four mechanisms and adds one query predicate plus one reset. Deletions are
the safest kind of restructure, and this one removes a contradiction rather than creating one: §2.3
already tells the reader a report is a delivery outcome, so parallel report machinery was the thing
that would have read as unexplained weight. **Answering R7 Q4: yes — the weight was telling me §2.3
was right and §2.5 had not caught up.**

Cost, stated honestly: the tick selector widens to
`status='pending' OR (status IN ('missed','abandoned') AND reported_at IS NULL)`, and the renderer
sections a mixed batch. One predicate, one renderer branch.

### H4 — the pass was correct in method and wrong in scope, for the second time **[MAJOR]**

Accepted. Re-run over the `find`-enumerated delta list: **eight mechanism-wrong scenarios across
three files**, up from five in one. The confirmed sixth is `audit-log:45-47` — `:123-124`'s error
verbatim — and I read it directly. **Answering R7 Q2: yes, scope is now mechanical. Both passes take
the same `find`-enumerated list; neither is allowed to run on a remembered set of files.** That is
the third time scope, not method, was the defect: round 4 (`design.md`), round 6 (three deltas),
round 7 (this).

### H5, H7–H9 and the carry-forward notes

H5 dissolved by H6 · **H7** cap check and reinstate transition in one transaction, since check-then-CAS
is safety-by-accident on the loop · **H8** in §2.2 · **H9** the v4 `description` *defines* `abandoned`
(crash budget) vs `missed` (time budget) rather than listing them, because the two were nearly
swapped in two files · **G7 extended** — N-d's `cancelled`-only restriction protects the crash budget
*and* the `reported_at` flag · **C4 scoped** to §2.6's seven transitions, `cancelled → pending` being
the only non-monotonic one · **m16 extended** — the audit write sits outside the post-send
transaction, so a committed `abandoned` with no receipt is reachable; m16 licenses that for
`scheduled`, where the owner is watching, and the scheduler-side transitions are where a missing
receipt matters *most*, so it says so explicitly rather than by inference.

---

## 1e. Round-8 resolutions

Every finding sits in the seam H6 opened. The reviewer withdrew their advice against H6 —
"your reasoning beat mine" — and J1 is its completion cost: one column against four removed.

### J1 — the report path inherited the counters but not their terminal actions **[CRITICAL]**

Accepted, and the trace is exact. §2.2's two give-ups are both **status transitions**
(`→ abandoned`, `→ missed`), and a row selected by the report branch has already reached them, so
both are no-ops. Worse, the terminal transition *resets* the counters, so the budget restarts once
and then climbs past a maximum §2.2 says SHALL NOT be exceeded, with nothing removing the row from
the selector. Meanwhile §2.5's cost paragraph still asserted the give-up. **The claim survived the
mechanism that produced it** — which is exactly the deletion failure now ruled for in §0.

**Resolution [C] — one column, not two, and it does double duty.** Neither the `reported_at`
sentinel nor a `report_failed` boolean, both of which were offered: the row records
**`terminal_at`** at the terminal transition, and it plays for the report precisely the role
`due_at` plays for a delivery. The report path then leaves the selector on **any** of three
conditions, mirroring delivery's structure exactly:

| Condition | Which budget | Outcome |
|---|---|---|
| `reported_at` written | — | success |
| `send_attempts` reaches its maximum | crash | leaves selector, error log |
| `now > terminal_at + late_grace` | time | *superseded by K3 — the report has no time budget; the gradient is inverted from a delivery's* |

On the sentinel option specifically: overloading a timestamp with a status meaning is the
compression the term-of-art rule exists to distrust, so it is rejected on that ground. And
`terminal_at` is preferable to `report_failed` because a boolean records only *that* it failed,
while `terminal_at` bounds the time budget **and** dates the transition — one column doing what
would otherwise take two. Net against round 6's four report columns: **minus two.**

### J2 — §2.2 contradicted itself two sentences apart **[MAJOR]**

Accepted, and it is J1's twin: the selector attempts a row the requirement says SHALL NOT be
attempted again. Now reads *"SHALL NOT be attempted again **as a delivery**; it is selected once
more to be reported, and leaves the selector when `reported_at` is written or the report's own
budgets expire"* — the sentence J1's fix makes true.

### J3 — the `pending → missed` transition had no transaction **[MAJOR]**

Accepted, and it is the third appearance of this class after F1 and G4. Grace expiry transitions a
row **with no send at all**, so "two transactions per outbound message" gave it nowhere to live.

**Resolution [C]:** a tick opens **one selection transaction before any send**, covering every row
whose grace window expired and performing the transitions those comparisons trigger. Rows it
transitions get `next_attempt_at = now`, so they are **reported in the same tick**, not the next —
which answers the "which tick reports them" half. The grace row's `n/a` cells are filled with the
selection transaction, and `n/a` is now a forbidden cell value (§0).

### J4 — `next_attempt_at` has three write sites and one Write-txn cell **[MAJOR]**

Accepted — H1's defect in the row added to satisfy H3, which is the sharper point: new rows arrived
*after* the split-column rule and were never audited by it. The cell now names all three sites
(creation, advance-on-failure, set-to-now at a terminal transition) with their three homes
(creating txn, post-send, selection-or-post-send), and the clear cell names the reinstate txn where
the reset actually lives. §0 gains "check every cell, **including the ones you just wrote**."

### J5 — the reset erases the evidence for its own decision **[MAJOR]**

Accepted; the sentence suffices, per your own lean. §2.2 now states that a terminal row carries no
record of its attempt history and that the history survives only in the audit log — which **raises
m16 from a note to a load-bearing dependency**, since a committed `missed` with a failed audit write
has no surviving explanation anywhere. That is the strongest possible argument for the m16 extension
already written, so it is stated where the reader meets the reset rather than three sections away.

### J6 — B must ship the complete final column set **[MAJOR]**

Accepted, and it is the finding that would have surfaced as a runtime `no such column` rather than a
review comment. `CREATE TABLE IF NOT EXISTS` with no migration path means a column added after the
table exists is never created — and `reported_at` and `terminal_at` were both invented inside **C**
requirements. Recorded in §5 with the standing question your Q4 asks for: *what does this new state
require of B?*, asked whenever C gains a column. Third consequence of the split, after F8 and B4.

### J7–J10 — four clauses

J7 the report is appended when it **fits**, not merely when a delivery shares the tick, and an
over-limit reminder's report form rides §2.1's send-alone clause · J8 `reported_at` is in the
post-send transaction's write list · J9 `reported_at` carries the same CAS discipline
(`WHERE id=? AND reported_at IS NULL`), so C4 covers eight mutations, not seven · J10 §2.3's
pending-cap-as-volume-backstop gains the clause that terminal-unreported rows generate messages
while counting toward no cap, bounded in practice by coalescing and pagination.

### The deletion rule, and §2.3's strengthening

§0 now carries the third rule (deletions are traced for what they leave asserting, names **and**
properties). And the note your Q5 asked for: §2.3 is **strengthened** by H6, not stalled — class
(b)'s three outcomes are now literally one mechanism, which is the coherence the clause was reaching
for. Said in §2.3's own text rather than left for a reader to notice.

---

## 1f. Round-9 resolutions

### The structural point, accepted — and acted on twice

The reviewer's observation is the most important thing in round 9, above any individual finding:
**three rounds, three unbounded loops, all in the report path** (G1's clear point, J1's missing
terminal actions, K2's evaluated exits). Each fix was sound and each contained one hole, and the
diagnosis is right: a state machine with budgets has a shape — states × selector × exits × write
sites — that paragraphs cannot hold, and the missing dimension differs every round. Counters were
tabulated in round 7 and **stopped producing findings**; states never were.

Two responses, because the diagnosis licenses both:

1. **§2.6 becomes the state-machine table** (K6): two columns added — *state written at this
   transition* and *txn* — and the report's two exits appear as rows. With empty cells forbidden,
   K2's defect could not have been written.
2. **The invariant goes behind a test, not a paragraph** (§2.8): *no row remains selected after N
   ticks under a deterministic fault.* Three rounds of prose produced three loops that one test run
   would have caught, and the reviewer's recommendation to stop specifying this mechanism in prose
   is correct. The test is the artifact that ends the class.

### K2 — two of three exits had no enforcement point **[CRITICAL]**

Accepted. The selector is a query, so it tests **written** state; a row that merely *satisfies* a
give-up condition still matches `reported_at IS NULL` and is selected forever. The prose said
"leaves the selector"; the predicate printed three bullets above said otherwise.

**Resolution [C] — write-form, per your lean.** Exactly two exits, both writing `reported_at` in
the post-send transaction: success (`report_failed` false) and the crash maximum (`report_failed`
true, error log, `report-abandoned` audit record). The selector tests one column.

**Answering R9 Q1 — why the check kept passing over this:** property-anchoring was framed for
*adjectives* (atomic, bounded, durable) and this claim is a **verb** — "leaves", "stops", "is
removed". So the rule gains its third form: **a claim that something *happens* must name the write
that makes it happen.** An evaluated condition is not an enforcement point when the enforcement
point is a query.

### K1 — the composition window was charged to no budget **[MAJOR]**

Accepted, and the structural half of your argument is what makes the fix obvious: per-message
increments are *impossible* before composition, because membership is not known until composition
ends — so the window was forced by the transaction layout, not overlooked. And it is exactly where
§2.5 says to expect the fault (a render error, an OOM on a large batch).

**Resolution [C]:** the tick's **pre-work** transaction increments `send_attempts` for every
selected row, before composition; each message keeps its own post-send transaction. H2's benefit
survives — only the unresolved message's members are redelivered. Net structure is now *simpler*
than round 8's: one pre-work transaction per tick, one post-send transaction per message.

### K3 — the report's time budget was calibrated against the opposite gradient **[MAJOR]**

Accepted, and the gradient argument is the part that decides it: delivery value **decreases** with
age (D7's own reasoning), report value **increases** with age. Reusing `late_grace` discarded the
report at up to `T + 48h`, leaving a reminder undelivered *and* unmentioned — and contradicted
§2.5's live promise that an unavailable channel makes the report wait.

**Resolution [C]: deleted.** The report keeps only the crash budget. A deletion, restoring a
promise the requirement already made. Your steelman is recorded as the accepted cost: a
terminal-unreported row sits on the 4h backoff tail against a permanently dead channel, costing one
query row and one failing send per 4h — against an agent that channel has already disabled.

This is also the mirror of the deletion rule: §0 covers *a claim surviving a deletion*; K3 was *a
claim contradicted by an addition*. Both are now covered by one sentence in §0.

### K4, K5, K6 — enum, write sites, columns

K4 `report-abandoned` in v4's enum, in B, with H9's three-way defining description; the standing
question generalizes from columns to **anything** (R9 Q3) · K5 `terminal_at`'s two write sites
enumerated with their two homes · K6 above.

### And one found by rebuilding the grid

The fifth column H3 added in round 7 **never landed on the reinstate row** — the single row H3 was
about. A ragged table row is invisible to a claim-grep and to a cell-audit that reads cells rather
than counting them, so §0's table rule gains: **after editing a table, verify every row has every
column.** Repaired, and the row now also shows `checked (G3)` where it had still read `UNCHECKED`.

**Answering R9 Q4 — the lesson about elegance:** `terminal_at` consolidated two jobs (bounding a
budget, dating a transition) and thereby consolidated two failure modes, one of which (K3) neither
rejected alternative could have had. So: **a field serving two purposes must be checked against
both purposes' failure modes independently** — consolidation is a real gain, and it is never free.

---

## 1g. Round-10 resolutions

### The recommendation, taken literally — and it paid immediately

The reviewer's round-9 recommendation was to put the invariant behind a test; their round-10
correction was sharper: *"I should have said 'write the test,' not 'put the invariant behind a
test.'"* That distinction is the round. §2.8's prose-specified test produced two findings in the
round that introduced it (L1, L2), which is prose failing in the way prose fails.

So the test was **written and run** (§2.9) — as a scratchpad model of the specified machine, not as
project source. It found **two defects neither ten rounds of review nor my own checks had caught**,
one of them the same class as the previous four criticals:

- **M1:** the crash-maximum's *evaluation* was specified in the post-send transaction while K1 had
  moved its *increment* to pre-work — and a crash is what prevents post-send from running, so the
  bound was never evaluated on the path it exists to bound. `send_attempts` reached 84 against a
  maximum of 3. Fixed by evaluating the maximum beside the increment.
- **M2:** a fault *before* the pre-work commit cannot be bounded by any counter, at any placement,
  because nothing persists. §2.8's termination claim is now scoped accordingly and the pre-commit
  region argued small rather than asserted bounded.

**Answering R10 Q1 and Q4 together, and they have one answer.** Q1 asks whether the test can be
specified or only written; Q4 asks whether four consecutive criticals inside the previous critical's
fix is convergence with a tail or a medium failure. §2.9 answers both: the medium was the problem,
the failures stop when the artifact becomes executable, and M1 is the fifth instance of the class
caught in one run rather than one round. The plan's remaining risk is not analytical.

### L1 — the invariant contradicted K3 **[CRITICAL]**

Accepted. §2.8 asserted termination under "a send that never confirms" while K3 deliberately makes
that case wait indefinitely — a crash budget cleared on every return can never expire there. Split
into **termination** under crash faults and **quiescence** under channel faults: the row stays
selected, frequency decays to the tail, and no owner-visible message is produced. §2.9 confirms all
three of those numbers empirically.

### L2 — termination is liveness and admits the worst bug **[MAJOR]**

Accepted; a post-send transaction that writes `reported_at` unconditionally empties the selector on
tick one and tells the owner nothing. The **conservation** assertion is now the primary property:
the success-terminal set equals exactly the acknowledged-message set, and every non-success exit
carries its marker, log and receipt. §2.9 asserts it and it passes.

### L3 — a failed message must not abort the tick **[MAJOR]**

Accepted, and the mechanism is exactly as traced: rows in later batches keep their pre-work attempt,
never receive an outcome, and reach the crash maximum **having never been sent**. One clause in
§2.2, and the fault is now a stage boundary in §2.9's matrix.

### L4 — the schedule index was unbounded into a seven-element list **[MAJOR]**

Accepted: "then 4h intervals" reads as complete to a human and is an `IndexError` to a list, and it
would raise in exactly the window K1 established as fault-becomes-abandonment. The clamp is stated,
and §2.9's quiescence run exercises it — `index_clamped_ok=True` at `unconfirmed_sends` well past
the schedule length.

### L5–L8 — four clauses

L5 the exit rows' cap cells say *"no effect — capacity was released at the terminal transition"*
rather than shrugging at an adjacent question, and the detached fragment is noted for merging into
the grid · L6 `N` derived from `max_attempts` × schedule length, not chosen (§2.9 computes it: 21) ·
L7 `report-abandoned` named in §5's audit sites and the `initiated_by` mapping, and m16's worst case
stated where m16 is · L8 §1c's superseded transaction structure annotated as §1c's G1 entry already
was.

**Answering R10 Q2** — five transaction findings, all "what happens *between* the steps you named"
(F1, G4, J3, K1, L3). The form that enumerates between-steps is not another table: it is §2.9's
fault-injection matrix, where every stage boundary is a test case rather than a paragraph. That is
why the matrix, not the prose, is what change C carries forward.

**Answering R10 Q3** — the audit for prose-completed schedules and bounds found one more beyond L4:
`recall_render_limit`-style caps are all numeric config with explicit comparisons, but the backoff
schedule was the only *sequence* described in prose. It is now the only one, and it is clamped.

---

## 1h. Round-11 resolutions — the final pass

### N1 — the fix was in the model and not in the spec **[CRITICAL]**

Accepted, and it is the round's real finding: **the model had become more correct than the
specification, and the specification is what gets built.** M1's diagnosis was right and its fix
landed in code only; five authoritative sites still homed the crash maximum in the post-send
transaction — the placement M1 proved is never reached on a crash. All five corrected: `terminal_at`
(both write sites now pre-work), `reported_at`/`report_failed` (success post-send, crash maximum
pre-work), §2.2's post-send bullet (no longer lists the crash-maximum transitions), and §2.6's
`abandoned` and *report abandoned* rows.

**New rule, and it is the first propagation failure in eleven rounds — it appeared the moment a
second artifact existed:** *the model is a finding-generator, not a fix location. A defect it finds
is unfixed until the requirement text changes, and the two artifacts are checked against each other
in both directions every round.*

### N2 — the test oracle took its truth from the code under test **[MAJOR]**

Accepted, and this is the sharpest of the round because it was found by injection rather than
reading. Conservation caught the bug L2 named (an unconditional success write) but was blind to a
more likely one: the outcome variable lying, so that the log and the write share the same falsehood.
**That is C1's entire subject matter** — D1 established the adapter cannot distinguish a lost
response from an unsent request. Fixed by recording the acknowledged set **at the channel double,
outside the code under test**, and verified both ways:

```
oracle check, Bug B (outcome lies): {'success_equals_acked': False, ...}   # now caught
```

General rule, answering R11 Q3: **an assertion's ground truth must come from outside the thing
asserted.** It is also what makes the assertion portable — production code has no `log` parameter,
so the real test must take its truth from the double regardless.

### N3 — the grace path had never executed **[MAJOR]**

Accepted: `grace_transitions` was **0** across all three properties, so J3's entire reason for the
pre-work transaction was unexercised and M1 was found through the `abandoned` branch rather than the
`missed` branch it was written for. `LATE_GRACE` shortened to 300s and rows seeded in the deep past;
the count is now **40**, and a grace-transition crash stage was added.

**And running it produced a further correction that answers R11 Q2 directly** — how much apparent
coverage was luck. A crash at the grace transition does *not* terminate, and filing it under
termination asserted the wrong property: the grace transition happens **inside** the pre-work
transaction, so a fault there belongs to M2's pre-commit region, not to termination. Moved, and both
pre-commit stages now assert detectability instead.

### N4 — `partial` did not exist in the model **[MAJOR]**

Accepted, and it mattered more than coverage: `partial` is the one outcome where "acknowledged" is
genuinely ambiguous, which is what conservation asserts over. The channel is now tri-valued and a
`partial` double asserts §2.1's and E9's rule — verified: `none_delivered: True`,
`all_backed_off: True`.

### N5 — right call, wrong argument **[MAJOR]**

Accepted. M2's scoping was justified by region size; M13 gives a far stronger justification. Because
the scheduler loop is exception-proof per tick, a pre-commit fault does not crash-loop the container
— it is caught, logged, and retried, so the failure is **unbounded but loud**: one error log per
tick, forever, in a live process, with no owner-visible message. §2.9 asserts both numbers. A
probabilistic argument about code size became an operator-actionable detection guarantee, and the
circuit-breaker answer to "is there any placement" is named.

### N6, N7 — model hygiene

Identity-based exclusion instead of dataclass field equality; `N` derived as
`2 × (max_attempts + 1) + 2 = 10` from the two sequential budgets rather than mixing the crash budget
with the schedule length; §2.9 now states that the model is disposable, that the **matrix** is what C
carries, and enumerates its known non-coverage.

### Final run

```
N_TERMINATION (derived) = 10
TERMINATION    {'compose': 'terminates', 'send0': 'terminates', 'post0': 'terminates'}
DETECTABILITY  {'pre-work': {'logs_per_tick': 1.0, 'owner_visible': 0, 'still_selected': True},
                'grace':    {'logs_per_tick': 1.0, 'owner_visible': 0, 'still_selected': True}}
QUIESCENCE     {'still_selected': True, 'interval_at_tail': True, 'index_clamped': True,
                'owner_visible': 0, 'attempts': 11}
CONSERVATION   {'success_equals_acked': True, 'unmarked_failures': [], 'max_never_exceeded': True,
                'grace_transitions': 40}
  oracle check, Bug B (outcome lies): {'success_equals_acked': False, ...}
PARTIAL        {'none_delivered': True, 'all_backed_off': True}
```

---

## 2. Requirements in full text

### 2.1 [C] Requirement: Due reminders are delivered in chunk-atomic batches

> The scheduler SHALL deliver reminders whose due time has passed, oldest-due first, by
> sending their **stored text unchanged** through the channel adapter's proactive
> owner-directed send. Delivery SHALL create no agent session, run no model turn, and
> consume no tokens. Delivery SHALL be independent of the serial turn queue and SHALL NOT
> be suppressed by a pending approval. The message SHALL carry a fixed marker
> distinguishing a reminder from a triage message.
>
> Reminders delivered in the same tick SHALL be packed into batches by a greedy
> **measure-before-add** rule: the next reminder is added to the current batch only if the
> **actual rendered batch, in its actual framing**, would still fit within one channel
> chunk; otherwise the current batch is closed and a new one begun. Each batch SHALL be
> sent as one message. Separately, **configuration load SHALL validate** that a
> maximum-length reminder rendered under the **largest** framing in use (on-time, late with
> original due times, or carrying a report section) still fits one chunk.
>
> A reminder whose own rendered form exceeds one chunk — possible for a row stored while a
> higher text limit was configured — SHALL be sent **alone**; that message MAY be split by
> the channel, and a `partial` outcome on it SHALL be handled as `failed` (left pending,
> redelivered).
>
> Because a single-chunk message cannot be split, a batch's outcome SHALL be `delivered` or
> `failed`; a `partial` outcome on a batch SHALL nonetheless be handled as `failed` for the
> whole batch. **A `failed` outcome means delivery was not confirmed — not that nothing
> arrived**: the adapter retries internally and cannot distinguish a lost response from an
> unsent request. Delivery is neither idempotent nor guaranteed: a confirmed delivery may
> have arrived more than once; an unconfirmed delivery may have arrived zero times.
> Duplicate delivery is accepted; non-delivery terminates as `missed` and is reported,
> never silent.
>
> A batch's per-reminder status transitions SHALL be written in **one transaction**, so a
> batch's attribution is all-or-nothing. (That guarantee rests on change B's store hardening
> — single-connection discipline and the missing `delete_containing` rollback — which is a
> cross-change dependency: C's transaction correctness requires B, and the split is exactly
> what makes an undeclared dependency dangerous.)
>
> **A batch is not a durable object.** Its members can carry different `unconfirmed_sends`
> values (a first attempt and a fifth batched together because both had `next_attempt_at <=
> now`), and after a failure their `next_attempt_at` values diverge, so they will not be
> batched together again. The only invariant across a failure is that every member stayed
> `pending` — never that "the batch" is redelivered.

Scenarios: 25 due → one message, one chunk, 25 transitions in one transaction · 40 due →
two messages, neither split · a `failed` batch leaves every member `pending` · straddle: a
reminder fitting at *k* but not *k+1* opens the next batch · an over-limit stored row is
sent alone, and a `partial` on it leaves it pending · a reminder due mid-turn is delivered
without waiting · config load rejects a text limit that cannot fit the largest framing.

### 2.2 [C] Requirement: Crash redelivery and unconfirmed sends are bounded separately

> Two failure budgets SHALL be carried by **two pieces of durable state**, because one
> counter cannot distinguish a crash from a returned failure.
>
> `send_attempts` SHALL be incremented before each send and **cleared when the send returns
> at all**, whatever its outcome. It therefore accumulates only across process death.
> **The maximum SHALL be evaluated in the pre-work transaction, immediately after the increment
> that could reach it** — not in the post-send transaction. A crash is precisely what prevents the
> post-send transaction from running, so a maximum evaluated there is never evaluated on the path
> it exists to bound: the counter climbs indefinitely and the row is selected forever. Verified by
> execution (§2.9): with the check in post-send, `send_attempts` reached **84** against a maximum
> of 3. Reaching the maximum SHALL move the reminder to `abandoned` (or, for a terminal-unreported
> row, write `reported_at` with `report_failed`), in that same pre-work transaction, and the row
> SHALL be excluded from the tick's composition. `abandoned` SHALL NOT be attempted again **as a
> delivery**; it is selected once more to be *reported*
> (§2.5), and leaves the selector when `reported_at` is written or the report's own budgets
> expire.
>
> `unconfirmed_sends` SHALL be incremented when a send returns `failed`, and SHALL drive a
> **backoff schedule** — 30s, 1m, 2m, 5m, 15m, 1h, 4h — recorded as `next_attempt_at`. **The final
> interval repeats indefinitely and the index SHALL be clamped to the schedule's last element**: the
> counter is unbounded by design after K3 (roughly 2,190 increments per year of dead channel), so an
> unclamped index into a seven-element list would raise inside the post-send transaction or during
> composition — which K1 has just established as the window where a deterministic fault becomes an
> abandonment. "Then 4h intervals" reads as complete to a human and is an `IndexError` to a list. Retries SHALL continue on that schedule until the late-grace window
> expires, at which point the reminder SHALL move to `missed`. The schedule, rather than the
> poll interval, is required: a channel that accepts messages but never confirms them would
> otherwise be retried once per tick, yielding 2,880 duplicate deliveries at a 30s tick
> across a 24h window. The schedule yields roughly a dozen.
>
> **Every counter in this capability SHALL be specified by the window it covers and the
> transaction its writes belong to** — a counter whose clear point sits outside the window it
> is meant to bound records nothing about that window:
>
> | State | Written | Window it covers | Cleared / reset | Write txn | Clear txn |
> |---|---|---|---|---|---|
> | `send_attempts` | in the tick's **pre-work** txn, for every selected row | composition, the send itself, **and** the gap to its bookkeeping | on any return | pre-work | post-send |
> | `unconfirmed_sends` | on a `failed` return | (schedule position, not a window) | on reinstate; on a `missed`/`abandoned` transition | post-send | reinstate / post-send |
> | `next_attempt_at` | three sites: **initialized to `due_at` on every path into `pending`** (`remind`, `/remind`, `reschedule_reminder` → new `due_at`, `reinstate_reminder` → `due_at`); **advanced** on each `failed` return; **set to now** at a terminal transition | (the tick selector — never null while selectable) | reset **to `due_at`**, never to null, on reinstate | creating txn · **post-send** (advance) · selection or post-send (terminal) | **reinstate txn** |
> | `terminal_at` | two sites: grace expiry → `missed`; delivery crash maximum → `abandoned` — **both in the pre-work txn** (N1) | anchors the report's time budget as `due_at` anchors a delivery's | never cleared — immutable once written | the txn performing the transition | none exists, by design |
> | grace budget (`due_at + late_grace` vs `now`) | a **comparison**, not a counter | the whole overdue lifetime | nothing to clear — it is derived from two stored values | **the selection txn** performs the `pending → missed` transition it triggers | none needed |
> | `reported_at` + `report_failed` | two homes: **success → post-send txn**; **crash maximum → pre-work txn** (N1) | the report's only exit — what the selector tests | never cleared — terminal | post-send (success) · pre-work (crash max) | none exists, by design |
>
> `next_attempt_at` is the tick query's selector, so a null value makes a row permanently
> unselectable: in SQL `NULL <= now` is not true, and a freshly scheduled reminder would sit
> `pending` forever while its owner had been told it was set — this capability's stated worst
> failure, on its primary path. It is therefore **state with explicit write points on every
> path into `pending`**, not a value derived from failures.
>
> **A tick SHALL open one pre-work transaction, then one post-send transaction per outbound
> message.** The pre-work transaction runs **before composition and before any send**, and does
> two things for every selected row: it performs the `pending → missed` transition that grace
> expiry triggers — a transition occurring with **no send at all**, which therefore has no
> send-shaped transaction to live in — and it **increments `send_attempts` for every selected
> row**. Rows it transitions become terminal-unreported with `next_attempt_at` at now, so they
> are reported **in the same tick**, not the next one.
>
> The increment SHALL happen here rather than per message, because **message membership is not
> known until composition finishes**, so a per-message increment leaves the composition window —
> exactly where §2.5 says to expect a render fault or an OOM on a large batch — charged to no
> budget at all, and unbounded under `restart: unless-stopped`. Counting an attempt for a row
> destined for a message that a crash prevented composing is the correct pessimism, on the same
> reasoning that requires counting a crash mid-send: the two are indistinguishable. **H2's
> benefit survives**: a crash after message 1's post-send transaction leaves message 1 resolved
> and only message 2's members carrying an attempt with no outcome, so only message 2 is
> redelivered.
>
> **A `failed` outcome on one message SHALL NOT abort the tick**: every message composed in a tick
> is attempted and receives its own post-send transaction. Otherwise rows in later batches keep the
> attempt the pre-work transaction charged them, never receive an outcome, and reach the crash
> maximum **having never been sent** — the exact outcome the pre-work transaction exists to prevent,
> arriving through message ordering instead of partial increments.
>
> **Each outbound message SHALL then make its own post-send transaction**, covering every member
> of that message — **per message, not per tick**: a tick delivering 40 reminders sends two
> messages, and per-tick bookkeeping would hold both messages' writes until the last send
> completed, so a crash after the first message would redeliver both. Per-message bookkeeping
> bounds redelivery to one batch. A report appended to a delivery message shares that message's
> two transactions; a report sent alone has its own pair. Transactions SHALL be strictly
> sequential and never nested — SQLite has no nested transactions and the store shares one
> connection (M11), so opening a second while one is open would error or implicitly commit.
>
> - the **post-send** transaction, covering **all** post-send writes together: clearing
>   `send_attempts`, incrementing `unconfirmed_sends`, setting `next_attempt_at`, **and any
>   status transition those writes trigger** (`delivered`, `delivered-late`, `abandoned` at
>   the crash maximum, `missed` at grace expiry). Status and bookkeeping SHALL NOT be two
>   transactions: a crash between them could leave `send_attempts` at its maximum with the
>   row still `pending`, and the next start would increment past a maximum this requirement
>   says SHALL NOT be exceeded.
>
> Two counters can distinguish a crash from a returned failure only if they are written
> together: a crash between clearing the first and incrementing the second would leave both
> budgets recording nothing and the reminder due on every tick, forever.
>
> The terminal transition's reset zeroes `send_attempts` and `unconfirmed_sends`, so **a
> terminal row carries no record of how many attempts preceded it**: that history survives only
> in the audit log's `reminder` records. This raises m16's non-blocking-audit licence from a note
> to a load-bearing dependency — a committed `missed` whose audit write failed has no surviving
> explanation anywhere, which is precisely why the scheduler-side transitions are the ones whose
> receipts matter most.
>
> `reported_at` is a row mutation like any other and SHALL be written with the same CAS
> discipline (`WHERE id=? AND reported_at IS NULL`), and it belongs to the post-send
> transaction's write list alongside the status transitions.
>
> These guarantees hold against SIGKILL and against power loss only while the store keeps
> `synchronous=FULL` (M11); relaxing it to `NORMAL` for write throughput would make a
> committed transaction losable and degrade this requirement silently.
>
> A **reinstate** SHALL reset `send_attempts`, `unconfirmed_sends` and `next_attempt_at`: it
> is a fresh owner intent, and a reminder deliberately restored must not resume at backoff
> position 4 or be abandoned by crashes charged to its previous life. **That reset is safe
> only because reinstate is `cancelled`-only (N-d):** an `abandoned` reminder cannot be
> reinstated, so a deterministic render fault cannot escape the crash budget by being
> restored. Widening reinstate to reach `abandoned` — an entirely natural future request —
> SHALL require re-examining the crash budget.
>
> Because `send_attempts` is cleared on any return, a backoff retry SHALL NOT consume the
> crash budget — the two paths diverge at the first returned outcome:
>
> | | attempt 1 | returns `failed` | attempt 2 (30s) | crashes mid-send | restart |
> |---|---|---|---|---|---|
> | `send_attempts` | 1 | **0** | 1 | 1 | 2 |
> | `unconfirmed_sends` | 0 | 1 | 1 | 1 | 1 |
>
> A failure notice SHALL NOT be fired into an unavailable channel: it rides the next
> successful contact. `abandoned` and `missed` reminders SHALL surface through the report
> requirement, not as their own message class.

Scenarios: a 5-minute channel outage delivers rather than abandoning · a bridge that
accepts but never confirms produces roughly a dozen duplicates over 24h, not one per tick ·
three crashes mid-send → `abandoned` · a returned failure followed by a crash leaves
`send_attempts` at 1, not 2 · a notice raised while the channel is down is delivered on the
next successful contact.

### 2.3 [C] Requirement: Cadence is condition-triggered with a hard cap on announcements

> Unprompted Signal messages carrying Henk-authored content SHALL be sent only in the
> following classes, and no others:
>
> - **(a)** an announceable incident;
> - **(b)** the outcome of a **delivery attempt on a specific owner-created reminder row**
>   reaching a terminal result — the reminder delivered, delivered late, or a bounded report
>   that it could not be delivered. A row is owner-created when it originates from an owner
>   command or from a gate-authorized `remind` / `reschedule_reminder` /
>   `reinstate_reminder` invocation in an untainted owner turn.
>
> **No unprompted message whose trigger is the passage of time rather than a delivery
> attempt on an owner-created schedule entry or an announceable incident SHALL exist** —
> digests, heartbeats, status pings, periodic summaries, nags and "all is well" messages
> included. A periodic report *about* reminders is not class (b): class (b) requires a
> delivery attempt on a specific row. The channel adapter SHALL NOT emit its own
> owner-facing notice on a proactive send failure; the caller owns failure messaging
> (channel-adapter spec). Approval prompts are not content messages and are governed by the
> approval-gate spec.
>
> **This requirement governs one-shot reminder rows.** A recurring schedule is a
> clock-triggered generator of class-(b) messages — it would satisfy class (b) at every
> firing while being a heartbeat by any ordinary reading — and SHALL NOT be introduced
> without its own amendment to this requirement. Recurrence therefore argues its cadence
> rather than inheriting permission from this clause.
>
> Reminder deliveries SHALL NOT consume the announceable-incident cap; their volume is
> bounded by the pending cap and the owner's own scheduling, not by a rate limit — a rate
> limit would drop something the owner explicitly asked for.
>
> Announceable incidents SHALL be limited by a configured hard cap per 24 hours;
> triageable incidents beyond the cap are suppressed from Signal only (their triage session,
> audit record, and handoff still occur), and the next announceable message SHALL note how
> many incidents were suppressed. Mutating invocations are the one exception to "Signal
> only": during a suppressed triage they fail closed silently per the approval-gate spec — a
> suppressed incident can never place an approval prompt (a context-free owner interruption)
> on the channel. The cap bounds unprompted-message volume, not token spend — token spend is
> bounded upstream by the curated source list, debounce, and cooldown. **The cadence cap
> window SHALL survive a process restart**: on startup the count of announceable incidents
> within the current cap window SHALL be reconstructed from the persisted audit log, so a
> restart does not reset the cap and allow the owner's cadence constraint to be exceeded.

Scenarios: quiet homelab → zero messages · **no system-scheduled message exists**: a week
of runtime with nothing scheduled and no events produces zero outbound messages · a
periodic report about reminders is not permitted by class (b) · reminder delivery does not
consume the incident cap · a failed proactive send produces no adapter-authored message ·
suppressed count surfaces later · cap holds across a restart · suppressed triage cannot
prompt.

Human read-through checklist item (not a suite test): read cold, this clause must forbid a
weekly summary of the owner's todos **and** a weekly summary of the owner's reminders.

### 2.4 [B] Requirement: Mutation receipts are durable at decision time (MODIFIED)

> Every mutating-tool authorization decision SHALL be appended to the audit log as its own
> `authorization` record at the time of the decision — without waiting for turn or session
> completion, and without depending on graceful session close or graceful shutdown.
> Model-initiated records carry `initiated_by: "model"`, the tool name, its authorization
> tier, the outcome, the one-time reference, the turn type, and a timestamp. Every mutating
> owner command (`/remember`, `/forget`, `/capture`, `/inbox done <id>`, `/remind`,
> `/reminders cancel <id>`, `/reminders reinstate <id>`) that changes state SHALL likewise
> write an `authorization` record at execution time with `initiated_by: "owner-command"`,
> `tier: null`, `turn_type: "command"`, outcome `authorized`, naming the command and a
> bounded summary of its effect. Read-only commands (`/memories`, `/inbox`, `/inbox all`,
> `/reminders`) and mutating commands that changed nothing (an unmatched `/forget`, an
> unknown `/inbox done`, `/reminders cancel` or `/reminders reinstate` id) SHALL NOT write
> one — receipts record mutations, and none occurred.
>
> **Reminder actions produce two records, deliberately, because they answer different
> questions:** the `authorization` record is the permission axis (was this allowed, under
> what tier), and the `reminder` record is the lifecycle axis (what happened to reminder
> #12). Both are required; neither substitutes for the other. A schedule rejected by
> validation leaves an `authorization` record with outcome `authorized` and no lifecycle
> record, matching how a failed `capture` already behaves — the gate authorizes before the
> tool runs.

Scenarios: existing four retained · `/reminders reinstate` writes an owner-command receipt ·
a `remind` tool call produces both an `authorization` and a `reminder` record · a
model-initiated `cancel_reminder` produces both, with `initiated_by: "model"` · an unknown
`/reminders cancel` id writes neither.

### 2.5 [C] Requirement: Undelivered reminders are reported, bounded, and not silently dropped

> A reminder that reaches `missed` or `abandoned` SHALL be reported to the owner as the
> terminal outcome of its own delivery attempt (class (b) of the cadence requirement), and
> SHALL NOT be dropped without a report or reported on a timer. Where the channel is
> unavailable the report waits for the next successful contact rather than being discarded,
> and the reminder remains visible through `reminders_read` in the interim — so an
> undelivered report is a delayed one, not a lost one. It is not claimed that the owner
> always receives it: a channel that never recovers delivers nothing, which is the same
> limit every other message class carries.
>
> The report SHALL name each reminder's original due time and text, SHALL be bounded in item
> count, and SHALL state the remainder as "and N more" when items are omitted — a report may
> paginate, whereas a delivery may not. When a delivery message is being sent in the same
> tick, the report SHALL be **appended to it** rather than sent separately; otherwise it is
> sent alone. It SHALL be batched chunk-atomically exactly as a delivery is.
>
> A reminder SHALL be reported **once per successful send-and-mark cycle**: the report is
> sent first and the reminders are marked reported second, so a crash between the two repeats
> the report rather than losing it. Duplicate reporting is accepted; an unreported terminal
> reminder is not.
>
> **A report is a send of those reminders, not a separate message class** — exactly what the
> cadence requirement already calls it: the terminal outcome of a delivery attempt on an
> owner-created row. It therefore reuses the delivery machinery of §2.1 and §2.2 unchanged, with
> no state of its own beyond a `reported_at` marker:
>
> - the tick's selector includes terminal-unreported rows —
>   `status = 'pending' OR (status IN ('missed','abandoned') AND reported_at IS NULL)`, with
>   `next_attempt_at <= now` — so no separate startup scan is needed to find a reminder that
>   went terminal and crashed before its report;
> - the `missed` / `abandoned` transition resets `send_attempts` and `unconfirmed_sends`, sets
>   `next_attempt_at` to now, and records **`terminal_at`**, inside the transaction that performs
>   it — giving the report its own budget under the same two-budget rule, with `terminal_at`
>   playing the role `due_at` plays for a delivery;
> - **the report path SHALL have its own terminal actions**, because §2.2's are status
>   transitions and this row has already reached them: `→ abandoned` and `→ missed` are no-ops
>   for a row that is already `abandoned` or `missed`, so inheriting the counters without
>   inheriting terminal actions would leave the report loop unbounded. **Both exits SHALL be
>   writes, not evaluated conditions**, because the selector is a query and a query tests
>   written state only: a row that merely *satisfies* a give-up condition still matches the
>   predicate and is selected again forever. There are exactly two exits, and each writes
>   `reported_at` in the post-send transaction — success (`report_failed` false) and the crash
>   maximum (`report_failed` true, an error log, and a `report-abandoned` audit record). The
>   selector therefore tests one column, `reported_at IS NULL`;
> - **the report has no time budget.** `late_grace` is calibrated for deliveries, where value
>   *decreases* with age — D7's reasoning is explicit that a day-old instruction delivered as
>   current is worse than useless. A report's value *increases* with age: the longer the owner
>   has relied on a reminder that never came, the more they need to know it did not. Reusing the
>   delivery window would discard the report at `terminal_at + late_grace` — up to 48h after the
>   original due time — leaving a reminder undelivered *and* unmentioned, and contradicting this
>   requirement's own promise that an unavailable channel makes the report wait. A
>   terminal-unreported row therefore stays selectable on the 4h backoff tail until it is
>   reported or the crash budget stops it; the cost is one query row and one failing send per 4h
>   against a channel whose permanent death has already disabled the whole agent;
> - `reported_at` is written **in the same post-send transaction that clears `send_attempts`**,
>   exactly as a delivery's status transition is. There is therefore **no send-to-mark gap for a
>   crash to fall into**: a crash before that transaction leaves `send_attempts` set and the
>   crash budget catches it; a crash after it leaves consistent state. The gap is closed
>   structurally rather than by a special clear point.
>
> Bounding, backoff, retry-until-grace, chunk-atomic batching and crash accounting are thus the
> ones already specified, not parallel copies of them.
>
> **What that costs, stated rather than hidden:** a terminal reminder can end up unreported when
> the report path is deterministically broken (a render fault, an OOM on a large batch) — it
> leaves the selector at the crash maximum, with an error log and a `report-abandoned` audit
> record. It remains discoverable through `reminders_read` and in the audit log,
> so it is not lost — but "discoverable if you look" is weaker than this capability's standard
> elsewhere, and the trade is taken because the alternative is a report loop that no condition
> ever ends.

No separate startup scan is required: because the tick selector includes terminal-unreported
rows, a reminder that went terminal and crashed before its report is picked up by the next
ordinary tick. The delivered-reminder note
(M12) SHALL **reference** the report rather than re-enumerating it: the note's item bound is 5
(m7) and a report may carry forty.

Scenarios: a mid-run `missed` reminder is reported when it happens, not at the next restart ·
a reminder that reached `missed` and crashed before its report is reported after restart ·
a report send that fails waits on the backoff schedule rather than giving up at three ticks ·
a crash between a report's send and its bookkeeping leaves `send_attempts` set, so the crash
budget bounds the repeat ·
a restart with both late and beyond-grace reminders produces delivery messages with the
report appended to the last · 40 missed reminders produce a bounded report with an "and N
more" remainder · a crash between a report's send and its post-send transaction leaves `send_attempts` set, so the crash budget bounds the repeat ·
a reported reminder is not reported again.

---

### 2.6 [B] The state × invariant grid — every transition checked against every invariant

Executed per R6 Q2. Rows are transitions **into** a status; columns are the invariants that
status carries. This grid is what found G3, and it is the artifact that keeps a future verb from
repeating it.

| Into | Via | Pending cap | Text limit | Horizon / not-past | Full validation | **Selectable by the tick query** | **State written at this transition** | **Txn** |
|---|---|---|---|---|---|---|---|---|
| `pending` | `remind`, `/remind` | checked ✓ | checked ✓ | checked ✓ | ✓ | `next_attempt_at = due_at` ✓ (H3) | `due_at`, `original_due_at`, `next_attempt_at`, `source` | creating txn |
| `pending` | `reschedule_reminder` | not a new occupancy — the row is already `pending` | text unchanged | **re-checked (N6)** ✓ | ✓ | `next_attempt_at = ` new `due_at` ✓ | `due_at`, `next_attempt_at`, `reschedule_count` | command/tool txn |
| `pending` | **`reinstate_reminder`, `/reminders reinstate`** | **checked (G3)** ✓ | text unchanged; a limit *lowered* since scheduling is carried by §2.1's send-alone clause ✓ | validated at original schedule; the horizon only shrinks toward now ✓ | not re-run — a now-past due time is carried by the delivery path's grace check (D7), late or `missed` ✓ | **reset to `due_at`, never null** ✓ (H3) | `status`, `send_attempts`=0, `unconfirmed_sends`=0, `next_attempt_at`=`due_at` | reinstate txn, with the cap check (H7) |
| `cancelled` | `cancel_reminder`, `/reminders cancel` | releases capacity | — | — | — | not selected: `cancelled` is never reported | `status` | command/tool txn |
| `delivered` / `delivered-late` | scheduler | releases capacity | — | — | — | not selected: the delivery *was* the message | `status`, `delivered_at`, counters cleared | post-send |
| `missed` | grace expiry (delivery path, D7) | releases capacity | — | — | — | **selected while `reported_at` is null** ✓ (H6) | `status`, `terminal_at`, counters reset, `next_attempt_at`=now | **pre-work** (K1/J3) |
| `abandoned` | delivery crash maximum | releases capacity | — | — | — | **selected while `reported_at` is null** ✓ (H6) | `status`, `terminal_at`, counters reset, `next_attempt_at`=now | **pre-work** (N1) |

**G3, resolved [B]:** reinstate is a **new path into `pending` that no cap check covered**, and
the exploit is trivial and repeatable — at 100 pending, cancel one (99), schedule a new one
(100), reinstate the cancelled one (**101**). §2.3 cites the pending cap as the backstop on
unprompted-message volume *in place of a rate limit*, so an unbounded cap is not a cosmetic
defect. **Reinstate SHALL be subject to the pending cap and SHALL fail honestly when it would
exceed it** (*"you're at 100 pending — cancel something first"*), changing nothing.

| *reported* — row leaves the selector | report send succeeds | no effect — capacity was released at the terminal transition | — | — | — | no longer selected: `reported_at` non-null | `reported_at`, `report_failed`=false | post-send |
| *report abandoned* — row leaves the selector | report crash maximum | no effect — capacity was released at the terminal transition | — | — | — | no longer selected: `reported_at` non-null | `reported_at`, `report_failed`=true, error log, `report-abandoned` audit record | **pre-work** (N1) |

The last two rows exist **because the write-site column was added**: they are the exits K2 found
specified as evaluated conditions rather than writes, and an empty write cell — a forbidden value —
is what makes that visible. This is the state-machine table the mechanism has needed since round 6.
Counters were tabulated in round 7 and stopped producing findings; states were not, and produced one
critical per round until now.

**Also caught by rebuilding this grid:** the fifth column ("selectable by the tick query") never
landed on the **reinstate** row when H3 added it in round 7 — the one row H3 was about. A ragged
table row is invisible to a claim-grep and to a cell-audit that reads cells rather than counting
them, so the rule gains a third amendment: **after editing a table, verify every row has every
column.**

**H7:** the cap check and the reinstate status transition SHALL be one transaction — check-then-CAS
is otherwise two steps whose atomicity rests on there being no `await` between them, which is the
safety-by-accident M11 exists to forbid.

**G7 extended:** N-d's `cancelled`-only restriction protects two things, not one — the crash budget
*and* the `reported_at` flag (a `cancelled` row is never reported, so a reinstated row's flag is
always clean). Both break if reinstate ever widens to reach `missed` or `abandoned`.

Scenarios: reinstate at the cap is rejected and the reminder stays `cancelled` ·
a freshly scheduled reminder is selected by the first tick after its due time ·
a reinstated reminder is selected by the next tick, not stranded · reinstate below
the cap restores to `pending` · the cancel→schedule→reinstate sequence cannot exceed the cap.

### 2.7 The mechanism-assumption pass — eight scenarios silently wrong, across three delta files

Run per G6 and **re-run per H4 over the `find`-enumerated delta list**, not one file. Every
existing scenario that *exercises* a changed mechanism (backoff schedule, the counters, the
transactions, four verbs, reinstate, chunk-atomic batching, the relocated grace check, the
widened tick selector). None contains a stale keyword, so §5's sweep could not see them.

Scope was the failure both previous times — round 4's sweep missed `design.md`, round 6's pass
missed three delta files — so **both passes now take the same `find`-enumerated file list.** One
list, two passes.

| Site | Assumption now false |
|---|---|
| `specs/reminders/spec.md:119-120` | *"fails once and succeeds **on the next tick**"* — retry is now 30s later on the backoff schedule (G6, reported) |
| `:123-124` | *"delivery fails on every attempt up to the maximum → **`abandoned`**"* — a channel failure now ends in **`missed`** via the time bound; `abandoned` is the crash-loop budget only. **Asserts the wrong terminal state** |
| `:126-128` | *"either delivered again (within the attempt bound) or already marked delivered"* — m5 already split the either/or, but "the attempt bound" now means `send_attempts` with a different window |
| `:133-135` | *"delivered **on startup**… status becomes `delivered-late`"* — D7 moved the grace check into the delivery path, so late delivery is not startup-specific |
| `:138-139` | *"appears in a **single missed-reminder summary**"* — replaced by §2.5's report, which may be appended to a delivery message and can fire mid-run |

| **`specs/audit-log/spec.md:45-47`** | *"delivery fails up to the attempt maximum and the reminder is **abandoned**"* — **`:123-124`'s error verbatim in a second file.** Confirmed by reading it: one mechanism change produced the same stale assumption twice, and a single-file pass catches one of them |
| `specs/audit-log/spec.md:23` | beyond the enum widening already in §5, the `scheduler`-transition **semantics** need the `missed`-vs-`abandoned` correction, not just more values |
| `specs/approval-gate/spec.md:35-37` | *"contains both the `scheduled` record and the `delivered` record"* — still **true** but no longer **sufficient**: a reinstated reminder's trail is `scheduled` → `cancelled` → `reinstated` → `delivered`, and the scenario tests provenance while ignoring the two records that explain it |

`:143` (*"no startup message of any kind is sent"*) is still true and is kept — it is one of the
clause's load-bearing tests. `incident-triage`, `agent-core` and `secure-deployment` scenarios
were checked against all eight mechanisms and are clean.

**H9:** because `abandoned` and `missed` were nearly swapped in two files, B's
`audit-record.v4.schema.json` `description` SHALL **define** them rather than list them —
`abandoned` = the crash budget exhausted, `missed` = the time budget exhausted. A schema whose
enum values proved confusable in review is exactly where the definition belongs.

---

### 2.8 [C] The selector-termination invariant, asserted by test rather than by prose

Three rounds produced three unbounded loops in this one mechanism, each caught by review rather than
by execution. One property covers all three, and it is cheap to assert:

**Two properties, not one** — K3 made them distinct and conflating them asserts something false:

> **Termination under crash faults, at or after the pre-work commit.** Under a render error, an OOM,
> or a kill at composition, at a per-message send, or at a post-send commit, every row SHALL leave
> the selector within `N` ticks, where **`N` is derived from configuration** — `max_attempts` × the
> backoff schedule's length, with the controllable clock advanced past each interval — not chosen as
> a generous constant.
>
> **Detectability, where termination is impossible.** A deterministic fault *before* the pre-work
> commit — in the selector region or in the grace transition, both inside that transaction — is
> unbounded by construction: no counter written by a transaction can bound a fault that prevents
> that transaction from committing, so nothing persists and no budget observes it. Verified by
> execution (§2.9): every stage at or after the commit terminates once the maximum is evaluated in
> pre-work; these two cannot, at any counter placement.
>
> The guarantee there is therefore **detection, not termination**: because M13 requires the
> scheduler loop to be exception-proof per tick, such a fault does not kill the process — it is
> caught, logged at error level, and retried. So the failure is **unbounded but loud**: exactly one
> error-level log per tick, indefinitely, in a process that stays up, and **no owner-visible
> message.** §2.9 asserts both numbers (1.0 logs/tick, 0 owner-visible, for both pre-commit
> stages). That is an operator-actionable signal, and it is a materially stronger claim than
> "the region is small" — which is a probabilistic argument about code size, not a property.
>
> A process-level circuit breaker (N consecutive ticks producing no successful work → stop the
> scheduler and alert) would bound the *loop* without bounding the *row*. Out of scope here, and
> named because it is the answer to "is there any placement that closes this".
>
> **Quiescence, not termination, under channel faults.** When the channel returns `failed`
> indefinitely, a terminal-unreported row SHALL remain selected — this is K3's deliberate design,
> since a report's value increases with age — while attempt frequency decays to the schedule's
> repeating tail and **no owner-visible message is produced.** A crash budget cleared on every
> return can never expire here, so asserting termination would assert the opposite of the design.
>
> **Conservation.** With the channel double recording every acknowledged message, the set of rows in
> a success terminal state (`delivered`, `delivered-late`, `reported_at` with `report_failed` false)
> SHALL equal exactly the set of rows contained in acknowledged messages; and every row in a
> non-success terminal state SHALL carry its failure marker, its error log, and its audit record.
> Termination is liveness and is satisfied by a bug that marks every row reported without sending
> anything — the capability's worst failure. Conservation is the property that actually matters.

Plus the two structural corollaries: every exit corresponds to a **written** column the selector
predicate tests (K2), and no counter exceeds its maximum (J1's climb-past-max).

**§2.9 records an executable check of these three properties against a model of the state machine**,
run before this plan was approved, because a test specified in prose inherits every weakness of
prose specification — which is how §2.8 produced two findings in the round that introduced it.

### 2.9 The executable check — run before approval, and it found two defects review did not

`verify_selector_invariants.py` (scratchpad) models the state machine **strictly as specified** —
§2.1's greedy measure-before-add, §2.2's pre-work and per-message post-send transactions and its
counters, §2.5's selector and report exits, §2.6's transitions — and asserts §2.8's three properties
with faults injected at each stage boundary. It is not the implementation (none exists); it answers
the one question prose cannot: *is the specified machine actually consistent with the properties
claimed of it?*

**It is not.** First run, `N = 21` derived from config:

```
TERMINATION under crash faults, per stage:
  [('pre-work', 60, 0), ('compose', 60, 84), ('send0', 60, 84), ('post0', 60, 84)]
```

Every stage failed, with `send_attempts` at **84** against a maximum of **3**.

**M1 — the crash-maximum check had no home on the crash path.** K1 moved the *increment* into the
pre-work transaction; the *maximum's evaluation* was still specified in the post-send transaction —
and a crash is exactly what prevents post-send from running. So the check was never reached on the
one path it exists to bound. Moving the evaluation beside the increment fixes three of four stages:

```
TERMINATION: [('pre-work', 60, 0)]          # compose, send, post-send all terminate
QUIESCENCE:  still_selected=True, interval_at_tail=True, index_clamped_ok=True,
             owner_visible_messages=0, attempts_made=11   # K3's design, confirmed
CONSERVATION: success_equals_acked=True, unmarked_failures=[], max_never_exceeded=True
```

**M2 — a fault before the pre-work commit is unbounded by construction.** The remaining failure is
not fixable at any counter placement: no counter written by a transaction can bound a fault that
prevents that transaction's commit. §2.8's termination property is therefore **scoped** to faults at
or after the pre-work commit, and the pre-commit region is argued small (one SELECT, arithmetic on
stored columns, one commit — no rendering, no I/O, no model output) rather than asserted bounded.
Asserting termination there would have been the eleventh borrowed guarantee.

Quiescence and conservation both pass, and quiescence's numbers confirm K3's intent empirically: the
row stays selected, the index clamps past the schedule's length, the interval sits at the 4h tail,
and **zero** owner-visible messages are produced across 3,000 ticks.

**The model is disposable; the fault-injection matrix is what change C carries** — pre-work commit,
grace transition, composition, each send, each post-send commit, plus a tri-valued channel double —
retargeted at the real store and scheduler. A model can only find *spec inconsistencies*, which is
the right scope for a pre-approval check: a spec inconsistency costs a review round, an
implementation defect costs a test run.

**Known non-coverage, stated so a green run is not read as broader than it is:** no framing-overhead
model, so N-a's largest-framing rule and the config-load validation are unexercised; lengths are
characters, not the bytes A will ship (D2); `compose()` produces messages that fit by construction,
so B1's single-chunk atomicity is **assumed, not verified** — this model cannot detect its
violation; the audit log is a counter, not records.

Value banked: **four defects found by running rather than reading** — M1 and M2 in round 10, then
N1's propagation failure and the grace-path classification error in round 11 — one of them the fifth
instance of the class that consumed rounds 6 through 10.

## 3. Settled carry-forward: CRITICAL / MAJOR

**C1** outcome-reporting `send` **[A]** · **C2** outbound mutual exclusion, bounded hold
**[A]** · **C3** verbatim bounded text echo on **every** write verb — `remind`,
`reschedule_reminder`, `cancel_reminder`, `reinstate_reminder`, `/remind`, `/reminders
cancel`, `/reminders reinstate` (E7); no-taint argued from the enforced predicate plus
delimiting and owner-only output reach **[B]** · **C4** CAS on every transition, scheduler
aborts on lost CAS, honest cancel reply **[B][C]** · **C5** → §2.2 · **M1/M2** →
§2.1/§2.3 · **M3** mechanical predicate, testability claim dropped **[C]** · **M4** →
§2.4 · **M5** registry-derived enumeration; prose may not state a tool count or absent
capabilities **[B]** · **M6** epoch storage resolved at schedule time; explicit-date
nonexistent times reject, bare `HH:MM` advances; ambiguous → first occurrence with a worded
echo; two hazards, two detectors **[B]** · **M7** subsumed by D7 **[C]** · **M8** → §2.5 ·
**M9** `surfaced_at` after a successful turn **[C]** · **M10** note read failure never
blocks **[C]** · **M11** event-loop-only store access (homed in `agent-core`),
`busy_timeout`, the verified-missing `delete_containing` rollback, first concurrency test
**[B]** · **M12** the note covers any owner-surfaced reminder event; `reminders_read` covers
terminal states **[C]** · **M13** exception-proof scheduler loop + `App.run` swallows
non-`CancelledError` **[C]** · **D1–D8, N-a–N-d** as resolved in R3, amended here by E1–E11.

## 4. Settled carry-forward: minors

m1 `source` gets meaning · m2 unfailable scenario deleted · m3/m4 reworded · m5 crash
scenario split · m6 window/clamp scenarios · m7 note bound default 5 · m8 clock skew 60s +
config key · m9 a command turn does not consume the note · m10 cite approval-gate · m11 no
dedup, explicitly accepted · m12 closed by the reschedule verb · m13 stale
`secure-deployment` header retained · m14 header and note outside the hashed recall block ·
m15 closed by the split · m16 a failed `scheduled` audit write does not fail the schedule
closed · m17 design paragraph on North Star principle 4 and model-supplied *timing*.

## 5. The D8/E1 sweep — enumerated by `find`, all nine artifacts

The round-4 sweep grepped `specs/` and `proposal.md` and stopped. It never opened
`design.md` — the artifact carrying *rationale*, where a reversed decision does the most
lasting damage, and the one place staleness no test would ever catch. Re-run with no filter
over every file `find` returns:

| Artifact | Stale sites |
|---|---|
| `design.md` | **201–215: the entire D8 section**, titled "cancellation is command-only", asserting "There is **no cancel tool**. The model can add; only the owner can remove" with its full rationale · 66–67 D1's schema tuple (missing all six new columns) · 76 status list · 219 D9's audit status list · 102 "correctable in one message (`/reminders cancel 12`)" · 248 risk table "cancellation is one command" · 280 migration step 3 "lists and cancels" |
| `specs/reminders/spec.md` | 85–94 the whole "Cancellation is owner-authored only" requirement and its two scenarios · 6 status enum + bookkeeping list · 59, 77–79 commands requirement (no reinstate, no cancelled tail) · 116 attempt count |
| `specs/agent-core/spec.md` | 52 "cannot cancel one" + two-tool enumeration · 60 scenario asserting the same · 33 command set missing `/reminders reinstate` |
| `specs/approval-gate/spec.md` | 6 declares `remind` only (four verbs needed) · scenarios' remedy text names only `/remind` |
| `specs/audit-log/spec.md` | 23 status enum (**including `report-abandoned`, invented in R9**) + the `initiated_by` mapping, which must name the report transitions as `scheduler` too · 33–35 cancellation scenario assumes owner-command only |
| `specs/incident-triage/spec.md` | 29 per-instance reference, now unexercised by design |
| `proposal.md` | 51–54 "There is deliberately **no cancel tool**" — the headline reversal · 49, 65, 80, 88 status lists, capability description, command enumeration |
| `tasks.md` | **61–63 task 3.6 tests "no registered tool can cancel, edit, or delete a reminder" — the inverse of the shipped design** · 33, 35, 72 store/scheduler test lists · 56–57 command tests · 137 deploy verification |
| `specs/secure-deployment/spec.md` | clean |

Resolved from this sweep: **`reschedule_count` appears nowhere in the artifacts**, so all six
new columns (`original_due_at`, `reschedule_count`, `send_attempts`, `unconfirmed_sends`,
`next_attempt_at`, reported flag) are additions — closing the reviewer's open question.

Outside the change, verified clean and noted as landing sites for task 8.1: `README.md`
(command table at 264, receipts sentence at 266 — no reminders content yet) and `config.yaml`
(no `reminders` section yet).

**K4 — `report-abandoned` joins the v4 enum, in B.** J1 created a terminal outcome the enum had
no value for: *Henk gave up telling the owner that a promise was broken* — the single most
receipt-worthy event in the capability, since it is the moment the attention contract fails
silently, and (per J5) the audit log is the only surviving evidence because the counters were
reset. v4's `reminder` statuses therefore become `scheduled`, `rescheduled`, `cancelled`,
`reinstated`, `delivered`, `delivered-late`, `missed`, `abandoned`, **`report-abandoned`**, with
H9's defining description distinguishing all three failure terminals: `missed` = the delivery
*time* budget expired · `abandoned` = the delivery *crash* budget expired · `report-abandoned` =
the report gave up. The record is written at the same moment the row leaves the selector.

**J6 — the split's standing question, now asked explicitly and generalized.** K4 is the fourth
time a C-side mechanism has demanded something of B after B was settled (F8's transaction
dependency, B4's enum, D4's `reinstated`, now this), and the third of those was *also* an enum
value rather than a column — so the question generalizes exactly as R9 Q3 asks: **not "what
columns does this need from B" but "what does this new *anything* — column, enum value, index,
constraint — require of B?"**, asked whenever C invents state of any kind. The store creates schema with
`CREATE TABLE IF NOT EXISTS` and has **no migration mechanism**, so a column added to the table
definition after the table exists is never created. **B SHALL therefore ship the complete final
column set, including columns only C writes** — `reported_at` and `terminal_at` among them, both
invented inside C requirements. Standing question for every future change in this split: *what does
this new state require of B?* This is the third consequence of the split that appeared only when a
later change invented state, after F8's transaction dependency and B4's enum, and it is the one
that would have produced a runtime `no such column` rather than a review finding.

Also flagged for the sync step (not blocking): the existing
`openspec/specs/approval-gate/spec.md` turn-scope requirement ends *"Both mutating tools
introduced by this change (`store_memory`, `capture`) are owner-turn-only"* — a change-scoped
sentence embedded in a binding spec. Adding four tools is the moment to generalize it or
consciously leave it as an artifact.

## 6. Change contents

**A `channel-send-integrity`** — outcome-reporting `send`; outbound mutex with bounded
hold; byte-measured `split_message`; explicit bridge send timeout; no adapter-authored
banner on proactive sends; core logs `partial`/`failed`; concurrency, partial-send and
byte-boundary tests. Fixes bugs that exist today.

**B `reminders-core`** — store + CAS + `busy_timeout` + rollback + first concurrency test;
DST-correct resolution with boundary fixtures; five tools; four commands with the
cancelled-tail listing; registry-derived prompt; audit v4 with the full eight-value enum
including `reinstated`; §2.4. **Merge boundary, deployed inert, verification = "verify
inert."**

**C `reminder-delivery`** — scheduler; §2.1 batching; §2.2 two-budget retry; grace check in
the delivery path; §2.5 report; the note; shutdown hardening; §2.3 plus its human
read-through. Deploy: full verification, rp5 hard stop.

## 7. Worst-case attention arithmetic

5 incident messages/24h + `ceil(N_due / batch_capacity)` reminder messages per tick (~25–30
typical per message, 3 at worst-case text length) + a bounded report appended to a delivery
message where one exists. Duplicates from unconfirmed sends: roughly a dozen per reminder
across a 24h grace window, not 2,880. No rate limit — the backstop is the pending cap and
the owner's own scheduling.
