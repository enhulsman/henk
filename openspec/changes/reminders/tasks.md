> **STATUS: SUPERSEDED — DO NOT WORK THESE TASKS.** They belong to the original
> single-change draft, which review split into three. `openspec list` shows this as
> "0/36 tasks" because none were ever started here and none ever will be; that count is
> not a backlog. The work has been done, elsewhere:
>
> | split | state |
> |---|---|
> | `channel-integrity` | shipped, deployed, archived `2026-08-20-channel-integrity` |
> | `reminders-core` | shipped, deployed to rp5 2026-08-20, archived `2026-08-20-reminders-core` |
> | `reminder-delivery` | **not written yet — this is the remaining work** |
>
> This directory is kept ONLY for `notes/README.md`, which carries the settled-decisions
> list, the scope cuts, and the two prerequisites `reminder-delivery` inherits. Start there.
> It is deliberately not archived: archiving implies completion, this was never implemented,
> and burying it would break the references that `2026-08-20-reminders-core/notes/` makes to
> that path.

# Tasks — Reminders

TDD throughout: each group starts by writing tests derived from the delta spec
scenarios (each Given/When/Then → at least one test; each SHALL → at least one
assertion), then implements to green. Implementation happens in a fresh session
via `/opsx:apply`. **Hard stop before any deploy to rp5 — explicit owner go
required.**

## 1. Config and time resolution

- [ ] 1.1 Tests: `RemindersConfig` load — defaults, `enabled` flag, timezone
      validated against `zoneinfo` with an unknown zone failing startup by name,
      caps/horizon/grace/poll/attempt values, and the section absent entirely
      (defaults, disabled)
- [ ] 1.2 Implement `RemindersConfig` in `henk/config.py` and add the
      `reminders` section to `config.yaml` with commented rationale, matching the
      existing sections' documentation style
- [ ] 1.3 Tests from the "Time resolution is validated and fails closed"
      scenarios: naive ISO interpreted in the owner timezone (not UTC), offset-
      carrying ISO honoured, past rejected, beyond-horizon rejected, unparseable
      rejected, clock-skew tolerance accepted
- [ ] 1.4 Tests for the command time forms: `HH:MM` next-occurrence (including
      the same-clock-time-tomorrow case), `YYYY-MM-DD HH:MM`, `+90m` / `+2h` /
      `+3d`, and an unrecognized form scheduling nothing
- [ ] 1.5 Implement `henk/reminders/timeparse.py`: resolution, validation, and
      owner-local rendering with weekday (one renderer shared by the tool result,
      the command replies, and `reminders_read` — a due time must read the same
      everywhere)

## 2. Reminder store

- [ ] 2.1 Tests: reminders table — schedule/list-pending-oldest-due-first/
      cancel/mark-delivered/mark-surfaced, status transitions, pending cap
      rejection naming the cap, text length limit, no-delete and no-edit
      invariants, restart survival, attempt counter increments durably
- [ ] 2.2 Implement `henk/store/reminders.py` (schema statements added to
      `henk/store/db.py`, repository following the `SqliteInboxStore` shape) and
      extend `build_stores`/`HenkStores` with the reminder repository
- [ ] 2.3 Tests: store read/write failures surface as `StoreError` and are never
      reported as an empty schedule or a successful schedule

## 3. Tools and owner commands

- [ ] 3.1 Tests from the `remind` tool scenarios: schedules and echoes id +
      resolved local due time with weekday, empty/over-limit text rejected,
      durable before the result reports success, registry rejects it without a
      tier (existing base-class validation), no approval prompt in an untainted
      owner session
- [ ] 3.2 Tests from the `reminders_read` scenarios: pending oldest-due first
      with local times, recent deliveries within the window, empty case, clamped
      maximum, unreadable store is an error not an empty schedule
- [ ] 3.3 Implement `henk/tools/reminders.py` (`RemindTool` mutating/standing/
      owner-only, `RemindersReadTool` read-only) and register both in
      `build_production_registry` behind `reminders.enabled`
- [ ] 3.4 Tests from the owner-command scenarios: `/remind` with each accepted
      time form, unrecognized form, `/reminders` listing, `/reminders cancel <id>`
      echoing the cancelled text, unknown id changing nothing, all with no agent
      session created and no tokens spent
- [ ] 3.5 Implement the three commands in `henk/agent/commands.py`, including the
      "not configured" replies when reminders are disabled
- [ ] 3.6 Test: no registered tool can cancel, edit, or delete a reminder
      (asserted against the production registry, not by inspection), and the
      system prompt states cancelling is an owner command

## 4. Scheduler and delivery

- [ ] 4.1 Tests from the delivery scenarios, driven through a channel-adapter
      test double and a controllable clock: due reminder delivered verbatim with
      the reminder marker, no session created, status becomes `delivered`,
      cancelled reminders never deliver, oldest-due first ordering
- [ ] 4.2 Tests from the failure scenarios: transient send failure retries on the
      next tick; attempts exhausted → `abandoned` + owner told; attempt counter
      written before the send so a crash mid-send is counted; a store error in a
      tick does not stop the scheduler
- [ ] 4.3 Implement `henk/reminders/scheduler.py`: poll loop, send-then-mark
      ordering, attempt bound, per-tick error containment (design D6)
- [ ] 4.4 Tests from the catch-up scenarios: overdue within the grace window
      delivered on startup marked with the original due time → `delivered-late`;
      older than the window → single missed summary, status `missed`, not
      delivered as a reminder; nothing overdue → complete silence
- [ ] 4.5 Implement the startup catch-up pass (design D7)
- [ ] 4.6 Test + implement scheduler lifecycle in `henk/app.py`: started with the
      core worker and coordinator, cancelled cleanly on shutdown, a scheduler
      crash logged without stopping message handling or triage

## 5. Conversation integration

- [ ] 5.1 Tests from the current-time-header scenarios: every owner turn carries
      it with the turn's own time, event turns carry none
- [ ] 5.2 Tests from the delivered-reminder-note scenarios: injected on the next
      owner turn, once only (`surfaced_at`), independent of recall state,
      survives a restart, window-bounded, count-bounded, absent from event turns,
      and does not taint the session (a following `capture` still executes)
- [ ] 5.3 Implement the time header and the delivered-reminder block in
      `henk/agent/core.py` alongside the existing recall injection, and update the
      system prompt's toolset enumeration in `henk/config.py`

## 6. Audit

- [ ] 6.1 Tests from the audit-log delta scenarios: a `reminder` record per
      transition, durable at transition time (SIGKILL after scheduling), correct
      `initiated_by` per path, **no reminder text in any record**, rejected
      schedules writing nothing, `abandoned` recorded
- [ ] 6.2 Implement `reminder_record` in `henk/audit/logger.py`, bump
      `SCHEMA_VERSION` to 4, commit `audit-record.v4.schema.json`, and keep v1–v3
      documents so historical records still validate
- [ ] 6.3 Test: records written under v4 validate against the v4 document, and
      existing v3 fixtures still validate against v3

## 7. Kill switch and end-to-end

- [ ] 7.1 Tests from the disable scenarios: `enabled: false` → no reminder tools
      registered, commands reply honestly, scheduler never started, stored
      reminders untouched; re-enabling runs the catch-up rules
- [ ] 7.2 Test: no scheduled or system-initiated message exists on any path other
      than an owner-scheduled reminder delivery — a week of simulated runtime with
      nothing scheduled and no events produces zero outbound messages
      (incident-triage delta)
- [ ] 7.3 Test: reminder deliveries do not consume the announceable-incident cap
- [ ] 7.4 Run the full suite plus lint; fix everything red before proceeding

## 8. Documentation and deploy prep

- [ ] 8.1 Update `README.md` (command set, tool list, the reminders config
      section) and the deployed-config note about the owner timezone being a
      host-side value
- [ ] 8.2 `/opsx:sync` the delta specs into `openspec/specs/`, filling the new
      `reminders` capability's Purpose from NORTH-STAR.md
- [ ] 8.3 Commit (publication-safe: no real numbers, no tailnet IPs — the
      pre-commit hook enforces it)
- [ ] 8.4 **STOP — owner go required before deploying to rp5.** Then: add the
      `reminders` section to rp5's locally-modified `config.yaml` with the real
      timezone, deploy with `enabled: false`, verify inert, flip to enabled
- [ ] 8.5 Deploy verification per design migration steps 3–4: tool-scheduled and
      command-scheduled reminders both echo local time and deliver verbatim; a
      follow-up turn shows Henk knows what he sent; `/reminders` lists and
      cancels; the catch-up path verified by stopping the container across a due
      time; audit records present with no reminder text
- [ ] 8.6 `/opsx:archive` the change with the deploy verification recorded
