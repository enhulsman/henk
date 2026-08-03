## 0. Resolve the open questions before writing code

- [ ] 0.1 Read the backoff-progression cases in `tests/test_event_intake.py` and decide D3
  (reset backoff on any frame, not only on a delivered event). If any test deliberately pins
  backoff surviving a frame-only reconnect, that intent wins and D3 is dropped — record which
  way it went and why, in this task.
- [ ] 0.2 Confirm the liveness deadline multiple. Observe actual keepalive arrival jitter
  against the measured `keepalive-interval: "45s"` on the vps instance before fixing the
  deadline in config; 3× (135s) is the conservative default, 2× (90s) only if jitter is tight.
- [ ] 0.3 Confirm `httpx.ReadTimeout` reaches `EventStreamError`. Verify the existing
  `except` clause in `NtfyEventStream.subscribe` actually catches it (it should, via
  `httpx.HTTPError`), because D1's whole mechanism depends on that normalisation already
  being in place. If it does not, widening that clause is part of task 2.1.

## 1. Tests first, from the spec scenarios

- [ ] 1.1 Silent stream is abandoned: a fake stream that yields nothing past the deadline
  causes a reconnect that resumes from the last-seen id.
- [ ] 1.2 Keepalive frames alone keep a quiet subscription healthy: a stream yielding only
  `keepalive` control frames well past the deadline triggers no reconnect. This is the test
  that proves liveness is decoupled from event volume (D2).
- [ ] 1.3 Deadline-versus-interval invariant: the configured deadline is asserted to be a
  multiple greater than one of the recorded server keepalive interval, so a future config
  edit that would make the watchdog flap fails the suite instead of production.
- [ ] 1.4 Single trip recovers quietly: one trip, healthy reconnect, logged, no owner notice.
- [ ] 1.5 Repeated trips notify once: consecutive trips past the bounded threshold send
  exactly one notice, and further trips while the condition persists send none.
- [ ] 1.6 Quiet period is verifiable: last-frame and last-reconnect state advance on
  keepalive frames and are readable after an event-free interval.
- [ ] 1.7 Liveness trip preserves the checkpoint: a trip must not discard or rewind the
  cursor, and must not be mistaken for a since-rejection. Guards the interaction between the
  new path and the durability work.
- [ ] 1.8 Confirm all new tests fail for the right reason before implementing.

## 2. Liveness implementation

- [ ] 2.1 Bounded read deadline in `NtfyEventStream` via `httpx.Timeout(None, read=...)`,
  deadline and its derivation from the server interval carried in config rather than
  hardcoded.
- [ ] 2.2 Frame-level liveness accounting in `EventIntake`: count every frame including
  control frames, record last-frame and last-reconnect timestamps, expose them.
- [ ] 2.3 Bounded consecutive-trip counter and one-shot owner notice, mirroring the existing
  `_recoveries` idiom rather than introducing a second notification policy in the module.
- [ ] 2.4 Apply or drop D3 per the 0.1 decision.
- [ ] 2.5 Full suite green (233 existing plus the new cases), `openspec validate --all` clean.

## 3. Ship liveness alone and verify it on rp5

- [ ] 3.1 **Owner-run:** redeploy with `--build` (code is `COPY`'d into the image, so a plain
  `up -d` runs stale code).
- [ ] 3.2 Confirm from the running container that frames arrive on the ~45s cadence and that
  last-frame state advances while no events exist. This is the observation that makes every
  later quiet window interpretable, so it must be recorded, not just seen.
- [ ] 3.3 Provoke a real trip — sever the path to ntfy long enough to exceed the deadline —
  and confirm intake reconnects, resumes from the correct cursor, loses nothing, and stays
  silent on the owner channel for a single trip.
- [ ] 3.4 Record the as-built liveness config. **No token values, and no prose stating that a
  value was withheld** (`repo-publication` 3.3: the framing is the leak).

## 4. Tranche 1 — high-signal routing, owner-run

- [ ] 4.1 Add to the Grafana notification policy, preserving the closed-allowlist shape:
  `InstanceDown`, `ContainerRestarting`, the six per-device DNS processing-time rules, and
  the Obsidian and rotate backup rules. `InstanceDown` is deliberately chosen over adding
  Gatus alerts for the three node exporters, since it already covers every scrape target.
- [ ] 4.2 Gatus alerting for both tunnels, `hulsman.dev`, and the three node exporters,
  cloned-alert-plus-`provider-override` pattern, thresholds by severity.
- [ ] 4.3 The D6 sensitivity experiment: `failure-threshold: 1` on the two tunnels only, with
  the rationale recorded. Everything else keeps its current threshold.
- [ ] 4.4 Record the route-or-not decision for **every** Gatus endpoint and **every**
  Prometheus rule, including the ones deliberately excluded. The specs require the decision
  to exist, not merely the coverage.
- [ ] 4.5 Verify the allowlist still denies by default: confirm a non-curated rule firing
  publishes nothing to the events topic.
- [ ] 4.6 Confirm end-to-end that one tranche-1 source actually reaches triage, rather than
  assuming the routing works because the config parsed.

## 5. Watch window — the point of the whole change

- [ ] 5.1 Fourteen days from tranche 1 landing. Success is non-vacuous cadence data: at least
  one real cooldown suppression **and** at least one multi-event debounce batch, both from
  non-probe events. Anything else is a documented finding, not a reason to widen (D8).
- [ ] 5.2 Watch audit-log growth. Every suppressed event writes a record and the log is
  unrotated; if volume warrants, rotation becomes a prerequisite for tranche 2 rather than an
  afterthought.
- [ ] 5.3 Verify from liveness state that the window's silence or activity is genuine, using
  3.2's accounting rather than a manual per-family audit as 5.4 needed.
- [ ] 5.4 Revisit the D6 cadence defaults against whatever data exists. If the data is thin,
  say so and leave the defaults, exactly as `henk-events` 5.4 did.

## 6. Tranche 2 — conditional, only if tranche 1 stays quiet

- [ ] 6.1 Decide from 5.1 whether tranche 2 is warranted at all. Skipping it is a legitimate
  outcome and must be recorded as a decision.
- [ ] 6.2 If warranted: `HighCPU`, `HighMemory`, and the low-severity Gatus endpoints
  (AdGuard's web UI, the Docker registry, Gokapi). These are the recognised false-positive
  generators and the first candidates for removal if they dominate the cadence data.
- [ ] 6.3 Second watch window, same falsifiable exit condition.

## 7. Close-out

- [ ] 7.1 Write the honest outcome either way. If a three-device homelab in steady state does
  not generate enough incidents to tune cadence empirically, that is the finding, and the D6
  defaults stand as reasoned defaults with the reason documented.
- [ ] 7.2 Update the `sensor-routing` and `event-intake` spec Purpose lines, both still
  reading `TBD - created by archiving change henk-events`.
- [ ] 7.3 `/opsx:sync` + `/opsx:archive`. Check no other in-flight change MODIFIES
  `sensor-routing` or `event-intake` first — two changes modifying one requirement silently
  stop matching once the first lands, and archive is transactional.
