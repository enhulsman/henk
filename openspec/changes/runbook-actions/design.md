## Context

Henk runs on rp5 as a non-root container with no Docker socket, no host mount beyond the
read-only docs corpus, a `tag:henk` tailnet identity with egress to four ports (rp5:8080, vps:9090, vps:8089, vps:2586) and no
inbound or SSH rules, and one ntfy token scoped per topic (secure-deployment spec). Every
mutating tool registered to date (`store_memory`, `capture`, `remind`, `cancel_reminder`)
writes a Henk-local store and is owner-turn-only (approval-gate spec, "Mutating tools
declare a turn scope, enforced per session").

The North Star constraints this design is bound by, quoted:

- Permission table: "**Never** (= unregistered) | Not in the registry; default-deny hook
  refuses it | raw shell, SSH, deletes (unless deliberately promoted to per-instance),
  everything unenumerated" (`NORTH-STAR.md:50`).
- "There is no generic 'restart a service'; there is `restart_container:<name>`, each
  individually enumerated and individually tiered" (`NORTH-STAR.md:43-45`).
- Principle 3: "Publisher-push over deny-all topics; zero inbound, ever. ... No new
  listener, port, or inbound grant."
- Principle 4: "No tool accepts a recipient, topic, or identity parameter."
- Principle 5: "Receipts always. ... An agent that acts without asking must be *more*
  accountable, not less."
- Data axis: "Credentials of any kind are structurally off-limits."
- AGENTS.md security posture: "Runs containerized with scoped tokens only — no `~/.ssh`".
- Approval-gate: "a tier grant rides code review and cannot be widened by configuration"
  (`openspec/specs/approval-gate/spec.md:12`).

Host facts (from `devices/pi5.md`, `services/monitoring.md`, `docs/remote-read.md`):

- `agent-job` is installed on rp5: `/usr/local/sbin/agent-job`, root-owned registries
  `/etc/agent-job/<project>.jobs` (seed projects `henk` and `finance`), line format
  `name|run_as|cwd|gate|argspec|command words...`, no shell between sudo and the command,
  every call logged to `/var/log/agent-job.log`. `gate=user` requires a terminal, so an
  unattended caller can only run `gate=auto` jobs. `add`/`remove` need a terminal, so no
  caller can register jobs for itself.
- ntfy runs on the vps (deny-all). rp5's host already publishes to it (Gatus runs in host
  network mode on rp5 and publishes there), so an rp5 host process reaching ntfy needs no
  ACL change.
- Henk's event feed today: Gatus tier-1/tier-2 endpoints (host TCP, DNS, Prometheus,
  Grafana, ntfy, Taiga) and six Grafana rules in folder `henk`, among them
  `HenkInstanceDown` (`up == 0`, `identity_scope: instance`) and `HenkContainerRestarting`
  (deployed, cannot fire; backlog item 1). rp5's app containers have no alert routed to
  Henk.
- The workstation is not documented as WoL-capable anywhere; it is a Windows 11 host with
  S3 sleep and may be off.

## Goals / Non-Goals

**Goals:**
- The North Star's verb registry made real for three actions on rp5, with tier per target,
  enumeration in code, and no path to anything unregistered.
- Zero inbound and no new mount, socket, credential class or ACL grant for the container.
- A standing fix in an event turn removes a message class, and silence is earned by
  evidence the model cannot forge.
- Every request, decision, result and verification is receipted.

**Non-Goals:**
- vps/rp2 targets, multi-step runbooks, compose lifecycle, deletes, redeploys.
- Changing what the owner-command path does.
- Defending against root on rp5 or on the vps (same stance as `agent-job`).

## Decisions

### D1 — Execution path: request topic + host-side executor (A)

| | A. Request topic + executor | B. Unix-socket broker | C. Docker socket | D. Forced-command SSH key |
|---|---|---|---|---|
| New listener | none (both ends dial out to ntfy) | yes, a host daemon accepting on a socket | no, but the dockerd API is the listener | rp5 sshd, a new inbound grant |
| New host mount | none | a writable socket bind mount | `/var/run/docker.sock` | none |
| New secret in the stack | none (two grants on the existing token) | none | none, but root-equivalent access | an SSH private key |
| Can express "only these targets" | yes, `agent-job` registry | yes, same registry | no, the API cannot be narrowed by container name without a proxy | yes, via the forced command |
| systemd units, WoL | yes (host-side) | yes (host-side) | no | yes |
| Moving parts | ntfy round-trip, cache and replay to handle | fewest | fewest | ssh, keys, ACL |

**A (chosen).** Henk publishes a request to a deny-all topic; a root-owned systemd service
on rp5 consumes it, maps `(action, target)` to the `agent-job` job `<action>.<target>` in a
new project `henk-actions`, runs it, and publishes a result to a second topic that Henk
reads. It keeps every constraint above as written. Its cost is the ntfy hop: requests can
sit in ntfy's 72-hour cache, so the executor must refuse stale and duplicate requests
(D5). It also depends on the vps being up, which event intake already does, so no new
failure domain appears: if ntfy is down, Henk also gets no events to act on.

**B (rejected, closest second).** A socket is technically simpler: request/response is
native, there is no cache or replay, and `SO_PEERCRED` names the caller. But it adds a
listener ("No new listener, port, or inbound grant", principle 3) and a second host mount,
which the secure-deployment spec forbids ("The corpus bind mount is the **only** permitted
host filesystem mount"). Both are deliberate spec changes we would make only if A proves
unworkable.

**C (rejected).** Docker socket access is root on the host: any process with it can start
a privileged container mounting `/`. The container spec says "no Docker socket mount".
Socket-filtering proxies narrow API paths, not container names, and add a third-party image.
It cannot restart systemd units or send WoL anyway.

**D (rejected).** It needs an SSH private key inside the stack ("The stack SHALL NOT
contain SSH keys", secure-deployment; "no `~/.ssh`", AGENTS.md), a new `tag:henk` ACL
grant to rp5:22 (today `tag:henk` has no SSH rules and egress to rp5 on 8080 only; the 8000 grant in the base secure-deployment spec is not deployed),
and it is the "SSH" entry of the Never tier by name. A forced command limits what the key
does, but not that Henk holds a key and opens an inbound session.

### D2 — The registry lives in code, one entry per (action, target)

`henk/actions/registry.py` holds a frozen tuple of entries:
`RunbookTarget(action, target, tier, event_bindings, rate)`. `action` is one of
`restart_container`, `wake_workstation`, `restart_unit`. `target` is a safe token
(`[A-Za-z0-9_.-]`, checked at import time). `tier` is `standing` or `per-instance`.
`event_bindings` is a tuple of alert matchers (D7), empty for every per-instance entry
(enforced at import). The registry's first contents are listed in the runbook-actions
spec itself, so the spec does not point at a proposal that will be archived. `rate` is the per-target limit (D8).

Configuration (`runbook:` in `config.yaml`) may set `enabled` (default false) and
`disabled_targets` (narrowing only). An unknown name in `disabled_targets` fails startup,
so a typo cannot silently leave a target enabled. Nothing in configuration adds a target,
changes a tier, or adds a binding.

The host side is generated from the same source: `deploy/runbook-executor/henk-actions.jobs`
is committed. A test asserts that its job names are exactly `{f"{a}.{t}"}` over the code
registry, that every line is `gate=auto` with an empty argspec, and that no command runs a
path outside root-owned locations. Placeholders (`<WORKSTATION_MAC>`) stay in the
committed file and are filled in at install time, so the repo stays publication-safe.

Job lines, for illustration:
```
restart_container.wordle|root|/|auto||docker restart wordle
restart_unit.taiga-mcp|root|/|auto||systemctl restart taiga-mcp.service
wake_workstation.workstation|nobody|/|auto||/usr/local/sbin/henk-wol <WORKSTATION_MAC>
```
Every job takes no arguments, so the only model-influenced input that reaches the host is
the choice of job name, and that comes from a closed enum.

`gate=auto` is unavoidable (the executor has no terminal), which means **the host registry
cannot tell standing from per-instance**. This is stated rather than hidden. The tier is
the boundary against an honest but misled Henk. The host registry and the executor's own
ceiling are the boundary against a compromised Henk container, which could request any
registered job without the owner's `yes`. The per-instance set is chosen with that in mind:
each unit there is one whose unapproved restart is a nuisance, not damage. See D11 for why
executor-side approval is left out.

### D3 — Three tools, closed enums, no ambient parameters from the model

`henk/tools/runbook.py` registers `restart_container(container)`, `restart_unit(unit)` and
`wake_workstation()`. Each JSON schema carries `enum` over the enabled registry targets for
that action. The tool re-checks membership itself and the gate checks it a third time,
because SDK-side schema validation is not a security boundary we control. A miss is denied
with outcome `unregistered` and nothing is sent. No tool has a host, topic, command,
recipient or free-text parameter (principle 4). The request and result topics come from
configuration.

`raises_taint` is false for all three. The result Henk gets back is a closed structure
(D5) with no executor output text, so no free text authored outside the owner's control
enters the session. A test asserts that the parsed result carries no string field outside
the enums.

### D4 — Tier per target, resolved by the gate

`Tool.authorization` stays the declaration for ordinary tools. A runbook tool declares
`authorization = PER_TARGET`, and the gate resolves the tier through
`tool.tier_for(arguments)`, which reads the registry entry. The kill switch applies after
resolution (standing becomes per-instance). Startup refuses a `PER_TARGET` tool whose
registry entries lack a tier, which mirrors "mutating tool without a tier rejected at
startup".

### D5 — Request and result wire format

Request (Henk → `henk-actions`, one ntfy message, `Priority: min`):
```json
{"v": 1, "request_id": "<uuid4>", "action": "restart_container", "target": "wordle",
 "issued_at": 1760000000, "expires_at": 1760000030}
```
Result (executor → `henk-action-results`):
```json
{"v": 1, "request_id": "<uuid4>", "result": "ok", "reason": null, "exit_code": 0,
 "duration_ms": 2140}
```
`result` ∈ `ok | failed | refused | expired`. `reason` ∈ `null | job-exit | unregistered |
malformed | stale | duplicate | host-rate-limit`. Both parsers are closed: an unknown key,
a wrong type or an out-of-enum value means the message is dropped and logged.

Executor rules, in order: parse (else drop and log, and publish `refused/malformed` only
when a `request_id` is readable); refuse `expires_at < receipt time` as `expired/stale`.
Expiry is checked once, when the message is received. A request accepted behind a slow job
waits in the executor's queue (jobs run one at a time) and is not expired by that wait. Henk and
the executor share rp5's clock, so a 30-second expiry is reliable. A `request_id` seen in
the last 24 h is refused as `refused/duplicate`. The id store is a root-owned file, kept
across restarts, and the executor's ntfy cursor (`since=<last id>`) is persisted too. A
job name missing from `/etc/agent-job/henk-actions.jobs` (read directly; the file is 0644)
is refused as `refused/unregistered`. The host ceiling gives `refused/host-rate-limit`.
Otherwise the executor runs `sudo /usr/local/sbin/agent-job run henk-actions <job>` with a
90-second kill timeout, and exit 0 gives `ok`; anything else gives `failed/job-exit` with
the exit code. Stdout and stderr go to the journal only, never to the result.

Henk waits for the matching `request_id` on the result topic with a short-lived stream
subscription (`since=` the publish time), up to `runbook.result_timeout_seconds` (default
120). No answer means outcome `timeout`, reported as "may or may not have run", and **never
retried automatically**. A publish failure means `not-sent`, which is honest because
nothing reached the host. Because the wait stream closes at the timeout, a late answer is
picked up by a sweep: at startup and before each new request, Henk reads the result topic
with `since=` the oldest unresolved request still inside the 24 h duplicate window, and
records every result whose `request_id` it issued but had not resolved.

### D6 — The executor is a host program from this repo, absent from the image

`deploy/runbook-executor/` holds `runbook_executor.py` (stdlib only, the session-publisher
precedent), `henk-wol.py` (stdlib magic-packet sender, so no package install),
`henk-runbook-executor.service`, `henk-actions.jobs` and an install section in the README.
It is installed by copy to root-owned paths (`/usr/local/sbin/henk-runbook-executor`,
`/usr/local/sbin/henk-wol`), never run from `/home/pi/Coding/henk`. A root job must never
run a script that a less-privileged user can write (`agent-job` trust model).

It runs as a dedicated system user `henk-executor`, with exactly one sudoers line:
`henk-executor ALL=(root) NOPASSWD: /usr/local/sbin/agent-job run henk-actions *`. Its ntfy
token (read `henk-actions`, write `henk-action-results`) is at
`/etc/henk-runbook-executor/ntfy-token`, mode 600, owned by `henk-executor`. It is never
in the Henk stack, and Henk's token is never on the executor.

### D7 — Event turns: standing only, and only the bound target

Turn-scope members become `owner`, `event` and `tainted-owner`. The last one is an owner
turn in a tainted session, and it is opt-in per tool:

| Tool | Scope | Target rule outside untainted owner turns |
|---|---|---|
| `restart_container` | owner, event, tainted-owner | must be a target bound to an incident handled in this session |
| `restart_unit` | owner, tainted-owner | any enumerated unit (owner's `yes` required) |
| `wake_workstation` | owner | refused (`out-of-scope`) |

A **binding** is an app-side match from an event's derived identity to a target, declared
in the registry entry (`EventBinding(source="grafana", alertname="HenkInstanceDown",
label="job", value="cadvisor-pi5")`). It matches on the `job` label, never on the
`instance` label, because instance values are addresses and must not enter the repo. When
an event turn starts, the core resolves the bound targets from the event, before the model
runs, and records them in the turn context and the session. The model cannot add to the set.
If the payload lacks the label, nothing is bound and every event-turn restart is refused
`out-of-scope` (fail closed).

What the binding does and does not prove: the identity is derived by first-match parsing
of the rendered notification (`henk/events/identity.py`), not from an authenticated field.
The guarantee is therefore "the model cannot choose the target", not "the alert is
genuine". A forged notification needs a publisher credential for `henk-events` (today
only the write-only sensor users have one) and still reaches only a bound target within
the rate limit. Two narrowing rules apply:
- a binding is made only from a single-alert frame whose derived state is FIRING (`[FIRING:1]`) and that carries exactly one `job` value, so a grouped or RESOLVED notification binds nothing;
- a `job` value may be bound only when it names exactly one rp5 target. A job name shared
  across hosts (a `node-exporter` job covering rp5 and the vps, for example) would let a
  vps outage restart a healthy rp5 container, so such a job is not bound. Apply task 0.2
  proves uniqueness per bound value; an unproven one is not bound.

Taint of any origin narrows the same way. In any turn of a tainted session,
`restart_container` is limited to the session's bound set. An incident-tainted session has
the bound targets of the incidents it handled. A session tainted only by a taint-source
tool (none in production today; `sessions_read` with titles is the planned first, roadmap
4b) has an empty set, so every restart there is `out-of-scope`.

`HenkContainerRestarting` is deliberately bound to nothing: restarting a crash-looping
container is the wrong fix.

Per-instance actions never run in event turns: a prompt during triage would be a new
context-free interruption, and the suppressed-turn rule exists for exactly that case.

### D8 — Rate limits: two layers, reconstructed from the audit log

Henk side, per target, enforced by the gate after scope and before tier: automatic
(event-turn) runs ≤ 1 per 6 h; all runs ≤ 3 per hour; `wake_workstation` ≤ 3 per hour;
and at most one invocation per target per event turn. A "run" is **every request that was
published**, counted at authorization: in flight, `ok`, `failed`, `refused`, `expired` or
`timeout` alike. `not-sent` does not count. Counting at authorization also closes two gaps.
After a timeout the model cannot simply call again (the app does not retry, but the model
could). And two invocations in one assistant message are counted one after the other, not
both against the old count.
Over the limit means outcome `rate-limited`: nothing is sent, and the tool result says so.
In an event turn, the app marks the turn so the triage message is sent with the rate-limit
line and not held. State is rebuilt on startup from `authorization` records that carry a
`request_id`, excluding ids whose `action` record says `not-sent` (the cadence-cap
precedent), so a restart does not reset the limit and counts exactly what live counting
counted.

Host side, independent: ≤ 5 runs of one job per hour, ≤ 20 in total, held in the
executor's root-owned state. Henk cannot change it, and it is the ceiling against a
compromised container.

### D9 — Verified silence, and how it meets the cadence pipeline

The cadence pipeline (`henk/events/pipeline.py`) runs debounce, then per-identity cooldown
(default 6 h), then the cap, in `evaluate`, **before** a turn exists. The identity key does
not depend on state (`identity.py`), so a RESOLVED event is an ordinary event of the same
identity, and inside cooldown it is suppressed. `evaluate` also takes the cap slot and
hands the turn its suppressed count before the model runs, and one debounce batch can carry
several identities. Verified silence has to be specified against all four facts.

**Register.** The moment a bound `restart_container` returns `ok`, the core registers that
item (identity key, request id, deadline = now + `runbook.verify_window_seconds`, default
600) with a verification watcher. The window starts at the restart's result, not at turn
end, so a RESOLVED that arrives while the model is still gathering evidence counts.

**Verify.** The watcher sees each intake event **before** debounce and cooldown. A RESOLVED
event whose identity key matches a registered, unexpired item marks it verified and is
**consumed**: it is recorded as a suppression with reason `verified` and is not forwarded
to the pipeline, so it cannot become a triage of its own once cooldown is lifted. Any
non-RESOLVED event (a new FIRING) for a registered, not-yet-dropped item's identity marks
that item `relapsed` and is also consumed (suppression reason `relapsed`): a relapse
between a mid-turn verification and the drop must not vanish into the cooldown this turn
armed. A relapsed item forces a send. Any other event passes through unchanged. The decision rests on two app-side facts, the
executor's `ok` and an identity-keyed resolved event, and never on model text. An injected
"say it is resolved" therefore cannot silence an incident. (A forged RESOLVED frame needs
the same publisher credential as a forged alert, D7.)

**Hold.** At turn end the composed message is held only when the turn ended `completed`,
is announceable, and **every** item in it is registered (bound, restarted `ok`). An
incomplete-triage notice is never held: it is sent with the action lines ("An incomplete
triage is reported, never silent"). If any item is not registered, the message is sent at
once with the appended lines. If every item is already verified at turn end, the message
is dropped at once. The handoff is published as usual and records the action. The held
text is stored **without** any appended note. The suppressed-incident note, the action
lines and the fixed-count note are applied only when a message is actually sent, startup
sends included, so a dropped message never consumes a note.

**Drop.** When every item is verified before its deadline:
- no Signal send, and the fixed count goes up by one per item;
- the turn's cap slot is given back (removed from the announce window);
- the turn's suppressed count goes back to the pending suppressed count, so the next
  sent message still reports those incidents;
- each item's **cooldown clock** is reset to its value before this turn, so a relapse
  inside 6 h is triaged. That triage then meets the one-automatic-restart-per-6-h limit
  (D8) and reaches the owner with the rate-limit line, instead of disappearing into
  cooldown.

Cooldown and recurrence are two clocks after this change. `evaluate` today uses one map
(`_last_triaged`) for both. The pipeline splits it into `_cooldown_armed_at` and
`_last_triaged` (recurrence). A drop resets only the first, so the relapse triage is still
framed as a recurrence and carries the prior handoff, the one that records the restart.
`rehydrate` rebuilds both the same way.

**Send.** If any item's deadline passes unverified, or any item relapsed, the message is
sent with the "no recovery was confirmed" line for each item that was not verified, and
the relapse line `(<container> went down again after Henk restarted it at <HH:MM>.)` for
each relapsed item. Verified items get no line. Cap, cooldown and suppressed count
stay as `evaluate` set them. An item's registration ends when its message is sent or
dropped; events of its identity after that pass to the pipeline unchanged.

**Restart and write order.** The held message is persisted (`held_messages`: the un-noted
text, the item identity keys, the request ids, the turn's suppressed count, the
deadlines). Verification `action` records are written as items verify. A drop is then
recorded **explicitly**: one `action` record with `phase: drop` per `request_id` in the
log, and, in one store transaction, the held row is deleted and the fixed count goes up.
A verified restart alone does not mean a drop, because mixed batches, error endings and
cap-overflow turns verify items too and are sent or suppressed as usual. On startup,
before `rehydrate`, a held message whose items are all verified and not relapsed gets any
missing drop records written and the same transaction run. Any other held message is sent
at once with the no-recovery line, so a restart errs toward telling the owner. `rehydrate`
then looks only at `phase: drop` records. A triage whose runbook `request_id`s have drop
records arms no cooldown. Cap reconstruction skips it: it neither counts as an announcement
nor resets or increments the pending suppressed count. It still sets the recurrence
clock. The cap window, cooldown, recurrence and suppressed count
therefore rebuild as they stood live.

**Cap refund ordering (accepted trade-off).** While a held message occupies its cap slot,
a new triage that hits the cap is suppressed to a count note, even if the slot is given
back minutes later. Holding the slot is the safe direction: the alternative would let a
held-then-sent message exceed the cap.

**The fixed count** lives in a one-row `runbook_state` table in the store. It is not
derived from the log, because the suppressed count, unlike it, is rebuilt by `rehydrate`.
It is cleared in the same transaction that marks the carrying message sent.

**Where the fixed-count note goes.** It is appended to the next triage message that is
sent, or the next reply to an owner message, whichever comes first. It never goes on an
approval prompt, a reminder delivery or an application notice. It may wait weeks; that is
acceptable, because the record (handoffs) already has the detail. Order after `Pickup:`:
the suppressed-incident note, then the action lines, then the fixed-count note.

Verification writes its own `action` record (`phase: verification`, `verified: resolved | not-resolved | relapsed`) for each item, a relapse included.

### D10 — Receipts

Two questions, two records (the reminder precedent). The `authorization` record answers
whether Henk was allowed, written at decision time, and now carries `request_id` and
`target`. The new `action` record answers what happened on the host, written when the
result or timeout arrives: `request_id`, `tool`, `target`, `result` (`ok | failed |
refused | expired | timeout | not-sent`), `reason`, `exit_code`, `duration_ms`,
`turn_type`, `phase` (`result`, `verification`, or `drop`; D9), `verified`. The session record's `approvals` entry gains `request_id`, `target`
and `result`. Two outcomes are added to `approvals`: `rate-limited` and `unregistered`.
That is a structural change, so schema version 6. No executor output text enters any
record.

On the host, `/var/log/agent-job.log` already logs every run with its argv, and the
executor journals each request id. The two logs join on `request_id`, which the executor
writes into its journal line beside the job name.

### D11 — Left out on purpose

- **Executor-side approval** (the executor asks the owner itself): a second channel to the
  owner and a second approval UX, against the attention contract. The limit on what a
  compromised container can do is the registry and the host ceiling (D2, D8).
- **Request signing** (HMAC): the key would live in the same container as the ntfy token,
  so it adds no independent factor against the threat that matters (a compromised
  container). The deny-all topic grant already authenticates the publisher.
- **`/actions` command**: a new owner command is new surface; handoffs and the fixed-count
  note cover "what did you do". It can follow if the owner wants it.

### D12 — System prompt and triage framing

The system prompt lists the three tools (agent-core spec: the prompt enumerates the full
toolset). The triage framing gains one paragraph: in an event turn Henk may restart only
the container the alert is bound to, the bound set is given (or "none"), and a restart is
never a substitute for the triage arc. The arc is still composed in full, because the
message may be sent.

## Risks / Trade-offs

- [A compromised Henk container can restart any registered target without approval] → the
  per-instance list is limited to tolerable restarts, the host ceiling caps the rate, and
  `agent-job.log` records every run. Stated in D2, not hidden.
- [ntfy cache replays a request after an executor outage] → 30 s expiry on a shared clock,
  a 24 h duplicate store, and a persisted cursor.
- [A timeout leaves the outcome unknown] → it is reported as unknown, never retried, and
  the result topic may still carry the late answer, which Henk logs as an `action` record
  with the original `request_id`.
- [Restarts mask a real fault] → one automatic restart per 6 h per target, then the owner
  hears about it. No binding for restart-loop alerts.
- [The event path fixes little on day one] → said plainly in the proposal. Coverage grows
  with homelab backlog items 1 and 4, not with this change.
- [WoL may not work at all] → owner-run check before `wake_workstation` is enabled; the
  target ships in `disabled_targets` until then.
- [Held message lost] → persisted; sent on startup.
- [A silent fix hides a relapse behind cooldown] → a drop resets the item's cooldown, so a
  relapse is triaged and meets the 6 h automatic-restart limit (D9).
- [A dropped message takes an unrelated incident with it] → a hold needs every item in the
  turn bound, restarted `ok` and later verified (D9).
- [Two registries drift] → committed jobs file plus a parity test; the installed file is a
  copy with placeholders filled.
- [Container names in docs differ from `docker ps`] → apply task confirms them read-only
  before the registry is written.

## Migration Plan

1. Merge with `runbook.enabled: false`. Nothing changes on the phone.
2. Owner provisions ntfy (two topics, user `henk-executor`, grants to `henk`) with
   `ntfy-provision`, and probe-verifies them (anonymous 403, cross-grant 403).
3. Owner installs the executor on rp5 (`su -l`): user, files, sudoers (`visudo -cf` first),
   registry with the real MAC, token, unit; `systemctl enable --now`.
4. Owner runs the WoL check from rp5 with the workstation asleep and off.
5. Owner turns `runbook.enabled: true` with `wake_workstation` in `disabled_targets` if
   step 4 failed; redeploy.
6. Live checks: owner-turn restart of `wordle`; a refused out-of-enum target; one unit
   restart approved and one denied; a `docker stop cadvisor` to fire `HenkInstanceDown` and
   observe a silent fix plus the fixed-count note on the next message.

Rollback: `runbook.enabled: false` and redeploy (the tools disappear), or
`systemctl disable --now henk-runbook-executor` on rp5 (every action times out and
escalates). Removing `/etc/agent-job/henk-actions.jobs` refuses every job.

## Open Questions

- Does Grafana send a RESOLVED notification for `HenkInstanceDown` through the henk route,
  and does its payload carry the `job` label? Verify at apply; without either, the event
  path stays bound to nothing.
- Exact scrape job names for the rp5 exporters (`cadvisor-pi5` is measured; the other two
  are to be read from Prometheus).
- WoL from rp5 to the workstation: BIOS and NIC settings, Windows fast startup, wired LAN.
