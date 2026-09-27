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
> **Blocked on the owner's proposal decisions** 1 and 2 (which triages; which flag). The tasks
> below assume the recommended picks (announceable only; `signal.acknowledge_owner`). If the
> owner picks otherwise, the tasks change before any code is written. Decision 3 (ship alone,
> or hold for roadmap item 5) gates only group 7.

## 1. Core tests (`tests/test_agent_core_acknowledgement.py`)

"The shared ordered list" below is `OrderedChannel.order` in the existing test file: every
send, every typing request and every content a session receives, in one list.

- [x] 1.1 **Replace** `test_event_turns_are_not_bracketed` (its scenario is removed by the
      agent-core delta; this is a spec change, not a weakened test): it becomes the
      *A cap-suppressed triage shows no indicator* test, a non-announceable `EventTurn` with
      no `acks`, its audit record written (`announceable: false`), and no typing request and
      no send in the shared ordered list.
- [x] 1.2 New tests over a real `OwnerAcknowledgement`, covering every agent-core ADDED
      scenario:
      - *A sent triage is bracketed*: announceable completed turn. `start` precedes the
        session's first `run_turn` content; `stop` follows the `("proactive", …)` entry, on
        the shared ordered list;
      - *A sent triage that did not complete clears after its notice*: parametrized over the
        three endings, the session raising, a refusal, and no reply; `stop` follows the
        incomplete-triage notice in every case;
      - *starting the incident session raises*: `stop` sent, no task left running, the
        exception propagates;
      - *An exception inside the bracket still stops the indicator*: `handoff_sink` raising
        inside the flush (the session published a handoff); acks `[start, stop]`, no
        proactive send, no task left, and the exception propagates out of `process` and to
        `AgentCore.run`'s logger as today;
      - *Cancellation during a sent triage sends no stop*: cancel the worker mid-turn;
        `CancelledError` propagates out of `process` and `run`, no `stop`;
      - cancellation at the bracket's entry (the "delivered while the bracket is being
        entered" clause): create the task, `await asyncio.sleep(0)` once, assert it is not
        done, cancel; assert it was cancelled, `factory.created == []` (proof the cancel
        landed at the bracket's entry yield, which runs before `_start_event_session`), no
        task left, no `stop`;
      - *A hung stop after a triage does not hold the next turn beyond the bound*: real
        `SignalAdapter` + `FakeBridge` with `ack_faults["stop"]` never set, an owner message
        queued behind the triage; the triage message is in `bridge.sends` and the next
        `run_turn` begins within `[0.9T, 1.5T)` of the close starting;
      - *Disabled acknowledgement leaves event turns unchanged*: parametrized over the keyword
        omitted, an explicit `working_indicator=None`, and enabled, as the owner-turn
        test `test_the_content_and_the_reply_are_unchanged_by_the_indicator` does
        (`tests/test_agent_core_acknowledgement.py:276-309` at the change's base commit). The session
        content equals `compose_event_turn_content(...)` for the turn and the proactive text
        equals the triage reply, in all three; enabled acks `[start, stop]`, the other two
        none;
      - a genuine audit failure (degraded-durability notice) inside a sent triage: both
        proactive sends precede the `stop`;
      - the approval pause in a sent triage (`test_the_indicator_pauses_during_an_approval_in_a_sent_triage`):
        a test-only per-instance tool with `turn_scope=(TurnType.OWNER, TurnType.EVENT)`
        (`EventScopedTool` in `tests/test_gate_authorization.py`), a real `ApprovalGate`, an
        announceable `EventTurn` whose session invokes that tool through the real
        `decide_tool_permission`, and the acknowledgement's `paused=gate.has_pending` on that
        gate, FakeTime-driven like the owner-turn approval test. Assert a stop within one
        poll of the prompt, no start while pending, and a start after `gate.deliver("yes")`
        and before the turn's final stop. Taint blocks only tools without `TurnType.EVENT` in
        scope (`henk/gate/approval.py:266-281`), so the tool reaches `_prompt_and_wait` in an
        announceable event turn with no special taint handling;
      - a long sent triage refreshes (`test_a_long_sent_triage_refreshes_the_indicator`):
        FakeTime; start at 0, refreshes at 7 s and 14 s, stop after the proactive send.

## 2. Guard (`tests/test_acknowledgement_guards.py`)

- [x] 2.1 One cap-driven case (channel-adapter scenarios *A triage the owner will receive shows
      the indicator* / *… will not receive shows nothing*): `EventCoordinator.dispatch_batch`
      with a real `EventPipeline(PipelineConfig(cap_per_24h=1))`, two batches with different
      identities, and the real core worker with a real `OwnerAcknowledgement` over a real
      `SignalAdapter` + `FakeBridge`. `FakeBridge.typing` holds exactly one start…stop span,
      all owner-addressed, closing after the first triage's send, and nothing for the second;
      `FakeBridge.sends` holds only the first triage's chunks; the second triage's audit
      record has `announceable: false`. The `App.run` harness is not used.

## 3. Implementation (`henk/agent/core.py`)

- [x] 3.1 In `_process_event`, enter `self._working()` when `turn.announceable`, else
      `nullcontext()`, from before `_start_event_session` to after the proactive send (design
      D2). The suppressed path runs under `nullcontext()`; the `if not turn.announceable:
      return` at `core.py:506` is unchanged. `_framed_turn`, `_flush_event_triage` and the
      send logic are unchanged. Update the module docstring's bracket bullet (event turns
      that will be sent are bracketed too) and the stale comments: `henk/runtime.py:270-271`,
      the `_working_indicator` comment at `core.py:278-283`, and the `_working` docstring
      (`core.py:413-421`).
- [x] 3.2 Confirm untouched and green: `tests/test_replay_isolation.py`,
      `tests/test_replay_grade.py`, `tests/test_graceful_shutdown.py`, the event-path tests.

## 4. Mutation check

- [x] 4.1 Apply each alone, confirm red on an assertion (or the fail-fast bound), restore,
      and record the table:

      | # | Mutant | Must be caught by |
      |---|---|---|
      | T1 | bracket every event turn (ignore `announceable`) | 1.1, 2.1 |
      | T2 | no bracket on event turns (the old behaviour) | 1.2 sent-triage test, 2.1 |
      | T3 | bracket closes before the proactive send | 1.2 sent-triage and not-completed tests |
      | T4 | bracket opens after `_start_event_session` | 1.2 session-start-raises and entry-cancel tests |
      | T5 | stop sent on the cancellation path | 1.2 cancellation test |
      | T6 | close's bound removed | 1.2 hung-stop test |
      | T7 | in `_process_event`, frame the event turn with `announceable=False` passed to `_framed_turn` (the gate then SUPPRESSES and never prompts) | 1.2 approval-pause test |

      T7 is a production mutant of this change's own code path. The shared `_indicate` pause
      branch is already covered by owner-acknowledgement's mutants (M4, M4 core) and is not
      counted here.

## 5. Documentation

- [x] 5.1 README `## Architecture` paragraph on acknowledgement: typing also shows while a
      triage that will be sent to the owner runs, and not for a cap-suppressed one. The
      `signal.acknowledge_owner` bullet names triage typing. The owner-acknowledgement
      deploy-verify checklist gains a triage item (7.2 below).

## 6. Verification and close-out

- [x] 6.1 Spec→test conformance sweep by a fresh reviewer (`project-scrutinizer`), scenario by
      scenario, including the bound tests' magnitude.
- [x] 6.2 Full suite green; record the count against the pre-change baseline.
- [x] 6.3 `openspec validate triage-working-indicator --strict`, then commit.

## 7. Deploy verification (owner-run on rp5, root shell)

- [ ] 7.1 Tag the running image as the rollback target, redeploy with the README recipe, apply
      the three "silently did nothing" tells.
- [ ] 7.2 **A sent triage shows typing.** Preconditions, checked first: no triage of the
      staged identity within its cooldown (default 6 h, `henk/events/pipeline.py:39`; check
      the audit log for the last `rp2` node_exporter triage), and cap headroom (fewer than
      `cap_per_24h` announceable triages in the last 24 h). Stage a harmless real event, as
      `triage-quality` 13.4 did (stop rp2's `node_exporter` for 10 min behind an on-host
      restart guard). Once the triage turn starts, "typing" shows in the Henk chat until the
      triage message lands. After the run, confirm the triage's audit record has
      `announceable: true` before judging the phone.
      `$C logs henk --since 30m | grep -E 'owner working indicator'` prints nothing.
- [ ] 7.3 **Shutdown mid-triage (optional).** `$C restart henk` during a triage stops
      within the grace period; "typing" clears on its own within about 15 s.
- [ ] 7.4 Record an *As-built* section here, then `openspec archive triage-working-indicator
      --yes`.

## As-built (archived 2026-09-27, before deploy)

Archived at the owner's instruction before the rp5 deploy, so group 7 is still open and is
carried by the README's owner-acknowledgement deploy checklist (its new **Sent triage**
item is 7.2). The deploy, its rollback tag and the phone check are recorded in the next
change that deploys, not here.

- **Implementation.** `henk/agent/core.py` `_process_event` runs inside `self._working()`
  when `turn.announceable`, else `nullcontext()`, from before `_start_event_session`
  through the proactive send. Written by a Codex autopilot from `autopilot.md`, reviewed by
  the owner, rebased onto `main`. The design held: no deviation from D1-D3.
- **Tests.** 18 new collected tests plus the 1.1 replacement; none edited after the
  failing-tests commit. Full suite 3630 passed, 6 skipped (baseline 3612 / 6).
- **Mutation.** T1-T7 all killed by their required tests (table in
  `notes/apply-decisions.md` Part C).
- **Conformance.** A fresh `project-scrutinizer` sweep (6.1) mapped every ADDED and changed
  scenario to a test that can fail and checked both bound tests at `[0.9T, 1.5T)` of one
  acknowledge timeout: APPROVED, no blocking finding.
- **README.** Architecture paragraph, the `signal.acknowledge_owner` bullet, and a
  **Sent triage** deploy-checklist item.
