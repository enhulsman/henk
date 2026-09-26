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
   incomplete-triage notice (errored) goes out through `_send_proactively`.

`turn.announceable` is decided before the turn is queued, by `EventPipeline._apply_cap`
(`henk/events/pipeline.py`), and is its only source. Event turns are framed as tainted, so
the gate blocks every mutating tool in them and no approval prompt can occur in a triage today.

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
before the turn was queued, and `_process_event` sends exactly when it is true. So an
indicator never ends in silence.

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

The early `return` for a non-announceable turn sits outside any bracket, because that turn
never entered one.

*Alternative.* Bracketing only `run_turn`: would drop the indicator during the audit flush and
the send, and split the close across outcome branches, as the owner-turn design already
rejected.

### D3 — Reuse the bracket and its wiring unchanged

`_working()` and `working_indicator` are reused as they are. `henk/channel/acknowledge.py`,
`henk/runtime.py`, `henk/app.py` and config are untouched. The bounds (one acknowledge
timeout per operation and for the close), cancellation (no stop, task awaited), once-per-turn
logging and the pause predicate all carry over. `paused=gate.has_pending` is never true during
a triage today (tainted turns cannot prompt), so it costs nothing now and is already correct
for roadmap item 5.

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

- **Typing starts late relative to the alert** (debounce, up to about 120 s). → Accepted and
  stated in the proposal; the indicator marks the turn, not the alert.
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

None, pending the owner's two decisions in the proposal.
