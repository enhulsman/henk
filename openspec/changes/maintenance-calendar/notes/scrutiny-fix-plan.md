# maintenance-calendar — fix plan (round 2)

Responds to round-1 findings 1–21. Verified before writing: token-place writes
`~/.config/<consumer>/<name>-token` files (bin/token-place header + DISP_FILE), the intake
checkpoint is a single last-write-wins id (henk/events/checkpoint.py:1-14), `submit_event`
is a queue put (core.py:300-302). All three findings that rest on those hold.

## A. Hold/release rework (findings 1, 2, 3, 4, 10, 11, 12)

A1. **Windows become rows.** New table `hold_windows(window_id PK, title, start, end,
    released_at NULL)` beside `held_events(event_id PK, window_id FK, identity_key, state,
    title, message, raw_json, published_at, arrival_time, held_at)`. A window row is
    inserted (`INSERT OR IGNORE`) when its first event is held; its `end` is the frozen end.

A2. **Hold only into unreleased windows (1b).** `active(t)` result is filtered against
    `hold_windows.released_at IS NULL`; an event whose only active window is already
    released goes to the normal pipeline. Scenario: "late backlog after a release" — an
    event published inside W that arrives after W released is triaged normally, W is not
    released again, no second `hold-release`.

A3. **Idempotent holds (11).** `held_events` PK is the ntfy event id; `INSERT OR IGNORE`;
    a duplicate writes no `hold` record. Scenario: replay of an already-held event.

A4. **Store `arrival_time` (12)** so the released `Event` is rebuilt faithfully.

A5. **Fail open on a hold write failure (3).** If the `held_events` row or the `hold`
    record cannot be written, that event goes to the normal pipeline as if no window
    existed. The checkpoint scenario is replaced by: "held-only batch advances via the
    marker once rows and records are durable" and "a hold that cannot be written → the
    event is triaged normally". Ordering: row first, then record; a record failure after a
    row success keeps the hold (the row is the durable fact; the audit failure is logged
    loud per *Audit failures are loud but non-blocking*).

A6. **Release only after intake has caught up (1a).** Coordinator tracks `caught_up`:
    set when, since the latest (re)subscribe, intake has yielded a keepalive frame AND the
    debouncer is empty AND no `dispatch_batch` is running. A window is eligible when
    `frozen_end + grace <= now` AND (`caught_up` OR the eligibility instant passed more
    than 15 minutes ago — the fallback for ntfy being unreachable, where nothing more can
    arrive anyway). Intake exposes keepalive receipt through its existing proof-of-life
    hook (intake.py `_note_proof_of_life`). Task 6.0 (new, first in group 6): probe on rp5
    that ntfy delivers every cached `since=` message before the first keepalive; if the
    probe disagrees, the code follows the probe (fallback: `caught_up` = a full
    keepalive interval with no message frame after subscribe). Scenario: restart across
    the window end with the resolve in the backlog → the identity counts as recovered,
    one release, no triage.

A7. **Mark released at durable flush (2).** `EventTurn` gains `release_window_id`. The
    coordinator keeps an in-memory in-flight set so the tick does not re-release a
    submitted window. The core, after `_flush_event_triage` writes the triage record
    successfully, calls a `release_sink(window_id)` (same shape as `_handoff_sink`,
    core.py:567-568) that writes `hold-release` and sets `hold_windows.released_at`. A
    release that produces no turn (all recovered, or every forwarded identity cooled
    down) marks itself directly. A release batch never carries an offset, so the
    checkpoint is untouched. Tests: kill between submit and flush → window unreleased at
    restart and released again; checkpoint file byte-identical across a release.
    Spec text "marked released after submission" replaced accordingly; "at most one
    `hold-release` per window_id" enforced by the `released_at IS NULL` guard in the sink.

A8. **Extension rule tightened (4).** Defer only when the current snapshot holds the same
    window id, it is active now (`start <= now < end + grace`), and its start is at or
    before the earliest held `published_at` of that window. Otherwise release at the
    frozen end. Added scenario "moved, not extended": a running window moved to next week
    → released at the frozen end + grace.

A9. **Release independent of `calendar.enabled` (10).** The release task runs whenever
    `hold_windows` has unreleased rows, enabled or not (the extension check is skipped
    when disabled). Framing for a release more than 24 h after the window's end states the
    hold age and tells triage the live state is unknown and must be checked. No new
    "expired" state. Rollback text in the design updated.

## B. Corrections of false claims (5, 7, 8, 16, 18, 21)

B1. **Weekday in the argument (5).** `start`/`end` format becomes `Ddd YYYY-MM-DD HH:MM`
    (schema pattern `^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{4}-\d{2}-\d{2} \d{2}:\d{2}$`);
    execution refuses a weekday that does not match the date. The verbatim prompt in the
    proposal and the approval-gate scenario become `start: <<<Sat 2026-11-07 20:00>>>`.
    The design sentence "the owner reads the prompt with that sentence above it" is
    deleted, with the reason (run_turn returns text only at turn end, sdk_session.py:597-615).
    The system-prompt rule keeps "only at the owner's request", drops "name the weekday
    first".

B2. **One-calendar claim (7).** The secure-deployment scenario becomes a code guarantee:
    every request the client can issue targets the configured calendar id or the token
    URI; no tool or config path can supply another id. Secure-deployment adds: domain-wide
    delegation SHALL NOT be enabled for the service account. Design D1/D4 state that the
    comms leg rests on the client never serialising `attendees` (test asserts the insert
    body's key set exactly), not on Google ACLs.

B3. **token-place (8).** D1 and the proposal bullet: `.env` is kept because
    secure-deployment says secrets reach the agent only through its process environment
    and it keeps one pattern; the token-place sentence is removed. Task 8.3 becomes:
    owner runs `base64 -w0 key.json` into a mode-600 file on rp5, appends
    `GOOGLE_CALENDAR_SA_KEY_B64=<that>` to henk's `.env` by hand (or token-places the file
    and moves the value in), then deletes the plaintext JSON and the intermediate file.

B4. **Prompt equals written value (16).** `title` additionally refuses leading/trailing
    whitespace, runs of whitespace, newlines and the sequences `<<<`/`>>>`; the tool
    refuses any argument other than the three. So `_render_argument`'s normalisation
    (approval.py:441-454) is the identity on every accepted value; a test asserts it.

B5. **DST (18).** Insert sends `dateTime` as RFC 3339 with the computed offset (fold=0 for
    ambiguous times) plus `timeZone`; the receipt's offset equals what was sent.

B6. **Key out of the subprocess env (21).** After config decodes the key, runtime pops
    `GOOGLE_CALENDAR_SA_KEY_B64` from `os.environ` before the SDK client is built; test
    asserts it is absent from `os.environ` after `build_runtime`.

## C. Owner-facing text (6, 13, 14, 15, 19)

C1. **Every short `Homelab` event holds alerts (6).** Proposal states it plainly under
    "What changes for you" and adds owner decision 2: (i) every timed `Homelab` event of 8 h
    or less is a hold window [pick, moderate — NORTH-STAR row 6 itself lists the Dawarich
    evening as maintenance], or (ii) only titles starting `Homelab maintenance`.
    `calendar_create_event`'s description says it, and its result reads, verbatim:
    `Created "Homelab: patch round", Sat 7 Nov 20:00–22:00. Alerts to Henk are held until 22:30.`
    `calendar_upcoming` marks hold windows with `(holds alerts)`.

C2. **Counts (13).** "Alerts" in the note, the release line and `hold-release` all mean
    distinct alert identities; stated in the agent-core and audit specs.

C3. **Release line variants (14).** recovered = 0:
    `(Held during "Homelab: patch round", 20:00–22:30: 1 alert.)` / `...: 2 alerts.)`;
    recovered > 0: `(Held during "Homelab: patch round", 20:00–22:30: 4 alerts, 3 of which recovered on their own.)`.
    The line is appended to whatever the released turn sends, including the incomplete-
    triage notice path (core.py:538-545). Both variants shown verbatim in the proposal.

C4. **Decision 1 framing (15).** Rewritten: NORTH-STAR allows messages the owner asked for,
    so a standing request for a post-window summary is legitimate (option (b)). The case
    for (a) is whether the owner would read a "4 held, 4 recovered" message or mark it
    read — the failure mode the attention contract names.

C5. **Time rendering (19).** `calendar_upcoming` lines and the owner-turn note use the
    reminders' shared renderer (weekday + zone). The release line keeps compact `HH:MM`
    and the spec states that exception (both times are the same evening, and the line
    names the window).

## D. Spec homes and collisions (9, 17, 20)

D1. **Collisions (9).** Proposal Impact and the standing rule in tasks name all three
    MODIFIED overlaps with `runbook-actions`: audit-log *Schema is versioned*,
    secure-deployment *Scoped secrets only*, approval-gate *Mutating tools declare a turn
    scope* (D3 below). Task: whichever archives second rebases its full MODIFIED text onto
    the archived version (carrying the other's version paragraph, secret entries and tool
    list), not just renumbers.

D2. **Non-admitted titles (17).** maintenance-calendar spec: non-admitted items are never
    logged, persisted, or passed to the model. The list request adds `q=Homelab`
    (defence in depth; `admit()` still decides). Pagination: follow `nextPageToken` up to 4
    pages (1000 items); beyond that the call fails loudly.

D3. **Stale enumerations (20).**
    - Add an `incident-triage` delta: ADDED requirement "Released alerts are triaged as
      survivors of a maintenance window" (framing: fired during the named window, still
      not resolved after end + grace, check live state; hold age when > 24 h).
    - approval-gate: MODIFY *Mutating tools declare a turn scope, enforced per session* to
      add `calendar_create_event` to the owner-only list (full requirement copied).
    - secure-deployment *Scoped secrets only*: the kept scenario "The publisher credential
      is not in the stack" drops "the enumerated secret set is unchanged from before the
      change" and keeps "the publisher's write-only token appears nowhere in the stack".

## E. Round-3 amendments (round-2 findings 1–10). Where these conflict with A–D, E wins.

E1. **A6 fallback clock (r2-1).** Replaces A6's fallback clause. A window is eligible when
    `frozen_end + grace <= now` AND (`caught_up` OR `max(frozen_end + grace,
    process_first_subscribe_attempt) + 15 min <= now`). The fallback therefore counts from
    this process's first subscribe attempt, never from a past instant. The rationale
    sentence "nothing more can arrive anyway" is deleted; the fallback exists so ntfy
    being down cannot hold alerts forever, and A2/E3 route anything arriving later to the
    normal pipeline. Added scenario: Henk down 21:50–23:45 across a window whose
    end + grace is 22:30, backlog holds identity A's resolve → exactly one release, A
    counted recovered, no triage.

E2. **A6 caught-up signal (r2-2).** Replaces A6's hook sentence. Intake gains a dedicated
    `on_keepalive` callback invoked only for frames with `event == "keepalive"`, separate
    from `_note_proof_of_life` (which fires for message frames too, intake.py:227-228);
    it is reset on every (re)subscribe. The debounce batch moves from a local in
    `run()` (coordinator.py:107-118) onto the coordinator instance. `caught_up` = a
    keepalive since the latest subscribe AND the pump queue (coordinator.py:149) empty AND
    no open debounce batch AND no `dispatch_batch` in progress. Test: cached message
    frames without a keepalive leave `caught_up` false; the keepalive after them sets it
    once the batch has been dispatched.

E3. **In-flight windows are closed to new holds (r2-3).** A2's filter becomes: a window
    is closed when `released_at IS NOT NULL` OR its id is in the coordinator's in-flight
    set; an event whose only active window is closed goes to the normal pipeline. Added
    scenario: an event for W dispatched while W's release is in flight → triaged
    normally, W released once, one `hold-release`.

E4. **Sink firing rule (r2-4).** The release sink fires when the triage flush returns
    true, or when audit is not configured (`self._audit is None`, the designed no-op at
    core.py:899-900). A genuine flush failure, or a session-setup error before the flush
    (core.py:457), leaves the window in flight and unreleased until restart, when it is
    released again; the durability latch's existing notice already tells the owner.
    Stated in design D8 and in the event-intake release requirement.

E5. **Suppressions in a release (r2-5).** Forwarded identities the pipeline cools down get
    `suppression` records exactly as `dispatch_batch` writes them (coordinator.py:70-81).
    One sentence in the release requirement plus a scenario.

E6. **A5 wording (r2-6).** The held-only marker condition is "every held row durable". A
    failed `hold` record is logged loud and does not block the marker or undo the hold.

E7. **Drop `q=Homelab` (r2-7).** D2's server-side `q` is removed; `admit()`'s prefix rule
    is the only filter, so admission is exactly what the spec states. The 4-page
    pagination bound stays.

E8. **Conditional hold wording (r2-8).** The tool result's second sentence and the
    `(holds alerts)` marker appear only when the event will actually hold: `calendar.enabled`,
    `calendar.hold_alerts` and `events.enabled` all true, timed, at most
    `max_window_hours` (and, under decision 2(ii), the maintenance prefix). Times in the
    result go through the shared renderer; the verbatim text in the proposal is updated
    to the renderer's output form, shown as an example with the renderer named.

E9. **Locale-free weekday (r2-9).** B1's weekday check uses a fixed
    `("Mon","Tue","Wed","Thu","Fri","Sat","Sun")` table indexed by `date.weekday()`, never
    `strptime("%a")`; test under a non-English locale (`LC_TIME=nl_NL.UTF-8` where
    available, else monkeypatched `locale`).

E10. **Duplicate check before routing (r2-10).** Before A2/E3 routing, an event whose id is
    already in `held_events` is dropped as a duplicate (no record, no triage), whatever its
    window's state. Scenario: replay of a held event after its window released → dropped.

## F. Round-4 amendments (round-3 findings 1–3). F wins over E.

F1. **Duplicate-only batches advance (r3-1).** The hold partition never returns early: it
    hands the pipeline the remaining (possibly empty) survivor list, so a batch whose events
    are all duplicates, held or suppressed advances through the existing marker
    (coordinator.py:96-99), subject to E6's "every held row durable". The E10 scenario
    asserts the checkpoint advanced past the duplicate-only batch.

F2. **Dispatch-in-progress span (r3-2).** `dispatch_in_progress` is set at the moment the
    instance-level debounce batch is flushed (today's `debouncer.flush()` site,
    coordinator.py:117-120), before the batch leaves the instance, and cleared only when
    `dispatch_batch` returns or raises (`finally`), across its `await self._core.submit_*`
    calls. Stated in design D8. Test: a release tick while `dispatch_batch` is suspended on
    its submit sees `caught_up` false.

F3. **Sink trigger is the audit write's `ok` (r3-3).** E4's wording becomes: the release sink
    fires when the triage's `_write_audit_record` returned ok, or audit is not configured;
    the call sits beside the existing `_handoff_sink` call after core.py:563, not on
    `_flush_event_triage`'s return value (which is the handoff id, core.py:551). The early
    return when `_acc is None or _acc.flushed` (core.py:561) means a second flush call
    never fires the sink twice.

F4. **Task 6.0 is a hard gate (recommendation).** No group-6 code before the ntfy
    keepalive-ordering probe is recorded in `notes/ntfy-keepalive-probe.md`.

## Ledger
- Round 1: REJECT — 2 blocking, 7 major, 12 minor.
- Round 2: REJECT — 3 blocking (A6 ×2, A2/A7), 1 major, 6 minor. Groups B–D closed.
- Round 3: APPROVE WITH CONCERNS — 0 blocking, 0 major, 3 minor (folded in as F).
