## Context

The event pipeline is complete and verified end-to-end on rp5, but it has processed zero real
events since deploy-verify. `henk-events` 5.4 closed on that finding and could only trust it
because the owner verified continuous subscription by hand, per trigger family.

The constraint that makes this change come first: **intake liveness is currently
unfalsifiable.** With `httpx.AsyncClient(timeout=None)` and no read deadline, a half-open socket
produces exactly the same observable as a healthy quiet tailnet, which is *nothing*. Any
conclusion drawn from an absence of events rests on intake having been up, and nothing in the
system can establish that.

Two measurements set the shape of the work:

- **`keepalive-interval: "45s"`** on the vps ntfy instance (`/opt/ntfy/config/server.yml`). ntfy
  pushes a `keepalive` control frame on that cadence regardless of message traffic, and
  `EventIntake._convert` currently drops every non-`message` frame on the floor. The watchdog is
  therefore built from a frame already being delivered.
- **Cancel-scope behaviour on the real dependency set** (httpx 0.28.1, py3.12.3), probed rather
  than assumed: the feared race between `asyncio.timeout` and httpx's own cancel scopes is
  **refuted**, there is no FD or task leak across repeated trips, and placing the scope too widely
  raises `CancelledError` into the consumer instead of being handled locally.

This change was originally scoped as `sensor-aperture`, pairing liveness with a widening of the
sensor aperture. Adversarial review established that the aperture half rested on a false premise
(there is no Alertmanager, so the "already tuned, just not routed" Prometheus rules deliver
**nowhere**, and routing to Henk would *displace* existing Discord delivery rather than add to
it). The change was split; this is the detection-and-observability half, and it stands on its own
because it fixes the original carry-forward defect. See `scrutiny-findings.md` for the full record
and the remaining three changes.

### Evidence discipline

Every mechanism claim below about Python or library behaviour has been **executed**, not reasoned
about. Probes live in `~/.claude-config/provisioning/henk-probes/` and are re-run on `httpx`
version drift (`httpx>=0.27` is an open bound):

| Probe | Establishes |
|---|---|
| `probe_nested_scope.py` | correct vs wide scope placement in the nested-generator topology |
| `probe_open_starvation.py` | a full-window-per-retrieval budget never fires under an `open` flood (D1) |
| `probe_anchor_trap_v2.py` | a budget established once outside the reconnect loop kills intake permanently (D1) |
| `probe_f6_silent_death.py` | an uncaught `TimeoutError` hangs the consumer forever (D1) |
| `probe_aclose_legality.py` | the broad `finally: await aclose()` is legal; the narrow form leaks (D1) |
| `probe_genexit_boundary.py` | `await` in a `finally` spanning a `yield` is legal; `yield` there is not |

This discipline exists because it was earned: of six such claims checked during review, **four
held and two inverted** — including two that had survived seven prior review rounds as prose.

## Goals / Non-Goals

**Goals:**
- Make a silently dead subscription self-detecting and self-recovering, using frames ntfy already
  sends, with no new dependency and no socket-level options.
- Make silence *checkable* from outside the process, so a future quiet window is evidence rather
  than an assumption.
- Make a connection that opens and then delivers nothing escalate and become visible, rather than
  retrying at a fixed interval forever.
- Emit a stably identifiable trip line, so the deferred owner notice can be derived from
  measurement.

**Non-Goals:**
- No sensor routing changes, no Alertmanager, no new monitoring, no new Gatus endpoints.
- **No owner-notification predicate.** Deliberately split out: its constants cannot be derived
  until the instrument measuring them ships. Build instrument → measure → define.
- **No trip-baseline extraction.** That moved to change D as its task zero; A's obligation is to
  emit the line, D's is to count it.
- No mutating tools, no triage or approval-gate behaviour change.
- No audit-log rotation, and no cadence retuning — this change routes no additional events.
- No connections-established counter. Distinguishing "connects then stalls" from "cannot connect"
  would be a nicer diagnostic, but the falsifiable claim is "intake is or is not delivering,"
  which both shapes answer identically. Recorded as rejected, not missed.
- **No out-of-process query surface for liveness state.** The emissions are the owner-facing
  surface; see D5.

## Decisions

### D1 — The deadline lives in `EventIntake`, as a proof-of-life budget scoped to frame retrieval

An earlier draft put the read deadline in the transport (`httpx.Timeout(None, read=...)` on
`NtfyEventStream`) on the reasoning that httpx already owns per-operation deadlines. **That is
inverted here**, for two reasons, one of which the earlier draft did not state and which is the
stronger:

1. **httpx's read timeout resets on any received bytes.** A peer dribbling newlines, or any
   traffic that is not a complete frame, defeats it — while a per-frame budget still fires, since
   `if line.strip()` means blank lines never satisfy `__anext__`. The application-level frame is
   the unit we actually care about.
2. `NtfyEventStream` is `# pragma: no cover` by design, so a transport-side deadline would be the
   one mechanism here that no test can drive.

Reason 2 is weaker than it looks and must be stated honestly, because it has been revised twice.
`asyncio.timeout` reads the event loop clock and is **not** reachable from `EventIntake`'s
injected `clock`/`sleep`, and `FakeStream` yields frames without awaiting — so without a seam,
"driven by fake-stream tests" is false, and the tests phrased as "well past the deadline" pass in
microseconds while proving nothing. The seam (below) is what makes the claim true, and even with
it, **the seam tests the budget arithmetic and never the cancellation**: a faked timeout raises
without cancelling anything. Cancellation semantics remain probe- and deploy-verified only —
exactly as a transport deadline would have been. That is still a net win, but the honest ledger is:
the seam buys arithmetic coverage, and reason 1 is what actually justifies the location.

**The mechanism.** Two injected seams, substituted **as a pair** (a fake clock with a real
`asyncio.timeout` is incoherent):

- `mono_clock: Callable[[], float] = time.monotonic` — mirroring `EventCoordinator`
  (`coordinator.py:43`, `:51-55`). The budget must be monotonic or an NTP step trips or stalls the
  watchdog; the *displayed* timestamp stays wall-clock.
- `timeout_ctx: Callable[[float], AbstractAsyncContextManager] = asyncio.timeout` — taking a
  **remaining budget** in seconds. Not `asyncio.timeout_at`: an absolute loop time cannot be
  expressed through a relative seam, and it would put the arithmetic out of the tests' reach.

Around frame retrieval only:

```
timeout_ctx(deadline - (mono_clock() - last_proof_of_life_mono))
```

**The wrong implementation is `timeout_ctx(deadline)`** — a full window every retrieval. It
restarts on `open` frames, so it is keyed on *any frame* and **never fires under an `open` flood**
(measured: 40 `open` frames, no trip, ran to the harness bound). Two placement rules, both
measured, both fatal to get wrong:

- **Re-establish the budget immediately before each subscribe call, *after* the backoff sleep.**
  Established once outside the reconnect loop, every post-trip connection inherits an expired
  budget and dies before its first frame — permanently, silently, capped at max backoff (measured:
  zero events after the first trip). Anchored *before* the sleep, a 30s backoff silently eats 30s
  of a 135s window.
- **Advance the anchor for a delivered event *after* its `yield` returns.** Consumer latency is
  not charged against liveness: liveness measures whether the stream is delivering, not how fast
  Henk processes what it delivers. A non-positive budget trips **instantly and silently**
  (measured: `-5.0`, `-0.001` and `0.0` all raise at 0.000s — no `ValueError`, no clamp, no log),
  so charging consumer time would make the slow-consumer test fail for the wrong reason and tempt
  an implementer into widening the scope, which is the trap that test exists to catch. Keepalives
  are never yielded, so they advance the anchor at classification. Both points get a comment.

**The loop must be desugared.** `async for raw in self._stream.subscribe(...)` (`intake.py:103`)
has no syntactic place for a retrieval-scoped timeout, so it becomes an explicit `agen` /
`await agen.__anext__()` loop. The path of least resistance — wrapping the whole `async for` — is
the fatal wide scope and *looks* compliant.

**Five traps, all measured or traced:**

1. **`TimeoutError` is not an `EventStreamError`** (`intake.py:48` is a plain `Exception`), so
   `except EventStreamError` at `:114` does not catch it. Unhandled, it escapes `events()`, kills
   the `_pump` task, and leaves `coordinator.run()` blocked on `await queue.get()` **forever**
   (measured: hung, producer dead with an unretrieved exception). The first trip would silently
   kill intake — strictly worse than the bug being fixed. Normalise at the point it fires with
   `status=None`, so `_is_since_rejection` (`:154-165`, testing `exc.status == 400`) can never
   misread it and `_recoveries` is untouched.
2. **Do not widen `:232` to `except BaseException`.** `httpx.ReadTimeout` is caught by that bare
   `except Exception`, not by the `HTTPStatusError` clause above it — but `CancelledError` is a
   `BaseException` and must escape it, or the watchdog is silently disabled.
3. **Generator cleanup goes in a broad `try/finally: await agen.aclose()`.** `await` inside a
   `finally` spanning a `yield` is **legal**; it is `yield` there that raises
   `RuntimeError: async generator ignored GeneratorExit`. The narrow form — `aclose()` on the
   timeout and error paths only — **leaks** on consumer abandonment, which is the most-travelled
   path (all 19 existing tests `break` out of `events()`) and the only path where `aclose()` has
   real work to do, every other having closed the generator by exception already. The broad
   `finally` only fires when the *outer* generator is closed, which is why `_pump` must hold and
   close it too (D6) and why the leak test must explicitly `aclose()` rather than only `break`.
4. **`StopAsyncIteration` must be caught inside `events()`.** It is itself an async generator, so
   a `StopAsyncIteration` escaping its body becomes
   `RuntimeError: async generator raised StopAsyncIteration`. The current `try/except/else` no
   longer routes clean end to `else:`.
5. **Refactoring `coordinator._pump` to `wait_for` reintroduces the wide-scope trap one level up.**

**Redundant transport floor, adopted.** `httpx.Timeout(None, read=…)` at a multiple of the
liveness deadline, alongside the intake budget. One line, normalises through the existing
`except Exception`, needs no cancellation of a live generator, and downgrades a broken hand-rolled
watchdog from a permanent hang to a bounded reconnect — which after traps 1 and 3 is not
hypothetical, since both were fatal and both survived seven prose review rounds. It **masks**
budget-arithmetic defects at deploy time, so the unit tests stay primary. Decided together with
the dead `open_timeout` (`:207`, assigned and never read; `timeout=None` at `:218` ignores it),
since both concern one `httpx.Timeout` object — and note that `timeout=None` today also means
there is no connect timeout.

**Alternative rejected:** `SO_KEEPALIVE` at the socket level. TCP keepalive detects a dead *peer*,
not a wedged application, its timings are OS-tunable rather than app-tunable, and it says nothing
about whether ntfy is still streaming.

### D2 — Liveness is decoupled from event volume, which is what makes the deadline safe

ntfy's keepalive is unconditional, so an idle healthy connection still receives frames every 45
seconds. A deadline at a multiple of the interval (3× = 135s, three consecutive missed keepalives)
cannot be tripped by a quiet homelab, only by a stream that has stopped.

This couples our config to the server's. The coupling is enforced by a validator, with an honest
limit: it compares Henk's deadline against Henk's **recorded copy** of the interval, so raising
`keepalive-interval` on the vps without updating Henk's config passes validation and flaps the
watchdog. The validator catches the *other* mistake — someone lowering Henk's deadline. The real
drift is addressed by a cross-reference on the vps side and in the homelab docs (task 3.5).
Flapping degrades gracefully rather than losing events (resume is exclusive), but it burns
connections and floods logs.

**The predicate is stated, not implied:** `deadline >= k · interval` with `k` a whole number
greater than one, `k = 3`. A bare `>` check would admit `deadline=60, interval=45` — 1.33×, where
a single late keepalive trips the watchdog.

**Two config sections, deliberately.** The recorded interval describes the **server**
(`endpoints.ntfy.keepalive_interval_seconds`); the deadline describes **Henk's policy**
(`events.liveness_deadline_seconds`). That split is why validation runs **post-assembly** rather
than inside either section's builder. **Naming hazard, verified:**
`endpoints.ntfy.timeout_seconds` already exists (`config.py:245`, default **10.0**, consumed at
`tools/__init__.py:81`) in that same section and is 13× smaller than the deadline — an implementer
wiring "config-driven timeouts" would reach for it and silently invert the ordering. It is **not**
the read timeout.

### D3 — Reset the backoff penalty on any proof-of-life frame, not only on a delivered event

**Load-bearing. Do not drop in isolation** — D4's termination rule depends on it.

Today `attempt = 0` happens only when a `message` frame converts to an `Event`, and the existing
comment says this is the only reset because `attempt` "tracks transport health, not cursor
validity." A keepalive frame *is* transport health, which the current code cannot see because it
discards the frame before the reset point. The latent bug: after a failure raises `attempt` and the
reconnect succeeds into a quiet period, nothing ever resets it — nine days of silence leaves the
counter parked, so the next genuine blip starts at maximum backoff.

**Verified landable, not merely asserted.** Every case that reaches the clean-end branch or
asserts on delays was read: `:101` delivers a message then errors; `:167`'s stream raises
immediately so no frame ever arrives; `:265`'s connections all 400; `:326` and `:308` both use
`RejectingStream(per_cycle=1)`, which yields one message then returns — so both reach the clean-end
branch; `:373` stops collecting before that branch. Decisive: `keepalive` appears in exactly one
test (`:77`), which also sends a `message`. **No test pins backoff surviving a frame-only
reconnect**, so the intent this decision worried about overriding does not exist.

One consequence to fix rather than discover: the comment at `tests/test_event_intake.py:330-332`
claims "`attempt` resets on every delivered event, so all delays are backoff_base and a
delay-value assertion cannot distinguish the two paths." After this change that test's delays
become `[1.0, 2.0, 1.0, 2.0, …]`, so a delay-value assertion **can** distinguish them. Both halves
of the comment fail — it is rewritten, not adjusted.

### D4 — Proof of life is one type-based definition with four named consumers

The concept was previously an inline positional phrase ("a post-`open` frame") at one call site
while three other consumers still said "any frame." An inline phrase cannot be cross-checked: no
name to search for, no declared consumers.

> A **proof-of-life frame** is any frame whose `event` is not `open`. An `open` frame proves a
> connection was accepted; it does not prove the stream is delivering.

**Type-based, not positional** — keyed on the frame's `event` value, not on what preceded it.
Positional phrasing invites classifying frames by position, which is harder to test and wrong for
any stream that never sends `open`.

**Its four consumers, re-derived against the *mechanism*, not the concept** — the distinction that
matters, because the previous table checked the concept and passed a deadline that did not honour
the unit:

| Consumer | Fits? |
|---|---|
| The deadline | **Only under D1's remaining-budget form.** A full window per retrieval restarts on `open` and measurably never fires under an `open` flood. This row is why D1's arithmetic is load-bearing rather than incidental |
| D3's penalty reset | Yes — this is the fix; `open` no longer zeroes `attempt` |
| `last_proof_of_life_at` | Yes, and the name carries the definition (see below) |
| The termination rule | Yes, same unit |

Any future change to the definition must be checked against all four, and any change to one of the
four against the definition — **against its mechanism, not its description.** Compatibility is a
claim to be re-verified, never an exemption.

**Why `open` does not count.** Treating it as proof of life disables backoff for the two shapes
that matter:

| Shape | With `open` as proof of life | With the definition above |
|---|---|---|
| open-then-EOF | reset → EOF → 1.0s → reset → … one reconnect per second, forever | 1, 2, 4, 8, 16, 30, 30 |
| open-then-silence (*the half-open socket*) | reset → trip → 1.0s → reset → … a reconnect every ~136s, never escalating | escalating, and the timestamp goes stale |

Worse, `open` would advance the exposed timestamp, so the observability surface would report a
frame every 1s or 136s while zero events flow — making this change's central claim false by
construction.

**The termination rule, unified.** The clean-end branch takes the backoff path unconditionally; a
proof-of-life frame resets the penalty. No per-connection "did this connection deliver?" flag is
needed — if the implementation grows one, the rule has been misread:

- Healthy stream with keepalives, then a clean end → the keepalive already zeroed the penalty, so
  the clean end costs `backoff_base`. **The observed delay is unchanged from today; the penalty
  counter now advances**, and the next proof-of-life frame zeroes it again. Today's clean end
  sleeps a flat base delay and does not touch `attempt`, so the divergence is real though bounded:
  an ntfy restart is precisely a clean end followed by connect failures, where the next failure now
  costs 2.0s instead of 1.0s. That is arguably better; it is recorded because "bit-identical" was
  claimed and is false.
- `open`-then-EOF or `open`-then-silence → nothing resets → escalating backoff.

This moves the code *toward* the existing standing spec, which already says "When the subscription
drops, Henk SHALL reconnect with backoff" — today's unconditional flat clean-end delay is arguably
already outside that sentence.

**The field is `last_proof_of_life_at`** — **added**, not renamed; `last_frame_at` never existed in
`henk/` or `tests/` and appeared only in this change's own documents across seven review rounds.
Naming it `last_frame_at` while it deliberately excludes a class of frames would reproduce the
name-versus-meaning gap that let "any frame" survive in three consumers.

**Control frames must not advance the cursor.** Today `open`/`keepalive` die at
`if event is None: continue` (`:105-106`) *before* `_last_id` is assigned (`:107`), and this change
moves classification into exactly that region. ntfy control frames carry an `id`, so
`self._last_id = raw.get("id") or self._last_id` is one plausible line from writing a keepalive id
into the resume cursor — which ntfy either 400s (→ retention replay + owner DM) or accepts,
**silently skipping messages**. Accounting reads `raw["event"]` only.

**Test assertions that make the collisions unreintroducible:** the open-flood case trips; the
open-then-EOF case asserts `[1, 2, 4, 8, …]` rather than repeated `1.0`; and a control-frame-only
connection reconnects from the last *message* id.

### D5 — The emissions are the owner-facing surface; the accessor is a seam

An accessor alone does not satisfy an observability requirement. `liveness_state()` is a method on
an in-process object, and there is no status tool, admin command, health endpoint, or coordinator
passthrough — so on rp5 the only way to read it would be a debugger. A requirement whose sole
consumer is `pytest` is not a shipped requirement.

So the **log emissions are the surface**: a one-shot first-proof-of-life line, a coarse periodic
line at a configured interval (sized so a healthy day is a handful of lines, not 1,920), and a
**stably identifiable** trip line. `liveness_state()` remains, as a test seam and a hook for a
future in-process reader, and the requirement claims no more than that.

**Deliberately deferred:** exposing liveness through a read-only tool the owner can ask Henk for
over Signal. That is the honest fulfilment of the original "without manual inspection" wording and
it fits the read-only posture — but it is new tool surface, registration and tests, i.e. a scope
increase in a change whose whole point is to be the small first quarter of a split. It becomes its
own change.

**The trip line is a contract, not a nicety.** Change D's baseline is extracted by matching it, so
it must not depend on incidental message wording — which is a live risk, because the shared backoff
helper this change introduces is one careless edit from erasing the distinction. Note the tension
and that both halves hold: a trip is **behaviourally** indistinguishable from any other transport
failure (same control flow, R1's guarantee) and **observationally** distinguishable (distinct log
identity).

### D6 — Clean-end and shutdown both need an explicit close

Two paths that the current `async for` handles implicitly and an explicit loop does not:

- **The clean-end log.** The backoff path lives inside `except EventStreamError as exc:` and logs
  `"event stream failed (%s)"` — there is **no `exc`** on the clean-end path. So the block becomes
  a shared helper taking a *reason*: clean end logs at INFO with a distinct message, an error end
  keeps WARNING. Without this, every healthy clean end emits "event stream failed", contradicting
  D5's handful-of-lines constraint and seeding exactly the misreading D4 exists to prevent. If the
  helper is reached by synthesizing an `EventStreamError`, it must carry `status=None` or a clean
  end can trigger a full retention replay plus an owner DM.
- **Shutdown.** `_pump` (`coordinator.py:134-136`) creates the intake generator inline via
  `async for` and never holds it, so cancelling the producer leaves `events()` **suspended, not
  closed** — finalised only if the loop reaches `shutdown_asyncgens()`. It must hold the generator
  and `aclose()` it in a `finally`, or D1's trap-3 cleanup is decorative on the one production path
  with a live connection open. `run()`'s `finally` catches only `CancelledError`, while
  `await producer` on an already-failed task re-raises its stored exception — so an escaped
  `TimeoutError` surfaces there.

This makes `coordinator.py` a fifth touched file. Small, and it is what makes trap 3 real rather
than notional.

## Risks / Trade-offs

- **Watchdog flaps if the server's keepalive interval is raised above the deadline** → Deadline at
  3× the measured 45s; the validator catches a lowered *Henk* deadline but **cannot** catch a
  raised *server* interval, so the real mitigation is the cross-reference in task 3.5.
  Reconnect-with-`since` is exclusive, so flapping costs connections and log lines, never events.
- **A penalty on a path the code calls "clean" reads as a bug** → The most surprising behaviour
  change here, which is why D4 records both halves in one place. Without it a future reader reverts
  it as an obvious defect and silently restores the fixed-interval spin.
- **The redundant transport floor masks budget-arithmetic defects at deploy time** → Accepted
  deliberately, because a masked defect plus a bounded reconnect beats an unmasked defect plus a
  permanent hang. The unit tests stay primary; D1 says so.
- **The seam does not cover cancellation** → The faked timeout tests arithmetic only, so the
  preserved probes are the only executable check on the cancellation path. They are re-run on httpx
  drift; that note is a task, not a hope.
- **Bounding the event queue would arm two hazards** → `coordinator.run()`'s queue is unbounded
  today, so the consumer of `events()` never blocks. Bounding it for backpressure would make
  `_pump`'s `put` into consumer time — which, if consumer time were ever charged against the
  budget, becomes spurious trips on a healthy stream. D1 does not charge it; a future reader
  changing that must revisit this row.
- **As-built notes are a leak surface** → The liveness config sits beside live tokens, so notes
  record neither the values **nor prose explaining that a value was withheld**
  (`repo-publication` 3.3: the framing is the leak).

## Migration Plan

Code change, tests, then an owner-run redeploy with `--build` (code is `COPY`'d into the image, so
a plain `up -d` runs stale code). Because `config.yaml` is a **read-only bind mount from the
checkout**, the new config values ship with the image rather than as a host-side edit — commit,
redeploy, keep a revert path. Then read the startup and periodic lines from the running container
to confirm the ~45s cadence, and provoke a real trip by severing the path to ntfy past the
deadline, confirming intake reconnects, resumes from the correct cursor, loses nothing, and is
**still delivering afterwards**.

Rollback is a single redeploy of the previous image. No data migration, no schema change, nothing
to undo in the audit log.

## Open Questions

None blocking. Both prior questions are resolved:

- **The deadline multiple is 3× (135s).** Previously deferred to a deploy-time jitter observation.
  Taken by decision instead: the risk is asymmetric — too low flaps the watchdog, too high detects
  in 135s instead of 90s, which is immaterial for a homelab watchdog — so blocking all code on an
  owner-run measurement to choose between them was a false dilemma. Held honestly: no jitter data
  for ntfy keepalive precision under load exists, so 3× is a conservative default, which is what D2
  claims it is.
- **The periodic-emission interval is a config field** (`events`, beside the deadline), not a
  hardcoded constant. It was previously tested by a task while having no value and no home. Sized
  so a healthy day yields a handful of lines rather than 1,920.
