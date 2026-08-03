## Context

The event pipeline is complete and verified end-to-end on rp5, but it has processed zero
real events since deploy-verify. `henk-events` 5.4 closed on that finding: the cadence
defaults from D6 (debounce 120s, cooldown 6h, recurrence 24h, cap 3/24h) have never met
real data, so the cadence requirement is met vacuously.

Three measurements taken while scoping this change set the shape of the work:

- **`keepalive-interval: "45s"`** on the vps ntfy instance (`/opt/ntfy/config/server.yml`).
  ntfy pushes a `keepalive` control frame on that cadence regardless of message traffic,
  and `EventIntake._convert` currently drops every non-`message` frame on the floor.
- **23 Prometheus alert rules** exist on the vps across `health.rules.yml`,
  `infrastructure.rules.yml`, and `adguard.rules.yml`. Exactly four families route to the
  events topic. `InstanceDown`, `ContainerRestarting`, `HighCPU`, `HighMemory`, six
  per-device DNS processing-time rules, and the Obsidian and rotate backup rules do not.
- **9 of 18 Gatus endpoints carry no `alerts:` block**, and the nine that do run at
  `failure-threshold: 2` (ten blocks) or `5` (eight blocks). 5.4's audit found every real
  failure in the window was an isolated single check, below the threshold of 2.

The constraint that orders the work: intake liveness is currently unfalsifiable. With
`httpx.AsyncClient(timeout=None)` and no read deadline, a half-open socket produces exactly
the same observable as a healthy quiet tailnet, which is *nothing*. Any conclusion drawn
from a wider aperture rests on intake having been up, so liveness lands first.

## Goals / Non-Goals

**Goals:**
- Make a silently dead subscription self-detecting and self-recovering, using frames ntfy
  already sends, with no new dependency and no socket-level options.
- Make silence *checkable*: last-frame and last-reconnect state readable at deploy time, so
  a future quiet window is evidence rather than an assumption.
- Route more of the signal the homelab already produces into the events topic, preserving
  the deny-by-default allowlist shape rather than inverting it.
- Produce non-vacuous cadence data, or an honest bounded finding that the infrastructure
  does not generate enough incidents to tune against.

**Non-Goals:**
- No new monitoring is deployed. No Alertmanager, no new exporters, no new Gatus endpoints.
- No mutating tools, no triage or approval-gate behaviour change.
- Not an open-ended aperture programme. Two tranches, then a written conclusion either way.
- No audit-log rotation in this change, though it may be identified as a prerequisite for
  tranche 2 (see risks).

## Decisions

### D1 — The read deadline lives in the transport; the observability lives in the intake

`NtfyEventStream` gets a bounded read timeout (`httpx.Timeout(None, read=<deadline>)`)
rather than `EventIntake` racing the async iterator against a timer. httpx already owns
per-operation deadlines, a read timeout normalises into the existing
`httpx.HTTPError → EventStreamError` path, and the intake's backoff-and-resume loop then
handles it as it handles any transport failure. Wrapping `__anext__` in `asyncio.wait_for`
inside the intake would reimplement that machinery in the layer that least needs it.

The cost is that `NtfyEventStream` is `# pragma: no cover` by design (it needs a live ntfy),
so the mechanism itself is deploy-verified, not unit-tested. That is why the *observable*
half goes in `EventIntake`, which the fake-stream tests do drive: intake records the
timestamp of the last frame of any kind and of the last reconnect, and exposes them. The
tests assert the accounting; the deploy verifies the deadline actually fires.

**Alternative rejected:** `SO_KEEPALIVE` at the socket level, which the carry-forward note
originally suggested. TCP keepalive detects a dead *peer*, not a wedged application, its
timings are OS-tunable rather than app-tunable, and it would tell us nothing about whether
ntfy is still streaming. The application-level frame is strictly more informative.

### D2 — Liveness is decoupled from event volume, which is what makes the deadline safe

The reason a read deadline is legitimate here at all: ntfy's keepalive is unconditional, so
an idle healthy connection still receives frames every 45 seconds. A deadline set at a
multiple of the interval (3× = 135s, so three consecutive missed keepalives) cannot be
tripped by a quiet homelab, only by a stream that has actually stopped.

This couples our config to the server's. If `keepalive-interval` on the vps is ever raised
above the deadline, the watchdog flaps: it reconnects every deadline on a perfectly healthy
system. That failure degrades gracefully rather than losing events (resume is exclusive, so
a reconnect with `since` delivers no duplicates), but it burns connections and floods logs.
The coupling is therefore recorded as a spec obligation, not just a comment, and the
measured interval is written into the as-built notes.

### D3 — Reset backoff on any frame, not only on a delivered event

Today `attempt = 0` happens only when a `message` frame converts to an `Event`, and the
existing comment is explicit that this is the only reset because `attempt` "tracks transport
health, not cursor validity." A keepalive frame *is* transport health, which the current code
cannot see because it discards the frame before the reset point.

The bug this fixes is latent but real: after a transport failure raises `attempt` and the
reconnect succeeds into a quiet period, nothing ever resets it. Nine days of silence leaves
the counter parked, so the next genuine blip starts at maximum backoff instead of the base
delay. Once frames are being counted for the watchdog, resetting on any frame is nearly
free and strictly more correct.

**This changes behaviour the existing tests were written against.** The 19 tests in
`tests/test_event_intake.py` include backoff-progression cases, and at least one may assert
persistence across an event-free reconnect. Those tests define the contract, so the change
is only correct if the assertions turn out to be incidental to event-vs-frame; if any test
deliberately pins backoff surviving a frame-only reconnect, that intent wins and D3 is
dropped. Resolve by reading the tests before touching the code, not after.

### D4 — Widening preserves the allowlist; it does not invert it

`sensor-routing` currently carries a scenario asserting that a non-curated alert publishes
nothing to the events topic. That deny-by-default posture is deliberate and inherited from
the security charter, so widening extends the *membership* of an explicit allowlist and must
not become "route everything except." The requirement text changes from a hardcoded list of
four families to a named, enumerated, still-closed set.

### D5 — Two tranches, high-signal first, second one conditional

Opening everything at once would produce cadence data with no attribution: if a week yields
forty events, we could not say which family drove the debounce batches. Staging costs
calendar time but buys causality.

**Tranche 1 (high signal, already tuned):** `InstanceDown` (the highest-value addition, and
the reason not to add Gatus alerts for the three node exporters separately, since it already
covers every scrape target rather than only the endpoints Gatus probes), `ContainerRestarting`,
the six DNS processing-time rules (already tuned to measured per-device baselines, which is
exactly the property wanted), and the Obsidian and rotate backup rules as adjacent members
of the already-curated backup-freshness family. On the Gatus side: both tunnels, `hulsman.dev`,
and the three node exporters.

**Tranche 2 (noisier, only if tranche 1 stays quiet):** `HighCPU`, `HighMemory`, and the
low-severity Gatus endpoints (AdGuard's web UI, the Docker registry, Gokapi). These are the
classic false-positive generators and the first candidates for removal if they dominate.
Making tranche 2 conditional is the honest position: we do not know yet whether tranche 1
is sufficient.

### D6 — Sensitivity is one small experiment, not a global threshold drop

The 5.4 finding says isolated single failures are real signal being filtered out. The
tempting move is `failure-threshold: 1` everywhere, which converts eighteen endpoints into a
firehose on the strength of one inference.

Instead, drop to 1 on the two tunnels only. A tunnel flap is genuinely user-visible and
single-occurrence-meaningful, debounce (120s) collapses any resulting storm into one batch,
and cooldown prevents re-fire, so the blast radius is one identity. If single-failure signal
proves real there, the finding generalises with evidence behind it. Everything else keeps
its current threshold.

### D7 — Watchdog trips recover silently; only a *persistently* failing stream notifies

A single trip is expected occasionally: Tailscale re-keying, an ntfy restart, a home
connection blip. Notifying on each would DM-storm the owner over self-healing events, which
is the failure mode the cap and cooldown exist to prevent elsewhere.

Mirror the `_recoveries` idiom already used for since-rejection: recover silently and log,
and send a single one-shot owner notice once consecutive trips without an intervening
healthy interval exceed a small bound. Reusing an existing, already-reasoned bounded-notice
pattern beats inventing a second notification policy in the same module.

### D8 — The watch window has a bounded, falsifiable success condition

5.4 nearly dangled as an open-ended watch. This one states its exit up front: within 14
days, either at least one real cooldown suppression **and** at least one multi-event
debounce batch from non-probe events, or a written finding that a three-device homelab in
steady state does not produce enough incidents to tune cadence empirically, with the D6
defaults standing as reasoned defaults and that fact documented rather than hidden.

The anti-goal is an aperture treadmill where every quiet window justifies opening wider.
Two tranches, then a conclusion.

## Risks / Trade-offs

- **Watchdog flaps if the server's keepalive interval is raised above the deadline** →
  Deadline set at 3× the measured 45s, coupling documented as a spec obligation and recorded
  in as-built notes; reconnect-with-`since` is exclusive so flapping costs connections and
  log lines, never events.
- **D3 contradicts an existing tested assertion** → Read the backoff tests first; if any test
  deliberately pins backoff surviving a frame-only reconnect, that intent wins and D3 is
  dropped rather than the test weakened.
- **Flood into an unrotated audit log** → The daily cap protects the Signal channel, but every
  suppressed event still writes a suppression record, and the durability design already flagged
  the audit log as unrotated. Watch log growth during tranche 1; if the volume warrants, log
  rotation becomes a prerequisite for tranche 2 rather than an afterthought.
- **Sensor edits are owner-run and outside the repo** → Grafana has no scoped token, only the
  root-held admin password, and rp5 sudo is a read-only docker allowlist. Every deployed-config
  step is marked owner-run in tasks, and the change cannot claim completion on agent action
  alone.
- **As-built notes are a leak surface** → Both the Gatus config and the Grafana contact point
  carry live tokens inline. Notes record neither the values nor prose explaining that a value
  was withheld, per `repo-publication` 3.3: the framing is the leak.
- **Tranche 1 may still be quiet** → That is an accepted outcome with a written home in D8,
  not a reason to widen further.

## Migration Plan

Liveness ships first and alone, so that any later quiet window is interpretable: code change,
tests, redeploy with `--build`, then confirm from the container that frames are arriving on
the 45s cadence and that last-frame state advances. Only then tranche 1, Grafana policy and
Gatus config in one owner-run pass, followed by the 14-day watch.

Rollback is per-layer and independent: revert the Gatus config file and restart the container;
remove the added matchers from the Grafana notification policy; redeploy the previous image for
the watchdog. No data migration, no schema change, nothing to undo in the audit log.

## Open Questions

- Is 3× the right deadline multiple, or is 2× (90s) enough? 3× is the conservative default;
  a deploy-time observation of keepalive jitter should confirm before it is fixed in config.
- Does any existing backoff test pin the event-only reset (D3)? Decided by reading, not guessing.
- Does tranche 1's volume push audit-log rotation ahead of tranche 2?
