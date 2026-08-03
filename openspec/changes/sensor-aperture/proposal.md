## Why

Henk's event pipeline is built, durable, and verified, but it has nothing to do: the
first-week watch recorded **zero real events in nine days** (`henk-events` 5.4), so the
debounce/cooldown/cap defaults chosen in `henk-events` D6 have never been tuned against
real data and the cadence contract is satisfied only vacuously. The pipeline is not
under-built, it is under-fed.

Two facts reframe what "under-fed" means, and both were measured rather than assumed:

1. **The signals already exist and are already tuned; they just do not route to Henk.**
   Prometheus carries 23 alert rules on the vps, of which only four families (`HealthEtl*`,
   backup freshness, disk above 85%, swap pressure) are curated into the events topic.
   `InstanceDown`, `ContainerRestarting`, `HighCPU`, `HighMemory`, the six per-device DNS
   processing-time rules, and the Obsidian backup verification rules all fire into Discord
   and are invisible to Henk. Widening the curated subset costs a notification policy edit,
   not new monitoring.
2. **Real Gatus failures are being filtered out, not absent.** 5.4's silence audit found
   every Gatus failure in the window was an isolated single check that never reached
   `failure-threshold: 2`. Nine of eighteen Gatus endpoints carry no alerting at all
   (three node exporters, both tunnels, `hulsman.dev`, AdGuard's web UI, the Docker
   registry, Gokapi), and the nine that do run at thresholds of 2 or 5.

There is also a correctness gap that must close *first*, because it decides whether this
change can be evaluated at all. `NtfyEventStream` opens its subscription with
`httpx.AsyncClient(timeout=None)` and no read deadline, so a half-open socket hangs
undetected. Today, "the aperture is wider and it is still quiet" and "intake died three
days ago" are indistinguishable from the outside. 5.4's silence was only trustworthy
because the owner verified continuous subscription by hand, per trigger family. Widening
the aperture without a liveness signal converts a manual audit into an unfalsifiable one.

Fortunately the liveness signal already arrives and is thrown away: ntfy interleaves
`keepalive` control frames into the JSON stream (measured on the vps instance:
`keepalive-interval: "45s"`), and `EventIntake._convert` discards every non-`message`
frame silently. A watchdog can be built from a frame that is already being delivered,
with no socket-level options and no new dependency.

## What Changes

- **Intake liveness watchdog.** Treat *any* frame, including `keepalive` control frames, as
  proof of life; declare the connection dead and force a reconnect when none arrives inside
  a configured deadline derived from the measured 45s interval. Reconnection reuses the
  existing backoff and `since` machinery, so a watchdog trip is indistinguishable from any
  other transport failure downstream.
- **Observable silence.** Intake exposes when it last received a frame and last reconnected,
  so "quiet" becomes a checkable state rather than an inference. This is what makes the
  aperture result interpretable.
- **Wider curated Prometheus subset.** Add owner-approved rule families to the Grafana
  notification policy that routes to the events topic. Prefer routing the existing
  `InstanceDown` rule over adding Gatus alerts for the three node exporters: it already
  exists, is already tuned, and covers every scrape target rather than only the three
  endpoints Gatus happens to probe.
- **Wider Gatus aperture.** Alerting for the nine bare endpoints, with per-endpoint
  thresholds set by severity rather than uniformly, and a deliberate sensitivity decision
  for the endpoints where isolated single failures are the signal rather than noise.
- **A tuning record.** A second watch window whose explicit success condition is
  *non-vacuous* cadence data, with the D6 defaults revisited against it, plus an honest
  written outcome if the infrastructure turns out to simply be healthy.

Not in scope: no new sensors are deployed, no Alertmanager, no mutating tools, and no
change to how triage or the approval gate behave. This change moves existing signal and
closes one durability gap.

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
- `sensor-routing`: The curated Prometheus subset requirement currently names exactly four
  rule families and forbids anything outside them from publishing to the events topic; that
  enumeration changes while the deny-by-default allowlist shape is preserved. The Gatus
  requirement gains coverage and per-endpoint sensitivity obligations for endpoints that
  currently publish nothing.
- `event-intake`: Gains requirements that a subscription delivering no frames is treated as
  failed rather than healthy, that recovery is bounded and notified only when persistent,
  and that last-frame state is observable so genuine silence is distinguishable from a hung
  socket. Existing resume, replay, and since-rejection-recovery requirements are unchanged.

## Impact

- **Code:** `henk/events/intake.py` (watchdog in `EventIntake.events`, read deadline and
  frame-level liveness in `NtfyEventStream`), its config plumbing in `henk/app` runtime
  wiring, and new tests alongside the existing 19 in `tests/test_event_intake.py`.
- **Deployed config, owner-run, outside the repo:** the Gatus config on rp5
  (`/opt/gatus/config/config.yaml`) and the Grafana notification policy on the vps. Both
  carry live tokens inline, so as-built notes must record neither the values **nor prose
  explaining that a value was withheld** (`repo-publication` 3.3).
- **Cadence:** more inbound events will exercise debounce, cooldown, and the daily cap for
  the first time under real load. The cap protects the Signal channel, so the risk of
  widening is bounded by design, but the D6 defaults are the thing under test.
- **No new dependencies.** The watchdog uses frames ntfy already sends.
- **Grafana access constraint:** there is still no scoped Grafana token, only the
  root-held admin password, so notification-policy edits are owner-run.
