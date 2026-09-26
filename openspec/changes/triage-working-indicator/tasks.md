# Tasks — Triage Working Indicator

> **Tests first, from the delta scenarios.** Each scenario gets at least one test, each
> SHALL/MUST at least one assertion. Standing rules inherited from `owner-acknowledgement`
> (archived `2026-09-26-owner-acknowledgement/tasks.md`): exercise a real `SignalAdapter` over
> `FakeBridge` for failure, hang and cancellation tests; wrap every await on a hanging bridge
> in `asyncio.wait_for(…, 2.0)`; placeholders only (owner `+31600000000`, Henk
> `+31611111111`, stranger `00000000-0000-4000-8000-000000000000`); elapsed-time bounds in
> `[0.9T, 1.5T)`, never a loose margin. **Hard stop before any deploy to rp5: explicit owner
> go required.** Record decisions and mutation results in `notes/apply-decisions.md`.
>
> **Blocked on the owner's two proposal decisions** (which triages; which flag). The tasks
> below assume the recommended picks (announceable only; `signal.acknowledge_owner`). If the
> owner picks otherwise, the tasks change before any code is written.

## 1. Core tests (`tests/test_agent_core_acknowledgement.py`)

- [ ] 1.1 **Replace** `test_event_turns_are_not_bracketed` (its scenario is removed by the
      agent-core delta; this is a spec change, not a weakened test): it becomes the
      *A cap-suppressed triage shows no indicator* test, a non-announceable `EventTurn` with
      no `acks`, its audit record written, and nothing in `calls`.
- [ ] 1.2 New tests over a real `OwnerAcknowledgement`, one per agent-core ADDED scenario:
      - *A sent triage is bracketed*: announceable completed turn. `start` precedes the
        session's first `run_turn` content; `stop` follows the `("proactive", …)` entry in
        `calls`, on one shared ordered list;
      - *An errored sent triage clears after its notice*: the session raises; `stop` follows
        the incomplete-triage notice;
      - *starting the incident session raises*: `stop` sent, no task left running;
      - *Cancellation during a sent triage sends no stop*: cancel the worker mid-turn;
        `CancelledError` propagates out of `process` and `run`, no `stop`;
      - *A hung stop after a triage does not hold the next turn beyond the bound*: real
        `SignalAdapter` + `FakeBridge` with `ack_faults["stop"]` never set, an owner message
        queued behind the triage; the triage message is in `bridge.sends` and the next
        `run_turn` begins within `[0.9T, 1.5T)` of the close starting;
      - *Disabled acknowledgement leaves event turns unchanged*: `working_indicator=None`;
        session content and the proactive send byte-identical to an unwired run;
      - a genuine audit failure (degraded-durability notice) inside a sent triage: both
        proactive sends precede the `stop`.

## 2. Guard (`tests/test_acknowledgement_guards.py`)

- [ ] 2.1 One `App.run` harness case (channel-adapter scenarios *A triage the owner will
      receive shows the indicator* / *… will not receive shows nothing*): submit an
      announceable and a non-announceable `EventTurn` through the core, over a real
      `SignalAdapter` + `FakeBridge(hold_open=True)`. `FakeBridge.typing` holds exactly one
      start…stop span, only owner-addressed, closing after the announceable triage's send, and
      nothing for the cap-suppressed one. `FakeBridge.sends` equals the triage's chunks alone.

## 3. Implementation (`henk/agent/core.py`)

- [ ] 3.1 In `_process_event`, enter `self._working()` when `turn.announceable`, else
      `nullcontext()`, from before `_start_event_session` to after the proactive send (design
      D2). The non-announceable early return stays outside any bracket. `_framed_turn`,
      `_flush_event_triage` and the send logic are unchanged. Update the module docstring's
      bracket bullet: event turns that will be sent are bracketed too.
- [ ] 3.2 Confirm untouched and green: `tests/test_replay_isolation.py`,
      `tests/test_replay_grade.py`, `tests/test_graceful_shutdown.py`, the event-path tests.

## 4. Mutation check

- [ ] 4.1 Apply each alone, confirm red on an assertion (or the fail-fast bound), restore,
      and record the table:

      | # | Mutant | Must be caught by |
      |---|---|---|
      | T1 | bracket every event turn (ignore `announceable`) | 1.1, 2.1 |
      | T2 | no bracket on event turns (the old behaviour) | 1.2 sent-triage test, 2.1 |
      | T3 | bracket closes before the proactive send | 1.2 sent-triage and errored tests |
      | T4 | bracket opens after `_start_event_session` | 1.2 session-start-raises test |
      | T5 | stop sent on the cancellation path | 1.2 cancellation test |
      | T6 | close's bound removed | 1.2 hung-stop test |

## 5. Documentation

- [ ] 5.1 README `## Architecture` paragraph on acknowledgement: typing also shows while a
      triage that will be sent to the owner runs, and not for a cap-suppressed one. The
      `signal.acknowledge_owner` bullet names triage typing. The owner-acknowledgement
      deploy-verify checklist gains a triage item (7.2 below).

## 6. Verification and close-out

- [ ] 6.1 Spec→test conformance sweep by a fresh reviewer (`project-scrutinizer`), scenario by
      scenario, including the bound tests' magnitude.
- [ ] 6.2 Full suite green; record the count against the pre-change baseline.
- [ ] 6.3 `openspec validate triage-working-indicator --strict`, then commit.

## 7. Deploy verification (owner-run on rp5, root shell)

- [ ] 7.1 Tag the running image as the rollback target, redeploy with the README recipe, apply
      the three "silently did nothing" tells.
- [ ] 7.2 **A sent triage shows typing.** Stage a harmless real event, as `triage-quality`
      13.4 did (stop rp2's `node_exporter` for 10 min behind an on-host restart guard). Once
      the triage turn starts, "typing" shows in the Henk chat until the triage message lands.
      `$C logs henk --since 30m | grep -E 'owner working indicator'` prints nothing.
- [ ] 7.3 **Shutdown mid-triage (optional).** `$C restart henk` during a triage stops
      within the grace period; "typing" clears on its own within about 15 s.
- [ ] 7.4 Record an *As-built* section here, then `openspec archive triage-working-indicator
      --yes`.
