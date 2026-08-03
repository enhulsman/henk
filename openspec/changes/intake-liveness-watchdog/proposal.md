## Why

Henk's event pipeline is built, durable, and verified, but nothing it reports can currently
be trusted as an observation. `NtfyEventStream` opens its subscription with
`httpx.AsyncClient(timeout=None)` and no read deadline, so a half-open socket hangs
undetected: "the homelab was quiet" and "intake died three days ago" produce the identical
observable, which is *nothing*.

That is not hypothetical. The first-week watch recorded zero real events in nine days
(`henk-events` 5.4), and that silence was only trustworthy because the owner verified
continuous subscription by hand, per trigger family. Every future conclusion drawn from a
quiet window rests on intake having been up, and there is currently no way to check.

The liveness signal already arrives and is thrown away. ntfy interleaves `keepalive` control
frames into the JSON stream (measured on the vps instance: `keepalive-interval: "45s"`), and
`EventIntake._convert` discards every non-`message` frame silently. A watchdog can be built
from a frame that is already being delivered, with no socket-level options and no new
dependency.

A second, subtler defect surfaced while scoping this: the intake's backoff penalty resets
only when a `message` frame converts to an `Event`, and its clean-end branch always waits a
flat base delay. A connection that is accepted and then delivers nothing — the primary
half-open-socket shape — therefore reconnects at a fixed interval forever, never escalating
and never becoming visible. Closing the liveness gap without closing this one would leave the
watchdog's own trips invisible.

## What Changes

- **Intake liveness watchdog.** A deadline scoped to *frame retrieval* inside `EventIntake`
  declares the connection dead and forces a reconnect when no proof-of-life frame arrives in
  time. Reconnection reuses the existing backoff and `since` machinery, so a watchdog trip is
  indistinguishable from any other transport failure downstream and cannot lose events.
- **One definition of proof of life, with its consumers named.** Any frame whose `event` is
  not `open` proves the stream is delivering; an `open` frame does not. The deadline, the
  backoff reset, the last-proof-of-life timestamp, and the termination rule are all keyed to
  that one unit, stated in one place with its consumers listed, so the four cannot drift apart.
- **A termination rule that escalates.** A connection ending without a proof-of-life frame
  takes the backoff path whether it ended cleanly or with an error; a proof-of-life frame
  resets the penalty. A healthy stream's clean end stays bit-identical to today's flat base
  delay, while open-then-silence escalates and goes stale visibly.
- **Observable silence.** A named `liveness_state()` accessor reports last proof-of-life, last
  reconnect, and current penalty, plus a healthy-path emission the owner can actually read at
  deploy time. This is what makes any later quiet window interpretable.
- **A stably identifiable trip line.** Trips are logged so their count and spacing can be
  recovered later by matching a stable identifier rather than incidental message wording. That is
  the input the deferred owner-notification predicate needs; extracting and counting it belongs to
  that change, not this one.

Not in scope: no sensor routing changes, no Alertmanager, no new monitoring, no mutating
tools, no owner-notification predicate, no trip-baseline extraction, and no change to how triage
or the approval gate behave. This change builds the instrument; it does not widen the aperture and
it moves no existing signal.

**What "liveness" does and does not cover.** This makes the *subscription* checkable. A pipeline
wedged downstream of intake would still leave liveness reporting perfect health, so the guarantee
is "frames are arriving," not "events are being handled." Stated so it is not over-read later.

## Capabilities

### New Capabilities
None. Liveness was initially scoped as a separate `intake-liveness` capability and
deliberately folded into `event-intake` instead: its subject is the same outbound
subscription the existing `event-intake` requirements already govern, so a standalone spec
would contain requirements that cannot be read without `event-intake` open beside them,
against the flat-spec principle. Splitting would also mean every future change to
subscription behaviour has to modify two specs, widening the archive-ordering hazard for no
benefit.

### Modified Capabilities
- `event-intake`: Gains requirements that a subscription delivering no proof-of-life frame is
  treated as failed rather than healthy, that the liveness deadline is ordered against the
  server's keepalive interval and that violating configuration is refused at load time, and
  that liveness state is observable through a named accessor so genuine silence is
  distinguishable from a hung socket. Existing resume, replay, and since-rejection-recovery
  requirements are unchanged.

## Impact

- **Code, five files:** `henk/events/intake.py` (the proof-of-life budget scoped to frame
  retrieval, two injected seams, the desugared loop, accounting, the unified termination rule, the
  emissions); `henk/events/coordinator.py` (`_pump` must hold and close the intake generator, or the
  cleanup is decorative on the one production path with a live connection open); `henk/config.py`
  (new liveness fields — none exist today — plus a post-assembly cross-section ordering validator);
  `henk/runtime.py` at the two construction sites; and `tests/test_event_intake.py`, which needs a
  harness extension before its new cases can assert anything, alongside the existing 19.
- **Deployed config:** `config.yaml` gains the new fields. It is a **read-only bind mount from the
  checkout** on rp5, so the values ship with the image rather than as a host-side edit.
- **A behaviour change to a path the code calls clean.** Applying a penalty to a clean stream
  end is the most surprising thing here, which is why it carries its own decision record: a
  future reader who found it unexplained would read it as a bug and revert it, silently
  restoring the fixed-interval spin.
- **Config naming hazard.** `endpoints.ntfy.timeout_seconds` already exists (default 10.0) in
  the same section as the stream's `base_url` and is an order of magnitude below the liveness
  deadline. The new fields are named distinctly, and the stream read timeout is explicitly
  **not** that field.
- **No new dependencies.** The watchdog uses frames ntfy already sends.
- **Deploy is owner-run.** rp5 sudo is a read-only docker allowlist, and the image `COPY`s
  code rather than bind-mounting it, so verification requires an owner-run redeploy with
  `--build`.
- **Cadence is untouched.** No additional events are routed, so debounce, cooldown, and the
  daily cap see exactly the load they see today.
