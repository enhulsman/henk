## Summary for the owner

Henk gets his first hands: three named actions, each on a fixed list of targets. He can
**restart a named container on rp5**, **wake the workstation**, and, with your `yes` each
time, **restart a named systemd unit on rp5**. He never gets a shell, SSH, or a "restart
anything" verb. A small program on rp5 runs the action and reports back. It takes requests
over a new deny-all ntfy topic, the same way events already reach Henk, so nothing new
listens anywhere.

What changes on your phone:

- **Fewer messages.** When an rp5 exporter goes down (`HenkInstanceDown`), Henk restarts
  it, waits for the alert to resolve, and sends nothing. You hear about it once, as a
  count, on the next message he sends anyway. Today that class of alert is exactly what
  the event path can fix: the other standing containers have no alert routed to Henk
  (see Impact), so for them the gain is "restart wordle" from the phone with no prompt.
  `HenkInstanceDown` is critical, so Grafana also sends it to Discord. That Discord ping
  stays; only Henk's Signal message goes away.
- **Up to 10 minutes later** for those bound alerts: while Henk waits to see whether his
  restart held, the triage message is held back. If the fix did not hold, you get it then.
- **One new approval prompt**, in today's format, for a unit restart:
  ```
  Approval needed. Resolved action:
    tool: `restart_unit`
    unit: <<<taiga-mcp>>>

  Reply `yes` to approve or `no` to deny. Anything else cancels it.
  ```
- **New lines Henk appends after `Pickup:`** in a triage message, verbatim in
  "New text you will read" below, when a fix he tried did not hold, failed, was refused by
  rp5, timed out, or was blocked by a rate limit. Those incidents still reach you; the line
  says what he already tried.

## Decisions that are yours

Each has my pick and a confidence. Nothing here is settled until you say so.

1. **First verb set and tiers.** Pick: exactly your 2026-08-07 table. `restart_container`
   is standing, `wake_workstation` is standing, `restart_unit` is per-instance. Some
   containers are per-instance (decision 2). Alternative: ship `restart_container` alone
   first and add the other two in a follow-up. Confidence: high.
2. **Which targets, and which are excluded as critical.** Names come from `devices/pi5.md`
   and are known to be partly wrong: the finance bot runs as `finance-automation-bot`
   (the `finance.jobs` seed), and compose stacks name containers `<project>-<service>-1`.
   A read-only `docker ps` should settle the names before you sign this decision.
   - Standing containers: `wordle`, `gokapi`, `filebrowser`, `node-exporter`, `cadvisor`,
     `adguard-exporter`, `adguardhome-sync`.
   - Per-instance containers: `finance-automation-bot`, `dawarich_app`, `dawarich_sidekiq`,
     `registry` (your money data, your partner's location sharing, the image store).
   - Per-instance units: `taiga-mcp`, `lighttpd`, `dawarich-watcher`, `influxdb` (it holds
     only regenerable case-sensor data, unlike the excluded Dawarich stores), `pironman5`.
   - **What "per-instance" does and does not protect.** Your `yes` stops a *misled* Henk.
     It does not stop a *compromised* Henk container: rp5 cannot tell an approved request
     from an unapproved one (design D2), so whoever controls the container could restart
     any registered target up to rp5's own ceiling (5 per target per hour). Only put a
     target on this list if an unapproved restart would be a nuisance, not damage.
     Option: a v1 per-instance list of `taiga-mcp`, `lighttpd` and `pironman5` only.
   - Excluded (never registered): `gatus` (the alerting path itself), `henk`, its
     `tailscale` sidecar and `signal-cli-rest-api` (a self-restart kills the turn that waits
     for the result), `dawarich_db` and `dawarich_redis` (stateful stores), `registry-ui`
     (non-functional, nothing to fix), `advent-notifier` (one-off), and the units
     `AdGuardHome`, `unbound` (tailnet DNS for every device), `cloudflared` (every public
     endpoint), `docker`, `tailscaled`, `ssh`, and `homelab-docs-pull` (a timer-driven
     one-shot: "restarting" it would run a root `git pull` now, which is not a restart).
   Alternative: start with the three exporters only. Confidence: moderate (the split
   between standing and per-instance containers is a judgement call about what you mind).
3. **May a standing action run during an event turn (triage)?** This is the central one.
   Today every mutating tool is owner-turn-only, so triage can only flag. Options:
   - (a) Owner turns only, as today. Nothing disappears from your phone; you type
     "restart it" after every triage message.
   - (b) **Pick:** event turns may run `restart_container` (standing only) **on the target
     the triggering alert is bound to in code**. Henk works the binding out from the alert's
     name and `job` label before the model runs, so the model cannot pick another target,
     however the event text is worded. What remains: anyone able to publish a convincing
     fake alert to `henk-events` could cause a restart of a bound target, within the rate
     limit. Today only the write-only sensor users can publish there. A binding is made
     only when its `job` value belongs to exactly one rp5 target. Per-instance actions and
     `wake_workstation` never run in an event turn.
   - (c) Event turns may run any standing target the model chooses. Rejected: event text is
     untrusted.
   Sub-decision 3b: in your **follow-up turn** inside a triage session (the session is
   tainted), may Henk ask to restart a unit after you reply "do it"? Pick: yes, with the
   normal prompt, because the argument is an enum and your `yes` is the authorization. Today's
   rule would answer "use `/new`". In any tainted session, whatever tainted it,
   `restart_container` is limited to the incident's bound target. A session tainted by a
   tool, rather than by an incident, has no bound target, so it can restart nothing.
   `wake_workstation` is refused there. Confidence: (b) moderate-high; 3b moderate.
4. **Rate limits and cool-downs.** Pick: at most **one automatic (event-turn) restart per
   container per 6 hours**; a second alert inside that window gets no restart and reaches
   you with the rate-limit line. (Today the 6-hour alert cooldown would swallow that
   second alert. A silent fix therefore lifts the cooldown for that alert, so a relapse is
   triaged instead of ignored.) At most **3 restarts of one container per hour** counting
   owner-asked ones, `wake_workstation` at most 3 per hour, and **one attempt per target
   per triage**. Every request that was sent counts, including ones whose outcome is
   unknown. Behind that, the rp5 executor
   holds its own ceiling (5 runs of one job per hour, 20 in total) that Henk cannot change.
   Confidence: moderate (numbers are a first guess; the shape is the point).
5. **Execution path from the container to the host.** Pick: **A**, a request topic plus a
   host-side executor that runs only jobs registered with `agent-job`. Henk posts
   `{action, target, request_id}` to a deny-all topic, and a root-owned service on rp5
   runs the matching registered job and posts the result back. Zero inbound, no SSH, no
   Docker socket, no new mount. B (a Unix socket mounted into the container) is simpler
   but adds a listener and a host mount, both of which the North Star and the deployment
   spec rule out. Comparison in `design.md` D1. Confidence: high.
6. **How you see what Henk did.** Pick: a receipt in the audit log for every action, the
   action written into the incident's handoff (`henk-pickup` shows it), and the fixed-count
   line on the next triage message or reply to you. It never rides an approval prompt or a
   reminder. No message per fix and no new command. Alternative: a short
   "fixed" message per fix for the first two weeks, as training wheels. Confidence: moderate.
7. **Silence needs proof.** Pick: Henk stays silent only when the alert's own **resolved**
   event arrives within 10 minutes of a successful restart, and only when **every** alert in
   that triage was fixed this way. If one triage covers two alerts and only one was fixed,
   you get the message. Otherwise the triage message goes out with the "no recovery was
   confirmed" line. A silent fix gives back its slot in the daily alert cap, and any "N
   incidents were suppressed" count it carried moves to the next message. Confidence:
   moderate-high.

## New text you will read

App-authored lines, appended after the `Pickup:` line (and after the existing
suppressed-incident note when both apply). `<container>` is the target name and `<HH:MM>`
is local time:

- `(Henk restarted <container> at <HH:MM>; no recovery was confirmed before this message was sent.)`
- `(<container> went down again after Henk restarted it at <HH:MM>.)`
- `(Henk tried to restart <container> at <HH:MM>; the restart failed on rp5.)`
- `(Henk asked rp5 to restart <container> at <HH:MM>; rp5 refused to run it.)`
- `(Henk asked rp5 to restart <container> at <HH:MM> and got no answer within 120 s; it may or may not have run.)`
- `(Henk did not restart <container>: it was already restarted automatically in the last 6 hours, which is the limit.)`
- `(Henk did not restart <container>: it was already restarted 3 times in the last hour, which is the limit.)`

On the next triage message, or the next reply to a message of yours, after one or more
silent fixes (never on an approval prompt or a reminder):

- `(Note: since the last message Henk fixed <n> incident<s> himself — retrieve them with henk-pickup.)`

## Why

The North Star says "mundane-and-fixable gets fixed, not flagged", and every verb with
standing authorization is one message class that stops existing. Roadmap item 5 is the last
open item, and its same-turn-taint prerequisite landed on 2026-09-27. Today Henk can only
diagnose: every triage ends with a `Fix:` line that you then carry out by hand.

## What Changes

- A **runbook registry in code**: each `(action, target)` pair is named, enumerated, given
  its tier, and optionally bound to the alert identities that may trigger it in an event
  turn. Configuration can switch the capability off or disable targets, never add or widen.
- Three **mutating Henk tools**, `restart_container`, `wake_workstation` and
  `restart_unit`, each taking only a closed-enum target (or none) and no recipient, topic,
  host or command parameter.
- A **host-side executor** on rp5 (`deploy/runbook-executor/`, stdlib Python, root-owned
  install, systemd service). It reads the request topic and runs `agent-job run
  henk-actions <action>.<target>` from a new registry project `henk-actions`, separate from
  `henk.jobs`. It refuses stale, duplicate, malformed or unregistered requests, enforces its
  own rate ceiling, and posts a result that contains no free text. WoL is sent from the
  host, which sits on the LAN, and not from the container network.
- **Gate changes**: tier resolved per target; a new turn-scope member for owner turns in a
  tainted session; event-bound targets in event and tainted turns; a `rate-limited` outcome;
  the kill switch also narrows runbook actions to owner turns.
- **Triage and cadence changes**: a held message (up to 10 minutes) after a standing fix,
  dropped when the resolved event arrives for every alert in the turn; the resolved event
  is matched before debounce and cooldown; a dropped message lifts that alert's cooldown,
  gives back its cap slot and carries its suppressed count forward; the appended lines;
  the fixed-count note.
- **Audit schema v6**: a new `action` record for each executor result and each
  verification; `request_id` and `target` on authorization records; two new outcomes.
- **Two ntfy topics and one ntfy user** (provisioned by you with `ntfy-provision`). Henk's
  existing token gets `+write` on the request topic and `+read` on the result topic. The
  executor's token lives only on rp5, outside the stack.

## Capabilities

### New Capabilities
- `runbook-actions`: the registry, the three tools, the request/result transport, the host
  executor's contract, event binding, rate limits, verification, owner-visible lines.

### Modified Capabilities
- `approval-gate`: per-target tier, tainted-owner scope, event binding, `rate-limited`, kill
  switch narrowing.
- `audit-log`: schema v6, `action` record, new outcomes and fields.
- `incident-triage`: triage may execute bound standing actions; an announceable incident
  whose fix was verified sends nothing; cooldown, cap and suppressed-count rules account
  for dropped messages.
- `secure-deployment`: the enumerated ntfy grants and secret set; no new surface.

## Deliberately left out

- Raw shell, SSH, `docker compose up/down/pull`, redeploys (the `henk.jobs` `redeploy` job
  stays `gate=user` and is in a registry the executor cannot reach), deletes, and anything
  not enumerated.
- **vps and rp2 targets** (Taiga, Grafana, ntfy itself). The executor runs on rp5 only; a
  vps executor is a follow-up change once this one has run for a while.
- Multi-step runbooks (restart A, then B, then check C).
- Henk proposing new verbs at runtime. New verbs arrive as reviewed changes (principle 1).
- Executor-side approval for per-instance actions, request signing, and a `/actions`
  command (design D11 says why).
- An executor liveness alert. If the executor is down, every action times out and you hear
  about it through the timeout line. A Prometheus liveness metric is a follow-up.
- Making `HenkContainerRestarting` fire, and routing Dawarich alerts to Henk (homelab
  backlog items 1 and 4). Those decide how much the event path can fix later, but they are
  infra work, not this change. A crash-looping container is deliberately **not** bound to a
  restart.

## Impact

- **Event path coverage today.** Of rp5's containers, only the scrape targets
  (`node-exporter`, `cadvisor`, `adguard-exporter`) have an alert that reaches Henk
  (`HenkInstanceDown`, scoped per instance). `wordle`, `gokapi` and `filebrowser` have no
  Gatus alert at all (tier 3, dashboard only). The event path therefore fixes very little
  on day one, and the standing tier mostly saves you a prompt in owner turns. That is the
  honest size of the first step.
- Code: `henk/actions/` (registry, transport), `henk/tools/runbook.py`, `henk/gate/approval.py`,
  `henk/agent/core.py` (held message, appended lines), `henk/audit/schema/audit-record.v6.schema.json`,
  `config.yaml` (`runbook:` section, off by default), `deploy/runbook-executor/`.
- Hosts (all owner-run): on rp5, a user, a systemd unit, a registry file, a sudoers line,
  and a WoL check; on the vps, ntfy users and grants. No ACL change. No new port or mount.
- Unverified facts the build depends on: the workstation wakes from a magic packet sent by
  rp5 (not documented anywhere today); the exact container names; that Grafana's
  `HenkInstanceDown` payload carries the `job` label the binding matches on, and that a
  RESOLVED notification for it reaches Henk. Without the last two, the event path is bound
  to nothing and every triage is sent as today.
