# Apply decisions — owner-acknowledgement

Decisions made while applying the change. Each one is either an ambiguity in the spec or tasks
that had to be resolved without the owner, or a build choice a reviewer might read as an
omission. The mutation results for each group are at the end of that group's section.

Suite baseline before group 1: 3478 passed, 4 skipped. After groups 1-3: 3550 passed,
4 skipped.

## Group 1 — Config

- **The acknowledge-timeout cap is a copy in `henk/config.py`
  (`_ACKNOWLEDGE_TIMEOUT_CAP_SECONDS = 7.0`), not an import of `TYPING_REFRESH_SECONDS`.**
  Deviates from task 1.2's "imported from `henk.channel.signal`" wording; decided at the group
  1-3 review gate. There is no import cycle (`signal.py` imports only `henk.channel.base`). The
  conflict is the replay's isolation: `tests/test_replay_isolation.py` `FORBIDDEN` lists
  `henk.channel.signal` as a module "a replay must never load", and the replay calls
  `Config.load` (`henk/replay/__main__.py:89`). A module-level import fails the import-graph
  test. The first attempt, a local import inside the validator, passed that test but was probed
  to load `henk.channel.signal` on every `Config.load`, so every real replay loaded it: the
  guard stayed green while the property it names was lost. The copy keeps the property true.
  Two tests hold it: `test_the_acknowledge_timeout_cap_is_the_typing_refresh_interval` checks
  the cap behaviourally against the adapter's constant, so moving one without the other fails;
  `test_loading_config_does_not_load_the_signal_adapter_module` runs `Config.load` in a
  subprocess and asserts `henk.channel.signal` is not in `sys.modules` (mutation-checked: the
  local import turns it red on its assertion).
- **A strict boolean means `isinstance(value, bool)`.** `"false"`, `"true"`, `1`, `0` and
  `None` (a blank YAML value) are all refused. The blank case is also tested through
  `Config.load` on a real YAML file, as the owner would write it.
- **"A non-number" timeout includes numeric strings.** `"5"` is refused, although the adjacent
  `send_timeout_seconds` goes through `float()` and would accept it. Task 1.1 asks for a
  non-number to be refused, and a quoted number is not a number. Ints are accepted and stored
  as `float` (`7` → `7.0`). NaN and infinity are refused by the positivity and cap comparisons.
- **The cap is inclusive** (`<=`), so exactly `7.0` loads, as task 1.1 requires.
- **`config.yaml` now declares both keys** (`true`, `5.0`), per task 1.3. rp5's file still
  carries neither, so its effective values come from the loader's fallbacks, which the
  absent-keys test pins.
- **1.4 line numbers moved.** The stale comment was at `config.py:352-356` as cited. It is now
  replaced at `:352-359` with the per-phase wording. `config.yaml:31-33` was replaced in place.

### Mutation results (group 1)

| # | Mutant | Caught by | Result |
|---|---|---|---|
| M11 | `from_dict` fallback for `acknowledge_owner` set to `False` | `test_acknowledgement_enabled_by_default_when_the_keys_are_absent` | killed (`assert False is True`) |
| M12 | `from_dict` not passing `acknowledge_owner` at all | `test_an_explicit_false_acknowledge_owner_is_honoured` | killed (`assert True is False`) |
| M20 | timeout cap against the refresh interval removed | `test_an_out_of_range_acknowledge_timeout_is_refused[7.5/8/60]`, `test_the_acknowledge_timeout_cap_is_the_typing_refresh_interval` | killed (`DID NOT RAISE`, 4 failures) |

## Group 2 — Transport

- **`FakeBridge` records an acknowledgement only when it completes**, after any scripted fault
  clears. So "recorded in `typing`" means "the bridge completed it", and group 4's ordering
  tests (stop after an in-flight start) assert completions, not issue order.
- **`FakeBridge.ack_attempts` is added**, although task 2.1 does not name it: `(operation,
  recipient)` for every call, including refused and hung ones. Without it, task 3.1's "the
  bridge saw exactly one attempt" cannot be checked, because a refused call records nothing
  in `receipts` or `typing`.
- **`ack_faults` is persistent per operation, not one-shot.** An exception is raised on every
  call of that operation. An `Event` is awaited on every call until it is set, then passes
  through. A test that needs "one hung refresh, then healthy" sets the event or removes the key.
- **`hold_open`** awaits a fresh `asyncio.Event()` that is never set, once the script is
  exhausted. A test covers both behaviours.
- **The three bridge methods share one private `_acknowledgement(method, path, payload, what)`**
  on `SignalCliRestBridge`. It uses `client.request(...)` for all three verbs, so the DELETE
  carries its body. Each call builds its client through `_build_client()`. There is no retry
  and no bound, and every failure becomes a `SignalBridgeError`.
- **The timeout pin in 2.2 is asserted at the transport.** The handler checks
  `request.extensions["timeout"]`, the per-phase dict httpx hands the transport. The test also
  checks the patched client's `timeout`. A method that bypassed the patched factory would
  reach no handler and fail on the request count.
- The routes (`/v1/receipts/{account}`, `/v1/typing-indicator/{account}`) are the ones task 2.2
  names. They are not verified against rp5's image. Tasks 12.3 and 12.5 do that.

## Group 3 — Contract and Signal adapter

- **Stranger placeholder.** Standing rule 3 says to use the stranger placeholder the existing
  tests already use, a `+31` number distinct from the owner's and the rollback placeholder. The
  pre-commit hook (`.githooks/pre-commit`, phone-number check) refuses every `+31` number in
  added lines except the two it allowlists, so that existing placeholder cannot appear in a new
  line. Where group 3 needed a
  non-owner sender, I used the UUID placeholder `00000000-0000-4000-8000-000000000000` for both
  `source` and `sourceUuid`. **Groups 5 and 8 will hit this too.** Their stranger tests need a
  non-owner identity that the hook accepts, such as the UUID placeholder or a non-`+31` value.
- **A reference is minted only from a JSON integer.** `_mint_channel_ref` returns `None` for an
  absent, zero, boolean or non-integer timestamp. signal-cli-rest-api reports milliseconds as
  an integer. A string timestamp therefore gets no reference and no receipt, rather than a new
  `int()` failure inside `messages()`. `InboundMessage.timestamp` is computed exactly as before.
- **A reference is parsed more strictly than `int()`.** `_parse_channel_ref` accepts only the
  exact form `_mint_channel_ref` produces: ASCII digits, positive, and a round trip through
  `str(int(x))` (so no leading zeros). `" 12"`, `"+12"`, `"1_000"`, `"012"`, non-ASCII digits,
  `"0"` and `"-5"` are refused without a bridge request. This follows the spec's "a channel
  reference that is not one the adapter mints" literally.
- **`acknowledge(None)` returns `False`**, as task 3.1 says. The spec's "no-op" (no request, no
  failure logged) is kept: the adapter never logs, and the dispatcher skips `None` before
  calling (task 5.3).
- **Only `SignalBridgeError` is caught** in the adapter's acknowledgement operations, as
  `_send_chunk` does for sends. Any other exception propagates to the caller. Group 4's
  bounded helpers catch `Exception` there.
- **The helper is named `_to_owner(operation, *args)`.** It passes `self._owner` as the
  recipient, which makes owner-only structural in one place. It takes no lock and has no
  bound, so it adds no `Lock`, `wait_for` or `timeout` call. The lock test and the
  client-construction test pass untouched.
- **3.2 extends the existing inspection test**
  (`test_no_send_operation_exposes_an_arbitrary_recipient`) to iterate `SEND_OPERATIONS` and
  a separate `ACK_OPERATIONS` dict, with the same exact-list and denylist assertions. This
  test and the new top-level `import httpx` are the only edits to existing test code.
- **3.4:** a test for `FakeChannel.acks` sits in `test_channel_adapter.py`. The standing
  rule 6 grep (`Dispatcher(`, `.sent`, `.calls`, `.sends`) was re-run after the change. Sorted,
  and excluding the two files this group edits, its hit set is identical to before, so no
  existing observer changed.
- **The DEPLOY-VERIFY note in `_convert`** now says that a delivered-but-never-read owner
  message means a silent allowlist drop, or a failed receipt, which is logged.

### Mutation results (group 3)

| # | Mutant | Caught by | Result |
|---|---|---|---|
| M9 | acknowledgement operations taking `_send_lock` | `test_an_acknowledgement_does_not_wait_behind_a_send_in_flight` | killed (`TimeoutError` from the 2 s bound) |
| M10 | `_convert` minting `"0"` for a timestamp-less envelope | `test_timestamp_less_envelope_carries_no_channel_reference` (4 cases) | killed (`assert '0' is None`) |
| M14 | `sender` parameter smuggled into `acknowledge` and used as the recipient | `test_no_send_operation_exposes_an_arbitrary_recipient` | killed (`Left contains one more item: 'sender'`) |

Each mutant was applied alone to a scratch-backed copy and reverted by copying the backup back.
A `diff` against the backups confirmed both source files were restored.

## Group 4 — Bounded acknowledgement (`henk/channel/acknowledge.py`)

Suite after group 4: 3574 passed, 4 skipped (3551 + 23 new in `tests/test_acknowledge.py`,
0.65 s for the file; stable over 15 serial runs and 18 runs six-way parallel).

- **The final stop has no bound of its own; the close's single bound covers it.** D6 lists
  what is bounded: each start, refresh and pause stop inside the task, and the close as a
  whole. A second bound on the final stop would add nothing: it starts later than the
  close's bound, so it can never fire first. It would also create a same-tick race between
  the two expiries. Each start, refresh, pause stop and resume start is bounded by
  `asyncio.timeout(timeout)` in `_attempt`.
- **The log budget is two flags per `working()` entry.** One flag covers the loop line (first
  start, refreshes, pause stop and resume start share it). The other covers the close line,
  shared by the task's refused or raising final stop and the close's own expiry or exception.
  So "at most one more line for the close" is structural, even if both a refused stop and an
  expiry could happen in one turn.
- **Paused sets `next_due = math.inf`.** The literal wait formula
  `max(0, min(PAUSE_POLL_SECONDS, next_due - clock()))` then gives the poll while paused.
  Without this, a refresh that fell due during the pause would make the wait 0 and the loop
  would spin. A test asserts every wait during the pause is exactly one poll.
- **The final stop is sent even when paused.** It is idempotent, and the pause stop may have
  failed. The pause is read only at wake-ups; the entry start is unconditional (the core queue
  is serial, so no approval can be pending at entry).
- **Only `Exception` from the body gets the close (and its stop).** Any other `BaseException`
  (`CancelledError`, `KeyboardInterrupt`, `SystemExit`) takes the cancellation path: cancel the
  task, `await asyncio.wait({task})`, send nothing, re-raise. This is the literal reading of
  "catch `Exception`, never `BaseException`".
- **A task that dies with an unexpected exception is logged by the close**, which calls
  `task.exception()` (so there is no "exception never retrieved" warning). The task catches
  `Exception` around every request, so this is only reachable through a bug, such as a
  raising `paused` predicate.
- **`receipt` logs one line for a refused receipt (`False`) too**, not only for an exception or
  expiry. The line names the reference (D2 allows it) and never any text; the module is never
  given text. `SignalBridgeError` stops in the adapter and becomes `False`, so a bridge error
  message (which can carry a URL) never reaches this log.
- **Log lines are told apart by fixed phrases**: "further start/refresh failures this turn are
  not logged" (loop), "working indicator close" (close), "owner read receipt" (receipt).
  Group 8 should know about these phrases.
- **Test apparatus.** An autouse fixture fails any test that leaves a task running, and
  cancels the leftovers so one failure cannot cascade. `FakeTime` is one clock plus a sleep
  that only `step()` releases, so each step is exactly one wake-up of the loop. `sleeping()`
  marks the moment between wake-ups when a test may change the pause flag or a fault; without
  it, flipping the flag raced the wake-up it was meant to precede. The one-hung-refresh
  scenario is modelled by a `ClockedBridge` start that advances the fake clock by T and then
  blocks until the real 0.05 s bound cancels it. That way the real `asyncio.timeout` does the
  cutting, and the fake clock records the time the hang consumed.
- **The "bound exhausted before the stop" test drives a hung refresh plus a hung stop.** The
  pure case, where an in-flight refresh alone exhausts the close's bound before any stop is
  issued, is unreachable. The refresh's own bound equals the close's and started earlier, so it
  always fires first and the stop is issued with a sliver of the bound left. The reachable
  shape, which the test drives: the refresh times out (the turn's one loop line), the stop
  hangs, and the close's bound runs out (the one close line). No stop completes, and
  `__aexit__` returns within the bound.
- **M19c was applied in-process, not on disk.** A `-p` plugin sets
  `henk.channel.signal.TYPING_REFRESH_SECONDS = 8.0` before collection, so every importer
  sees 8.0 exactly as a source edit would. I did not edit `signal.py` because groups 1-3 were
  being committed concurrently, and a mutant on disk could have been staged.

### asyncio probes (Python 3.12.3, run as code, not read from docs)

| Claim relied on | Result |
|---|---|
| `await asyncio.wait({task})` on a cancelled child returns and does not raise the child's `CancelledError` | holds |
| An outer cancel during `await asyncio.wait({task})` raises `CancelledError` in the awaiting task and does **not** cancel the child | holds (child still running) |
| `try: await task / except CancelledError: pass` swallows an OUTER cancel, and forwards it into the child | holds, both (this is M17's defect) |
| Timeout expiry inside `asyncio.timeout`, handler does a second `await asyncio.wait` and re-raises: surfaces as `TimeoutError` at the `async with` | holds |
| An outer cancel inside `asyncio.timeout` (not expired) propagates as `CancelledError` past `except Exception` | holds |
| `asyncio.wait` refuses bare coroutines | holds (`TypeError`) |
| `asyncio.timeout` enter/exit does not yield to the loop (so the entry start is issued in the task's first step) | holds |
| A task cancelled before its first step is done after `asyncio.wait` | holds |

**A finding the spec should know about (double cancel).** On the cancel path, a **second**
cancel arriving while the bracket awaits `asyncio.wait({task})` makes the bracket exit
**before** the cancelled child has finished. `try: await task / except CancelledError: pass`
followed by the bare `raise` does not have this problem: it waits for the child, and still
re-raises the original cancellation. That is why M17b (below) survives: it is equivalent under
every single-cancel test, and it behaves *better* under a double cancel. The spec's "never
outlives the turn" therefore holds for one cancellation, not for two. The window is the
child's own cleanup (one loop iteration of `_wake`'s `finally`), so it is harmless at
shutdown. The implementation follows the task's literal "`asyncio.wait({task})`". If this
ever matters, the fix is to repeat `asyncio.wait({task})` until `task.done()` and then
re-raise. Not changed here.

**A second observation (the close's bound and an in-flight refresh).** A refresh issued just
before the close that runs to its own bound uses up almost all of the close's single bound.
The stop is then issued with milliseconds left. On a healthy stop it usually lands, but that
is a race against the close's expiry, and losing it only leaves the indicator to the ~15 s
client expiry. So the scenario *A start in flight cannot land after the stop* ("both
complete within one acknowledge timeout") holds for a refresh that returns with room to
spare, which is what its test drives, and not for one that hangs to its bound. Cosmetic, and
consistent with D4. I am recording it so the spec's wording is not read as a guarantee for
that case.

### Mutation results (group 4)

Each mutant was applied alone to `henk/channel/acknowledge.py` from a scratch backup. I ran
`tests/test_acknowledge.py` under `python -B` and restored the backup after each run. `cmp`
against the backup and an empty `git diff --stat` confirmed nothing was left behind.

| # | Mutant | Caught by | Result |
|---|---|---|---|
| M4 | `paused` ignored | `test_the_indicator_is_suspended_while_paused_and_resumes_at_the_next_poll` | killed (`assert 20.0 == 4.0`: no pause stop) |
| M5 | stop sent on the cancellation path | `test_a_cancelled_body_sends_no_stop[idle,hung]`, `test_a_cancel_during_entry_leaves_no_indicator_task` | killed (`('stop', OWNER) not in …`) |
| M6 | `asyncio.timeout` removed from `receipt` | `test_a_hung_receipt_returns_within_the_bound_and_logs_one_line` | killed (2 s fail-fast bound) |
| M7 | close issues the stop itself from the closing coroutine (task's stop removed) | `test_a_refresh_in_flight_at_exit_completes_before_the_stop` (+ log test) | killed (typing `[start, stop, start]`) |
| M8 | failure logged every tick | `test_refresh_failures_are_logged_once_per_turn` | killed (`8 == 1`) |
| M13 | `except BaseException` in all bounded helpers | outer-cancel receipt, cancelled-body[hung], outer-cancel-during-close | killed |
| M13a | … `receipt` only | `test_an_outer_cancel_during_a_hung_receipt_propagates` | killed ("the cancellation was swallowed") |
| M13b | … `_attempt` only | `test_a_cancelled_body_sends_no_stop[hung]` | killed (2 s fail-fast bound: the task swallows the cancel and keeps looping) |
| M13c | … the close only | `test_an_outer_cancel_during_the_close_propagates` | killed ("swallowed by the close") |
| M15 | loop sleeps out the poll instead of waking on `exit_event` | `test_a_healthy_close_wakes_on_exit_not_on_the_poll` (+ 6 others) | killed (stop missing from `typing`) |
| M16 | `asyncio.timeout` removed from the close | `test_a_hung_stop_is_bounded`, `test_the_bound_exhausted_…` | killed (2 s fail-fast bound) |
| M17 (a) | `try: await task / except CancelledError: pass` in the close | outer-cancel-during-close, hung-stop, bound-exhausted | killed (cancel swallowed; the expiry's line vanishes, `0 == 1`) |
| M17b | the same on the cancel path | none | **survives: equivalent mutant** (see the double-cancel finding above; it re-raises the original cancellation either way) |
| M18 | entry `sleep(0)` moved above the `try` | `test_a_cancel_during_entry_leaves_no_indicator_task` | killed (orphaned indicator task in the in-test `all_tasks()` check) |
| M19 | interval measured from a start's return | `test_one_hung_refresh_does_not_let_the_indicator_expire` | killed (starts `[0, 7]`, no start at 14) |
| M19 | fixed poll wait instead of `min(poll, next_due − clock())` | `test_one_hung_refresh_does_not_let_the_indicator_expire` | killed (`[0, 7, 15] != [0, 7, 14]`) |
| M19 | `TYPING_REFRESH_SECONDS = 8.0` (in-process) | `test_one_lost_refresh_stays_under_the_client_expiry` (+ 6 cadence tests) | killed (`8.0 * 2 < 15.0`) |

## Group 5 — Dispatcher receipt (`henk/app.py`)

Suite after groups 5-7: 3598 passed, 4 skipped (3574 + 24 new: 8 in `tests/test_app.py`,
14 in `tests/test_agent_core_acknowledgement.py`, 2 in `tests/test_runtime.py`). The three
files take 0.65 s together, and were stable over 15 serial runs and 18 runs six-way
parallel.

- **The receipt sits after routing on both branches.** The gate branch's early `return` became
  an `else:`, so every allowlisted message reaches one receipt call: a `yes`/`no` routed to
  the gate, an unrelated message that failed an approval closed and was re-queued, and a
  plain turn. A message with `channel_ref is None` skips the call entirely, so nothing is
  logged (D2).
- **`app.py` imports `OwnerAcknowledgement` for the annotation.** `henk.app` is already in
  the replay's `FORBIDDEN` list, and `acknowledge.py` imports only `henk.channel.base`, so
  the replay's import graph is unchanged.
- **Every acknowledgement-enabled test in group 5 runs a real `SignalAdapter` over a
  `FakeBridge`**, not only the failure and stranger tests. The adapter is also the gate's and
  the core's channel, the way the runtime wires it. The disabled test uses `FakeChannel`,
  because its `acks` list is what "no entry of any kind" is about.
- **Order is observed, not inferred.** `_OrderedBridge` records a receipt when it is
  *issued*, and the test's own wiring wraps `core.submit` on the instance. `receipts` only
  records completions, so it cannot show that the message was queued before the receipt
  began.
- **5.2 converts the envelopes through the real adapter's `messages()`.** So each message
  carries the reference that a misplaced receipt would use, and the test asserts it has one
  (otherwise the test is vacuous). The stranger uses the UUID placeholder for both `source`
  and `sourceUuid`. The owner's group envelope carries only `source`, because `_convert`
  prefers `sourceUuid`. The core worker runs during the test, so a stranger message that got
  queued would create a session. The drop lines are the allowlist's own
  (`henk/channel/allowlist.py`): `dropped message from non-owner sender=%s` and
  `dropped group message from sender=%s`. They are asserted verbatim, and compared with a
  run through a dispatcher that has no acknowledgement ("logged exactly as with
  acknowledgement disabled").
- **One test was added that the task list does not name**: a message without a reference is
  queued, with no `ack_attempts` and nothing logged. It covers the dispatcher half of scenario
  *A message without a channel reference is not acknowledged and logs nothing*. Group 4
  covered the `receipt(None)` half.
- **Standing rule 6.** Re-grepped `Dispatcher(\|AgentCore(\|\.sent\b\|\.calls\b\|\.sends\b`.
  The two pre-existing constructions, `tests/test_app.py:74` (it was `:68`; the new imports
  moved it down 6 lines) and `tests/test_reminders_runtime.py:199`, are unchanged. They
  construct without the keyword and pass. No test monkeypatches `Dispatcher`, `on_inbound`
  or `_process_owner`. The only change to existing lines in `test_app.py` is the import block
  (`logging`, `time`, `pytest`, `OwnerAcknowledgement`, `SignalAdapter`,
  `TYPING_REFRESH_SECONDS`, and `FakeBridge` added to the conftest import). No existing test
  body changed.
- **Structural addressing, seen under M1.** With the receipt moved above the allowlist, the
  stranger's message *is* acknowledged, but to the **owner**, `(OWNER, 1700000000101)`, never
  to the stranger. That is D1's claim that the ordering property became a structural one.
  M1 is still a violation (a receipt for a message that failed the check), and 5.2 catches it
  on `receipts == []`.

## Group 6 — Core bracket (`henk/agent/core.py`)

- **`_Sender` is not widened** (design D3, deliberate). The core never calls
  `acknowledge`/`start_working`/`stop_working`. It only enters the injected bracket, so a core
  test double does not need to implement them.
- **The unwired bracket is `contextlib.nullcontext()`**, entered with `async with` (supported
  since 3.10). Its `__aenter__`/`__aexit__` never suspend, so a core without an indicator
  schedules an owner turn exactly as before. `_framed_turn` is unchanged.
- **`core.py` does not import `henk.channel.acknowledge`.** `tests/test_replay_isolation.py`
  limits the replay's channel imports to `henk.channel`, `.base` and `.allowlist`, and the
  replay imports the core. The parameter is typed
  `Callable[[], AbstractAsyncContextManager[None]] | None` from `contextlib`.
- **Placement is D3's diagram**: `/new` and the app-side command return before the
  `async with`. The bracket then encloses `_ensure_session`, the composition, the gate-framed
  `run_turn`, and the reply or error-reply send. The error branch's `return` leaves the
  bracket normally, so its stop follows the error reply. An `_ensure_session` exception
  passes through the bracket's `except Exception` (so the close runs) and propagates out of
  `process` to `AgentCore.run`'s logger, as it did before. The test asserts that it raises.
- **Module docstring**: one bullet added to the responsibility list.
- **The `working_indicator=None` test** runs three wirings (keyword omitted, explicit `None`,
  enabled). Each is checked against the hardcoded composition the existing recall and
  time-header tests pin: `"{header}\n\n{recall}\n\nhello"`, then `"{header}\n\nand again"`,
  with recall on the first turn only. Replies are `ok:{content}`. The enabled run is held to
  the same bytes, and only its `acks` differ.
- **The approval-integration test** drives a real `ApprovalGate(demote_standing=True)` with
  `StandingTool` (from `test_gate_authorization.py`) through the real `decide_tool_permission`.
  The acknowledgement's `paused` is that gate's `has_pending`. The cadence runs on group 4's
  `FakeTime`. The test imports only the class, so `test_acknowledge.py`'s autouse fixture does
  not come along, and this file has its own leftover-task fixture. It advances to t = 10,
  past the t = 7 refresh, while the prompt is pending, and asserts that only the pause stop
  followed the prompt. It then calls `gate.deliver("yes")` directly (the dispatcher route is
  covered in 5.1). The session holds after the tool runs, so the resume start is asserted
  while the turn is still running.
- **The hung-stop test** takes "the bracket began to close" to be the moment the first
  reply's send completed, because the bracket exits right after it. It asserts
  `gap < T + MARGIN`, and `gap >= 0.9 T` to prove the close waited on the hung stop. It waits
  until both turns' close lines are logged before cancelling the worker, so the cancel cannot
  race the second close.
- **The cancellation test is parametrized** over `process` and `AgentCore.run`. In both,
  `task.cancelled()` holds, `acks == [("start", None)]`, and the autouse fixture proves the
  indicator task is done.

## Group 7 — Runtime wiring (`henk/runtime.py`)

- One `OwnerAcknowledgement` is built right after the gate (after the adapter), when
  `config.signal.acknowledge_owner` is set, with `timeout=acknowledge_timeout_seconds`,
  `refresh_seconds=TYPING_REFRESH_SECONDS` and `paused=gate.has_pending`. It is passed to
  `Dispatcher(..., acknowledgement=...)` (now `runtime.py:276-278`), and its bound `working`
  is passed to `AgentCore(working_indicator=...)` (`runtime.py:272`). When the flag is off,
  both are `None`.
- `henk/runtime.py` imports `TYPING_REFRESH_SECONDS` from `henk.channel.signal`, which it
  already imported. `test_replay_isolation.py` still passes: `henk.runtime` is itself in
  `FORBIDDEN`, so the replay never loads it.
- **The 7.1 test uses a non-default timeout (2.5)**, so a wiring that fell back to 5.0 cannot
  pass by coincidence. Identity is asserted through the bound methods:
  `core._working_indicator.__self__ is acknowledgement`, and
  `acknowledgement._paused.__self__ is app._core._gate`, which is also the dispatcher's
  gate.

### Mutation results (groups 5-7)

Each mutant was applied alone to a scratch-backed copy of one source file. The named tests
were run with `python -B`, then the backup was copied back. `cmp` against each backup
confirmed that all four files were restored, and `git diff --stat` shows only the intended
changes.

| # | Mutant | Caught by | Result |
|---|---|---|---|
| M1 | receipt moved above the allowlist check in `on_inbound` | `test_stranger_gets_nothing_with_acknowledgement_enabled` alone (8.4 does not exist yet), plus 4 ordering tests | killed (`receipts` held `(OWNER, 1700000000101)` and `(OWNER, 1700000000102)`, expected `[]`) |
| — | receipt skipped for approval replies (APPROVE/DENY return before it) | `test_an_approval_reply_is_routed_to_the_gate_and_acknowledged` | killed (the `yes` message's receipt was missing) |
| M4 (core) | `paused` ignored (`self._paused = lambda: False`, whatever the wiring) | `test_the_indicator_is_suspended_while_an_approval_is_pending` | killed ("refreshed while pending": `[('start',)] != [('stop',)]`) |
| M5 (core) | the cancel path closes (and so sends the stop) instead of cancelling the task | `test_a_cancelled_worker_propagates_and_sends_no_stop[process,run]` | killed (`('stop', None)` extra) |
| M16 (core) | `asyncio.timeout` removed from the close (`timeout(None)`) | `test_a_hung_stop_does_not_hold_the_next_turn_beyond_the_bound` | killed (2 s fail-fast bound: the second turn never started) |
| — | bracket moved to enclose only `run_turn` | normal-turn (stop before the reply send), errored-turn, session-setup-raises (`[] != [start, stop]`), approval, hung-stop | killed (5 tests) |
| — | bracket applied to `/memories` | `test_commands_and_reset_are_not_bracketed[/memories]` | killed (`acks` not empty) |
| — | runtime passes a different gate's `has_pending` | `test_owner_acknowledgement_is_one_instance_on_the_apps_adapter_and_gate` | killed |
| — | runtime passes a fresh `SignalAdapter` | same | killed (`_adapter is not app._adapter`) |

### For group 8

- `tests/test_app.py` now holds `ACCOUNT`, `STRANGER` (the UUID placeholder),
  `_OrderedBridge`, `_wire_ack` and `_received`, which the `App.run` harness can reuse. The
  harness wires the acknowledgement as `build_runtime` does: one object, `paused` from the
  core's gate, and the App's adapter.
- In the harness, `("stop", OWNER)` in `bridge.typing` is a completion, so group 8's stop
  condition is reached only after the close has finished.

## Group 8 — Guard tests

Suite after group 8: 3602 passed, 4 skipped (3598 + 4 new in
`tests/test_acknowledgement_guards.py`; the 8.3 change extends an existing test). The new
file's tests take about 0.3 s together (1.2 s with imports on a cold start) and were stable
over 10 serial runs. **No defect was found. Nothing in `henk/` changed.**

- **The harness is wired by hand, not through `build_runtime`.** `build_runtime` constructs
  its own `SignalCliRestBridge`, and patching it out would test the patch. The harness copies
  the relevant wiring: one `OwnerAcknowledgement` on the App's adapter, `paused` the core's
  gate's `has_pending`, its bound `working` as the core's `working_indicator`, and the same
  object on the Dispatcher. It also wires a real `AuditLog` with `MutationReceipts` (on the
  gate and the core), a real store under `tmp_path` with `MemoryRecall` and `OwnerCommands`,
  and `FakeSessionFactory`. The store is seeded with one memory, so the first turn carries a
  recall block and the store file is really written and read. Reminders and events stay
  off: the time header and delivery note are reminder-only, and the event path is not an
  owner turn.
- **The acknowledge bound in the harness is 0.5 s, not the 5 s default.** Nothing in these
  runs hangs, so the value only has to be real. A smaller bound means a regression that did
  hang still fails inside the 5 s drive bound, on an assertion.
- **Envelopes carry both `envelope.timestamp` and `dataMessage.timestamp`**, as the bridge
  reports them. The stranger uses the UUID placeholder for `source` and `sourceUuid`. The
  owner's group envelope carries only `source`, as in 5.2.
- **The drive** is `_until` and `_cancel`, imported from `tests/test_app.py` together with
  `ACCOUNT` and `STRANGER`. It runs until the owner DMs' stops are all in `bridge.typing`,
  then cancels `App.run` and awaits it, inside `asyncio.wait_for(…, 5.0)`. The store's
  connection is closed after that await, and only then are the files scanned. Closing it
  moves WAL content into the main file, and the scan reads every file under `tmp_path`
  anyway, so a left-over `-wal` or `-shm` is covered too. An autouse fixture fails any test
  that leaves a task running after `App.run` has shut down.
- **8.1** compares `inspect.signature(AgentCore.submit)` parameters after `self` with
  `["text"]`. Both assertion messages name design D2 and finding 10, and say what to do.
- **8.2 sentinel: `1987654321987`.** It has 13 digits like a real millisecond timestamp, but
  falls in 2032. No clock value in the run can contain it. `time.time()` renders 10 integer
  digits and at most 7 fraction digits, so its repr cannot hold 13 contiguous digits. No
  other envelope, id or hash can contain it either (a 13-digit decimal run inside a hex digest
  is about a 1-in-10^14 event). The scan looks for its decimal ASCII form, which is the
  reference itself. It does not look for the seconds float `InboundMessage.timestamp`
  carries (`1987654321.987`). D2 notes that the same value crosses the boundary as that
  field, and the spec restricts only the reference. A binary integer encoding (for example,
  a SQLite INTEGER column) would also escape a byte scan. No owner-turn path writes one
  today, and adding one would take a schema change, which review would see.
- **Non-vacuity checks in 8.2**: the receipt carried the sentinel. The audit file holds
  exactly one session record, with `turn_count == 1`, so the flush ran. The store file holds
  the memory, and the session turn holds the recall block and the text. At least one send
  was made.
- **8.3** adds the five tokens and one assertion to the existing scan: a `MUST_SCAN` list
  (`henk/channel/acknowledge.py`, `henk/app.py`) that must be among the scanned files. The
  scan already used `rglob`, so this proves coverage rather than adding it. Probed: a
  `"/v1/receipts"` constant planted in `acknowledge.py` turns the scan red on its assertion.
  In passing I noticed that `receiptMessage`, a token that predates this change, appears
  nowhere in `henk/`, `signal.py` included. It can only ever catch a new leak, which is fine
  for a denylist, and it is left as it was.
- **8.4** also asserts that the set of every recipient across `sends`, `receipts`, `typing`
  and `ack_attempts` is exactly `{OWNER}`. It also checks that one receipt was attempted,
  and that the only session saw only the owner DM. The two allowlist lines are asserted
  verbatim.
- **8.5 is one test that runs the harness twice**, in `tmp_path/enabled` and
  `tmp_path/disabled`, so it can compare the two runs directly. `FakeSessionFactory` replies
  `"{reply}:{content}"`, so the expected reply is computed from what the session saw. The
  test asserts the reply splits into more than one chunk at `safe_length=80`. The disabled
  run has no stop to wait for. It drives until the reply's last chunk is sent, then gives
  the loop 50 more iterations before cancelling, so a stray acknowledgement after the reply
  would have been recorded. Its `ack_attempts` is asserted empty as well as `receipts` and
  `typing`. Probed: a `start_working` that also sends a message turns 8.5 red on the
  `sends` equality.

## Group 9 — mutation check

Method: each mutant alone, applied by exact replacement from a scratch backup, run with
`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q -p no:cacheprovider …`, then
restored and compared byte for byte (`filecmp`) against the backup. `git diff --stat`
afterwards shows only this group's two test files, plus a `README.md` change that is not
this group's (see the report). "Earlier" means the result was recorded by the group named,
and is not re-run here unless marked **re-run**.

| # | Mutant | Killed by | How it failed | Source |
|---|---|---|---|---|
| M1 | receipt moved above the allowlist check | `test_app.py::test_stranger_gets_nothing_with_acknowledgement_enabled` (5.2); `test_acknowledgement_guards.py::test_a_stranger_and_a_group_message_get_nothing_end_to_end` (8.4) | 5.2: `receipts` held the stranger's and the group message's refs, expected `[]`. 8.4: `receipts` had 3 entries, index 0 `(OWNER, …301)` ≠ the owner DM's `…303`. Both misdirected receipts were addressed to the **owner** (D1's structural addressing) | **re-run** against both |
| M2 | `channel_ref` on `OwnerTurn`, passed through `submit` (and by the dispatcher) | `test_the_owner_turn_carries_the_text_alone` (8.1) | `['text', 'channel_ref'] == ['text']`, with the D2 / finding 10 message. 8.2 stays green under M2 alone, as it should: the reference is queued but never written, sent or composed. That is why 8.1 is structural | **new** |
| M3a | the reference written into the session audit record via a side channel (`Dispatcher` → `core.note_inbound_ref(ref)` → `record["inbound_ref"]` in `_write_audit_record`), leaving `OwnerTurn`/`submit` untouched | `test_the_channel_reference_is_never_audited_persisted_sent_or_put_in_a_turn` (8.2) | "the channel reference was persisted in […/audit/audit.jsonl]". 8.1 stays green, so 8.2 bites on its own | **new** |
| M3b | the M2 route carried on into the record (`process` stores `turn.channel_ref`, `_write_audit_record` writes it) | 8.1 and 8.2 | 8.1 as M2; 8.2 as M3a | **new** |
| M3c | the reference put into the turn content (`submit(f"{text}\n[message {ref}]")`), `submit` still text-only | 8.2 | the session-turn check: the content ends `[message 1987654321987]`. The echoed reply would fail the send check next | **new** |
| M4 | `paused` ignored | `test_the_indicator_is_suspended_while_paused_and_resumes_at_the_next_poll`; core: `test_the_indicator_is_suspended_while_an_approval_is_pending` | `20.0 == 4.0` (no pause stop); core: "refreshed while pending" | groups 4, 6 |
| M5 | stop sent on the cancellation path | `test_a_cancelled_body_sends_no_stop[idle,hung]`, `test_a_cancel_during_entry_leaves_no_indicator_task`; core: `test_a_cancelled_worker_propagates_and_sends_no_stop[process,run]` | a `("stop", OWNER)` / `("stop", None)` that must not be there | groups 4, 6 |
| M6 | `asyncio.timeout` removed from `receipt` | `test_a_hung_receipt_returns_within_the_bound_and_logs_one_line` | 2 s fail-fast bound | group 4 |
| M7 | the close issues the stop from the closing coroutine | `test_a_refresh_in_flight_at_exit_completes_before_the_stop` | typing `[start, stop, start]` | group 4 |
| M8 | failure logged on every tick | `test_refresh_failures_are_logged_once_per_turn` | `8 == 1` | group 4 |
| M9 | acknowledgement operations take `_send_lock` | `test_an_acknowledgement_does_not_wait_behind_a_send_in_flight`; re-run also `test_the_lock_wraps_the_shared_sequence_not_the_two_wrappers` | 2 s bound; lock-count guard | group 3; **re-run** full suite: 2 failed |
| M10 | `_convert` mints `"0"` for a timestamp-less envelope | `test_timestamp_less_envelope_carries_no_channel_reference` ×4 | `'0' is None` | group 3; **re-run** full suite: the same 4 failed, nothing else |
| M11 | `from_dict` fallback for `acknowledge_owner` is `False` | `test_acknowledgement_enabled_by_default_when_the_keys_are_absent` | `False is True` | group 1 |
| M12 | `from_dict` never passes `acknowledge_owner` | `test_an_explicit_false_acknowledge_owner_is_honoured` | `True is False` | group 1 |
| M13 | `except BaseException` in the bounded helpers (and 13a-c singly) | outer-cancel receipt, cancelled-body[hung], outer-cancel-during-close | cancel swallowed / 2 s bound | group 4 |
| M14 | a smuggled `sender` parameter on `acknowledge` | `test_no_send_operation_exposes_an_arbitrary_recipient` | "Left contains one more item: 'sender'" | group 3 |
| M15 | the close waits out the poll | `test_a_healthy_close_wakes_on_exit_not_on_the_poll` (+ others) | stop missing from `typing` | group 4; **re-run** with a different shape (`_wake` waits `ALL_COMPLETED`): 20 failed, all 3 `App.run` guards among them (the 1 s poll outlasts the 0.5 s close bound, so no stop completes and `_until` fails) |
| M16 | `asyncio.timeout` removed from the close | `test_a_hung_stop_is_bounded`, `test_the_bound_exhausted_before_the_stop_leaves_the_indicator_to_expiry`; core: `test_a_hung_stop_does_not_hold_the_next_turn_beyond_the_bound` | 2 s fail-fast bound | groups 4, 6; **re-run** (`timeout(None)`) full suite: exactly these 3 failed |
| M17 | `try: await task / except CancelledError: pass` in the close | outer-cancel-during-close, hung-stop, bound-exhausted | cancel swallowed; expiry line missing | group 4 (M17b on the cancel path: equivalent mutant, see group 4) |
| M18 | entry `sleep(0)` above the `try` | `test_a_cancel_during_entry_leaves_no_indicator_task` | orphaned indicator task | group 4 |
| M19 | refresh 8.0 / measured from return / fixed poll wait | `test_one_lost_refresh_stays_under_the_client_expiry`; `test_one_hung_refresh_does_not_let_the_indicator_expire` | `8.0 * 2 < 15.0`; `[0, 7]`; `[0, 7, 15] != [0, 7, 14]` | group 4 |
| M20 | acknowledge-timeout cap removed | `test_an_out_of_range_acknowledge_timeout_is_refused[…]`, `test_the_acknowledge_timeout_cap_is_the_typing_refresh_interval` | `DID NOT RAISE` | group 1 |

No survivors, apart from group 4's recorded equivalent mutant M17b. Two further probes of the
group 8 guards, which are not in the table: a wire route planted in `acknowledge.py` (8.3
red), and a `start_working` that also sends a message (8.5 red).

## Group 10 — Documentation

Written by the orchestrating session, in parallel with group 8 (README only, no shared files).
`## Configuration` gains the two keys and names `send_timeout_seconds`/`open_timeout_seconds`,
which the `signal.*` bullet omitted; `## Architecture` gains the two-sentence owner/stranger
paragraph; the v1 checklist's *Owner identity* item gains the never-read diagnostic; a new
`### Deploy-verify checklist (owner acknowledgement — deploy day)` holds group 12 in short form
and points to this change for the full commands; `## Rollback` gains the flag, the backup-first
pointer and the loader check expecting `False`.

## Group 11 — Verification and close-out

- **11.1, read end to end.** *Outbound sends are serialized* speaks only of send sequences
  ("reply or proactive"), and the transport delta says acknowledgement requests are not send
  sequences, so it does not read as covering them. One scenario over-promised and was corrected
  in the delta: *A start in flight cannot land after the stop* said the refresh and the stop
  "both complete within one acknowledge timeout". That holds only for a refresh that returns
  early: a refresh that hangs until its own bound (the same length, started earlier) uses up
  almost all of the close's bound, so the stop can be left unsent (group 4 finding 3). It now
  says the stop is sent only after the refresh has completed or been cut off, the turn's exit
  completes within one timeout of the close beginning, and a stop left unsent is cleared by the
  client-side expiry, which is what the requirement text already said for an exhausted bound.

## Review round (11.2) — test gaps closed

A reviewer found four test gaps and no implementation defect. Tests only; `henk/` is
unchanged, and no new test exposed a defect.

- **Gap 1, the configured bound.** The elapsed checks allowed `T + 0.5` on `T = 0.05`
  (about 11x), so a close bound of 5x or 2x and a receipt bound of 3x passed the suite. Two
  fixes, each enough on its own. (a) `test_every_bounded_operation_gets_exactly_the_configured_timeout`
  replaces the module's `asyncio` global with a spy (`ack_asyncio` fixture, via
  `monkeypatch`, so only `acknowledge.py`'s own calls are seen and the real module is restored)
  and drives a receipt plus a turn with a refresh, a pause stop, a resume start, a second
  refresh and the close. It asserts seven `asyncio.timeout` calls, each with exactly the
  configured value (a distinctive 0.321), and pins the issue times so the scenario provably ran.
  The final stop opens no bound of its own: it runs inside the close's. (b) The measuring tests
  (hung receipt, hung stop, bound exhausted in `test_acknowledge.py`; hung stop in
  `test_agent_core_acknowledgement.py`; hung receipt in `test_app.py`) now use a 0.2 s bound
  and hold the elapsed time to `[0.9, 1.5) x bound`. 0.2 s keeps scheduling noise small
  against the bound, and 1.5x sits below the smallest multiple a mutant would plausibly
  apply (2x). `MARGIN` is gone. `T = 0.05` stays for tests that need a bound to exist but do
  not measure it.
- **Gap 2, exceptions through the indicator path.** `test_refresh_failures_are_logged_once_per_turn`
  is parametrized `refused`/`raising` (`RuntimeError` from the bridge, which the Signal
  adapter lets through, as a second adapter might for anything). The start and later the stop
  raise; the refresh count, one loop line and one close line still hold. New
  `test_a_failing_stop_logs_one_close_line_and_the_close_returns[refused|raising]`. The
  final-stop narrowing mutant turned out to be **equivalent at the log level**: the task dies
  with the `RuntimeError`, and the close's dead-task branch logs the same line. It is killed by
  asserting the indicator task (found by the same spy's `create_task`) ends without an
  exception. Pinning that is justified because the two layers must each hold: with both gone,
  a raising stop would go unlogged. New `test_an_indicator_task_that_dies_is_logged_once_by_the_close`
  makes the `paused` predicate raise, so the task dies outside every per-request handler; the
  close returns, logs exactly one line naming the `RuntimeError`, and leaves no task behind.
  `FakeTime.sleeping()` now fails the test with a message instead of raising `TimeoutError`
  when no sleeper registers (a dead loop), and the refresh test's outer bound is 2x
  `FAIL_FAST` so that message wins the race.
- **Gap 3, the daemon scenario.** `test_the_signal_daemon_is_not_configured_to_send_read_receipts`
  in `test_channel_adapter.py`, beside the other static repo guards (no compose test existed).
  It parses `docker-compose.yml` and fails on `receipt` (case-insensitive) in the
  signal-cli-rest-api service's environment (list or mapping form, keys and values), command
  or entrypoint. It also requires `MODE=` in that environment, so the guard cannot pass on an
  empty or moved service, and forbids an `env_file` on the service, which would carry settings
  the guard cannot read. It names the scenario *The daemon does not acknowledge on Henk's
  behalf* and says it pins the repo only: **task 12.2 still verifies the deployed daemon.**
- **Gap 4, "in order".** `MESSAGE_A` has three identical chunks, so the overlap test could not
  see their order. New `ORDERED_CHUNKS`/`MESSAGE_ORDERED` (chunks starting `1`, `2`, `3`, which
  is what `HoldingBridge.log` records) is used by that test alone. It asserts the split, the
  exact log `["chunk:1", "receipt", "start", "chunk:2", "chunk:3"]` and the exact `sends` list.
  Other `MESSAGE_A` users are untouched.

### Mutation results (review round 11.2)

The `acknowledge.py` mutants were loaded in-process with the reviewer's `mutplug` (no repo edit),
each over the full suite. The compose and permutation mutants were file edits through
`mutate.py`, restored and byte-compared afterwards.

| Mutant | Killed by | Failure |
|---|---|---|
| close bound x5 | structural test; hung stop; bound exhausted; core hung stop | `[0.321, …] != [0.321]*7`; `1.001s is more than one 0.2s bound`; core: `_until` 2 s fail-fast (two 1 s closes) |
| close bound x2 | structural; hung stop; bound exhausted; core hung stop | `0.401s is more than one 0.2s bound`; core `0.400s …` |
| receipt bound x3 | structural; hung receipt; `test_app` hung receipt | `the receipt`; `0.601s is more than one …` |
| `_attempt` catches only `TimeoutError` | `test_refresh_failures_are_logged_once_per_turn[raising]` | `Failed: the indicator loop never went back to sleep` |
| final stop catches only `TimeoutError` | `test_a_failing_stop_logs_one_close_line_and_the_close_returns[raising]` | `RuntimeError('adapter bug') is None` (log-equivalent, see above) |
| dead-task log removed | `test_an_indicator_task_that_dies_is_logged_once_by_the_close` | `[]` (no close line) |
| compose env `SIGNAL_CLI_SEND_READ_RECEIPTS=true` | daemon guard | `['environment: SIGNAL_CLI_SEND_READ_RECEIPTS=true']` |
| compose `command: ["--send-read-receipts"]` | daemon guard | `['command: --send-read-receipts']` |
| compose mapping-form `Auto_Read_Receipt: "1"` | daemon guard | `['environment: Auto_Read_Receipt=1']` |
| expected sends permuted (1, 3, 2) | overlap test | list mismatch |
| expected log permuted | overlap test | list mismatch |

No survivors. Flake check on the four touched files: 15 serial runs and 6 parallel processes
x 5 runs, 45 of 45 green (150 tests each).
- **11.2, conformance sweep.** Done by a fresh reviewer against both deltas, scenario by
  scenario. No implementation defect. It found four test gaps, all closed and mutant-proven in
  the *Review round (11.2)* section above: the bound tests allowed ~11x the configured timeout
  (a close bounded at 5x passed the whole suite), no test sent an adapter exception through the
  indicator path, the daemon scenario had no repo guard, and the send-overlap test could not
  observe chunk order. The daemon scenario is now guarded in the repo and still verified on the
  instance by 12.2.
- **11.3.** Full suite 3608 passed, 4 skipped, against the pre-change baseline of 3478 passed,
  4 skipped (+130). No existing test was modified beyond those the tasks name
  (`test_no_send_operation_exposes_an_arbitrary_recipient`, `WIRE_FORMAT_TOKENS`) and the
  send-overlap test this change itself added. No project linter is configured: `uvx ruff check`
  over the changed files shows no new rule class against the same files at `cdb13ef`; the
  handful of new findings copy idioms already present in those files (`# noqa: BLE001` on the
  bridge's exception wrapper, unused unpacked `_wire()` values), and the one import-order
  finding in a new test file was fixed.
- **11.4.** `openspec validate owner-acknowledgement --strict` passes.
