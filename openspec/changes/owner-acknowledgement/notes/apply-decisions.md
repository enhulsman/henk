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
