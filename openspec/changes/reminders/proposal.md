> **STATUS (superseded — do not implement as written).** Review established that this
> change must split into three: `channel-integrity` (**shipped and archived**), `reminders-core`
> (store, time resolution, tools, commands, audit — **shipped, deployed to rp5 2026-08-20 and
> archived**) and `reminder-delivery` (scheduler and delivery — **the only part still to
> write**).
> Several decisions below were reversed — notably "there is deliberately no cancel tool" — and
> roughly twenty sites in this change's artifacts are known stale.
>
> **Start at `notes/README.md`** in this directory — it carries the three-change split, the
> scope cuts, two open defects that must be fixed while rewriting, what is settled and must not
> be re-litigated, and a warning about the least-reviewed part. `notes/revision-record.md` is
> the full eleven-round record, and `notes/verify_selector_invariants.py` is a runnable model of
> the delivery state machine. Rewrite from those before implementing anything here.

# Reminders

## Why

Henk can remember a fact and catch a thought, but he cannot bring either back at
the moment it matters — "remind me at six to move the bins" has nowhere to land,
so it becomes a capture the owner has to go looking for. NORTH-STAR.md names
reminders as roadmap change 2, and it is the change that makes the attention
contract's own distinction real: *owner-scheduled sends are fine; system-scheduled
digests, heartbeats and "all is well" messages stay banned*. Today's spec text
does not draw that line — incident-triage forbids unprompted messages "on a timer"
without qualification — so the line has to be drawn deliberately, not by
implication.

## What Changes

- **One-shot reminders, owner-scheduled.** A durable reminder is a short text plus
  an absolute due time. Two write paths, mirroring the memory/capture precedent: a
  `remind` agent tool (mutating, **standing**, owner-turn-only) and a `/remind`
  owner command that costs no model turn. The reminder table joins memories and the
  inbox in the same SQLite file on the same backed-up volume — no new deploy
  surface. Recurring schedules are explicitly **out of scope** (own change).
- **Time is resolved by the model, validated by the app.** `remind` takes an
  absolute ISO-8601 timestamp; the app parses it in the owner's configured
  timezone, rejects the past and anything beyond a horizon, and **echoes the
  resolved local time back** so a mis-resolution is visible in the same breath.
  No natural-language parsing dependency. Because a model cannot resolve
  "tomorrow at 8" without knowing now, **every owner turn carries a one-line
  current-time header** — a session an hour old must not mis-schedule by an hour.
  The `/remind` command accepts explicit forms only (`18:00`, `2026-08-20 07:30`,
  `+2h`).
- **Delivery is verbatim and costs nothing.** When a reminder comes due the
  scheduler sends the stored text through the existing proactive owner-directed
  send. No session, no agent turn, no tokens, and no model between what the owner
  asked for and what arrives.
- **Henk knows what he just sent.** A delivery that has not yet been surfaced is
  injected — once, durably tracked, bounded, delimited as data and never as
  instructions — as a **delivered-reminder note** on the next owner turn, so
  "why did you ping me?" and "yeah, do that" resolve without a lookup. The note
  does **not** taint the session: reminder text can only originate in an owner
  turn (owner-turn-only tool scope plus the existing taint denial), so no
  event-derived content can ever reach a reminder, let alone be replayed from one.
  A read-only `reminders_read` tool covers everything outside that window.
- **Downtime never eats a reminder.** On startup, reminders due while Henk was
  down are delivered immediately and marked late with their original due time,
  within a configured grace window (default 24h). Older ones are reported once as
  a missed-reminder summary rather than delivered as stale instructions. Nothing
  is silently dropped, and nothing is deleted — a reminder moves
  pending → delivered / delivered-late / missed / cancelled.
- **Removal is owner-authored only.** `/reminders` lists pending reminders with
  their ids; `/reminders cancel <id>` cancels one. There is deliberately **no
  cancel tool** — the model may add a reminder (additive, receipted, echoed) but
  cannot un-schedule one, matching the existing `/inbox done` precedent where the
  destructive half of a capability is command-only.
- **The no-timers clause is amended, not relaxed.** Incident-triage's cadence
  requirement becomes explicit: unprompted *incident* messages remain
  condition-triggered and capped, and no system-scheduled digest, heartbeat or
  "all is well" message may exist — while an owner-scheduled reminder delivering
  at its due time is not a timer-driven message in that sense and does not consume
  the incident cap. Reminder volume is bounded by its own pending cap, and every
  reminder exists because the owner asked for it.
- **Receipts for a path with no turn.** The scheduler acts outside any session and
  outside the gate — its authority was granted when the reminder was scheduled, not
  when it fired — so the audit log gains a `reminder` record type covering the full
  lifecycle (`scheduled`, `delivered`, `delivered-late`, `missed`, `cancelled`,
  `abandoned`) with `initiated_by` of `model`, `owner-command` or `scheduler`.
  Records carry the reminder's id, due time and outcome, **not its text** — the
  store holds the content; the audit log holds the evidence. `schema_version`
  bumps to 4.
- **Kill switch, consistent with events.** `reminders.enabled: false` unregisters
  both tools, disables the commands (honest "not configured" reply) and never
  starts the scheduler.

## Capabilities

### New Capabilities
- `reminders`: owner-scheduled one-shot reminders — the durable reminder store,
  the `remind` tool and `/remind` command, time resolution and validation, the
  scheduler and verbatim delivery, the late/missed catch-up policy, listing and
  cancellation, the delivered-reminder note, and the caps that bound all of it.

### Modified Capabilities
- `incident-triage`: the cadence requirement's "never on a timer" clause is
  amended to distinguish owner-scheduled delivery (permitted) from
  system-scheduled digests and heartbeats (still banned), and to state that
  reminder deliveries do not consume the announceable-incident cap.
- `agent-core`: the owner command set gains `/remind`, `/reminders` and
  `/reminders cancel <id>`; every owner turn carries a current-time header; the
  next owner turn after a delivery carries the delivered-reminder note (which does
  not taint the session and never appears on event turns); the system prompt
  enumerates the two new tools; the scheduler runs as a task alongside the core
  worker and the event coordinator.
- `audit-log`: `schema_version` 4 adds the `reminder` record type and the
  `scheduler` value for `initiated_by`; reminder records are durable at the moment
  of each lifecycle transition and carry no reminder text.
- `approval-gate`: names the reminder tools' tier and scope (`remind`: mutating,
  standing, owner-turn-only; `reminders_read`: read-only) and states explicitly
  that scheduler-initiated delivery is app-initiated, authorized at schedule time,
  and outside the gate — so no reader has to wonder how a message reaches the
  owner without an approval.
- `secure-deployment`: the reminders table shares the existing memory/inbox SQLite
  store on the backed-up audit volume; the change adds no volume, port, socket,
  ACL grant or secret.

## Impact

- **Code:** new `henk/store/reminders.py` (repository + schema) and
  `henk/reminders/` (scheduler, time resolution); new `henk/tools/reminders.py`
  (`remind`, `reminders_read`); `henk/agent/commands.py` (three commands);
  `henk/agent/core.py` (time header, delivered-reminder note injection);
  `henk/app.py` + `henk/runtime.py` (scheduler task wiring, startup catch-up);
  `henk/config.py` + `config.yaml` (new `reminders` section);
  `henk/audit/` (v4 schema document, `reminder_record` builder);
  `henk/tools/__init__.py` and the system prompt (toolset enumeration).
- **Dependencies:** none added. Timezone handling uses the stdlib `zoneinfo`;
  timestamp parsing uses `datetime.fromisoformat`.
- **Deployment:** one new config section on rp5 (including the owner's timezone);
  no new volume, port, socket, ACL grant or secret. The existing store file gains
  a table — the audit volume's backup allowlist entry already covers it.
- **Rollback:** `reminders.enabled: false` returns Henk to current behaviour;
  scheduled reminders remain in the store, undelivered, until re-enabled.
