# Design — Triage Working Indicator

## Context

`owner-acknowledgement` (archived 2026-09-26) shipped `OwnerAcknowledgement.working`, a bounded
typing-indicator bracket, and wired it into `AgentCore` as `working_indicator`. The core enters
it only in `_process_owner`, through `AgentCore._working()`, which returns a `nullcontext()`
when nothing is wired. Event turns were excluded on purpose.

Current event path (`henk/agent/core.py`, `_process_event`, read 2026-09-26):

1. `_start_event_session(turn)` displaces any open session and starts the incident session.
2. The accumulator records the incidents; recall and the handoff digest are composed in.
3. The turn runs inside `_framed_turn(TurnType.EVENT, announceable=turn.announceable)`.
4. The ending is classified, the triage recorded, and `_flush_event_triage` writes the audit
   record and advances the checkpoint. On a genuine audit failure it also sends the
   one-shot degraded-durability notice.
5. **Only when `turn.announceable`**, the triage message (completed) or the
   incomplete-triage notice (a turn that did not complete: an error, a refusal or no
   reply) goes out through `_send_proactively`.

`turn.announceable` is decided before the turn is queued, by `EventPipeline._apply_cap`
(`henk/events/pipeline.py`), and is its only source.

The turn is queued after the debounce, which is a fixed window, not a quiet period: the
first event opens it and the batch is dispatched when it closes, however many events
arrived meanwhile (`henk/events/coordinator.py:108-120`, `deadline = mono_clock() +
window` at `:110`). So the triage turn starts about 120 s after the first alert, later if
the core's serial queue is busy with another turn.

Every registered mutating tool is owner-turn-scoped (`henk/tools/base.py:60,95`; the four
overrides in `henk/tools/memory.py`, `capture.py` and `reminders.py` all declare
`(TurnType.OWNER,)`), so an event turn's mutating calls are denied `out-of-scope` before
any prompt (`henk/gate/approval.py:266-273`). Taint governs owner turns in a tainted session
(`approval.py:274-281`). A future EVENT-scoped per-instance tool (roadmap item 5) would
prompt inside an announceable triage; in a non-announceable one it is `SUPPRESSED` without
a prompt (`approval.py:238-246`).

## Goals / Non-Goals

**Goals:** the owner sees "typing" for as long as a triage that will reach them is running,
and never for one that will not; no new failure mode, bound or setting.

**Non-Goals:** typing for cap-suppressed triages, owner commands, or non-turn proactive sends
(reminder deliveries, the degraded-durability notice on its own); any change to the cap,
debounce, or what a triage sends.

## Decisions

### D1 — Bracket on `turn.announceable`, known before the turn starts

The core enters `self._working()` when `turn.announceable` is true, and a `nullcontext()`
otherwise. `announceable` is the send decision itself, not a prediction of it: the cap ran
before the turn was queued, and `_process_event` sends exactly when it is true. So a triage
the cap has not held back always ends in a message unless the triage itself fails between
the turn and the send (for example the recurrence `handoff_sink` raising inside
`_flush_event_triage`) or the delivery fails; the indicator is stopped on every exit either
way. An audit write failure is not such a case: `AuditLog.write` catches `OSError` and
returns `False`, which leads to the degraded-durability notice and then the send.

*Alternatives.* Bracketing every event turn: a cap-suppressed triage would show typing and
then send nothing, a false promise. Deciding after the turn: too late, since the indicator is
only useful while the turn runs.

### D2 — Placement: from before the incident session starts to after the proactive send

The bracket encloses steps 1-5. Opening before `_start_event_session` mirrors the owner
turn, where the bracket opens before `_ensure_session`: an exception there passes through
the bracket's close. Closing after the proactive send mirrors the owner turn's "after the
reply is sent": the owner's wait ends when the message lands, and Signal clients clear typing
when a message from that sender arrives.

The degraded-durability notice, when a genuine audit failure triggers it, is sent inside the
bracket. That is accurate: more is coming, since the triage message follows it.

The suppressed path runs under `nullcontext()`; the `if not turn.announceable: return` at
`core.py:506` is unchanged.

*Alternative.* Bracketing only `run_turn`: would drop the indicator during the audit flush and
the send, and split the close across outcome branches, as the owner-turn design already
rejected.

### D3 — Reuse the bracket and its wiring unchanged

`_working()` and `working_indicator` are reused as they are. `henk/channel/acknowledge.py`,
`henk/runtime.py`, `henk/app.py` and config are untouched. The bounds (one acknowledge
timeout per operation and for the close), cancellation (no stop, task awaited), once-per-turn
logging and the pause predicate all carry over. `paused=gate.has_pending` is never true during
a triage today: every registered mutating tool is owner-turn-scoped
(`henk/tools/base.py:60,95`), so an event turn's mutating calls are denied `out-of-scope`
before any prompt (`henk/gate/approval.py:266-273`), and taint governs only owner turns in a
tainted session. A future EVENT-scoped per-instance tool (roadmap item 5) would prompt inside
an announceable triage. The pause test in task 1.2 proves the pause for event turns with a
test-only EVENT-scoped tool, so item 5 inherits a tested behaviour.

### D4 — No new flag

`signal.acknowledge_owner` governs this too. A triage indicator without the chat indicator, or
the reverse, has no scenario, and one flag is one rollback. This is the owner's decision 2 in
the proposal; if they choose a separate flag, D4 changes to a new `from_dict`-pinned key with
the same two-test pattern as `acknowledge_owner`.

### D5 — The replay is unaffected

`henk/replay` never constructs `AgentCore` (the only construction is `henk/runtime.py`), and
`core.py` still imports nothing from `henk.channel.acknowledge`. The replay-isolation tests
must pass untouched.

## Risks / Trade-offs

- **Typing starts late relative to the alert**: about 120 s after the first event, because
  the debounce is a fixed window opened by that event (`coordinator.py:108-120`), and later
  when the serial queue is busy with another turn. → Accepted and stated in the proposal;
  the indicator marks the turn, not the alert.
- **Typing that ends with nothing arriving.** A FAILED or PARTIAL delivery of the triage
  message stops typing with nothing (or only part) arriving, and an exception between the
  turn and the send (the `handoff_sink` raising inside the flush) stops it with no send at
  all. → The same accepted residual as an owner turn's failed reply (owner-acknowledgement
  D3); the delivery outcome is logged, and the exception still reaches `AgentCore.run`'s
  logger as today.
- **Typing right after a reply, when a triage is queued behind an owner turn.** The owner
  sees Henk's reply, then "typing" again at once. → The cue is accurate: Henk is working on
  something for the owner, and the triage message that follows explains it. That the
  triage displaces the owner's open conversation predates this change.
- **A long triage means many refreshes** (about one per 7 s for a minute-long Opus turn). →
  Negligible; the same cost an equally long owner turn already has.
- **A hung stop after a triage holds the next queued turn by at most one acknowledge
  timeout.** → The same bound as owner turns, tested at the core level.
- **Displacing an open owner conversation.** The incident session already displaces it today;
  the serial queue means a triage indicator and a chat indicator never overlap.

## Migration Plan

Image rebuild and `up -d henk` with the README's redeploy recipe. No config edit. Rollback:
`signal.acknowledge_owner: false`, which now also turns off triage typing, or the previous
image, tagged before deploy.

## Open Questions

None, pending the owner's three decisions in the proposal.
