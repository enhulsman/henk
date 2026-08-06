Every task maps to a spec scenario or a design decision, and every scenario maps to at least one
task. The mapping is stated per task so a future edit cannot orphan either side.

**Read `design.md` D1 before starting section 2.** Tasks 2.1–2.8 are **one coupled edit** to the
same region of `EventIntake.events()`. They are numbered for reference, not for sequencing into
separate commits: writing one location in one task and a second location in another is the drift
this change exists to prevent.

## 0. Settle before writing code

- [x] 0.1 **(D3)** D3 is **decided: it lands.** Verified by reading every case that reaches the
  clean-end branch or asserts on delays: `:101` (delivers a message then errors), `:167` (stream
  raises immediately, no frame ever arrives), `:265` (every connection 400s), `:326` and `:308`
  (both `RejectingStream(per_cycle=1)`, which yields one message then returns — so both reach the
  clean-end branch), and `:373` (stops collecting before the clean-end branch). Decisive:
  `keepalive` appears in exactly one test (`:77`), which also sends a `message`, so **no test pins
  backoff surviving a frame-only reconnect.** Confirm the trace still holds and record it. Note D3
  is load-bearing for D4's termination rule — dropping it is not a local edit.
- [x] 0.2 **(D1)** Build the `TimeoutError` normalisation; do not merely verify it. `except
  EventStreamError` at `intake.py:114` does **not** catch `TimeoutError` (`EventStreamError` is a
  plain `Exception`, `:48`), and the stream's `except Exception` at `:232` is one frame below where
  the deadline now fires. Convert at the point it fires, with `status=None` so
  `_is_since_rejection` (`:154-165`, which tests `exc.status == 400`) can never misread it and
  `_recoveries` is untouched. Do **not** widen `:232` to `except BaseException`: `CancelledError`
  must escape it or the watchdog is silently disabled.

## 1. Tests first, from the spec scenarios

- [x] 1.1 **(harness, prerequisite for 1.2–1.5)** Extend the test harness so the deadline is
  driven, not elapsed. `asyncio.timeout` reads the event loop clock and is unreachable from
  `EventIntake`'s injected `clock`/`sleep`, and `FakeStream` (`:37-45`) yields every scripted frame
  back-to-back with no awaits — so any test phrased as "well past the deadline" passes in
  microseconds while proving nothing. The fake `timeout_ctx` **must raise `TimeoutError`** (not
  `CancelledError`, or 0.2's handler is never exercised) and **must raise whenever the budget it
  receives is `<= 0`**, which makes 1.4's stale-budget case deterministic. Record every budget
  passed, so assertions can target the arithmetic rather than which context manager ran.
- [x] 1.2 **(scenario: silent stream is abandoned)** A stream that yields nothing until the budget
  is exhausted causes a reconnect that resumes from the last-seen id. Cannot use `_collect` — the
  stream yields no events, so `_collect` never returns; use the driver pattern from
  `test_persistent_failure_backs_off_without_crashing`.
- [x] 1.3 **(scenario: keepalive frames alone keep a quiet subscription healthy)** A stream
  yielding only `keepalive` frames across several driven budget windows triggers no reconnect
  **and** accumulates no backoff penalty. This is the sole spec-level bound on the top risk
  (D2's flap), so it must assert the absence of a trip across gaps the test explicitly caused —
  not merely that nothing happened.
- [x] 1.4 **(scenario: a stream delivering only open frames still trips; D4)** Two shapes, and the
  first is the one that fails under a per-frame budget: an `open` **flood** (repeated `open`, never
  another frame) still exhausts the budget; and open-then-silence trips within approximately one
  deadline of the last proof-of-life frame. Assert the trip *timing relative to the budget*, not
  merely that a trip eventually happens — a trip-eventually assertion cannot distinguish a correct
  implementation from one keyed on any frame. Run across **at least two connections**, so a budget
  established once outside the reconnect loop fails here.
- [x] 1.5 **(scenario: a connection that opens and then ends without delivering escalates)** The
  open-then-EOF case asserts the delay sequence `[1, 2, 4, 8, …]`, **not** repeated `1.0`, and
  asserts the last-proof-of-life timestamp goes stale. This is what makes D4's collision
  unreintroducible.
- [x] 1.6 **(scenario: a clean end after a healthy period costs only the base delay; D4)** A stream
  delivering proof-of-life frames then ending cleanly waits exactly `backoff_base`. Add the
  follow-on case — healthy clean end, then an immediate error — asserting `[1.0, 2.0]` **as
  intended**, since the penalty counter now advances where today it does not.
- [x] 1.7 **(scenario: a liveness trip does not kill intake)** After a trip, intake yields a
  subsequent event. Distinct from 1.2: that asserts the reconnect happens, this asserts intake did
  not terminate. Without it, the failure is a permanently hung consumer with no log line.
- [x] 1.8 **(scenario: a control frame's id is never used as a resume point)** A connection
  delivering `open` and `keepalive` frames that **carry `id` values**, then dropping before any
  message, must reconnect with `since` equal to the last message id — or cold if there has been
  none. No existing test guards this: `test_control_frames_skipped` asserts only that no events are
  yielded, never that the cursor is unchanged.
- [x] 1.9 **(scenario: consumer latency does not trip the watchdog)** A stream on a healthy cadence
  whose consumer takes longer than the deadline to return causes no trip. This is the deliberately
  slow consumer, and it pins two things at once: that the timeout scope excludes the consumer, and
  that consumer time is not charged against the budget (D1). Under the wrong scope it raises
  `CancelledError` into the consumer; under the wrong anchor placement it trips.
- [x] 1.10 **(D1, M1)** Repeated trips leak no generator or file descriptor. Must **explicitly
  `aclose()`** the intake generator (or force collection) rather than only `break`ing out of it —
  the broad `finally` fires when the *outer* generator is closed, so a test that merely abandons
  measures `shutdown_asyncgens` timing instead of the code.
- [x] 1.11 **(scenario: deadline is a permitted multiple / a deadline below it is refused)** The
  validator's predicate is asserted exactly: `deadline >= k · interval` with `k` the stated whole
  multiple greater than one. Include the cases that a bare `>` check would wrongly admit — a
  deadline greater than the interval but below `k ×` it must be **refused**. Assert on values
  **loaded from a mapping**, not only on constructor defaults, or the validator is never exercised
  against anything but its own defaults.
- [x] 1.12 **(scenario: a healthy stream is readable at deploy time / trips are countable)** The
  one-shot first-frame line fires exactly once per process; the periodic line fires on its
  configured interval rather than per frame; the trip line carries its stable identifier.
- [x] 1.13 Confirm all new tests fail for the right reason before implementing.

## 2. Implementation — one coupled edit across five files

- [x] 2.1 **(D1, M2)** Desugar the retrieval loop. `async for raw in self._stream.subscribe(...)`
  (`intake.py:103`) has **no syntactic place** for a retrieval-scoped timeout, so it becomes an
  explicit `agen = self._stream.subscribe(...)` / `await agen.__anext__()` loop. Catch
  `StopAsyncIteration` **inside** `events()` as the clean-end signal — `events()` is itself an async
  generator, so a `StopAsyncIteration` escaping its body becomes
  `RuntimeError: async generator raised StopAsyncIteration`. The existing `try/except/else` no
  longer routes clean end to `else:`.
- [x] 2.2 **(D1, M1)** Cleanup: a **broad** `try/finally: await agen.aclose()` around the retrieval
  loop. Measured: `await` inside a `finally` spanning a `yield` is **legal** — it is `yield` there
  that raises `RuntimeError: async generator ignored GeneratorExit`. The narrow alternative
  (`aclose()` on the timeout and error paths only) **leaks** on consumer abandonment, which is the
  most-travelled path: all 19 existing tests `break` out of `events()`, and it is the only path
  where `aclose()` has real work to do since every other path has already closed the generator by
  exception. See `~/.claude-config/provisioning/henk-probes/probe_aclose_legality.py` and
  `probe_genexit_boundary.py`.
- [x] 2.3 **(D1)** Add the `mono_clock` seam to `EventIntake.__init__`, mirroring
  `EventCoordinator` (`coordinator.py:43`, `:51-55`) and its comment. Budget arithmetic must be
  monotonic or an NTP step trips or stalls the watchdog; the displayed timestamp stays wall-clock.
  `mono_clock` and `timeout_ctx` are substituted **as a pair** — a fake clock with a real
  `asyncio.timeout` is incoherent.
- [x] 2.4 **(D1, 1.1)** Add the `timeout_ctx` seam:
  `timeout_ctx: Callable[[float], AbstractAsyncContextManager] = asyncio.timeout`, taking a
  **remaining budget** in seconds. Not `asyncio.timeout_at` — an absolute loop time cannot be
  expressed through a relative seam, and it would force the arithmetic out of reach of the tests.
- [x] 2.5 **(D1)** Restructure the `except EventStreamError` block into a shared backoff helper
  taking a **reason**, so the clean-end path can reach it without an exception to log. Clean end
  logs at INFO with a distinct message; an error end keeps WARNING. Without this, the naive
  implementation emits `"event stream failed"` on every healthy clean end — contradicting the
  handful-of-lines constraint and seeding exactly the misreading D4 exists to prevent. The helper
  must preserve a **stable trip identifier** (a distinct reason or structured field) — change D's
  baseline is extracted by matching it, so an incidental message-wording change would erase it.
- [x] 2.6 **(0.2)** Normalise a budget expiry into the backoff path with `status=None`, per 0.2.
- [x] 2.7 **(D4)** The budget, keyed on proof of life. Compute the remaining window as
  `deadline - (mono_clock() - last_proof_of_life_mono)` and pass it to `timeout_ctx`. **The wrong
  implementation is `timeout_ctx(deadline)`** — a full window every retrieval, which restarts on
  `open` frames and never fires under an `open` flood (measured: 40 `open` frames, no trip). Two
  placement rules, both load-bearing:
  - **Re-establish the budget immediately before each subscribe call, *after* the backoff sleep.**
    Established once outside the reconnect loop, every post-trip connection inherits an expired
    budget and dies before its first frame — permanently, silently, capped at max backoff
    (measured: zero events delivered after the first trip). Anchored *before* the sleep, a 30s
    backoff silently consumes 30s of a 135s window.
  - **Advance the anchor for a delivered event *after* its `yield` returns**, so consumer latency
    is not charged against liveness — liveness measures the stream, not Henk's processing speed. A
    non-positive budget trips instantly and silently (measured: `-5.0`, `-0.001` and `0.0` all
    raise at 0.000s — no `ValueError`, no clamp, no log), so charging consumer time would make
    1.9 fail for the wrong reason and tempt widening the scope. Keepalives are not yielded, so they
    advance the anchor at classification; state both points in the code.
- [x] 2.8 **(D4, M13)** Proof-of-life classification and the penalty reset at the **classification**
  point — is this frame's `event` not `open`? — and **before** the `_convert`/`continue` guard
  (`:105-106`), never inside it. Today's reset sits at `:112`, after that guard, so control frames
  never reach it. Accounting reads `raw["event"]` **only** and must **not** write `_last_id`: ntfy
  control frames carry an `id`, and writing one into the cursor either gets 400ed (→ retention
  replay + owner DM) or is accepted and **silently skips messages**.
- [x] 2.9 **(D4)** **Add** `last_proof_of_life_at`, seeded to process start. (Not a rename —
  `last_frame_at` has never existed in `henk/` or `tests/`; it appears only in this change's
  documents.) Hold it twice: monotonic for the budget arithmetic, wall-clock for display.
- [x] 2.10 **(R3)** `liveness_state()` accessor returning last proof-of-life, last reconnect and
  current penalty, plus the one-shot first-frame line, the coarse periodic line, and the trip line
  with its stable identifier. The **emissions are the owner-facing surface**; the accessor is a
  test seam and a hook for any future in-process reader.
- [x] 2.11 **(D2, R2)** `henk/config.py`: add `events.liveness_deadline_seconds` (Henk's policy)
  and `endpoints.ntfy.keepalive_interval_seconds` (the recorded server value) — **no liveness
  fields exist today** — plus the ordering validator. Deliberately two sections: the interval
  describes the server, the deadline describes Henk, which is why validation runs **post-assembly**
  rather than inside either builder. Name them distinctly from the pre-existing
  `endpoints.ntfy.timeout_seconds` (`:245`, default 10.0), which sits in the same section and must
  **not** be reused as the read timeout. Also add the periodic-emission interval, which otherwise
  has no home despite being tested by 1.12.
- [x] 2.12 **(D1)** Adopt the redundant transport read floor: `httpx.Timeout(None, read=…)` at a
  multiple of the liveness deadline, on `NtfyEventStream`. One line, normalises through the existing
  `except Exception` at `:232`, needs no cancellation of a live generator, and downgrades a broken
  hand-rolled watchdog from a permanent hang to a bounded reconnect. It **masks** budget-arithmetic
  defects at deploy time, so the unit tests stay primary. Decide the dead `open_timeout` (`:207`,
  assigned and never read; `timeout=None` at `:218` ignores it) in the same edit, since both concern
  one `httpx.Timeout` object: either delete it or wire it as `connect=`. Record which, and record
  that `timeout=None` today means there is no connect timeout either.
- [x] 2.13 **(M1, N5)** `henk/events/coordinator.py`: `_pump` (`:134-136`) creates the intake
  generator inline via `async for` and never holds it, so cancelling the producer leaves `events()`
  **suspended, not closed** — finalised only if the loop reaches `shutdown_asyncgens()`. Hold it
  (`agen = self._intake.events()`) with `try/finally: await agen.aclose()`, or 2.2's broad cleanup
  is decorative on the one production path with a live connection open. Note `run()`'s `finally`
  catches only `CancelledError`, while `await producer` on an already-failed task re-raises its
  stored exception — check that path too.
- [x] 2.14 **(D1)** `henk/runtime.py`: wire the new config at the `NtfyEventStream` site (~`:149`,
  the read floor and the `open_timeout` disposition) and the `EventIntake` site (~`:161`, the
  deadline, the clocks and the emission interval).
- [x] 2.15 **(D3)** Fix the comment at `tests/test_event_intake.py:330-332`. It says "`attempt`
  resets on every delivered event, so all delays are backoff_base and a delay-value assertion
  cannot distinguish the two paths." After this change the delays in that test become
  `[1.0, 2.0, 1.0, 2.0, …]` (its `RejectingStream` reaches the clean-end branch), so a delay-value
  assertion **can** now distinguish them — the interleaved trace is no longer the *only*
  discriminator. Both halves of the old comment fail; rewrite it rather than adjusting one word.
- [x] 2.16 Full suite green (233 existing plus the new cases), `openspec validate --all` clean. Do
  **not** refactor `coordinator._pump` to `wait_for` while in there — it reintroduces the wide-scope
  trap one level up.
- [x] 2.17 Resolve **every** code identifier these four artifacts name against `henk/` and
  `tests/`. `last_frame_at` (2.9) was not an unlucky one-off; it was the first one checked.

## 3. Ship it and verify on rp5

- [x] 3.1 **Owner-run:** redeploy with `--build` (code is `COPY`'d into the image, so a plain
  `up -d` runs stale code). The container has been up 9 days on pre-change code. Because
  `config.yaml` is a **read-only bind mount from the checkout**, 2.11's values ship with the image
  rather than as a host-side edit — commit, redeploy, and keep a revert path.
- [x] 3.2 **(scenarios: a healthy stream is readable at deploy time; a quiet period is verifiable
  after the fact)** Read the startup line and the periodic lines from the running container and
  confirm frames arrive on the ~45s cadence while no events exist. Record it. The **emissions** are
  the surface here — `liveness_state()` has no out-of-process reader, so do not plan to call it.
  Then verify the second scenario is actually satisfied: from the recorded lines alone, and without
  inspecting the subscription by hand, establish whether intake was continuously alive across that
  window. If that cannot be done from the lines, the emission content is wrong — not the
  observation.
- [ ] 3.3 **(scenario: silent stream is abandoned / a liveness trip does not kill intake)** Provoke
  a real trip — sever the path to ntfy past the deadline — and confirm intake reconnects, resumes
  from the correct cursor, loses nothing, **and is still delivering afterwards**. There is no owner
  notice in this change, so the expected channel behaviour is silence.
- [x] 3.4 Record the as-built liveness config. **No token values, and no prose stating that a value
  was withheld** (`repo-publication` 3.3: the framing is the leak). Record the measured server
  keepalive interval beside the deadline that derives from it (D2).
- [x] 3.5 **(D2, M17)** Add the coupling cross-reference the validator cannot enforce: it compares
  Henk's deadline against Henk's *recorded copy* of the interval, so raising
  `keepalive-interval` on the vps without updating Henk's config passes validation and flaps the
  watchdog. Note the coupling on the vps side and in the homelab ntfy docs page — a `/docs-update`
  trigger.
- [x] 3.6 Copy the probes backing this change's measured claims into
  `~/.claude-config/provisioning/henk-probes/` and fold them into the existing "re-run on httpx
  version drift" note (`httpx>=0.27` is an open bound). After the `timeout_ctx` seam, the probes are
  the **only** executable check on the cancellation path — the faked timeout tests the arithmetic
  and never the cancellation.

## 4. Close-out

- [x] 4.1 Update the `event-intake` spec Purpose line, still reading
  `TBD - created by archiving change henk-events`.
- [ ] 4.2 Archive. Check no other in-flight change MODIFIES `event-intake` first — two changes
  modifying one requirement silently stop matching once the first lands, and archive is
  transactional.

**Moved out of this change:** the trip-baseline extraction that was section 4 now belongs to change
D (the owner notice) as its task zero. D is "deferred until A's baseline exists," so the extraction
is D's first task, not A's last — otherwise A cannot archive until a multi-day observation with no
stated N completes, on a measurement whose expected result (zero trips) makes it unnecessary. A's
obligation is to **emit** the stably-identifiable trip line (2.5); D's is to count them.

## Implementation record (2026-08-03)

Recorded because three tasks ask for it (0.1, 2.12, 3.6) and because two of these facts were
measured rather than reasoned about.

**0.1 — D3's trace re-confirmed, and the successor claim measured.** The read of every
clean-end-reaching and delay-asserting case still holds: no test pins backoff surviving a
frame-only reconnect. The consequence D3 predicted was then *measured* rather than predicted:
`test_repeated_rejection_backs_off_after_the_first_recovery`'s delays are now
`[1.0, 2.0, 1.0, 2.0, …]` (clean end at penalty 0, paced recovery at penalty 1). Its comment is
rewritten and the test gained a delay-value assertion as a second, independent discriminator —
the interleaved trace is no longer the only one.

**2.12 — `open_timeout` disposition: WIRED, not deleted.** It becomes `connect=` on the one
`httpx.Timeout` object that also carries the new `read=`. Recorded as the task asks: the previous
`timeout=None` meant there was **no connect timeout either**, so deleting `open_timeout` would
have left that gap undocumented and unfixed. The read floor is `2 ×` the liveness deadline
(`_READ_TIMEOUT_MULTIPLE` in `runtime.py`), i.e. above it, so intake's own budget is always what
fires first and the transport stays a backstop.

**Every documented wrong implementation is caught by a named test.** The tests were checked by
mutating the implementation into each wrong form the artifacts warn about and confirming a *named*
test fails — the check that actually matters, since "the right implementation passes" says nothing
about discrimination. All ten were caught:

| Wrong form | Caught by |
|---|---|
| `timeout_ctx(deadline)` — full window per retrieval | `test_open_flood_still_trips_…` |
| budget anchored once outside the reconnect loop | `test_silent_stream_is_abandoned_…` |
| anchor not advanced after the `yield` | `test_consumer_latency_does_not_trip_the_watchdog` |
| `TimeoutError` not normalised | `test_silent_stream_is_abandoned_…`, `test_a_liveness_trip_does_not_kill_intake` |
| no broad generator cleanup | `test_repeated_trips_finalise_every_stream_generator` |
| `open` treated as proof of life | `test_open_flood_still_trips_…` |
| classification after the `_convert` guard | `test_keepalives_alone_keep_a_quiet_subscription_healthy` |
| clean end reusing the failure line | `test_a_clean_end_is_not_logged_as_a_failure` |
| control-frame `id` written to the cursor | `test_control_frame_id_is_never_used_as_a_resume_point` |
| validator using a bare `>` | `test_deadline_below_the_required_multiple_is_refused` |

**One test beyond the plan: real cancellation is now covered in-suite.** D1's honest ledger said
the seam buys arithmetic coverage only, leaving cancellation probe- and deploy-verified.
`test_a_real_timeout_cancels_the_pending_read_and_intake_recovers` closes half of that gap
cheaply: the real `asyncio.timeout` at a 0.05s deadline over a stream that genuinely suspends,
asserting the pending read was cancelled, that no `CancelledError` reached the consumer, and that
the resume cursor was right afterwards. It covers **intake's** scope; httpx's own cancel scopes
remain probe territory.

**The one claim the change INTRODUCED, now measured too.** `_pump` holding and `aclose()`ing the
intake generator (2.13) makes shutdown close a *live* httpx stream inside a cancellation unwind —
which the review record flagged as an unbounded teardown and left unmeasured. New
`probe_shutdown_teardown.py` mirrors `_pump` and `run()`'s finally against a stalling server with a
finite `read=`: the unwind takes **0.3 ms** across three trials, so the teardown does not wait on
the network and shutdown is not bounded by the read timeout. Concern refuted, by execution.

**3.6 — probes re-run, not merely copied.** All nine were already in
`~/.claude-config/provisioning/henk-probes/`; all were re-run against httpx 0.28.1 / CPython
3.12.3 with every conclusion unchanged. The drift note now lives in that directory's `README.md`
(with the re-run date), rather than only inside the review record.

**3.5 — the coupling cross-reference landed in two places.** The vps side
(`services/monitoring.md`, ntfy → Configuration) states that `keepalive-interval: "45s"` is
coupled to Henk's deadline and that Henk validates against its own *recorded copy*, so raising the
server interval passes validation and then flaps. The Henk side
(`services/applications.md`) replaces the "latent risk — a half-open socket would hang undetected"
bullet with what closed it, plus how to read liveness from `docker logs` without `docker exec`.

## As-built (rp5, 2026-08-03)

| Value | Setting |
|---|---|
| `events.liveness_deadline_seconds` | 135 |
| `endpoints.ntfy.keepalive_interval_seconds` (recorded server value) | 45 |
| `events.liveness_report_interval_seconds` | 3600 |
| transport read floor (`2 x` deadline, `runtime.py`) | 270 |
| connect timeout (the former dead `open_timeout`, now wired) | 30 |

Deployed by `docker compose -p henk -f /home/pi/Coding/henk/docker-compose.yml up -d
--build henk`. Startup was clean: config accepted (so the >= 3x ordering validator passed
against the recorded interval), subscription resumed from the persisted checkpoint with
HTTP 200 — no cold subscribe, no since-rejection — and zero errors or warnings.

**The 45s interval is now confirmed in production, not just read from the server config.**
Container start 16:57:49, first proof-of-life frame 16:58:34 — a 45s gap, and with no
message traffic that frame can only have been a keepalive. That is the measurement the
135s deadline derives from, observed at the point of use.

Log surface after startup is three lines total, matching D5's handful-of-lines intent:

```
INFO henk starting henk with config=/app/config.yaml
INFO httpx HTTP Request: GET http://vps:2586/henk-events/json?since=<id> "HTTP/1.1 200 OK"
INFO henk.events.intake intake liveness: first proof-of-life frame; last proof-of-life
     2026-08-03T16:58:34 (0s ago), last reconnect never, penalty 0
```

**Deploy-path defects found on the way, both now recorded as memories rather than only
here.** rp5's `config.yaml` is a permanently uncommitted local modification (it carries
values the repo keeps as placeholders) which makes `git pull`/`reset` fail there while
`git status` reports clean; and running compose from a `henk.old` directory silently
creates a separate `henkold` project on empty volumes, whose symptom is a wall of DNS
resolution failures. Neither is a defect in this change, but both are deploy hazards for
the next one, and the first came within one command of bringing Henk up with an owner id
that would have dropped every DM at the allowlist.

### 3.2 closed — 66 hours of evidence (2026-08-03 → 2026-08-06)

The second scenario ("a quiet period is verifiable after the fact") is satisfied from the
emitted lines alone, which is the test the task set: **66 consecutive hourly lines**, every
one reading `80 proof-of-life frames in the last 3600s`, `last reconnect never`, `penalty 0`
— with **zero** trips, errors or warnings across the whole window.

80 x 45s = 3600s exactly, so the frame count alone pins the delivery cadence to the server's
keepalive interval, and `last reconnect never` holding for 66 hours establishes one unbroken
connection. A quiet homelab is now *evidence* of quiet: before this change the same 66 hours
of silence would have been indistinguishable from a dead socket. The emission content is
therefore right — the check the task demanded (can continuity be established without
inspecting the subscription by hand?) passes.
