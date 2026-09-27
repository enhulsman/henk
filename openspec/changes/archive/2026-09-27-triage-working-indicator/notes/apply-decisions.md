# Apply decisions — triage-working-indicator

Decisions made while applying the approved fix plan to the artifacts (part A) and writing
the tests for task groups 1 and 2 (part B). The session had no owner to ask, so each entry
is either an ambiguity resolved in place or a build choice a reviewer might read as an
omission. Nothing under `henk/` was changed (`git diff --stat henk/` is empty).

## Part A — the fix plan applied to the artifacts

The plan (F1-F8, the answers to the uncomfortable questions, and the round-2 corrections
M1-M3) is applied to `proposal.md`, `design.md`, `specs/agent-core/spec.md` and
`tasks.md`. `openspec validate triage-working-indicator --strict` passes.

- **`specs/channel-adapter/spec.md` is unchanged.** No plan item targets it. Its MODIFIED
  block was diffed against `openspec/specs/channel-adapter/spec.md`: it is a full copy whose
  only differences are the intended ones (the widened working-indicator sentence and the two
  triage scenarios). The agent-core MODIFIED block was diffed the same way. Its only
  differences are the event-turn sentence and the removed *Event turns are not bracketed*
  scenario.
- **Every cited line was re-grepped on 2026-09-26/27 before it was written:**
  `coordinator.py:108-120` (`Debouncer` at 108, `deadline = … + self._window` at 110,
  `flush()` at 120); `pipeline.py:39` (`cooldown_seconds = 6 * 3600.0`); `tools/base.py:60,95`
  (`DEFAULT_TURN_SCOPE`, `turn_scope`); `approval.py:238-246` (SUPPRESSED), `:266-273` (EVENT
  out-of-scope), `:274-281` (taint); `runtime.py:270-271`; `core.py:278-283`, `:413-421`
  (`_working`), `:506` (`if not turn.announceable:`). The four `turn_scope` overrides in
  `henk/tools/` (memory, capture, reminders ×2) all declare `(TurnType.OWNER,)`.
- **Two plan citations were off, and the artifacts use the corrected ones.** F1 cites
  `approval.py:268-283` for the taint rule. The EVENT denial is at 266-273 and the taint
  check is at 274-281, so `tasks.md` 1.2 cites `:266-281`. F6 cites
  `tests/test_agent_core_acknowledgement.py:277-309`, but the test's decorator is at 276.
  This change's own edits then move that test to 310-342. `tasks.md` therefore names the test
  and cites `:276-309` "at the change's base commit".
- **The owner-facing wording of M3's example.** The proposal is written for the owner, so
  M3's "a `handoff_sink` raising inside the flush" becomes "the step that remembers the
  triage's handoff for next time raising". The design names `handoff_sink` and states why an
  audit write failure does not cause silence: `AuditLog.write` catches `OSError` and returns
  `False`.
- **Decision 3 (Q1) gates only group 7.** The `tasks.md` header now says decisions 1 and 2
  block the code, and decision 3 (ship alone, or hold for item 5) gates only deploy.
- **F3's "sent" wording.** The ADDED requirement lists "a sent triage message, a sent
  incomplete-triage notice". Here "sent" means the send was attempted, as the requirement's
  earlier "after the proactive send has been attempted" says. A FAILED/PARTIAL delivery also
  stops the indicator. Design Risks states that residual. The wording is left as the plan
  gave it.

### Promised changes: grep hits

| Promise | Hit |
|---|---|
| T7 row as a production mutant | `tasks.md:111` (row), `:113` ("T7 is a production mutant …") |
| "exception inside the bracket" scenario | `specs/agent-core/spec.md:67` |
| three parametrized endings | `tasks.md:33`; `specs/agent-core/spec.md:64` ("raises, is refused, or produces no reply") |
| 7.2 preconditions | `tasks.md:135` ("Preconditions, checked first"), `:142` (`announceable: true`) |
| cancel-at-entry parity sentence | `specs/agent-core/spec.md:57` (ADDED), matching `:10` (owner requirement) |
| `OrderedChannel.order` | `tasks.md:19` |
| dispatch_batch-based 2.1 | `tasks.md:77` |
| decision 3 (Q1) | `proposal.md:26` |
| fixed debounce window (F2) | `proposal.md:11`; `design.md:27`, `:112` |
| M3 example / `OSError` | `design.md:56`, `:58`, `:117` |
| gate mechanism (F4) | `design.md:34`, `:91` |
| Q2 risk | `design.md:121` |
| runtime comment only (F8) | `proposal.md:79`; `tasks.md:92` |
| `nullcontext()` wording (F8) | `design.md:76`; `tasks.md:89` |

## Part B — tests (groups 1 and 2; no implementation)

Tests added: 17 new test functions/cases in `tests/test_agent_core_acknowledgement.py`, plus
the sanctioned replacement of `test_event_turns_are_not_bracketed` (task 1.1), and 1 in
`tests/test_acknowledgement_guards.py`. That is 18 new collected tests plus the
replacement. The only edits to existing lines are that replacement and additions to the two
import blocks. No other existing test body was touched.

- **1.1 replacement** (`test_a_cap_suppressed_triage_shows_no_indicator`) uses
  `EventSessionFactory`, which does not write into `OrderedChannel.order`. So
  `order == []` means no typing request and no send. A real `AuditLog` under `tmp_path`
  proves the record is written with `announceable: false` and `outcome: completed`.
- **The three endings** come from the ending the session reports (`TurnEnding(refusal=True)`
  through an `ending()` method, as `_ScriptedSession` in `test_triage_ending.py` does), from
  `fail=True` (raise), and from `reply=""` (no reply). Each case asserts the notice's prefix
  and its ending phrase, so the three really take different branches.
- **The exception inside the bracket** is `handoff_sink` raising (M3). The session reports
  `handoff_stats("hf-1")`. No audit is wired, because `_write_audit_record` returns the
  handoff id even when there is no audit. The test is parametrized over `process` (the
  exception propagates) and `run` (`AgentCore.run` logs "unexpected error processing turn"
  with that exception).
- **The entry-cancel test (M2).** Before implementation, its first failing assertion is
  `not task.done()`. With cooperative doubles, the unbracketed event path has no suspension
  point, so the triage runs to its end in one loop step. That is the right reason: it never
  yielded at a bracket entry. `factory.created == []` holds under the scratch
  implementation. That proves the cancel landed before `_start_event_session`.
- **The hung-stop test.** The queued owner turn reuses the triage session (the continuation
  path), and it holds until the test has removed the stop fault. So only the triage's stop
  hangs, and the owner turn's own stop completes: that is the drive's end condition, with no
  race. The first assertion is the full `ack_attempts` sequence
  `[start, stop, start, stop]`. Before implementation it fails as `['start', 'stop']`, which
  names the missing triage indicator rather than a timing figure.
- **The disabled test (F6)** pins the session content to
  `compose_event_turn_content(turn, recall=None, tool_names=None, digest=read_digest(None,
  turn))` and the proactive call to `("proactive", TRIAGE_REPLY, TRIAGE_FAILURE_NOTICE)`, as
  the plan says. That is "unchanged" relative to the composer. The composer's own output is
  pinned by the framing tests.
- **The pause test (F1)** uses `EventScopedTool` from `tests/test_gate_authorization.py`, an
  existing test-only per-instance tool with `(OWNER, EVENT)` scope. It needs no
  `demote_standing`. "A stop within one poll of the prompt" is checked exactly: a stamping
  channel records FakeTime at the prompt send and at the pause stop. The first assertion is
  that the indicator is up (`order[0] == START`), before any FakeTime call. Before
  implementation, the test therefore fails on that assertion, not on FakeTime's "loop never
  went back to sleep".
- **The refresh test** checks the start stamps `[0.0]` first, then `[0.0, 7.0, 14.0]`, then
  that the order ends `[proactive, stop]`.
- **The guard (2.1, F5)** does not use the `App.run` harness. `_SequencedBridge` records
  completed sends and typing requests in one list. `SAFE_LENGTH = 80` makes the triage
  multi-chunk, so "only the first triage's chunks" is not vacuous. After the second record is
  written, the test lets the loop run 50 more iterations before cancelling, so any stray
  request for the second triage would be recorded.

### Full suite, before implementation

Baseline on main: 3608 passed, 4 skipped. This branch with the tests: **16 failed,
3610 passed, 4 skipped** (12 deselected by the default config). 3610 = 3608 − 1 replaced +
1 replacement + 2 disabled-wiring cases that already hold. Every pre-existing test passes.

| Test | Now | Failure line, or why passing is correct |
|---|---|---|
| `test_a_cap_suppressed_triage_shows_no_indicator` | passes | today no event turn is bracketed; pins behaviour that must survive |
| `test_a_sent_triage_is_bracketed` | fails | `['turn', 'proactive'] == ['start', 'turn', 'proactive', 'stop']` |
| `…did_not_complete_clears_after_its_notice[raise/refusal/no-reply]` | fail (3) | `['turn', 'proactive'] == ['start', …, 'stop']` |
| `test_the_indicator_clears_when_starting_the_incident_session_fails` | fails | `[] == [('start',), ('stop',)]` (the raise itself propagates as today) |
| `test_an_exception_inside_the_bracket_still_stops_the_indicator[process/run]` | fail (2) | `['turn'] == ['start', 'turn', 'stop']` (propagation and the log line already hold) |
| `test_cancellation_during_a_sent_triage_sends_no_stop[process/run]` | fail (2) | `[] == [('start', None)]` |
| `test_a_cancel_at_the_brackets_entry_…` | fails | `the triage ran to its end without entering a bracket` |
| `test_a_hung_stop_after_a_triage_…` | fails | `['start', 'stop'] == ['start', 'stop', 'start', 'stop']` |
| `test_disabled_…_unchanged[omitted/explicit-none]` | pass (2) | disabled is unchanged by definition |
| `test_disabled_…_unchanged[enabled]` | fails | `[] == [('start', None), ('stop', None)]` |
| `test_a_degraded_durability_notice_…_precedes_the_stop` | fails | `['turn', 'proactive', 'proactive'] == ['start', …, 'stop']` |
| `test_the_indicator_pauses_during_an_approval_in_a_sent_triage` | fails | `no indicator on the sent triage` (`('turn', …) == ('start',)`) |
| `test_a_long_sent_triage_refreshes_the_indicator` | fails | `no indicator on the sent triage` (`[] == [0.0]`) |
| `test_the_alert_cap_decides_which_triage_shows_the_indicator` | fails | `[] == [('start', OWNER), ('stop', OWNER)]` |

No failure is an ImportError, TypeError, fixture error or AttributeError.

### Scratch implementation check (in-process, not in the repo)

`/tmp/claude-1000/-home-wsl-Coding-henk/4016312d-44e5-4407-9b2e-f39d232000d4/scratchpad/twi/twi_impl.py`,
loaded with `-p twi_impl`, wraps the existing `AgentCore._process_event` in
`self._working()` when `turn.announceable`, else `nullcontext()`. That is task 3.1's
minimal form: the bracket encloses steps 1-5.

- Both test files: **36 passed**, stable over 8 runs. Every new failing test turns green.
- Full suite under the plugin: **3626 passed, 4 skipped**, including the replay-isolation
  and event-path tests.
- T7 in-process (`TWI_T7=1`: `_framed_turn` gets `announceable=False` for EVENT turns): only
  the pause test goes red (`timed out waiting for the approval prompt`, the fail-fast
  bound), as M1 requires.
- T1 in-process (`TWI_T1=1`: bracket every event turn): 1.1 goes red (`[start, stop] == []`),
  and so does 2.1 (typing holds two spans).

These are early evidence, not group 4's mutation table. That table must be run against the
real implementation.

## Part C — implementation and close-out (2026-09-27)

The implementation (task 3.1) was written by a Codex autopilot on
`autopilot/2026-09-27-typing` from `autopilot.md`, owner-reviewed, then rebased with the
spec and test commits onto `main` (which had gained the unrelated
`BUILTIN_HOST_TOOLS` fix). It touches `henk/agent/core.py` (`_process_event` runs inside
`self._working()` when `turn.announceable`, else `nullcontext()`, from before
`_start_event_session` through the proactive send; module docstring, attribute comment
and `_working` docstring updated) and the one `henk/runtime.py` comment. No test was
edited after the failing-tests commit.

### Mutation table (task 4.1), against the real implementation

Each mutant applied alone to the rebased branch, the two acknowledgement test files run,
the file restored. Every mutant compiled, so each red is the check and not a SyntaxError.

| # | Mutant | Caught by (required) | Also red |
|---|---|---|---|
| T1 | `async with self._working():` for every event turn | `test_a_cap_suppressed_triage_shows_no_indicator` (1.1), `test_the_alert_cap_decides_which_triage_shows_the_indicator` (2.1) | — |
| T2 | `async with nullcontext():` (the old behaviour) | `test_a_sent_triage_is_bracketed`, 2.1 | every other sent-triage test (16 in all) |
| T3 | proactive-send tail dedented out of the bracket | `test_a_sent_triage_is_bracketed`, `…did_not_complete_clears_after_its_notice[raise/refusal/no-reply]` | degraded-durability, hung-stop, refresh, approval-pause, 2.1 |
| T4 | `_start_event_session` moved before the bracket | `test_the_indicator_clears_when_starting_the_incident_session_fails`, `test_a_cancel_at_the_brackets_entry_…` | — |
| T5 | `working`'s `BaseException` branch calls `_close` (sends a stop) | `test_cancellation_during_a_sent_triage_sends_no_stop[process/run]` | the entry-cancel test, and the owner-turn `test_a_cancelled_worker_propagates_and_sends_no_stop[process/run]` |
| T6 | `_close`'s bound is `asyncio.timeout(None)` | `test_a_hung_stop_after_a_triage_does_not_hold_the_next_turn_beyond_the_bound` | the owner-turn hung-stop test |
| T7 | `_framed_turn(TurnType.EVENT, announceable=False)` in `_process_event` | `test_the_indicator_pauses_during_an_approval_in_a_sent_triage` | — (only that test, as M1 requires) |

No mutant survived.

### Full suite (task 6.2)

`main` before this change (with the `BUILTIN_HOST_TOOLS` fix): 3612 passed, 6 skipped.
The rebased branch: **3630 passed, 6 skipped** (12 deselected). 3630 = 3612 − 1 replaced +
1 replacement + 18 new. The 6 skipped are the SDK-gated tests; the SDK is not installed
locally.

### Conformance sweep (task 6.1)

A fresh `project-scrutinizer` mapped every ADDED scenario of both deltas, and the changed
blocks of the MODIFIED ones, to at least one test that can fail; the removed *Event turns
are not bracketed* scenario's test is gone, replaced by 1.1. Both hung-stop tests assert
`[0.9T, 1.5T)` with T the acknowledge timeout. Verdict APPROVED. Non-blocking notes: the
"does not wait on the start's response" clause is covered at the bracket level
(`tests/test_acknowledge.py`), not on the event path; the disabled test's oracle is the
composer itself rather than literals, with its arguments pinned independently.
