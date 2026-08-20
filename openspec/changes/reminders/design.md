# Design — Reminders

## Context

Henk today is purely reactive on the owner side (a message in, a reply out) and
condition-triggered on the event side (a sensor alert in, a triage message out).
Nothing in the process initiates anything at a time of its own choosing; the only
clock-driven code is the event debouncer and the intake liveness watchdog, neither
of which can put a message on the channel by itself.

This change introduces the first path where **Henk speaks because a clock said so**.
That is the whole security and product weight of it. The attention contract already
anticipated the distinction — *owner-scheduled sends are fine; system-scheduled
digests, heartbeats and "all is well" messages are banned* — but the spec text
currently reads "never on a timer", so the amendment has to be explicit and narrow.

What already exists and is reused unchanged:

- the SQLite store on the backed-up audit volume (`henk/store/`), lazily opened,
  WAL, shared by memories and the inbox;
- the proactive owner-directed send (`ChannelAdapter.send`), which cannot name a
  recipient — the owner identity comes from configuration (North Star principle 4);
- the append-only audit log with decision-time receipts;
- the two-axis permission model: an authorization tier per named action, plus a
  turn scope enforced with session taint;
- the app-side owner-command dispatcher, which never passes text through the model;
- the coordinator precedent for "a long-running task alongside the core worker,
  cancelled on shutdown".

Constraints inherited and not up for renegotiation here: no new volume, port,
socket, ACL grant or secret; no inbound listener; no client data; every mutation
receipted; fail closed on ambiguity.

## Goals / Non-Goals

**Goals:**

- One-shot, owner-scheduled reminders that survive restarts and container
  recreation, and that are never silently lost.
- Delivery that costs zero tokens and cannot be reworded between scheduling and
  firing.
- Henk able to hold a conversation *about* a reminder he just sent, without a
  lookup and without paying for it when the owner does not reply.
- A no-timers amendment that is narrower after the change than a reader would
  assume from the word "reminders" — system-scheduled anything stays banned.
- Every reminder lifecycle transition durably receipted, including the ones that
  happen with no session and no owner present.

**Non-Goals:**

- Recurring reminders (daily/weekly/cron). Own change: recurrence needs a
  per-schedule cap, a missed-occurrence policy, DST rules and an end condition, and
  each of those is a separate attention-contract question.
- Natural-language date parsing in the application. The model resolves; the app
  validates and echoes.
- Snooze, edit-in-place, or reminder priorities.
- Reminders about anything Henk observes on his own (that would be a
  system-scheduled message wearing a reminder costume).
- Delivery to anywhere but the owner, or through any channel but the configured
  adapter.

## Decisions

### D1 — The reminder store is a third table in the existing SQLite file

`reminders(id, text, due_at, created_at, source, status, delivered_at,
surfaced_at, attempts)` alongside `memories` and `inbox`, same file, same lazy
connection, same volume, same backup allowlist entry.

*Alternatives considered.* A separate database file (needless second connection and
a second thing to back up); reusing the inbox table with a due date (conflates two
capabilities with different eviction, listing and lifecycle rules — the inbox
never evicts and drains oldest-first, a reminder has a due order and terminal
states). Rejected both.

Statuses are `pending`, `delivered`, `delivered-late`, `missed`, `cancelled`,
`abandoned`. **Nothing is deleted, ever** — same principle as the inbox: a
terminal status is a state change, not a removal, so the record of what the owner
asked for survives.

There is deliberately no seam interface here, unlike `InboxStore`. The inbox got
one because a personal-inbox service is a named future replacement; no equivalent
service is planned for reminders, and inventing a seam for a backend nobody has
proposed is speculative generality. If one appears, extracting the interface is a
mechanical refactor over three call sites.

### D2 — The model resolves the time; the app validates, clamps and echoes

`remind(text, when)` takes `when` as an ISO-8601 timestamp. The app:

1. parses with `datetime.fromisoformat`, interpreting a naive value in the
   configured owner timezone (`zoneinfo`) — a naive timestamp is the common case
   and must not silently mean UTC on a UTC container;
2. rejects a due time in the past (beyond a small tolerance for clock skew),
   beyond the horizon (default 365 days), and text over the length limit;
3. **echoes the resolved time back in the tool result**, rendered in the owner's
   timezone with the weekday ("Reminder #12 set for Wednesday 20 August at 07:30").

The echo is the actual safety mechanism. A model that mis-resolves "next Tuesday"
produces a wrong-but-visible confirmation the owner reads in the same reply, rather
than a silent surprise a week later. It also makes the failure mode correctable in
one message (`/reminders cancel 12`).

*Alternatives considered.* An app-side NL parser (`dateparser`): a new dependency,
and a long tail of near-misses the model handles better; a relative-offset-only
tool API (`in_seconds`): forces the model to do the same arithmetic with less
context and makes the echo less checkable. Rejected both. The `/remind` command
path keeps explicit forms only (`HH:MM`, `YYYY-MM-DD HH:MM`, `+90m`) — a command
must be deterministic, and a command is not the place to guess.

### D3 — Every owner turn carries a one-line current-time header

Without it the model cannot resolve any relative time, and a session that has been
open for 55 minutes would resolve "in an hour" against its start time. Per-turn,
not per-session, for exactly that reason. Roughly 15 tokens; it rides the same
content-composition point as the recall block, is delimited as data, and is
**owner-turn-only** — event turns are left untouched, because triage does not
schedule and the untrusted-payload path gains nothing from a wider prefix.

### D4 — Delivery is verbatim, sessionless, and outside the gate

The scheduler sends the stored text through the existing proactive send. No
session is created, no model runs, nothing is composed. The message is prefixed
with a fixed marker so the owner can tell a reminder from a triage message at a
glance, and a late delivery additionally states its original due time.

Authority for that send was granted when the reminder was scheduled — by the owner
directly (`/remind`) or by a gate-authorized standing-tier tool call in an
untainted owner turn. The gate governs *model-initiated* invocations; the scheduler
is app-initiated, like an owner command, and is therefore outside it. This is
stated in the approval-gate delta rather than left implicit, because "a message
reached the owner and no approval was involved" is exactly the sentence a security
reader must not have to reconstruct.

*Alternative considered.* Firing as an agent turn so Henk could add live context
("your backup reminder — it's still failing"). Rejected as the default: it puts
tokens, latency and an API failure mode on the promise-keeping path, and it lets
the model reword what the owner asked for. D5 recovers the conversational half at a
fraction of the cost.

### D5 — The delivered-reminder note: durable, once, on the next owner turn

A delivery sets `delivered_at` and leaves `surfaced_at` null. The next **owner**
turn is prefixed with a delimited note listing deliveries that are not yet
surfaced and fall within a window (default 12h), bounded in count; injecting it
sets `surfaced_at`, so it appears exactly once. It is injected regardless of
whether the recall block was already given this session, because the delivery may
land mid-conversation.

Durability in the store rather than in-process state is the point: a restart
between delivery and the owner's reply must not lose the context, and the 12h
window prevents a delivery from resurfacing days later in an unrelated
conversation.

**The note does not taint the session.** Taint exists because event payloads are
untrusted sensor data. Reminder text cannot be event-derived: `remind` is
owner-turn-only and denied in any tainted session, and the command path is
owner-authored by construction. So the content being replayed is content the owner
either wrote or approved by reading its echo. It is still rendered inside a
delimited data block framed as "messages I sent you", never as instructions —
delimiting is cheap and the framing habit should not develop exceptions.

### D6 — Polling scheduler, send-then-mark, bounded attempts

An async task ticks every `poll_interval_seconds` (default 30), selects pending
reminders with `due_at <= now`, and delivers them oldest-due first. Polling rather
than sleeping-until-the-next-due-time: it needs no wake-up bookkeeping when a
reminder is scheduled or cancelled mid-sleep, and it is inherently robust to
container clock jumps and suspend/resume. The cost is up to one poll interval of
delivery latency, which is immaterial for a reminder and is the honest reading of
"at 18:00" anyway.

Ordering is **send first, then mark delivered**, with an incrementing `attempts`
counter written before the send. A crash between send and mark redelivers on
restart (annoying); the reverse ordering would silently swallow (a broken promise).
A reminder that reaches `max_delivery_attempts` (default 3) without a successful
mark goes to `abandoned` with an audit record and is surfaced to the owner, so a
crash loop cannot spam indefinitely.

The scheduler does **not** ride the core's serial turn queue. It creates no
session and consumes no model, so serializing it would only add latency. It is
also not suppressed by a pending approval: a reminder is a plain message that
cannot be mistaken for an approval prompt, and the existing gate rule (an
unrelated *inbound* message fails the pending action closed) is about what the
owner sends, not what Henk sends.

### D7 — Catch-up: late within the grace window, summarised beyond it

On startup (and on the first tick), pending reminders with `due_at` in the past are
delivered immediately, each marked with its original due time, provided they are
within `late_grace_seconds` (default 24h). Older ones are not delivered as
reminders — a day-old instruction delivered as if current is worse than useless —
but are reported once in a single missed-reminder summary and moved to `missed`.

*Alternatives considered.* Always fire late (a week of downtime dumps a burst of
stale instructions); drop silently (breaks the promise, which is the one thing a
reminder capability cannot do); drop with no report (same). The grace window is
config, so the owner can set it to zero-tolerance or to effectively-always without
a code change.

### D8 — `remind` is standing; cancellation is command-only

`remind` is mutating, **standing**, owner-turn-only — the same containment argument
as `capture` and `store_memory`: an additive write into a Henk-local store whose
only external effect is a message to the owner, receipted every time, denied in
event turns and in any tainted session. Prompting for it would make the fastest
path ("remind me at six") slower than not using Henk.

There is **no cancel tool**. The model can add; only the owner can remove, via
`/reminders cancel <id>`. This mirrors the existing shape — `capture` has no
delete tool and `/inbox done` is command-only — and it means a model error can
only ever produce a spurious reminder the owner can cancel, never the silent loss
of one they were relying on. The asymmetry is deliberate: an extra reminder is
noise, a missing reminder is a broken promise.

### D9 — Audit records carry ids and times, not reminder text

A new `reminder` record type is written at each lifecycle transition
(`scheduled`, `delivered`, `delivered-late`, `missed`, `cancelled`, `abandoned`),
durable at that moment, carrying the reminder id, due time, status, `initiated_by`
(`model` / `owner-command` / `scheduler`) and a timestamp — and **not** the
reminder's text. The store holds the content; the audit log holds the evidence.
This follows the recent tightening that stopped recording tool-result text, and it
keeps a log that gets read and pasted around free of owner-personal free text.
`schema_version` bumps to 4, with v1–v3 documents retained so historical records
still validate.

### D10 — `reminders.enabled` is a kill switch, and it only narrows

False unregisters both tools, makes the three commands reply honestly that the
capability is not configured, and never starts the scheduler. Pending reminders
stay in the store, undelivered, and are picked up (as late or missed, per D7) if
it is re-enabled. Consistent with `events.enabled`, and with the rule that
configuration may narrow authority but never widen it.

## Risks / Trade-offs

- **A reminder is the first clock-driven message, and the attention contract's
  guardrail is now a distinction rather than a prohibition.** → The amended clause
  names what stays banned (system-scheduled digests, heartbeats, "all is well"),
  reminder deliveries are excluded from the incident cap but bounded by their own
  pending cap, and every delivery traces back to an owner-authored or
  owner-echoed schedule in the audit log. A reader can still answer "why did Henk
  message me?" with "because you asked him to, at this time, and here is the
  record".
- **The model can schedule reminders the owner did not clearly ask for.** → The
  tool result echoes the resolved time in the same reply, so a spurious reminder is
  visible immediately; `max_pending` bounds accumulation; cancellation is one
  command; every schedule is receipted.
- **Model time resolution is wrong in a way the owner does not read carefully.** →
  The echo includes the weekday and the local time, which is the form a human
  actually checks. Residual risk accepted: the failure is a reminder at the wrong
  time, recoverable and non-destructive.
- **Send-then-mark can duplicate a delivery across a crash.** → Accepted by
  choice (duplicate beats loss), bounded by `max_delivery_attempts` and an
  `abandoned` terminal state with a receipt.
- **Delivery can land mid-turn, interleaving with a reply or an approval prompt.**
  → It is a plain, marked message; the gate's keyword matching is unaffected
  (it classifies inbound text only). Same property triage messages already have.
- **A long-past reminder resurfacing as a note.** → The delivered-reminder note is
  window-bounded (12h) and once-only via `surfaced_at`.
- **Timezone misconfiguration on rp5** would shift every reminder by the offset. →
  The resolved-time echo makes the first reminder reveal it; the config value is
  validated at load time against `zoneinfo` and startup fails on an unknown zone
  rather than falling back to UTC.
- **Clock jumps (host suspend, NTP step).** → Polling absorbs them: a jump forward
  delivers overdue reminders on the next tick; a jump backwards delays delivery
  rather than losing it.

## Migration Plan

1. Ship with `reminders.enabled: false` and the code registered but inert; the new
   table is created by the existing lazy schema path on first connection
   (`CREATE TABLE IF NOT EXISTS`) — no data migration, no downtime.
2. Add the `reminders` section to the deployed rp5 config, **including the owner's
   timezone** (the repo default carries a placeholder; rp5's config is locally
   modified by design, so this is a host-side edit).
3. Enable, and verify: schedule a reminder two minutes out via the tool and one via
   `/remind +2m`; confirm both echo the resolved local time, both deliver verbatim,
   the follow-up turn shows Henk knows what he sent, `/reminders` lists and cancels,
   and the audit log carries `scheduled` + `delivered` records with no reminder text.
4. Verify catch-up explicitly: schedule one for +1m, stop the container, restart
   after it is due, confirm the late-marked delivery.
5. **Rollback:** set `reminders.enabled: false` and restart. Scheduled reminders
   remain in the store; nothing is deleted. No schema rollback is needed — an
   unused table is inert, and audit records already written under v4 stay valid
   against their committed schema document.

## Open Questions

- **Owner timezone as config vs. a fixed `Europe/Amsterdam`.** Config is specced;
  the fixed value would be simpler but bakes a personal fact into code. Resolved in
  favour of config unless the owner objects at apply time.
- **Should `/remind` accept a bare weekday (`/remind mon 09:00 ...`)?** Not specced
  — it is the first step onto the NL-parsing slope the app deliberately avoided. Can
  be added later without a spec change to anything but this capability.
- **Delivery marker wording** ("⏰ Reminder:") is a product detail settled at
  implementation; the spec requires only that a reminder be distinguishable from a
  triage message and that a late one state its original due time.
