# Scrutiny record: sensor-aperture

Outcome of a 7-round adversarial review of the `sensor-aperture` proposal (2026-08-03).
The proposal as committed is **superseded by this record**: it should become four changes,
and its liveness half needs the rewrites below before implementation. Kept in the change
directory so the reasoning survives; see the Verdict section at the end for what to do next.

---

# Fix plan v7: `intake-liveness-watchdog` (was sensor-aperture)

Round 6 verdict: **NEEDS REWORK** — one CRITICAL (R6-1) plus five MAJORs and one MINOR. All
accepted. R6-1's proposed fix is **simpler than mine**, so v7 adopts it wholesale rather than
defending my version.

## Answering Q1, Q2, Q3

**Q1: did any "keep" disposition get re-read against the new escalation, or did "keep" mean
"already checked"?** It meant already-checked. That is the blind spot: my mechanical
enumeration assigns a disposition to every identifier, but I treated `keep` as terminal and
only scrutinised `rewrite` rows. D3 was the cheapest disposition in the table and it was the
one that broke the fix. **The enumeration now requires re-reading every `keep` row against any
definition that changed in the same pass** — `keep` is a claim about compatibility, not an
exemption from checking.

**Q2: I wrote "enumerate the wire inputs first" and then wrote a positional predicate in the
same document. What would have caught it?** Naming the concept. An inline positional phrase at
a call site ("post-`open` frame") cannot be cross-checked, because it has no name to search for
and no declared consumers. A **named definition with an explicit consumer list** can be, and
that is what R6-1's fix produces. This is a step, not recall: any concept used by more than one
call site gets a name, a definition, and a list of its consumers, all in one place.

**Q3: what does the owner actually do on day one if the healthy path emits nothing?** Nothing,
under v6 — which is why R6-5 is right that task 3.2 was unperformable. Fixed in v7 by naming a
surface and giving the healthy path an emission (below).

---

## R6-1 (CRITICAL) — one type-based definition, three consumers, one rule

The defect: I changed one consumer of proof-of-life and left three saying "any frame" —
Requirement 1's SHALL (`spec.md:4`), D3 (`design.md:81`, marked **keep**), and task 2.2's
accounting. With D3 kept, `open` resets `attempt`, so:

- **open-then-EOF:** reset → EOF → delay `min(1 × 2⁰, 30)` = 1.0s → reset → … one reconnect per
  second forever. My v6 claim that this "converts an infinite silent spin into an escalating
  backoff" was **false under my own D3 disposition**.
- **open-then-silence — the primary watchdog shape, the actual half-open socket:** reset →
  deadline trips at 135s → pays 1s → reset → … a reconnect every ~136s forever, never
  escalating, never visible.
- **Observability:** `open` advances `last_frame_at`, so the exposed state reports a frame every
  1s or 136s while zero events flow. A's central claim — silence is checkable — stays false.

**The definition, stated once:**

> A **proof-of-life frame** is any frame whose `event` is not `open`. An `open` frame proves a
> connection was accepted; it does not prove the stream is delivering.

Type-based, not positional — which also fixes a gap my version had: the test fakes never send
`open` at all, so a literal implementation of "post-`open`" would reclassify every fake's clean
end as a failure.

**Consumers, named explicitly** (this list is the artefact that makes future collisions
findable): the deadline; D3's backoff reset; `last_frame_at`; and the termination rule.

**The escalation collapses to one rule, with no per-connection flag:** the clean-end branch
takes the backoff path unconditionally; a proof-of-life frame resets the penalty. Behaviour:

- Healthy stream with keepalives, then a clean end → the keepalive already zeroed `attempt`, so
  the clean end costs `backoff_base` = **bit-identical to today's flat 1s**.
- `open`-then-EOF or `open`-then-silence → nothing resets → 1, 2, 4, 8, 16, 30, 30. Escalating,
  visible, and `last_frame_at` correctly goes stale.

Simpler than v6 (no "did this connection deliver a post-`open` frame" state), and it moves the
code *toward* the existing standing spec, which already says "When the subscription drops, Henk
SHALL reconnect with backoff" — today's unconditional flat 1s clean-end path is arguably
already outside that sentence.

**Requirement 1's SHALL is rewritten** to key the deadline, the backoff reset, and the
timestamp on proof-of-life frames, and extended to cover the second termination shape: *a
connection that ends, cleanly or otherwise, without delivering a proof-of-life frame is a
failure.* v6 had a scenario for that shape sitting under a SHALL that did not describe it —
the identical defect I had just diagnosed for the ordering invariant, one paragraph later.

**D3 becomes load-bearing, recorded as such:** if D3 were dropped, the escalation would still
work but a healthy clean end would start costing escalating backoff. A future reader must not
drop D3 in isolation.

**Test assertion that makes the collision unreintroducible:** the open-then-EOF test asserts
delays `[1, 2, 4, 8, …]`, not repeated `1.0`.

### Checking the definition against each of its four consumers

| Consumer | Does the unit fit? |
|---|---|
| The deadline | Yes. `open` does not restart it, so open-then-silence trips at D after connect. The clock starts at subscribe; a healthy stream's first keepalive at ~45s is well inside 135s |
| D3's backoff reset | Yes — this is the fix; `open` no longer zeroes `attempt` |
| The last-frame timestamp | Yes, **but the field name is now wrong** — see below |
| The termination rule | Yes, same unit |

**A naming defect of exactly the kind that caused R6-1:** the field is called `last_frame_at`
while it now deliberately **excludes** a class of frames. A future reader sees a name promising
"any frame" and an implementation excluding `open` — which is the same name-versus-meaning gap
that let "any frame" survive in three consumers. **Rename it `last_proof_of_life_at`**, so the
name carries the definition and the collision cannot recur silently.

**One thing considered and deliberately rejected:** distinguishing "connects then stalls" from
"cannot connect at all" would need a connections-established counter, since neither
`last_proof_of_life_at` nor `last_reconnect_at` separates them. It would be a nicer diagnostic.
But A's falsifiable claim is "intake is or is not delivering," which both shapes answer
identically, and no reader in the repo needs the finer split — adding it would repeat exactly the
R3-6 error of hardening a component whose warrant was never established. Recorded as rejected,
not missed.

## R6-2 — keep scenario `:17`, restated by frame type

Deleting it was wrong twice over: it is the only spec-level assertion that a keepalive-only
stream is healthy, which is the sole bound on `design.md:165` (A's top risk — the watchdog
flapping if the server's keepalive interval is raised), and task 1.2 existed to test it, so v6
orphaned a task in mirror image of R4-2. Under R6-1 it becomes *more* load-bearing: keepalive
must be proof of life or the escalation fires on healthy quiet streams.

**Requirement 1 carries four scenarios:** `:13` keep; `:17` keep, restated as "delivers only
`keepalive` frames"; **add** open-then-EOF escalates; `:22` keep. Plus a scenario for the
ordering-invariant requirement, since a requirement with no scenario fails my own gate.

**Validation becomes bidirectional:** every scenario maps to a task **and** every task maps to
a scenario or a decision. v6's one-directional criterion structurally could not detect a task
with no scenario, which is the direction this failure ran.

## R6-3 — prose enumerated by opening words, not by count

"Delete the four routing bullets" was ambiguous over six. Assigned explicitly:

| `design.md` Risks bullet | Disposition |
|---|---|
| `:165` "Watchdog flaps if the server's keepalive interval is raised" | **Keep — A's top risk** |
| `:169` "D3 contradicts an existing tested assertion" | **Keep**, annotated: verified landable, and D3 is now load-bearing |
| `:172` "Flood into an unrotated audit log" | Delete → change C |
| `:176` "Sensor edits are owner-run and outside the repo" | Delete → change C |
| `:180` "As-built notes are a leak surface" | **Keep, one line** — task 3.4 is retained and needs its rationale |
| `:183` "Tranche 1 may still be quiet" | Delete → change C |

`proposal.md` additional unmarked spans, all now assigned: `:9` (the "two facts reframe" lead-in
to deleted items) delete; `:98` Impact bullet rewrite; and **`:60-62`**, whose closing sentence
"This change moves existing signal and closes one durability gap" is **false after the split** —
A moves no signal. Same species as `:82-84`, which I did catch. Rewrite both.

Greps get `Alertmanager|aperture|widen|two tranches|moves existing signal` added as a
**backstop**, with sentence-level re-reading of the two narrative files as the actual method —
a keyword grep can only find stale text that happens to use my keywords.

## R6-4 — the deferral gets an exit condition, and A produces the baseline

v6 rested on "D's constants get derived from the trip-rate baseline change A produces" while
nothing in A produced one, and deleted D8 — the bounded falsifiable watch discipline invented
for exactly this. That is `henk-events` 5.4 recurring, which is the finding that started all
of this.

**New task in A:** after N days, extract trip count and inter-trip intervals from the trip log
lines and hand them to D. **Exit rule, including the branch that is actually likely:** if N days
yield **zero** trips, that is itself sufficient — it demonstrates the natural trip rate is below
any threshold D would pick, so D's predicate is derived from the deadline arithmetic with a
conservative bound and **ships anyway**, rather than waiting indefinitely for an event a healthy
system will not produce.

**Is that rationalising a vacuous outcome, the way 5.4 nearly did?** No, and the distinction is
principled rather than convenient. 5.4 needed to **fit** constants to a distribution — which
debounce window collapses real storms, what cooldown matches real re-fire intervals — and a
distribution cannot be fitted from an absence, so zero events left the cadence values genuinely
unsupported. D's threshold asks a different *kind* of question: **does the natural trip rate
exceed the threshold?** That is a bound, not a fit, and an absence answers it directly — zero
trips in N days means 3-in-60-minutes demonstrably does not occur naturally, giving a measured
false-positive rate of zero over the window. Absence is evidence for a bound and is not evidence
for a fit; 5.4 needed the latter and D needs the former. Stated explicitly so the branch cannot
be mistaken for the same move.

## R6-5 — name the observability surface, then rewrite 3.2 against it

"Log-on-state-change" means a healthy stream logs nothing, so task 3.2 — "confirm frames arrive
on the ~45s cadence and last-frame state advances" — had nothing to read. Since 3.2 is the sole
delivery mechanism for Goal 2, A would have shipped its instrument unread.

**Fix:** a named `liveness_state()` accessor returning last-proof-of-life-frame, last-reconnect,
and current backoff, which the unit tests assert against; plus healthy-path emissions the owner
can actually read at deploy time — a one-shot "first proof-of-life frame received" line at
startup, and an every-Nth-frame debug line (N chosen so a healthy day is a handful of lines, not
1,920). Requirement 3's scenario `:51` says state "is inspected" without naming a surface; it now
names this one. Task 3.2's method is rewritten against it.

## R6-6 — the escalation gets a decision record

After the split, `design.md` keeps D1/D2/D3 and creates nothing new, so the most subtle
behaviour change in A — applying a penalty to what the code calls a *clean* termination — would
be justified only in a scratchpad that is never archived. A future reader would see a penalty on
a successful stream end, find no rationale, and revert it as an obvious bug, silently restoring
the 1/s spin.

**New decision, holding both halves in one place:** the proof-of-life definition, its four named
consumers, why `open` does not count, and the unified termination rule.

## R6-7 — config traps, verified

- **`endpoints.ntfy.timeout_seconds` already exists** (`config.py:245`, default **10.0**,
  consumed by the notify tool at `tools/__init__.py:81`) and sits in the same section as the
  stream's `base_url`. It is 13× smaller than the 135s deadline, so an implementer wiring
  "config-driven timeouts" would reach for it and silently invert the ordering invariant. The new
  fields are named distinctly and the plan states explicitly that the stream read timeout is
  **not** that field.
- **`open_timeout` is dead** (`intake.py:207`, assigned and never read; `timeout=None` at `:218`
  ignores it). `connect_timeout` **reuses/renames that parameter** rather than adding a second
  one beside it.
- The ordering validator spans two config sections, and `config.py` builds sections
  independently, so it gets a deliberate home stated in the task: a post-assembly validation step
  rather than inside either section's builder.

---

## Carried unchanged

Change A's remaining scope and the four-way split (A watchdog / B cadence-mechanism-validation,
owner-run and sequenced after A / C routing, blocked on the Henk-as-Alertmanager explore
question / D notice, deferred with R5-1's attribution half, R5-2, R5-3, R5-4, R5-6, R5-7, R5-8
recorded); `asyncio.timeout` scoped to frame retrieval with `status=None` normalisation; the
`git mv` rename (`.openspec.yaml` needs no edit); the M8 rejection and change-C task zero; probe
results (cancel-scope race and FD/task leak both refuted on the real dependency set, my own
harness's false positive recorded); the mechanical identifier table from v6 (36 tasks, 9 spec
headings, 8 decisions — with `keep` rows now re-read per Q1); `last_frame_at` seeded to process
start; the trip counter staying out of A; and the note that refactoring `coordinator._pump` to
`wait_for` would reintroduce Trap A one level up.

---

## Verdict and next steps

Seven review rounds, six `NEEDS REWORK` verdicts, no `REJECT`. The review never approved, and
the loop was stopped deliberately rather than converging — see "the pattern" above: every
round from 3 onward found a real defect in the **same** component, the owner-notification
predicate, because its constants cannot be derived until the instrument that measures them
exists. That is the finding, not a failure to try hard enough.

**What is settled and evidence-backed:**

1. The original proposal's central cost claim was **false**. There is no Alertmanager
   (`activeAlertmanagers: []`, no `alerting:` block), so Prometheus-native rules deliver
   **nowhere** — routing them to Henk is building new delivery, which the design listed as a
   non-goal.
2. The henk notification route runs `continue: false`, so routing an existing Grafana rule to
   Henk **removes it from Discord**. Routing `InstanceDown` (severity critical) there would make
   a deliberately-suppressing consumer the sole path for host-down alerts.
3. Cadence *values* cannot be tuned from 14 days at this incident rate; the mechanism can be
   validated synthetically in an afternoon, against config knobs that need no logic redeploy.
4. The liveness deadline belongs in `EventIntake`, not the transport. Measured on the real
   dependency set (httpx 0.28.1 / py3.12.3): the cancel-scope race is **refuted**, no FD or task
   leak, and the wrong scope placement raises `CancelledError` into the consumer.
5. `open` frames are not proof of life. Treating them as such disables backoff entirely for the
   primary half-open-socket shape.

**Recommended split:**

- **A — `intake-liveness-watchdog`:** detection + observability + wiring. Ready to implement
  after the rewrites recorded above. Fully fixes the original carry-forward defect (a socket
  that hangs undetected).
- **B — `cadence-mechanism-validation`:** synthetic sweep, owner-run, sequenced after A.
- **C — routing:** **not proposed.** Blocked on an owner decision: should Henk be the delivery
  path for undelivered Prometheus rules, or should Alertmanager be deployed with Henk consuming
  from it? Take this to `/opsx:explore`, not to a change.
- **D — the owner notice:** deferred until A's baseline exists.

**Do not implement from the committed proposal as written.**
