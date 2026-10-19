## ADDED Requirements

### Requirement: Runbook actions are enumerated per target in code
Henk SHALL know a runbook action only as a registry entry declared in code that pairs one action (`restart_container`, `wake_workstation`, or `restart_unit`) with one target name, and declares that pair's authorization tier (`standing` or `per-instance`), its event bindings, and its rate limit. The first registry SHALL contain exactly these entries, with container names as confirmed against the running host before apply:
- `restart_container`, standing: `wordle`, `gokapi`, `filebrowser`, `node-exporter`, `cadvisor`, `adguard-exporter`, `adguardhome-sync`;
- `restart_container`, per-instance: `finance-automation-bot`, `dawarich_app`, `dawarich_sidekiq`, `registry`;
- `restart_unit`, per-instance: `taiga-mcp`, `lighttpd`, `dawarich-watcher`, `influxdb`, `pironman5`;
- `wake_workstation`, standing: `workstation`.

Event bindings SHALL exist only on standing `restart_container` entries whose `HenkInstanceDown` scrape job is proven to name that one rp5 target. A per-instance entry SHALL declare no event binding, and startup SHALL fail if one does. Target names SHALL consist only of `[A-Za-z0-9_.-]`. Configuration SHALL be able to disable the capability (`runbook.enabled`, default false) or disable individual targets (`runbook.disabled_targets`). It SHALL NOT be able to add a target, change a tier, or add a binding. An unknown name in `disabled_targets` SHALL fail startup with an error naming it.

#### Scenario: Capability off by default
- **WHEN** Henk starts with no `runbook` configuration
- **THEN** none of the three runbook tools is registered and the system prompt does not list them

#### Scenario: Excluded targets are absent
- **WHEN** the registry is inspected
- **THEN** it contains no entry for `gatus`, `henk`, `signal-cli-rest-api`, the Tailscale sidecar, `dawarich_db`, `dawarich_redis`, `registry-ui`, `advent-notifier`, `AdGuardHome`, `unbound`, `cloudflared`, `docker`, `tailscaled`, `ssh`, or `homelab-docs-pull`

#### Scenario: A typo in disabled_targets fails loudly
- **WHEN** `runbook.disabled_targets` names a target that is not in the registry
- **THEN** startup fails with an error naming that value

#### Scenario: Configuration cannot add a target
- **WHEN** configuration contains any key that would add a target, tier, or binding
- **THEN** startup fails with an error naming the key, and the registry is unchanged

### Requirement: Runbook tools accept only an enumerated target
`restart_container` and `restart_unit` SHALL each take exactly one argument whose value is restricted to the enabled targets of that action. `wake_workstation` SHALL take no argument. No runbook tool SHALL accept a host, command, topic, recipient, identity, or free-text parameter. A value outside the enumeration SHALL be refused before anything is published, by the tool and independently by the gate, with outcome `unregistered` in the receipt.

#### Scenario: Model-supplied target outside the enum is refused
- **WHEN** the agent invokes `restart_container` with `container` set to `gatus` (or any name not in the enabled enumeration)
- **THEN** nothing is published to the request topic, no channel message is sent, the tool result says the target is not registered, and the receipt records outcome `unregistered`

#### Scenario: Disabled target is outside the enum
- **WHEN** `wordle` is listed in `runbook.disabled_targets` and the agent invokes `restart_container` with `container` `wordle`
- **THEN** the invocation is refused with outcome `unregistered` and nothing is published

### Requirement: Requests travel over a deny-all topic and results carry no free text
An authorized runbook invocation SHALL publish exactly one request to the configured request topic, carrying only `v`, a fresh `request_id`, `action`, `target`, `issued_at`, and `expires_at` (30 seconds after `issued_at`). Henk SHALL wait for a result with the same `request_id` on the configured result topic for at most `runbook.result_timeout_seconds` (default 120). A result SHALL be accepted only when it carries exactly `v`, `request_id`, `result` (`ok`, `failed`, `refused`, `expired`), `reason` (null, `job-exit`, `unregistered`, `malformed`, `stale`, `duplicate`, `host-rate-limit`), `exit_code`, and `duration_ms`, with no other key. Any other message on the result topic SHALL be ignored and logged. The tool result given to the model SHALL be built from these fields alone. Runbook tools SHALL NOT declare `raises_taint`.

#### Scenario: Standing action executed end to end
- **WHEN** the agent invokes `restart_container` with `container` `wordle` in an untainted owner turn and the executor answers `ok`
- **THEN** one request was published, the tool result reports success, no approval prompt was sent, and the result reached the model with no executor output text

#### Scenario: A result with an extra field is ignored
- **WHEN** a message on the result topic carries the right `request_id` and an additional `stdout` key
- **THEN** it is not accepted as the result, and the invocation resolves as a timeout if no valid result follows

### Requirement: Failures and timeouts are honest and never retried automatically
A publish failure SHALL resolve the invocation as `not-sent`, and the tool result SHALL say that nothing reached rp5. No valid result within the timeout SHALL resolve it as `timeout`, and the tool result SHALL say that the action may or may not have run. A `failed`, `refused`, or `expired` result SHALL be reported as such, with its reason. None of these SHALL trigger an automatic retry by the application. A result that arrives after the timeout SHALL be recorded and SHALL NOT be presented to the model; Henk SHALL find such results by reading the result topic, at startup and before each new request, from the oldest request it issued and has not resolved within the last 24 hours.

#### Scenario: Executor timeout
- **WHEN** the agent invokes `restart_container` and no valid result arrives within the timeout
- **THEN** the tool result says the action may or may not have run, no second request is published, and an `action` record with result `timeout` is written

#### Scenario: Late result is recorded only
- **WHEN** a valid result for a timed-out request is on the result topic and Henk next starts or next publishes a request
- **THEN** an `action` record carrying that result and the original `request_id` is written, and no message is sent and no turn is started

### Requirement: The host executor runs only registered jobs
The rp5 executor SHALL map a request to the `agent-job` job `<action>.<target>` in the project `henk-actions`, and SHALL run it only through `agent-job run henk-actions <job>` with no further argument. Before running, it SHALL refuse a request as `expired`/`stale` when `expires_at` is earlier than the time the executor received it (a request accepted in time and then queued behind a running job SHALL NOT expire while it waits; jobs run one at a time), as `refused`/`duplicate` when its `request_id` was seen in the last 24 hours (the store survives restarts), as `refused`/`unregistered` when the job is absent from `/etc/agent-job/henk-actions.jobs`, as `refused`/`malformed` when the request does not parse to exactly the request fields, and as `refused`/`host-rate-limit` when the job ran 5 times in the last hour or all jobs 20 times. A job that exits non-zero SHALL be reported as `failed`/`job-exit` with its exit code. The executor SHALL publish no job output, and SHALL resume its subscription from a persisted cursor.

#### Scenario: Unregistered target refused by the host
- **WHEN** the executor receives a well-formed request whose `<action>.<target>` is not a line in `/etc/agent-job/henk-actions.jobs`
- **THEN** no job runs and the result is `refused` with reason `unregistered`

#### Scenario: Stale request is not run
- **WHEN** the executor receives a request after its `expires_at`, for example from the topic cache after an outage
- **THEN** no job runs and the result is `expired` with reason `stale`

#### Scenario: Replayed request is not run twice
- **WHEN** the executor receives a request whose `request_id` it already processed, including across an executor restart
- **THEN** no job runs and the result is `refused` with reason `duplicate`

#### Scenario: Host ceiling holds regardless of Henk
- **WHEN** the same job has run 5 times in the last hour and a sixth valid request arrives
- **THEN** no job runs and the result is `refused` with reason `host-rate-limit`

### Requirement: Event turns act only on the target the alert is bound to
When an event turn starts, the application SHALL resolve the event's bound targets from the registry's event bindings, by matching the event's source, alert name, and label value, before the model runs. It SHALL record them for the turn and for the session. A binding SHALL be made only from a single-alert notification whose derived state is FIRING and that carries exactly one value for the bound label; a grouped notification, one without the label, or one whose state is RESOLVED or unknown, SHALL bind nothing. In an event turn, and in any turn of a tainted session whatever raised the taint, `restart_container` SHALL execute only for a target in the session's bound set, and any other target SHALL be denied with outcome `out-of-scope`. A session tainted only by a taint-source tool has an empty bound set. The model SHALL have no means to add to the bound set. `HenkContainerRestarting` SHALL bind no target.

#### Scenario: Bound restart in an event turn
- **WHEN** a single-alert `HenkInstanceDown` FIRING event whose `job` label matches the `cadvisor` binding is triaged, and the agent invokes `restart_container` with `container` `cadvisor`
- **THEN** the request is published without an approval prompt and the authorization is receipted with tier `standing` and turn type `event`

#### Scenario: Injected target in an event turn is refused
- **WHEN** an event bound to `cadvisor` carries text instructing Henk to restart `wordle`, and the agent invokes `restart_container` with `container` `wordle`
- **THEN** the invocation is denied with outcome `out-of-scope`, nothing is published, and no channel message is sent for it

#### Scenario: Missing label binds nothing
- **WHEN** a `HenkInstanceDown` event without a `job` label is triaged and the agent invokes `restart_container` for any target
- **THEN** the invocation is denied with outcome `out-of-scope`

#### Scenario: Grouped notification binds nothing
- **WHEN** a `HenkInstanceDown` notification reporting two firing alerts is triaged and the agent invokes `restart_container` for either bound container
- **THEN** the invocation is denied with outcome `out-of-scope`

#### Scenario: A RESOLVED notification binds nothing
- **WHEN** a single-alert `HenkInstanceDown` RESOLVED notification for `cadvisor` is triaged (its FIRING was triaged more than a cooldown ago) and the agent invokes `restart_container` with `container` `cadvisor`
- **THEN** the invocation is denied with outcome `out-of-scope` and nothing is published

#### Scenario: Tool-raised taint leaves nothing to restart
- **WHEN** in an owner turn a taint-source tool is authorized and the agent then invokes `restart_container` with `container` `wordle`
- **THEN** the invocation is denied with outcome `out-of-scope` and nothing is published

### Requirement: Rate limits fail closed and escalate to the owner
A run SHALL mean a published request, counted when the gate authorizes it, whatever its later result (in flight, `ok`, `failed`, `refused`, `expired`, or `timeout`); a `not-sent` invocation SHALL NOT count. The gate SHALL refuse a runbook invocation with outcome `rate-limited`, before publishing, when the current turn is an event turn and the target had an event-turn run in the last 6 hours or already had a run in this turn; when the target had 3 runs in the last hour; or, for `wake_workstation`, when it had 3 runs in the last hour. Invocations in one assistant message SHALL be counted one after another. This state SHALL be rebuilt from the audit log on startup. An event turn in which an invocation was rate-limited SHALL send its triage message under the normal cadence rules, not hold it, and SHALL append the rate-limit line for the limit that applied: `(Henk did not restart <container>: it was already restarted automatically in the last 6 hours, which is the limit.)` or `(Henk did not restart <container>: it was already restarted 3 times in the last hour, which is the limit.)`.

#### Scenario: Rate limit reached escalates
- **WHEN** `cadvisor` was restarted automatically 2 hours ago and that fix was dropped as verified, the same alert fires again, and the agent invokes `restart_container` for `cadvisor`
- **THEN** the re-fire is triaged rather than suppressed by cooldown, nothing is published, the receipt records outcome `rate-limited`, and the triage message is sent with `(Henk did not restart cadvisor: it was already restarted automatically in the last 6 hours, which is the limit.)` after the `Pickup:` line

#### Scenario: Retry after a timeout is refused in the same turn
- **WHEN** an event-turn restart of `cadvisor` timed out and the agent invokes `restart_container` for `cadvisor` again in the same turn
- **THEN** the second invocation is refused with outcome `rate-limited` and nothing is published

#### Scenario: Two invocations in one assistant message
- **WHEN** `wordle` had 2 runs in the last hour and the agent issues two `restart_container` invocations for `wordle` in a single assistant message in an owner turn
- **THEN** the first is published and the second is refused with outcome `rate-limited`

#### Scenario: Limit survives a restart
- **WHEN** an automatic restart of `cadvisor` was published, Henk restarts, and the alert fires again within 6 hours
- **THEN** the invocation is refused with outcome `rate-limited`

### Requirement: Silence after a standing fix requires a resolved event for every item
When a bound `restart_container` returns `ok` in an event turn, the application SHALL at once register that item for verification, with a deadline `runbook.verify_window_seconds` (default 600) after the result. A RESOLVED event whose identity key matches a registered, unexpired item SHALL be matched before debounce and cooldown, SHALL mark the item verified, and SHALL be consumed: it SHALL be recorded as a suppression with reason `verified` and SHALL NOT reach the pipeline or start a triage. A FIRING event for a registered item's identity before the message is dropped SHALL mark the item relapsed, SHALL be consumed as a suppression with reason `relapsed`, and SHALL force the message to be sent. At turn end the application SHALL hold the composed triage message only when the turn ended `completed`, is announceable, and every item of the turn is registered; otherwise it SHALL send the message as today, with the action lines. An incomplete-triage notice SHALL never be held. The handoff SHALL be published as usual and SHALL record the action. Held text SHALL be stored without any appended note; the suppressed-incident note, action lines, and fixed-count note SHALL be applied only when a message is sent. When every item is verified before its deadline, at turn end or later, the held message SHALL be dropped without a send, and the application SHALL:
- give back the turn's cadence-cap slot;
- return the turn's suppressed count to the pending suppressed count;
- reset each item's cooldown clock (not its recurrence clock) to its value before the turn;
- increment a persisted fixed count by the number of items.

If any item's deadline passes unverified, or any item relapsed, the message SHALL be sent with `(Henk restarted <container> at <HH:MM>; no recovery was confirmed before this message was sent.)` for each item not verified and `(<container> went down again after Henk restarted it at <HH:MM>.)` for each relapsed item, with cap, cooldown, and suppressed count unchanged; verified items SHALL get no line. Held messages SHALL be persisted. An item's registration SHALL end when its message is sent or dropped; events of its identity after that SHALL pass to the pipeline unchanged. A drop SHALL be recorded explicitly: an `action` record with `phase` `drop` per `request_id` is written first, then the held row is deleted and the fixed count incremented in one store transaction. At startup, before cadence rehydration, a held message whose every item is verified and none relapsed SHALL get any missing drop records written and that transaction run; any other SHALL be sent at once with the no-recovery line. No model output SHALL decide whether a held message is dropped. On startup, a triage record whose runbook `request_id`s have `phase` `drop` records SHALL arm no cooldown, SHALL be skipped by cap reconstruction (neither an announcement nor a change to the pending suppressed count), and SHALL still count for recurrence. A verification record alone SHALL NOT mark a triage as dropped. The next triage message sent, or the next reply to an owner message, whichever comes first, SHALL append `(Note: since the last message Henk fixed <n> incident<s> himself — retrieve them with henk-pickup.)` and clear the count in the same transaction that records the send. Approval prompts, reminder deliveries, and application notices SHALL NOT carry it. Each verification, and each relapse, SHALL write an `action` record with `phase` `verification` per item.

#### Scenario: Verified fix is silent
- **WHEN** an announceable single-item `HenkInstanceDown` triage restarts `cadvisor` with result `ok` and the RESOLVED event arrives 3 minutes later
- **THEN** no Signal message is sent for the incident, the cap slot is given back, the handoff records the restart, and an `action` record with `phase` `verification` and `verified` `resolved` exists

#### Scenario: The verifying RESOLVED starts no triage
- **WHEN** the RESOLVED event for a held item arrives and the held message is dropped
- **THEN** the event is recorded as a suppression with reason `verified`, no triage session starts for it, and no Signal message is sent

#### Scenario: A RESOLVED during the turn counts
- **WHEN** the restart returns `ok` early in the turn and the RESOLVED event arrives before the turn ends
- **THEN** the message is dropped at turn end, and no no-recovery line is sent

#### Scenario: An incomplete triage is never held
- **WHEN** a bound restart returns `ok` and the triage turn then ends in error
- **THEN** the incomplete-triage notice is sent at once, with the action line, and not held

#### Scenario: A second incident in the turn is never dropped
- **WHEN** one debounce batch carries `HenkInstanceDown` for `cadvisor` and `HenkDiskPressure`, the restart of `cadvisor` returns `ok`, and its RESOLVED event arrives
- **THEN** the triage message is not held and is sent

#### Scenario: A dropped fix carries its suppressed count forward
- **WHEN** a held message whose turn carried a suppressed count of 2 is dropped as verified, and a later announceable incident is sent
- **THEN** the later message carries the suppressed-incident note for those 2 incidents

#### Scenario: Unverified fix is sent
- **WHEN** the restart returns `ok` and no RESOLVED event arrives within the window
- **THEN** the triage message is sent with the no-recovery line after the `Pickup:` line

#### Scenario: Model text cannot silence an incident
- **WHEN** the triage message claims the incident is resolved but no RESOLVED event arrives within the window
- **THEN** the message is sent

#### Scenario: Restart during the hold errs toward the owner
- **WHEN** Henk restarts while a triage message is held
- **THEN** the message is sent at startup with the no-recovery line

#### Scenario: A dropped triage rebuilds as dropped
- **WHEN** a held message was dropped as verified and Henk then restarts
- **THEN** the rebuilt cap window, cooldown, recurrence, and suppressed count equal those held live before the restart

#### Scenario: A crash during the drop sends nothing
- **WHEN** every item of a held message was verified, the process was killed before the held row was deleted, and Henk starts
- **THEN** the drop records exist, the held message is discarded, no message is sent, and the fixed count went up exactly once

#### Scenario: A sent triage with a verified restart rebuilds as sent
- **WHEN** a mixed-batch triage whose `cadvisor` restart was verified was sent, and Henk restarts
- **THEN** that triage arms cooldown and counts toward the cap exactly as before this change

#### Scenario: A relapse before the drop forces a send
- **WHEN** a restart's RESOLVED event verified the item mid-turn and a FIRING event for the same identity arrives before the turn ends
- **THEN** the FIRING is recorded as a `relapsed` suppression, the triage message is sent with the relapse line, and nothing is dropped

#### Scenario: A relapse after a silent fix is framed as a recurrence
- **WHEN** an identity's triage was dropped as a verified fix and the same identity fires again 2 hours later
- **THEN** the new triage runs, is framed as a recurrence, and carries the prior handoff that records the restart

#### Scenario: Triages without runbook actions rebuild as before
- **WHEN** Henk restarts with a log of triage records carrying no runbook `request_id`
- **THEN** every one of them arms cooldown and counts toward the cap exactly as before this change

#### Scenario: The fixed count rides the next triage or reply
- **WHEN** two incidents were fixed silently, an approval prompt is then sent, and then the owner's message gets a reply
- **THEN** the approval prompt carries no note, and the reply ends with `(Note: since the last message Henk fixed 2 incidents himself — retrieve them with henk-pickup.)` and the count is reset

### Requirement: Failed and timed-out standing fixes reach the owner with what was tried
In an event turn, a `restart_container` invocation that resolves as `failed` SHALL cause the triage message to be sent (under the normal cadence rules) with `(Henk tried to restart <container> at <HH:MM>; the restart failed on rp5.)`. One that resolves as `refused` or `expired` SHALL cause it to be sent with `(Henk asked rp5 to restart <container> at <HH:MM>; rp5 refused to run it.)`. One that resolves as `timeout` or `not-sent` SHALL cause it to be sent with `(Henk asked rp5 to restart <container> at <HH:MM> and got no answer within 120 s; it may or may not have run.)`, with the configured timeout in place of 120. These lines and the rate-limit and no-recovery lines SHALL be app-authored, SHALL follow the `Pickup:` line and any suppressed-incident note, and SHALL appear once per action.

#### Scenario: Failed restart escalates
- **WHEN** an event-turn restart of `cadvisor` returns `failed` with reason `job-exit`
- **THEN** the triage message is sent, not held, and carries the failed line after the `Pickup:` line

#### Scenario: Refused restart says it did not run
- **WHEN** an event-turn restart of `cadvisor` returns `refused` with reason `host-rate-limit`
- **THEN** the triage message is sent with the refused line, not the failed line

#### Scenario: Timed-out restart escalates
- **WHEN** an event-turn restart of `cadvisor` times out
- **THEN** the triage message is sent with the no-answer line naming the configured timeout

### Requirement: Wake-on-LAN is sent from the host
`wake_workstation` SHALL be executed by a host job that sends the magic packet from rp5's LAN interface, using a MAC address present only in the installed host registry and never in the repo or the Henk stack. The tool SHALL be owner-turn-only.

#### Scenario: The MAC is not in the repo
- **WHEN** the committed host registry file is inspected
- **THEN** the `wake_workstation` line carries the placeholder `<WORKSTATION_MAC>` and no hardware address

#### Scenario: Wake refused in a tainted session
- **WHEN** the owner follows up on a triage message in the session the event turn started and the agent invokes `wake_workstation`
- **THEN** the invocation is denied with outcome `out-of-scope`, nothing is published, and the tool result names the incident taint and `/new` as the remedy

### Requirement: The committed host registry matches the code registry
The repo SHALL carry the host registry file `deploy/runbook-executor/henk-actions.jobs`. An automated test SHALL assert that its job names are exactly `<action>.<target>` over the code registry, that every line has `gate` `auto` and an empty argspec, and that no line runs a path under `/home`, `/tmp`, or the repo checkout.

#### Scenario: Registries in step
- **WHEN** the test suite runs
- **THEN** the job names in the committed file equal the code registry's pairs, and every line passes the gate, argspec, and path checks
