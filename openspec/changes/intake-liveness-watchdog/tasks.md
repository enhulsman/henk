Every task below maps to a spec scenario or a design decision, and every scenario maps to at
least one task. The mapping is stated per task so a future edit cannot orphan either side.

## 0. Resolve the open questions before writing code

- [ ] 0.1 **(D3)** Read *every* backoff-progression case in `tests/test_event_intake.py` and
  decide D3 (reset the penalty on any proof-of-life frame, not only on a delivered event). Two
  are already verified landable: `test_persistent_failure_backs_off_without_crashing` (stream
  raises immediately, no proof-of-life frame, so `[1,2,4,8]` is unaffected) and
  `test_first_recovery_reconnects_without_sleeping` (stops collecting before the clean-end
  branch, so `slept == []` holds). Check the rest. If any test deliberately pins backoff
  surviving a frame-only reconnect, that intent wins and D3 is dropped — record which way it
  went and why, in this task. Note D3 is load-bearing for D4's termination rule, so dropping it
  is not a local edit.
- [ ] 0.2 **(D2, open question 1)** Confirm the liveness deadline multiple. Observe actual
  keepalive arrival jitter against the measured `keepalive-interval: "45s"` on the vps instance
  before fixing the deadline in config; 3× (135s) is the conservative default, 2× (90s) only if
  jitter is tight.
- [ ] 0.3 **(D1)** Confirm how a timeout expiry reaches the failure path. The bare
  `except Exception` at `intake.py:232` — **not** the `httpx.HTTPStatusError` clause above it —
  is what catches `httpx.ReadTimeout`. Verify the `asyncio.timeout` path normalises with
  `status=None` so `_is_since_rejection` cannot misread it. Do **not** widen that clause to
  `except BaseException`: `CancelledError` must escape it or the watchdog is silently disabled.

## 1. Tests first, from the spec scenarios

- [ ] 1.1 **(scenario: silent stream is abandoned)** A fake stream that yields nothing past the
  deadline causes a reconnect that resumes from the last-seen id.
- [ ] 1.2 **(scenario: keepalive frames alone keep a quiet subscription healthy)** A stream
  yielding only `keepalive` frames well past the deadline triggers no reconnect **and**
  accumulates no backoff penalty. This is the test that proves liveness is decoupled from event
  volume (D2) and that keepalive counts as proof of life (D4).
- [ ] 1.3 **(scenario: a connection that opens and then ends without delivering escalates)** The
  open-then-EOF case asserts the delay sequence `[1, 2, 4, 8, …]`, **not** repeated `1.0`. This
  is the assertion that makes D4's collision unreintroducible: treating `open` as proof of life
  fails here instead of shipping a silent 1/s spin. Cover open-then-silence (deadline trip) as
  well as open-then-EOF (clean end) — D4's table lists both.
- [ ] 1.4 **(scenario: a clean end after a healthy period costs only the base delay)** A stream
  that delivers proof-of-life frames and then ends cleanly waits exactly `backoff_base`,
  pinning the "bit-identical to today" claim so a future reader cannot mistake D4's termination
  rule for a penalty on healthy streams.
- [ ] 1.5 **(scenario: deadline exceeds the server keepalive interval)** The configured deadline
  is asserted to be a multiple greater than one of the recorded server interval, so a future
  config edit that would make the watchdog flap fails the suite instead of production.
- [ ] 1.6 **(scenario: a deadline below the keepalive interval is refused)** Config loading with
  a deadline at or below the recorded interval raises `ConfigError` naming both values. Include
  the equal case — "greater than one" excludes 1×.
- [ ] 1.7 **(scenario: quiet period is verifiable)** `liveness_state()` reports last
  proof-of-life, last reconnect, and current penalty, and they advance on keepalive frames and
  remain readable after an event-free interval.
- [ ] 1.8 **(scenario: a healthy stream is readable at deploy time)** The one-shot first-frame
  emission fires exactly once per process, and the periodic emission fires on its interval
  rather than per frame.
- [ ] 1.9 **(R1 SHALL: "cannot lose events")** A liveness trip preserves the checkpoint: it must
  not discard or rewind the cursor, and must not be mistaken for a since-rejection. Guards the
  interaction between the new path and the durability work.
- [ ] 1.10 Confirm all new tests fail for the right reason before implementing.

## 2. Implementation — four files, not one

- [ ] 2.1 **(D1)** `henk/events/intake.py`: the deadline as an `asyncio.timeout` in
  `EventIntake` **scoped to frame retrieval only**, normalised to the existing failure path with
  `status=None`. **Not** a transport-side `httpx.Timeout` on `NtfyEventStream` — that class is
  `# pragma: no cover` by design, so a deadline there would be the one mechanism in this change
  no test can drive. Scope placement is load-bearing: too wide and `CancelledError` reaches the
  consumer. Reuse or rename the **dead** `open_timeout` parameter (`intake.py:207`, assigned and
  never read; `timeout=None` at `:218` ignores it) rather than adding a second parameter beside
  it.
- [ ] 2.2 **(D4)** Proof-of-life accounting in `EventIntake`, keyed on the single definition —
  any frame whose `event` is not `open` — with all four consumers wired to it: the deadline,
  D3's penalty reset, the timestamp, and the termination rule. Record the definition in code
  where the classification happens, not at each call site.
- [ ] 2.3 **(D4)** The unified termination rule: the clean-end branch takes the backoff path
  unconditionally; a proof-of-life frame resets the penalty. No per-connection "did this
  connection deliver?" flag — if the implementation needs one, the rule has been misread.
- [ ] 2.4 **(D4)** Rename `last_frame_at` → `last_proof_of_life_at`, seeded to process start,
  so the field name carries the definition it enforces.
- [ ] 2.5 **(D4, R3)** `liveness_state()` accessor returning last proof-of-life, last reconnect,
  and current penalty; plus the one-shot first-frame line and the coarse periodic line.
- [ ] 2.6 **(D2, R2)** `henk/config.py`: new liveness fields — **none exist today** — carrying
  both the deadline and the recorded server keepalive interval, plus the ordering validator.
  The validator spans two config sections and `config.py` builds sections independently, so it
  goes in a **post-assembly** validation step, not inside either section's builder. Name the
  fields distinctly from the pre-existing `endpoints.ntfy.timeout_seconds` (`config.py:245`,
  default 10.0), which sits in the same section as the stream `base_url` and must **not** be
  reused as the read timeout.
- [ ] 2.7 **(D1)** `henk/runtime.py`: wire the new config through at the `NtfyEventStream`
  construction site (~`:149`) and the `EventIntake` construction site (~`:161`).
- [ ] 2.8 **(D3)** Apply or drop D3 per the 0.1 decision.
- [ ] 2.9 **(D3)** Fix the now-stale comment at `tests/test_event_intake.py:330-332`. It states
  "`attempt` resets on every delivered event" and uses that to justify why an interleaved trace
  is the only unambiguous discriminator in that test. The reasoning survives; the unit becomes
  "every proof-of-life frame." Left unfixed it is a comment asserting the old reset unit beside
  code implementing the new one — the exact name-versus-meaning gap D4 exists to prevent.
- [ ] 2.10 Full suite green (233 existing plus the new cases), `openspec validate --all` clean.
  Do **not** refactor `coordinator._pump` to `wait_for` while here: it reintroduces D1's
  wrong-scope trap one level up.

## 3. Ship it and verify on rp5

- [ ] 3.1 **Owner-run:** redeploy with `--build` (code is `COPY`'d into the image, so a plain
  `up -d` runs stale code). Note the container has been up 9 days on pre-change code.
- [ ] 3.2 **(scenario: a healthy stream is readable at deploy time)** Read `liveness_state()`
  and the healthy-path emissions from the running container and confirm frames arrive on the
  ~45s cadence while no events exist. This is the observation that makes every later quiet
  window interpretable, so it must be recorded, not just seen.
- [ ] 3.3 **(scenario: silent stream is abandoned)** Provoke a real trip — sever the path to
  ntfy long enough to exceed the deadline — and confirm intake reconnects, resumes from the
  correct cursor, and loses nothing. There is no owner notice in this change, so the expected
  channel behaviour is silence.
- [ ] 3.4 Record the as-built liveness config. **No token values, and no prose stating that a
  value was withheld** (`repo-publication` 3.3: the framing is the leak). Record the measured
  server keepalive interval alongside the deadline that derives from it (D2).

## 4. Produce the baseline the deferred notice needs

- [ ] 4.1 **(design non-goal: no notification predicate)** After a bounded window, extract the
  trip count and inter-trip intervals from the trip log lines. This is the measurement the
  owner-notification change is waiting on, and the reason it was split out: its constants
  cannot be derived until this instrument ships.
- [ ] 4.2 **Exit rule, including the likely branch.** If the window yields **zero** trips, that
  is sufficient rather than inconclusive: it demonstrates the natural trip rate is below any
  threshold the notice would pick, so the predicate is derived from the deadline arithmetic
  with a conservative bound and ships anyway. Absence is evidence for a **bound**, not for a
  **fit** — this is why it does not repeat `henk-events` 5.4, which needed to fit cadence
  constants to a distribution and could not.

## 5. Close-out

- [ ] 5.1 Update the `event-intake` spec Purpose line, still reading
  `TBD - created by archiving change henk-events`.
- [ ] 5.2 Archive. Check no other in-flight change MODIFIES `event-intake` first — two changes
  modifying one requirement silently stop matching once the first lands, and archive is
  transactional.
