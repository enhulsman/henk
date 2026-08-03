## Context

The event pipeline is complete and verified end-to-end on rp5, but it has processed zero
real events since deploy-verify. `henk-events` 5.4 closed on that finding and could only
trust it because the owner verified continuous subscription by hand, per trigger family.

The constraint that makes this change come first: **intake liveness is currently
unfalsifiable.** With `httpx.AsyncClient(timeout=None)` and no read deadline, a half-open
socket produces exactly the same observable as a healthy quiet tailnet, which is *nothing*.
Any conclusion drawn from an absence of events rests on intake having been up, and nothing
in the system can establish that.

Two measurements set the shape of the work:

- **`keepalive-interval: "45s"`** on the vps ntfy instance (`/opt/ntfy/config/server.yml`).
  ntfy pushes a `keepalive` control frame on that cadence regardless of message traffic,
  and `EventIntake._convert` currently drops every non-`message` frame on the floor. The
  watchdog is therefore built from a frame already being delivered.
- **Cancel-scope behaviour on the real dependency set** (httpx 0.28.1, py3.12.3), probed
  rather than assumed: the feared race between `asyncio.timeout` and httpx's own cancel
  scopes is **refuted**, there is no FD or task leak across repeated trips, and placing the
  scope in the wrong position raises `CancelledError` into the consumer instead of being
  handled. Probes preserved at `~/.claude-config/provisioning/henk-probes/`. `httpx>=0.27`
  is an open bound, so these are re-run on version drift.

This change was originally scoped as `sensor-aperture`, pairing liveness with a widening of
the sensor aperture. Adversarial review established that the aperture half rested on a false
premise (there is no Alertmanager, so the "already tuned, just not routed" Prometheus rules
deliver **nowhere**, and routing to Henk would *displace* existing Discord delivery rather
than add to it). The change was split; this is the detection-and-observability half, and it
stands on its own because it fixes the original carry-forward defect. See
`scrutiny-findings.md` for the full record and the remaining three changes.

## Goals / Non-Goals

**Goals:**
- Make a silently dead subscription self-detecting and self-recovering, using frames ntfy
  already sends, with no new dependency and no socket-level options.
- Make silence *checkable*: liveness state readable through a named accessor, and a healthy
  stream readable at deploy time, so a future quiet window is evidence rather than an
  assumption.
- Make a connection that opens and then delivers nothing escalate and become visible, rather
  than retrying at a fixed interval forever.
- Produce a trip baseline that a later owner-notification predicate can be derived from.

**Non-Goals:**
- No sensor routing changes, no Alertmanager, no new monitoring, no new Gatus endpoints.
- **No owner-notification predicate.** Deliberately split out: its constants cannot be
  derived until the instrument that measures them ships. Build instrument → measure → define.
- No mutating tools, no triage or approval-gate behaviour change.
- No audit-log rotation, and no cadence retuning — this change routes no additional events,
  so cadence sees exactly the load it sees today.
- No connections-established counter. Distinguishing "connects then stalls" from "cannot
  connect at all" would be a nicer diagnostic, but the falsifiable claim here is "intake is
  or is not delivering," which both shapes answer identically. Recorded as rejected, not
  missed.

## Decisions

### D1 — The deadline lives in `EventIntake`, scoped to frame retrieval

An earlier draft put the read deadline in the transport (`httpx.Timeout(None, read=...)` on
`NtfyEventStream`) on the reasoning that httpx already owns per-operation deadlines and a
read timeout normalises into the existing `EventStreamError` path. **That is inverted here,
and the inversion is the single most important correction in this design.**

`NtfyEventStream` is `# pragma: no cover` by design — it needs a live ntfy — so a
transport-side deadline would be the one mechanism in the change that no test can drive. The
watchdog would ship deploy-verified only, which for the component whose entire purpose is to
make silence checkable is the wrong trade. `EventIntake`, by contrast, is driven by
fake-stream tests, so a deadline there is unit-testable in every shape that matters.

The deadline is therefore an `asyncio.timeout` in `EventIntake`, **scoped to frame retrieval
only** — around obtaining the next frame, not around the whole subscription and not around
the consumer's handling of a frame. Scope placement is load-bearing: probing established that
a scope placed too widely raises `CancelledError` into the consumer rather than being handled
locally. A timeout expiry is normalised to the existing failure path with `status=None`, so
it flows through backoff-and-resume exactly as a transport error does and is
indistinguishable downstream.

**Two traps recorded because both are easy to walk into:**

- `httpx.ReadTimeout` is caught by the bare `except Exception` at `intake.py:232`, **not** by
  the `httpx.HTTPStatusError` clause above it. `CancelledError` is a `BaseException`, so it
  correctly escapes that clause — but widening it to `except BaseException` to "be safe"
  would swallow the watchdog's own cancellation and silently disable the mechanism.
- Refactoring `coordinator._pump` to use `wait_for` would reintroduce the same wrong-scope
  trap one level up. The deadline belongs at frame retrieval, once.

**Alternative rejected:** `SO_KEEPALIVE` at the socket level, which the original
carry-forward note suggested. TCP keepalive detects a dead *peer*, not a wedged application,
its timings are OS-tunable rather than app-tunable, and it would say nothing about whether
ntfy is still streaming. The application-level frame is strictly more informative.

### D2 — Liveness is decoupled from event volume, which is what makes the deadline safe

The reason a deadline is legitimate here at all: ntfy's keepalive is unconditional, so an
idle healthy connection still receives frames every 45 seconds. A deadline set at a multiple
of the interval (3× = 135s, so three consecutive missed keepalives) cannot be tripped by a
quiet homelab, only by a stream that has actually stopped.

This couples our config to the server's. If `keepalive-interval` on the vps is ever raised
above the deadline, the watchdog flaps: it reconnects every deadline on a perfectly healthy
system. That failure degrades gracefully rather than losing events (resume is exclusive, so a
reconnect with `since` delivers no duplicates), but it burns connections and floods logs. The
coupling is therefore a **spec obligation enforced by a config validator**, not a comment:
the recorded server interval lives in config beside the deadline, and a violating ordering is
refused at load time.

**Naming hazard, verified:** `endpoints.ntfy.timeout_seconds` already exists
(`config.py:245`, default **10.0**, consumed by the notify tool at `tools/__init__.py:81`) and
sits in the *same config section* as the stream's `base_url`. It is 13× smaller than the 135s
deadline, so an implementer wiring "config-driven timeouts" would reach for it and silently
invert the ordering invariant. The new fields are named distinctly, and the stream read
timeout is **not** that field.

### D3 — Reset the backoff penalty on any proof-of-life frame, not only on a delivered event

**Load-bearing. Do not drop in isolation** — see D4, which depends on it.

Today `attempt = 0` happens only when a `message` frame converts to an `Event`, and the
existing comment is explicit that this is the only reset because `attempt` "tracks transport
health, not cursor validity." A keepalive frame *is* transport health, which the current code
cannot see because it discards the frame before the reset point.

The bug this fixes is latent but real: after a transport failure raises `attempt` and the
reconnect succeeds into a quiet period, nothing ever resets it. Nine days of silence leaves
the counter parked, so the next genuine blip starts at maximum backoff instead of the base
delay. Once frames are being counted for the watchdog, resetting on any proof-of-life frame
is nearly free and strictly more correct.

**This changes behaviour existing tests were written against**, so it is resolved by reading
them first (task 0.1). Two were checked while scoping, and both are safe:
`test_persistent_failure_backs_off_without_crashing` drives a stream that raises immediately,
delivering no proof-of-life frame, so its `[1, 2, 4, 8]` assertion is unaffected; and
`test_first_recovery_reconnects_without_sleeping` stops collecting before the clean-end branch
is reached, so `slept == []` still holds. If any test turns out to deliberately pin backoff
surviving a frame-only reconnect, that intent wins and this decision is dropped rather than
the test weakened.

**A stale comment this creates:** `tests/test_event_intake.py:330-332` states that "`attempt`
resets on every delivered event, so all delays are backoff_base," and uses that fact to
justify why an interleaved trace is the only unambiguous discriminator in that test. The
reasoning survives; the stated unit does not. Left unfixed it is a comment asserting the old
reset unit next to code implementing the new one — the same name-versus-meaning gap D4 exists
to prevent — so updating it is an explicit task, not a drive-by.

### D4 — Proof of life is one type-based definition with four named consumers

This decision exists because the concept was previously expressed as an inline positional
phrase ("a post-`open` frame") at one call site while three other consumers still said "any
frame." An inline phrase cannot be cross-checked: it has no name to search for and no declared
consumer list. Naming it, and listing what depends on it, is what makes a future collision
findable.

> A **proof-of-life frame** is any frame whose `event` is not `open`. An `open` frame proves a
> connection was accepted; it does not prove the stream is delivering.

The definition is **type-based, not positional** — keyed on the frame's `event` value rather
than on its position relative to an `open` frame. Positional phrasing invites a literal
implementation that classifies frames by what preceded them, which is both harder to test and
wrong for any stream that never sends `open` at all.

**Its four consumers, named:** the deadline; D3's backoff-penalty reset; the
last-proof-of-life timestamp; and the termination rule below. Any future change to the
definition must be checked against all four, and any change to one of the four must be checked
against the definition. Compatibility is a claim to be re-verified, never an exemption.

**Why `open` does not count.** With `open` treated as proof of life, D3's reset fires on
connection acceptance, which disables backoff entirely for the two failure shapes that matter:

| Shape | With `open` as proof of life | With the definition above |
|---|---|---|
| open-then-EOF | reset → EOF → 1.0s → reset → … one reconnect per second, forever | 1, 2, 4, 8, 16, 30, 30 — escalating |
| open-then-silence (*the half-open socket*) | reset → deadline trips → 1.0s → reset → … a reconnect every ~136s, forever, never escalating | escalating, and the timestamp goes stale |

Worse, in both shapes `open` would advance the exposed last-frame timestamp, so the
observability surface would report a frame every 1s or 136s while zero events flow — making
this change's central claim, that silence is checkable, false by construction.

**The termination rule, unified.** The clean-end branch takes the backoff path
unconditionally; a proof-of-life frame resets the penalty. No per-connection "did this
connection deliver?" flag is needed, and the two behaviours fall out:

- Healthy stream with keepalives, then a clean end → the keepalive already zeroed the penalty,
  so the clean end costs `backoff_base`, **bit-identical to today's flat 1s**.
- `open`-then-EOF or `open`-then-silence → nothing resets → escalating backoff.

This also moves the code *toward* the existing standing spec, which already says "When the
subscription drops, Henk SHALL reconnect with backoff" — today's unconditional flat 1s
clean-end path is arguably already outside that sentence.

**The field is renamed `last_proof_of_life_at`.** Calling it `last_frame_at` while it
deliberately excludes a class of frames reproduces exactly the name-versus-meaning gap that
let "any frame" survive in three consumers. The name carries the definition so the collision
cannot recur silently.

**Test assertion that makes the collision unreintroducible:** the open-then-EOF test asserts
the delay sequence `[1, 2, 4, 8, …]`, not repeated `1.0`. A future reader who reintroduces
`open` as proof of life fails that test rather than shipping a silent 1/s spin.

## Risks / Trade-offs

- **Watchdog flaps if the server's keepalive interval is raised above the deadline** →
  Deadline set at 3× the measured 45s, the ordering enforced by a config validator rather than
  documented, and the measured interval recorded in as-built notes. Reconnect-with-`since` is
  exclusive, so flapping costs connections and log lines, never events.
- **D3 contradicts an existing tested assertion** → Read the backoff tests before touching the
  code (task 0.1). Two were already verified landable while scoping. If any test deliberately
  pins backoff surviving a frame-only reconnect, that intent wins and D3 is dropped rather than
  the test weakened. Note that D3 is now load-bearing for D4's termination rule, so dropping it
  is not a local edit.
- **A penalty on a path the code calls "clean" reads as a bug** → It is the most surprising
  behaviour change here, which is why D4 records it with both halves of the reasoning in one
  place. Without that record a future reader would revert it as an obvious defect and silently
  restore the fixed-interval spin.
- **As-built notes are a leak surface** → The liveness config sits beside live tokens, so notes
  record neither the values **nor prose explaining that a value was withheld**
  (`repo-publication` 3.3: the framing is the leak).

## Migration Plan

Code change, tests, then an owner-run redeploy with `--build` (code is `COPY`'d into the image,
so a plain `up -d` runs stale code), then read `liveness_state()` and the healthy-path emission
from the running container to confirm frames arrive on the ~45s cadence. Then provoke a real
trip by severing the path to ntfy for longer than the deadline, and confirm intake reconnects,
resumes from the correct cursor, and loses nothing.

Rollback is a single redeploy of the previous image. No data migration, no schema change,
nothing to undo in the audit log. Config gains fields with defaults, so an un-updated
`config.yaml` keeps working — note that `config.yaml` on rp5 is a read-only bind mount from the
checkout, so any config change is owner-run plus restart plus revert.

## Open Questions

- Is 3× (135s) the right deadline multiple, or is 2× (90s) enough? 3× is the conservative
  default; a deploy-time observation of keepalive jitter confirms before it is fixed in config.
- What interval makes the periodic healthy-path line coarse enough to be readable but frequent
  enough to be useful? Bounded by "a healthy day is a handful of lines, not 1,920."
