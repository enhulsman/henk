## Summary for the owner

Henk gets read and write access to the shared **Agents** Google calendar, limited to
events whose title starts with `Homelab`. With it he can answer "what homelab work is
planned?", put a maintenance item on the calendar after you say `yes`, and stay quiet
while planned maintenance runs.

### Your decisions (all taken, 2026-10-30)

1. **After a window, you hear only about what still needs you.** When a window plus its
   grace ends, alerts that are still firing are triaged as usual and reach you in one
   triage message. That message ends with one line about the rest. If everything
   recovered, Henk sends nothing. Within 24 hours you can ask him "how did the patch round
   go?" and he answers from the hold record. (You weighed this against a summary after
   every window. The North Star allows a summary you asked for. The deciding question
   was whether you would read "4 held, 4 recovered" or mark it as read.)
2. **Every timed `Homelab` event of 8 hours or less holds alerts**, whichever hosts it
   touches, the Dawarich evening included. Patch rounds usually cover several hosts, so
   per-host scoping was dropped. When Henk creates an event, he tells you it will hold
   alerts.
3. **Grace after the end time is 30 minutes.**
4. **Henk does not propose events unprompted in v1.** He writes only when you have asked
   in an owner conversation, and every write waits for your `yes`. He never sends a
   message just to suggest a calendar item.

These decisions also settle the question of holding at all versus triaging every alert
with the maintenance context. You chose holding, for three reasons:

- triage charges the daily cap whether or not Henk stays silent;
- a triaged alert arms a 6-hour cooldown that would mute a service that stays broken
  after the window;
- an alert that fires once and never resolves is exactly what the after-window check
  catches.

Build decisions I have made, reported in case you want one reversed:

- **The key lives in `.env` as one base64 line** (`GOOGLE_CALENDAR_SA_KEY_B64`), not as a
  mounted file. Henk's secrets reach him only through his process environment, and this
  keeps one pattern. You place it by hand; `token-place` writes files and cannot add the
  line to `.env`.
- **No new Python package in the image.** The service-account sign-in uses `pyjwt` and
  `cryptography`, which the image already ships (they arrive with the Agent SDK).
  `pyproject.toml` gains `pyjwt[crypto]` as a direct dependency; `google-auth` is not
  added.
- **Only events created by allowlisted accounts count**: your Google account, Pam's
  service account, and Henk's own. An invitation from anyone else that lands on the
  calendar is invisible to Henk, even if its title starts with `Homelab`.
- **All-day events and events longer than 8 hours never hold alerts.** They still appear
  in "what's planned". A mistaken three-day event must not silence Henk for three days.
- **If the calendar is unreachable, alerts flow normally.** Henk works from the last
  calendar copy he fetched for up to 6 hours. After that he holds nothing until a fetch
  succeeds again.
- **No change to the approval gate.** The prompt shows the weekday, date and time exactly
  as they will be written, in your time zone.

### What changes for you

- **Planned work is answerable.** "What homelab work is planned?" gets the next 14 days of
  `Homelab` events: titles and times only, never descriptions or attendees.
- **One new approval prompt**, in today's format:
  ```
  Approval needed. Resolved action:
    tool: `calendar_create_event`
    title: <<<Homelab: patch round>>>
    start: <<<Sat 2026-11-07 20:00>>>
    end: <<<Sat 2026-11-07 22:00>>>

  Reply `yes` to approve or `no` to deny. Anything else cancels it.
  ```
  After a `yes`, Henk's tool result reads like this. The times come from the same
  renderer as reminders, and the second sentence appears only when the event will
  actually hold alerts:
  ```
  Created "Homelab: patch round", Sat 7 Nov at 20:00 CET until Sat 7 Nov at 22:00 CET. Alerts to Henk are held until Sat 7 Nov at 22:30 CET.
  ```
  The event lands with no description and no attendees, so nobody gets an invitation.
- **Quiet during maintenance.** From a window's start to 30 minutes after its end, Gatus
  and Grafana alerts that reach Henk are recorded and do not reach your phone. If you
  message him during the window, he knows it is running and says so ("patch round in
  progress, 3 alerts held so far"). The wording is his, not a fixed string.
- **After the window**, alerts that are still firing reach you as one normal triage
  message, ending with one of these lines:
  ```
  (Held during "Homelab: patch round", 20:00–22:30: 4 alerts, 3 of which recovered on their own.)
  (Held during "Homelab: patch round", 20:00–22:30: 1 alert.)
  ```
  The second form is used when none recovered. If nothing is still firing, you get no
  message. "Alerts" counts distinct alerts, not individual notifications.
- **Henk's direct alert routes are unchanged.** Anything Grafana or Gatus also sends to
  Discord or ntfy still arrives there during a window. Only Henk's own Signal messages are
  held.
- **One more setup step on rp5:** the base64 key line in `.env`, then flip
  `calendar.enabled`. Task 8.3 spells it out.

### Deliberately left out

- Updating or deleting events (not registered; you edit the calendar directly).
- Henk suggesting events in a message of his own (decision 4).
- Holding only some alerts, per host or per service (decision 2).
- Reading Pam's events, event descriptions, attendees, or any other calendar.
- A gate hook that checks the arguments before the prompt (a follow-up once
  runbook-actions has landed).

## Why

Planned maintenance on rp5 and the other hosts (the monthly patch round, the weekend
image prune) produces a burst of Gatus and Grafana alerts. Henk triages them and sends them
to the owner's phone, even though the owner caused them and is watching. That is exactly
the noise the attention contract forbids. The maintenance schedule already lives on the
shared Agents calendar, and NORTH-STAR roadmap item 6 (revised 2026-10-30) chose direct
Google Calendar API access for it.

## What Changes

- New read-only tool `calendar_upcoming`: the next 14 days of events on the configured
  calendar whose title starts with `Homelab` and whose creator is allowlisted. Titles and
  start and end times only, with hold windows marked.
- New mutating tool `calendar_create_event`, **per-instance** and owner-turn-only. It takes
  `title` (must start with `Homelab`), `start` and `end` (wall-clock
  `Ddd YYYY-MM-DD HH:MM` in the owner's zone, with the weekday checked against the date).
  It inserts an event with no description and no attendees, with `sendUpdates=none`. It
  writes an `authorization` receipt (existing) plus a new `calendar` record with the
  Google event id.
- A **maintenance-window provider**: polls the calendar every 5 minutes, persists its last
  good snapshot to the audit volume, and fails open once that snapshot is older than
  6 hours. Windows are timed `Homelab` events of 8 hours or less, extended by a 30-minute
  grace.
- **Alert hold in the event pipeline**: an event whose ntfy publish time falls inside an
  open window is stored and recorded instead of triaged. Release happens once the window
  plus grace has passed **and** intake has caught up on the ntfy backlog. Held alerts
  that are still firing go through the normal pipeline as one batch with hold framing.
  The release counts as done only once that triage's record is durable. Holding fails
  open whenever a hold cannot be stored.
- Owner turns during an active window, or within 24 hours after one, carry a maintenance
  note (title, times, held and recovered counts).
- **BREAKING (principle 3, deliberately):** a named exception to "publisher-push, zero
  inbound". It allows one outbound API (`oauth2.googleapis.com`, `www.googleapis.com`)
  with a calendar-scoped service-account credential. Still zero inbound.
- Audit schema version increments, with new `calendar`, `hold` and `hold-release` record
  types.
- Config: a new `calendar` section (disabled by default) and
  `personal_data.calendar_creator_allowlist`. Secret: `GOOGLE_CALENDAR_SA_KEY_B64`.

## Capabilities

### New Capabilities
- `maintenance-calendar`: the calendar data source, both tools, the creator and prefix
  filters, credential handling, and the window provider.

### Modified Capabilities
- `event-intake`: holding, durable held storage, catch-up-gated release, and checkpoint
  semantics for held, duplicate and released events.
- `incident-triage`: held events are not triaged until released; released alerts carry
  window framing.
- `agent-core`: the owner-turn maintenance note; the release line appended to a released
  triage's message.
- `approval-gate`: the calendar tools' tier and scope, and `calendar_create_event` in the
  owner-only tool list.
- `audit-log`: schema version increment; `calendar`, `hold` and `hold-release` records.
- `secure-deployment`: the principle-3 exception, the new secret, no domain-wide
  delegation, and the snapshot on the audit volume.

## Impact

- Code: new `henk/calendar/` (token, client, windows) and `henk/tools/calendar.py`.
  Changes to `henk/events/{intake,coordinator,pipeline}.py`, `henk/agent/{core,turns}.py`,
  `henk/store/db.py`, `henk/runtime.py`, `henk/config.py`, `henk/audit/logger.py` and
  `henk/tools/__init__.py`, plus a new audit schema document.
- Dependencies: `pyjwt[crypto]` declared directly. It is already in the lock and the
  runtime image through `mcp`.
- rp5: one `.env` line, config edits (calendar id, creator allowlist), and an egress probe
  for `oauth2.googleapis.com` (only `www.googleapis.com` was probed). Before any
  hold/release code is written, a probe confirms that ntfy sends its cached backlog
  before the first keepalive.
- Coordination: `runbook-actions` (branch `aw/runbook-actions-proposal`) MODIFIES three of
  the same requirements:
  - audit-log *Schema is versioned*;
  - secure-deployment *Scoped secrets only*;
  - approval-gate *Mutating tools declare a turn scope, enforced per session*.

  Whichever change archives second rebases its full MODIFIED text onto the archived
  version, carrying over the other change's version paragraph, secret entries and tool
  list. Renumbering alone is not enough.
- Docs: the homelab docs page for Henk gains the calendar and the egress exception
  (`/docs-update` at deploy).
