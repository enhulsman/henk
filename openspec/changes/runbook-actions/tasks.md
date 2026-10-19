Tests come first in every group: each `#### Scenario:` in this change's delta specs gets at
least one failing test before the code that makes it pass. Steps marked **[owner-run]** run
in the owner's root shell on the named host (`su -l` on rp5) or through `ntfy-provision`.
The agent never runs them, and hands over the exact commands. Steps marked
**[departs?]** are where a finding at apply could change what the approved proposal
promised. If it does, stop and raise it with the owner before building.

## 0. Pre-apply facts (read-only)

- [ ] 0.1 Confirm the rp5 container and unit names against the running host (`remote-read rp5 docker ps --format '{{.Names}}'` and `systemctl list-units --type=service`). Ideally this happens before the owner signs proposal decision 2; at the latest it happens here. `finance-bot` is already known to be `finance-automation-bot`. **[departs?]** If any other name differs or is missing, list the differences for the owner before writing the registry.
- [ ] 0.2 Confirm that Grafana's `HenkInstanceDown` notification to `henk-events` carries the `job` label and that a RESOLVED notification reaches the topic: read one real fire and resolve from the audit log or the ntfy cache. Read the scrape job names of the three rp5 exporters from Prometheus, and prove that each value names exactly one rp5 target (not a job shared with the vps or rp2); an unproven value is not bound. Confirm that a single `HenkInstanceDown` fire arrives as a `[FIRING:1]` frame with one `job` value. **[departs?]** If either fact is missing, the event path ships bound to nothing (design D7 fail-closed). Say so to the owner, because proposal decision 3 then has no effect on day one.
- [ ] 0.3 **[owner-run]** WoL check on rp5: with the workstation asleep, then powered off, send a magic packet from rp5 (any sender: `wakeonlan` from apt, or the 4.3 script once it exists; this task may run after 4.3) and report whether it wakes. **[departs?]** If it fails, `wake_workstation` ships listed in `runbook.disabled_targets`.

## 1. Registry and configuration

- [ ] 1.1 Tests: capability off by default; excluded targets absent; a typo in `disabled_targets` fails loudly; configuration cannot add a target, tier or binding; a per-instance entry with a binding fails at import; target names restricted to `[A-Za-z0-9_.-]`.
- [ ] 1.2 Implement `henk/actions/registry.py` (frozen entries, event bindings, rates) and the `runbook:` config section (`enabled`, `disabled_targets`, `request_topic`, `result_topic`, `result_timeout_seconds`, `verify_window_seconds`), closed schema, off by default in `config.yaml`.
- [ ] 1.3 Tests: committed `deploy/runbook-executor/henk-actions.jobs` matches the code registry (names, `gate=auto`, empty argspec, no path under `/home`, `/tmp` or the checkout, `<WORKSTATION_MAC>` placeholder present and no hardware address).
- [ ] 1.4 Write `deploy/runbook-executor/henk-actions.jobs`.

## 2. Gate

- [ ] 2.1 Tests: per-target tier rejected at startup when a target is untiered; tier follows the target; out-of-enum and disabled targets refused with `unregistered` and nothing published.
- [ ] 2.2 Tests: turn scope with `tainted-owner`: per-instance runbook action in an incident follow-up prompts; per-instance never runs in an event turn; a tainted session refuses an unbound restart; tool-raised taint leaves nothing to restart; wake refused in a tainted session with the `/new` remedy; existing `store_memory`/`capture`/`remind` scope scenarios still pass unchanged.
- [ ] 2.3 Tests: kill switch demotes a standing runbook action (prompt shows the delimited target) and keeps runbook actions out of triage (no prompt, `out-of-scope`).
- [ ] 2.4 Tests: rate-limited invocation denied before publish and before any prompt; every published request counts (in flight, `timeout`, `failed`, `refused` included; `not-sent` excluded); retry after a timeout in the same event turn refused; two invocations in one assistant message counted one after another; the limit is rebuilt from `authorization` records after a restart; scope and rate re-checked when an approval arrives.
- [ ] 2.5 Tests: per-instance action approved (one request, `approved` receipt with request id) and denied (nothing published, `denied`).
- [ ] 2.6 Implement in `henk/gate/approval.py` and `henk/tools/base.py`: `PER_TARGET` tier resolution, the `tainted-owner` scope member, bound-target check, `rate-limited` and `unregistered` outcomes, kill-switch narrowing, re-check on approval.

## 3. Tools and transport (Henk side)

- [ ] 3.1 Tests: request shape is exactly the six fields with a 30 s expiry; standing action executed end to end against a fake ntfy; a result with an extra field is ignored; executor timeout gives "may or may not have run" and no second request; late result found by the startup and pre-request sweep and recorded only; publish failure gives `not-sent`; the tool result carries no free text; `raises_taint` false on all three tools.
- [ ] 3.2 Implement `henk/actions/transport.py` (publish, short-lived result stream filtered by `request_id`, closed result parser) and `henk/tools/runbook.py` (three tools, closed enums from enabled targets, tool-side membership check).
- [ ] 3.3 Tests: system prompt lists the three tools only when enabled; triage framing names the bound set or "none".
- [ ] 3.4 Implement the system-prompt and triage-framing wording (design D12).

## 4. Host executor (code in this repo, tested here, absent from the image)

- [ ] 4.1 Tests (no network, the real `agent-job` script under its test seams (`AGENT_JOB_DIR`, `AGENT_JOB_EXEC`), a fake only for ntfy): unregistered target refused by the host; stale request not run (expiry judged at receipt; a request queued behind a slow job is not expired by the wait); replayed request not run twice, including across a restart; host ceiling holds regardless of Henk; malformed request; job exit non-zero gives `failed/job-exit` with code; no job output in any result; cursor persisted and resumed.
- [ ] 4.2 Implement `deploy/runbook-executor/runbook_executor.py` (stdlib only) and `henk-runbook-executor.service`.
- [ ] 4.3 Tests then implement `deploy/runbook-executor/henk-wol.py` (stdlib magic packet; refuses anything that is not a MAC).
- [ ] 4.4 Test that the Docker build context excludes `deploy/runbook-executor/` (the session-publisher precedent).

## 5. Triage behaviour

- [ ] 5.1 Tests: bound restart in an event turn (no prompt, `standing`, turn type `event`); injected target in an event turn refused; missing label binds nothing; grouped notification binds nothing; bound standing restart is the only mutation during triage; triage tool calls audited; a RESOLVED notification binds nothing.
- [ ] 5.2 Tests: verified fix is silent (no send, cap slot given back, handoff records it, verification record); the verifying RESOLVED is consumed and starts no triage; a RESOLVED during the turn counts (window starts at the restart's result); an incomplete triage is never held; notes are applied only at send time, so a dropped message consumes neither the suppressed note nor the fixed-count note; a second incident in the turn is never dropped; a dropped fix carries its suppressed count forward; a relapse after a silent fix is triaged, not cooldown-suppressed, is framed as a recurrence with the prior handoff, and meets the rate-limit line; unverified fix is sent with the no-recovery line; model text cannot silence an incident; restart during the hold errs toward the owner; a dropped triage rebuilds as dropped (cap, cooldown, recurrence, suppressed count); a crash during the drop sends nothing and counts once; a sent triage with a verified restart rebuilds as sent; a relapse before the drop forces a send with the relapse line; the no-recovery line names only unverified items; triages without runbook actions rebuild as before; the fixed count rides the next triage or reply, never an approval prompt or reminder.
- [ ] 5.3 Tests: rate limit reached escalates with the verbatim line for each of the two limits; failed restart escalates; refused restart uses the refused line; application notes follow the arc in the fixed order (suppressed note, action lines, fixed-count note); timed-out restart escalates with the configured timeout; every appended line follows `Pickup:` and any suppressed-incident note, once per action.
- [ ] 5.4 Implement event binding resolution in the core, the `held_messages` and `runbook_state` tables, the verification watcher ahead of debounce and cooldown (consuming verifying RESOLVED events as `verified` suppressions), the split of `_last_triaged` into a cooldown clock and a recurrence clock in `henk/events/pipeline.py`, the drop path (cap slot refund, suppressed count return, cooldown-clock reset, write order, `rehydrate` treating dropped triages as such), the fixed count, and the verbatim lines in `henk/agent/core.py`.

## 6. Audit schema v6

- [ ] 6.1 Tests: new records declare v6 and validate; old records still validate; mixed v5/v6 log rehydrates identically; a dropped triage is the one rehydration difference; runbook action in the session record; runbook receipt carries the request id before the request leaves; result receipted at result time (SIGKILL); timeout receipted; `action` records carry no output text.
- [ ] 6.2 Write `henk/audit/schema/audit-record.v6.schema.json` and the logger changes.

## 7. Deployment surface

- [ ] 7.1 Tests: the compose file gains no mount, socket or port; the enumerated secret set is unchanged.
- [ ] 7.2 README: "Runbook actions (rp5)" install section with the exact owner commands for groups 8 and 9.
- [ ] 7.3 Full suite green; `openspec validate runbook-actions --strict`.

## 8. Host install (owner-run)

- [ ] 8.1 **[owner-run]** On the vps via `ntfy-provision`: topics `henk-actions` and `henk-action-results`; user `henk-executor` with read `henk-actions` and write `henk-action-results`; extend `henk` with write `henk-actions` and read `henk-action-results`; probe-verify (anonymous 403; `henk` read on requests 403; `henk` write on results 403; `henk-executor` write on requests 403).
- [ ] 8.2 **[owner-run]** On rp5 (`su -l`): create system user `henk-executor`; install `runbook_executor.py` to `/usr/local/sbin/henk-runbook-executor` and `henk-wol.py` to `/usr/local/sbin/henk-wol` (root:root 755); token to `/etc/henk-runbook-executor/ntfy-token` (600, `henk-executor`); state dir `/var/lib/henk-runbook-executor` (700).
- [ ] 8.3 **[owner-run]** On rp5: install `/etc/agent-job/henk-actions.jobs` (root 644) from the committed file with `<WORKSTATION_MAC>` filled in, then check it with `agent-job list henk-actions`, which needs a terminal.
- [ ] 8.4 **[owner-run]** On rp5: sudoers drop-in `henk-executor ALL=(root) NOPASSWD: /usr/local/sbin/agent-job run henk-actions *`, checked with `visudo -cf` before install (mode 440); verify `sudo -u henk-executor sudo -n /usr/local/sbin/agent-job run henk redeploy` is refused, and that `sudo -u henk-executor sudo -n /usr/local/sbin/agent-job run henk-actions restart_container.wordle extra` is refused with exit 2 (arity), and `sudo -u henk-executor sudo -n /usr/local/sbin/agent-job list henk-actions` is refused by sudo.
- [ ] 8.5 **[owner-run]** On rp5: install and `systemctl enable --now henk-runbook-executor`; confirm `ss -ltnp` and `ss -lx` are unchanged.
- [ ] 8.6 **[owner-run]** On rp5: `config.yaml` sets `runbook.enabled: true` (with `wake_workstation` disabled if 0.3 failed); redeploy via the existing `redeploy` job (`gate=user`).
- [ ] 8.7 Update the homelab docs (`devices/pi5.md` services table, ntfy users in `services/monitoring.md`) with `/docs-update`, and show the diff to the owner.

## 9. Live checks (owner present)

- [ ] 9.1 Owner asks "restart wordle": no prompt, `wordle` restarts, receipt and `action` record joined on `request_id`, and `agent-job.log` has the matching line.
- [ ] 9.2 Owner asks for an excluded target (`gatus`): refused, nothing published.
- [ ] 9.3 Owner asks "restart taiga-mcp": the prompt text matches the proposal verbatim; once approved and once denied.
- [ ] 9.4 **[owner-run]** `docker stop cadvisor` on rp5: `HenkInstanceDown` fires; Henk restarts it; RESOLVED arrives; no Signal message; the next message carries the fixed-count note.
- [ ] 9.5 **[owner-run]** Stop the executor, ask "restart wordle": the timeout line or tool result says "may or may not have run"; restart the executor and confirm the stale request is not run.
