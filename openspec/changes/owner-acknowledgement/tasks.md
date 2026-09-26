# Tasks — Owner Acknowledgement

> **TDD is not optional here.** Every group writes its tests from the delta scenarios *before*
> its implementation. Each scenario maps to at least one test, and each SHALL/MUST to at least
> one assertion; the task that names a test names the scenario. Implementation happens in a
> fresh session via `/opsx:apply`. **Hard stop before any deploy to rp5: explicit owner go
> required.**
>
> **Standing rules for this change.**
> 1. **Exercise the production path, not only a double.** `FakeChannel` is cooperative and
>    never fails. Every failure, hang and stranger test runs a real `SignalAdapter` over a
>    `FakeBridge` scripted to reject or hang.
> 2. **Test defaults through `Config.from_dict` with the key absent**, never against a
>    dataclass attribute. rp5's `config.yaml` carries neither new key.
> 3. **No real identifiers.** Owner `+31600000000`, Henk's account `+31611111111`, stranger
>    the UUID placeholder `00000000-0000-4000-8000-000000000000` (the stranger number the
>    existing tests use is rejected by the pre-commit hook on any new line; corrected at apply,
>    group 1-3 gate). The pre-commit hook enforces it.
> 4. **Re-grep before relying on a cited line.** The line numbers in `design.md` were read on
>    2026-09-26.
> 5. **`henk/channel/signal.py` must stay clean for its guards.** No new `Lock`, no
>    attribute call named `wait_for` or `timeout`, none of the strings `priority`,
>    `max_chunks`, `chunk_cap`, `hold_timeout`, and no `AsyncClient(` outside `_build_client`.
>    `test_the_lock_is_the_whole_mechanism_and_nothing_more` and
>    `test_no_bridge_code_path_constructs_a_client_without_a_timeout` must pass **untouched**.
> 6. **Before changing a signature, grep for what observes it**: constructions, monkeypatches
>    and recording attributes (`grep -rn "Dispatcher(\|\.sent\b\|\.calls\b\|\.sends\b" tests/`).
>    A test that patches an old symbol goes silently dead rather than failing.
> 7. **Bounded tests fail fast.** Every test that drives a hanging bridge wraps its own await
>    in `asyncio.wait_for(…, 2.0)`, so a missing bound fails the test instead of hanging the
>    suite.
>
> Record decisions and each group's mutation results in `notes/apply-decisions.md`.

## 1. Config

- [x] 1.1 Tests in `tests/test_config.py`, all through `Config.from_dict` (scenarios *Enabled
      by default when the keys are absent*, *An explicit false is honoured*, *A non-boolean flag
      is refused*, *An out-of-range acknowledge timeout is refused*):
      - a `signal` section carrying only `bridge_url`, `account`, `safe_length` loads
        `acknowledge_owner is True` and `acknowledge_timeout_seconds == 5.0`;
      - `acknowledge_owner: false` loads `False`. This is the test that catches a builder that
        never reads the key (design D8), so it must exist even though the first one passes;
      - `acknowledge_owner: "false"` (a string), `acknowledge_owner: 1` and a blank
        `acknowledge_owner:` (YAML `null`) each raise `ConfigError`;
      - `acknowledge_timeout_seconds` of `0`, a negative number, a non-number, `true` (a
        boolean, which `float()` would accept as `1.0`) or anything above
        `TYPING_REFRESH_SECONDS` (`7.5`) raises `ConfigError`; `2.5` and exactly `7.0` load;
      - the existing signal keys' effective values are unchanged.
- [x] 1.2 Implement in `henk/config.py`: `SignalConfig.acknowledge_owner: bool = True` and
      `SignalConfig.acknowledge_timeout_seconds: float = 5.0`, **and** the `from_dict` builder
      reading each key with an explicit fallback, exactly as the adjacent
      `send_timeout_seconds` / `open_timeout_seconds` do (`config.py:1014-1027`). Strict
      boolean check (a blank value refused too); positive, non-boolean number check; cap at
      `TYPING_REFRESH_SECONDS` imported from `henk.channel.signal` (design D8 gives the gap
      arithmetic). Do **not** add unknown-key rejection to `signal:`; that is out of scope, and
      the rollback check covers the misspelt-key case (task 10.4). The comments say what the timeout is: a
      whole-operation bound enforced by cancellation outside the adapter, not a phase timeout
      and unrelated to `send_timeout_seconds`.
- [x] 1.3 `config.yaml`: add both keys under `signal:` with one-line comments in the same
      terms as 1.2. The flag's comment names it as the rollback.
- [x] 1.4 Correct the stale wording this change would otherwise sit next to:
      `SignalConfig.send_timeout_seconds`' comment (`config.py:352-356`) and `config.yaml:31-33`
      still say "TOTAL budget … decomposed/split across httpx's transport phases". Replace them
      with what shipped: the configured value applies to each phase in full, and no total is
      specified for sends. Comment-only; no test change.

## 2. Transport: bridge endpoints

- [x] 2.1 Extend `tests/conftest.py` `FakeBridge` (`:151-169`), keeping `sends` and the receive
      script unchanged:
      - `receipts: list[tuple[str, int]]` (recipient, timestamp);
      - `typing: list[tuple[str, str]]` (`"start"`/`"stop"`, recipient);
      - `ack_faults: dict[str, Exception | asyncio.Event]`, keyed `"receipt"`, `"start"`,
        `"stop"`: an exception instance to raise, or an event to await (a hang until the test
        sets it, or forever), applied per operation, so a test can hang only the stop. Sends are
        unaffected;
      - **`hold_open: bool = False`**. When true, `receive()` awaits an `asyncio.Event` that is
        never set once its script is exhausted, instead of returning. Without it an `App.run`
        test cannot terminate and the script replays: after a clean stream end,
        `SignalAdapter.messages()` sleeps `backoff_base` and calls `receive()` again
        (`signal.py:113-116`), and `receive()` iterates the same `_script` each time
        (`conftest.py:162-166`). A one-envelope script was measured yielding the same DM six
        times in 0.3 s. Default `False` keeps every existing test's behaviour.
- [x] 2.2 Tests for `SignalCliRestBridge`, with `_build_client` patched to return an
      `httpx.AsyncClient` on an `httpx.MockTransport` that keeps the configured timeout.
      Assert the timeout is still present on the patched client, so the patch cannot bypass the
      guarantee. Scenarios *A read receipt names the owner and the message* and *Typing
      indicator start and stop reach the owner*, at the wire:
      - `send_receipt(recipient, timestamp)` → `POST {base}/v1/receipts/{account}` with JSON
        exactly `{"recipient": …, "receipt_type": "read", "timestamp": <int>}`;
      - `start_typing(recipient)` → `PUT {base}/v1/typing-indicator/{account}` with
        `{"recipient": …}`;
      - `stop_typing(recipient)` → `DELETE` on the same path with the same body. httpx's
        `client.delete()` takes no body, so this must use `client.request("DELETE", …,
        json=…)`, and the test pins that a body is sent;
      - a non-2xx response or a transport error raises `SignalBridgeError`, as `send` does.
- [x] 2.3 Implement the three methods on the `SignalBridge` Protocol (`signal.py:39-47`) and on
      `SignalCliRestBridge`, each through `_build_client()`. No retry, no bound here (design D6).
      Run the lock and client-construction tests: both must pass untouched (standing rule 5).

## 3. Contract and Signal adapter

- [x] 3.1 Tests first (scenarios *Inbound message received*, *Timestamp-less envelope carries
      no channel reference*, *A read receipt names the owner and the message*, *Typing indicator
      start and stop reach the owner*, *A failed acknowledgement request is reported, not
      raised*, *An uninterpretable reference makes no request*, *An acknowledgement does not wait
      behind a send in flight*, *The receipt cannot be addressed to the sender*):
      - `_convert` sets `channel_ref` to the envelope timestamp as a decimal string, and to
        `None` (not `"0"`) when both `dataMessage.timestamp` and `envelope.timestamp` are
        absent or zero;
      - `acknowledge(ref)` makes exactly one `FakeBridge.receipts` entry, `(owner, int(ref))`,
        where `owner` is the adapter's constructor `owner`. Run it through `messages()` on an
        envelope whose `source`/`sourceUuid` differ from `owner`, then acknowledge that
        message's reference: the receipt still goes to `owner`, since no input carries a sender;
      - `acknowledge(None)` and `acknowledge("not-a-number")` make no bridge call and return
        `False`;
      - `start_working()` / `stop_working()` record `("start", owner)` / `("stop", owner)`;
      - with `ack_faults` set to an exception for each operation, each returns `False`, does not raise, and the bridge saw
        exactly one attempt;
      - with a `SlowBridge`-style send holding `_send_lock` mid-chunk, `acknowledge` and
        `start_working` complete before the send finishes, and the send's chunks stay
        contiguous.
- [x] 3.2 Extend the recipient-inspection test (`tests/test_channel_adapter.py:199-231`) with
      `ACK_OPERATIONS = {"acknowledge": ["channel_ref"], "start_working": [], "stop_working":
      []}`, checked on both `ChannelAdapter` and `SignalAdapter` for the exact parameter list
      **and** the `RECIPIENT_DENYLIST`, exactly as for sends (scenario *No recipient reachable
      through any acknowledgement operation*). Do not merge it into `SEND_OPERATIONS`; that
      dict's name is its claim.
- [x] 3.3 Implement:
      - `InboundMessage.channel_ref: str | None = None` in `henk/channel/base.py` (no
        `repr=False`: design D2 explains why hiding it would protect nothing). The default
        keeps every existing construction valid (`inbound()` in conftest, `_msg()` in
        `test_app.py`);
      - the three operations on the `ChannelAdapter` Protocol, with docstrings stating the no-
        raise, no-recipient and owner-only rules;
      - `SignalAdapter.acknowledge/start_working/stop_working`: single attempt, recipient
        `self._owner`, **no `_send_lock`**, catch `SignalBridgeError` and return `False`, no
        logging (the caller owns the log line);
      - `_convert` minting the reference;
      - `TYPING_REFRESH_SECONDS = 7.0` in `signal.py`, with a comment citing the ~15 s client
        expiry and the one-lost-refresh arithmetic (2 × 7 = 14 < 15; design D4);
      - update the `DEPLOY-VERIFY` note (`signal.py:217-222`): a delivered-but-never-read owner
        message now diagnoses a silent allowlist drop (or a failed receipt, which is logged).
- [x] 3.4 Extend `tests/conftest.py` `FakeChannel` (`:17-46`) with the three operations, each
      returning `True` and appending to a **new** `acks: list[tuple[str, str | None]]`
      (`("receipt", ref)`, `("start", None)`, `("stop", None)`). `sent` and `calls` stay
      exactly as they are, so every existing `.sent`/`.calls` assertion passes untouched.
      Re-run standing rule 6's grep and confirm that.

## 4. Bounded acknowledgement (`henk/channel/acknowledge.py`)

- [x] 4.1 Tests first, against `OwnerAcknowledgement` over a real `SignalAdapter` + `FakeBridge`
      unless the test is about cadence alone. Use small timeouts (≈0.05 s). Where the test is
      about the poll or refresh interval, inject **both** `sleep` and `clock` (scenarios *An
      acknowledgement failure never fails or delays the turn beyond the bound*, *The indicator
      stays up through a long turn*, *The indicator is suspended during an approval wait*, *A
      start in flight cannot land after the stop*, *A healthy close does not wait out a poll*,
      *A hung stop is bounded*, *Refresh failures are logged once per turn*, *Shutdown mid-turn
      sends no stop*, *A message without a channel reference is not acknowledged and logs
      nothing*).

      **Receipts:**
      - with a hanging bridge, `receipt(ref)` returns within the timeout plus a small margin,
        logs one line, and does not raise; with a raising bridge, likewise;
      - `receipt(None)` makes no bridge call and logs nothing;
      - an outer `task.cancel()` during a hanging `receipt` propagates `CancelledError` and is
        not swallowed.

      **Entry and cadence:**
      - entering `working()` issues a start before the body's first statement runs, without
        awaiting its response (with a hanging start, the body still begins immediately);
      - `TYPING_REFRESH_SECONDS * 2 < 15`, asserted against the constant;
      - a body lasting several refresh intervals on the injected clock produces a start at each
        interval, never a gap ≥ 15 s, and exactly one stop after the body;
      - **one hung refresh** (scenario *One hung refresh does not let the indicator expire*): on
        the injected clock, with T = `TYPING_REFRESH_SECONDS`, one refresh hangs for T. The next
        start is issued no later than `2 × TYPING_REFRESH_SECONDS` after the last successful
        start, with no extra poll in between;
      - `paused()` turning true sends one stop, and no start while it stays true; turning false
        sends a start at the next poll, before the next refresh interval would have elapsed.

      **Close:**
      - **no request in flight**: with the injected `sleep` blocked on an event the test never
        sets, `__aexit__` returns and the stop is recorded **without the fake sleep being
        released**. This proves the loop wakes on `exit_event`, not on the poll;
      - **a refresh in flight at exit**: with a start that blocks on an event the test releases,
        the stop is recorded after that start in `FakeBridge.typing`, never before it. Only the
        indicator task issues typing requests (assert by patching the adapter's `stop_working`
        to record the calling task);
      - **a hung stop**: with `ack_faults["stop"]` a never-set event, `__aexit__` returns within
        T plus margin, exactly one line is logged, the task is done, and nothing raises;
      - **the bound exhausted before the stop**: with a refresh in flight that hangs past T,
        `__aexit__` returns within T plus margin and no stop is recorded as completed. The
        indicator is left to client expiry, and one line is logged;
      - the body raising `Exception`: stop sent, the exception propagates unchanged.

      **Cancellation:**
      - the body cancelled: `CancelledError` propagates, the task is done, and
        `FakeBridge.typing` contains **no** `"stop"`;
      - **cancelled during entry**: cancel the owning task while it is at the entry yield. The
        indicator task is done afterwards, and `CancelledError` propagates;
      - **outer cancel during the close**: with a hung stop, cancel the owning task while
        `__aexit__` awaits. `CancelledError` propagates out of the owning task (it is not
        swallowed by the await on the child), and the indicator task is done.

      **Logging, and no leftovers:**
      - every start and refresh failing across a long body logs **exactly one** failure line;
        the close logs at most one more;
      - no task is left running after any of the above (`asyncio.all_tasks()` check).
- [x] 4.2 Implement `OwnerAcknowledgement(adapter, *, timeout, refresh_seconds, paused=None,
      sleep=asyncio.sleep, clock=time.monotonic)`, with `async receipt(channel_ref)` and
      `@asynccontextmanager working()`, per design D3-D6. `PAUSE_POLL_SECONDS = 1.0` is a
      module constant.
      - **One task** issues every typing request: the first start, the refreshes, the pause stop
        and resume start, and the final stop.
      - Each wake-up waits on `exit_event` **or** `sleep(max(0, min(PAUSE_POLL_SECONDS,
        next_due - clock())))`, whichever comes first. `next_due` is the last start's **issue**
        time plus the interval, whether that start succeeded or not, so an overdue refresh fires
        as soon as a hung one times out (design D4). Use `asyncio.wait(..., return_when=
        FIRST_COMPLETED)` over two **tasks**, since 3.11+ refuses bare coroutines: one for
        `exit_event.wait()`, one for the sleep. Cancel the loser **and await it**, or 4.1's
        `asyncio.all_tasks()` check fails.
      - The entry `await asyncio.sleep(0)` sits **inside** the `try`.
      - The close sets `exit_event` and awaits the task inside **one**
        `asyncio.timeout(timeout)`. On expiry it cancels the task.
      - The close's await is wrapped so that **any `BaseException` there**, including an outer
        cancellation arriving during the close, cancels the task, awaits `asyncio.wait({task})`,
        and re-raises. `asyncio.wait` raises the outer `CancelledError` but does not cancel the
        child, so without this the task would outlive the turn.
      - Nesting: the `BaseException` wrapper sits **inside** `asyncio.timeout`, and the
        `except Exception` that logs and returns sits **outside** it, so an expiry surfaces as
        `TimeoutError` at the `async with` boundary and is logged, never raised out of the close.
      - The bracket awaits the task with `await asyncio.wait({task})`, **never**
        `try: await task / except CancelledError`, which would swallow an outer shutdown cancel
        (design D4).
      - On `CancelledError` it cancels the task without setting the event, awaits it the same
        way, sends nothing, and re-raises.
      - The bound is `asyncio.timeout(...)` here, never in `signal.py`. Catch `Exception`, never
        `BaseException`. The module must carry no wire token (the group 8 scan covers it).

## 5. Dispatcher receipt (`henk/app.py`)

- [x] 5.1 Tests first in `tests/test_app.py` (scenarios *Owner message receives a read
      receipt*, *An approval reply is acknowledged*, *A hung receipt does not hold the message it
      acknowledges*, *Disabled means nothing is sent*):
      - owner DM: `core.submit` is called, then one receipt with the message's reference,
        in that order;
      - pending approval plus a `yes`: routed to `gate.deliver` and acknowledged; an unrelated
        message during a pending approval: fails closed, is requeued, and is acknowledged once;
      - hanging receipt: the message is already on the core queue when the receipt starts, and
        `on_inbound` returns within the timeout plus margin;
      - `acknowledgement=None`: no `acks` entry of any kind, and behaviour identical to today.
- [x] 5.2 **The stranger test, re-run with acknowledgement enabled** (scenario *Stranger gets
      nothing with acknowledgement enabled*; this is the one that matters). Add
      `test_stranger_gets_nothing_with_acknowledgement_enabled` beside
      `test_stranger_dropped_before_reaching_core` (`test_app.py:92-97`), leaving that test
      untouched: a stranger DM and an owner group message through a Dispatcher wired with an
      `OwnerAcknowledgement` over a real `SignalAdapter` + `FakeBridge`. Assert
      `FakeBridge.receipts == []`, `FakeBridge.typing == []`, `FakeBridge.sends == []`,
      `factory.created == 0`, and the allowlist's drop log lines present.
- [x] 5.3 Implement: `Dispatcher.__init__(allowlist, gate, core, *, acknowledgement=None)`.
      Keyword-only with a `None` default, so `tests/test_app.py:68` and
      `tests/test_reminders_runtime.py:199` construct unchanged; confirm both still pass, and
      record that they were checked. The receipt goes after routing, skipped when
      `message.channel_ref is None`.

## 6. Core bracket (`henk/agent/core.py`)

- [x] 6.1 Tests first, in a new `tests/test_agent_core_acknowledgement.py`, with a real
      `OwnerAcknowledgement` over `FakeChannel` unless a failure is needed (every agent-core
      delta scenario):
      - normal turn: `acks` shows `start` before the session's `run_turn` saw the content, and
        `stop` after the reply appears in `sent`;
      - errored turn: `stop` after the error reply;
      - `_ensure_session` raising (a factory whose `create` raises): the bracket still stopped,
        no task left running;
      - empty reply: stop, and nothing in `sent`;
      - `/memories` and `/new`: no `acks` entries;
      - event turn: no `acks` entries, and output still via `send_proactive`;
      - worker cancelled mid-turn: `CancelledError` propagates out of `process` (and out of
        `AgentCore.run`, so a shutdown cannot leave the worker looping on `queue.get()`), no
        `stop`;
      - `working_indicator=None`: session content and `sent` byte-identical to a run without
        this change (compare against the existing recall/time-header expectations);
      - **approval integration**: a real `ApprovalGate` with `demote_standing=True` and a
        session that invokes a standing tool, so the gate prompts; the acknowledgement's
        `paused` is that same gate's `has_pending`. While the prompt is pending no refresh
        start is sent; after `gate.deliver("yes")` a start is sent before the turn ends;
      - **hung stop at the core level** (scenario *A hung stop does not hold the next turn
        beyond the bound*): a real `SignalAdapter` over a `FakeBridge` with
        `ack_faults["stop"]` a never-set event, and two owner messages queued. The first reply
        is in `bridge.sends`, and the second turn's `run_turn` begins within T plus margin of
        the first bracket's close.
- [x] 6.2 Implement: `AgentCore(..., working_indicator=None)`, typed as a zero-argument callable
      returning an async context manager. In `_process_owner`, `async with` it (or a null async
      context) placed exactly as design D3 shows, from before `_ensure_session` to after the
      reply or error send. `_framed_turn` is unchanged. **`_Sender` (`core.py:120-126`) is not
      widened** (design D3); record that as a decision so a reviewer does not read it as an
      omission. Update the module docstring's responsibility list with one bullet.

## 7. Runtime wiring (`henk/runtime.py`)

- [x] 7.1 Tests first in `tests/test_runtime.py`:
      - flag on (the default): the Dispatcher's acknowledgement and the core's
        `working_indicator` are the **same** `OwnerAcknowledgement` instance; its adapter **is**
        the App's adapter (the same-instance rule the scheduler already follows,
        `runtime.py:256-258`); its timeout equals `config.signal.acknowledge_timeout_seconds`;
        its refresh equals `signal.TYPING_REFRESH_SECONDS`; its `paused` is the gate's
        `has_pending`, on the same gate the core frames;
      - flag off: both are `None`.
- [x] 7.2 Implement in `build_runtime`: construct one `OwnerAcknowledgement` after the gate and
      adapter exist, when `config.signal.acknowledge_owner`; pass it to the Dispatcher
      (`runtime.py:249`) and its `working` to `AgentCore` (`runtime.py:192`).

## 8. Guard tests

**The `App.run` harness** used by 8.2, 8.4 and 8.5, because no existing test drives `App.run`
with a real `SignalAdapter`. The existing ones use a one-shot adapter
(`tests/test_reminders_runtime.py:170-180`), and a plain `FakeBridge` replays its script
forever (task 2.1):
- `FakeBridge(script, hold_open=True)` under a real `SignalAdapter`;
- a real `OwnerAcknowledgement` wired to both the Dispatcher and the core, as `build_runtime`
  wires it;
- `RecordingSession` via `FakeSessionFactory`;
- an audit log (and any store) under `tmp_path`.

Start `App.run` as a task. Drive with `_until` until `("stop", OWNER)` is in `bridge.typing`
for the last owner DM. Then cancel the `App.run` task and await it, exactly as `_cancel` does
(`test_app.py:84-89`). **Run the file checks only after that await returns**: `App.run`'s
`finally` has then run `core.aclose()`, which is what flushes the session's audit record. Wrap
the whole drive in `asyncio.wait_for(…, 5.0)` (standing rule 7).

- [x] 8.1 `OwnerTurn` guard (scenario *The channel reference never reaches the agent core*):
      `[f.name for f in dataclasses.fields(OwnerTurn)] == ["text"]`, and `AgentCore.submit`'s
      parameters (minus `self`) `== ["text"]`. The failure message names design D2 and finding
      10, so whoever trips it knows the rule it guards.
- [x] 8.2 Sentinel scan (scenario *The channel reference is never audited, persisted, sent or
      put in a turn*), on the harness: the owner envelope's timestamp is a distinctive sentinel
      integer. Assert:
      - it appears in `FakeBridge.receipts`;
      - it is absent from every byte under `tmp_path` (checked after the `App.run` await);
      - it is absent from every session turn's content;
      - it is absent from `FakeBridge.sends`.

      Logs are deliberately not scanned (design D2).
- [x] 8.3 Wire-token scan: extend `WIRE_FORMAT_TOKENS` (`test_channel_adapter.py:275`) with
      `receipt_type`, `typing-indicator`, `/v1/receipts`, `/v2/send`, `/v1/receive`. The last
      two are routes the scan's own comment claims to cover and its list lacks. Not `typing`:
      it matches `from typing import`. Confirm the scan passes, including over
      `henk/channel/acknowledge.py`.
- [x] 8.4 End-to-end stranger run with acknowledgement enabled, on the harness: the script
      holds a stranger envelope, an owner group envelope and, last, an owner DM, so the
      drive's stop condition is reached only after both drops have been processed. Assert:
      - `FakeBridge.receipts` has exactly one entry, the owner DM's;
      - `FakeBridge.typing` holds only owner-addressed entries;
      - nothing is addressed to the stranger's number anywhere in the bridge's records;
      - the allowlist's two drop lines are logged.
- [x] 8.5 Acknowledgements are not messages (scenario *Acknowledgements are not messages*): run
      the harness twice, with acknowledgement enabled and disabled, for the same owner DM and a
      multi-chunk scripted reply. In both runs, `FakeBridge.sends` equals exactly
      `[(OWNER, c) for c in split_message(reply, safe_length)]`. The disabled run's
      `receipts` and `typing` are empty.

## 9. Mutation check

- [x] 9.1 Apply each mutant alone, run the named tests, and confirm each fails. Record the table
      in `notes/apply-decisions.md`. Any survivor is a missing assertion: fix the test, not the
      mutant.

      | # | Mutant | Must be caught by |
      |---|---|---|
      | M1 | receipt moved above the allowlist check in `on_inbound` | 5.2, 8.4 |
      | M2 | `channel_ref` added to `OwnerTurn` and passed through `submit` | 8.1 |
      | M3 | the reference written into the session's audit record | 8.2 |
      | M4 | `paused` ignored in the loop | 4.1 pause test, 6.1 approval integration |
      | M5 | stop sent on the cancellation path | 4.1 and 6.1 cancellation tests |
      | M6 | `asyncio.timeout` removed from `receipt` | 4.1 hang test (fails fast per standing rule 7) |
      | M7 | the close issues the stop itself, from the closing coroutine, while the task may still have a request in flight | 4.1 in-flight ordering test |
      | M8 | a failure logged on every tick | 4.1 log-once test |
      | M9 | acknowledgement operations taking `_send_lock` | 3.1 does-not-wait test |
      | M10 | `_convert` minting `"0"` for a timestamp-less envelope | 3.1 |
      | M11 | `from_dict` fallback for `acknowledge_owner` set to `False` | 1.1 absent-keys test |
      | M12 | `from_dict` not passing `acknowledge_owner` at all | 1.1 explicit-false test |
      | M13 | `except BaseException` in the bounded helpers | 4.1 cancellation-propagates tests |
      | M14 | receipt addressed to `message.sender` via a smuggled parameter | 3.2 inspection |
      | M15 | the loop sleeps out the poll instead of waking on `exit_event` (close waits out the poll) | 4.1 no-request-in-flight close test |
      | M16 | `asyncio.timeout` removed from the close | 4.1 hung-stop test, 6.1 hung-stop test |
      | M17 | `try: await task / except CancelledError: pass` instead of `asyncio.wait({task})` | 4.1 outer-cancel-during-close test |
      | M18 | entry `sleep(0)` moved above the `try` | 4.1 cancelled-during-entry test |
      | M19 | `TYPING_REFRESH_SECONDS = 8.0`; or the interval measured from a start's return rather than its issue; or a fixed poll wait instead of `min(poll, next_due − clock())` | 4.1 constant assertion and 4.1 one-hung-refresh test |
      | M20 | timeout cap against the refresh interval removed | 1.1 |

## 10. Documentation

- [x] 10.1 README `## Configuration`: one bullet for `signal.acknowledge_owner` (true, the
      rollback flag, and the note that rp5's file will not carry it, so it is on after deploy)
      and `signal.acknowledge_timeout_seconds` (5.0, a whole-operation bound enforced by
      cancellation). Correct the `signal.*` bullet list to name `send_timeout_seconds` and
      `open_timeout_seconds`, which it omits.
- [x] 10.2 README `## Architecture` (or the channel section): two sentences on what the owner
      now sees, and that strangers see nothing more than before.
- [x] 10.3 README: a `### Deploy-verify checklist (owner acknowledgement — deploy day)` holding
      group 12's checks in short form. In the existing v1 checklist, the *Owner identity* item
      gains: "a delivered-but-never-read DM means the allowlist dropped it, or the receipt
      failed; the log says which".
- [x] 10.4 README `## Rollback`: the flag, with a pointer to the backup-first recipe for editing
      rp5's `config.yaml`, **and the check after the edit**: re-run task 12.1's loader one-liner
      and expect `False`. The `signal:` section does not reject unknown keys, so a misspelt key
      is silently ignored and would leave acknowledgement on (design D8).

## 11. Verification and close-out

- [x] 11.1 Read every requirement this change touches end to end, in final assembled form,
      against each other: *Owner-only allowlist*, *Signal transport via signal-cli-rest-api*,
      *Swappable channel-adapter contract*, *Owner-only acknowledgement of inbound messages*,
      *Outbound sends are serialized* (unchanged, but it must not read as covering
      acknowledgements), and agent-core's *Owner agent turns are bracketed by the working
      indicator*.
- [x] 11.2 Spec→test conformance sweep: for each scenario, does its test assert the
      *scenario*, or what the implementer built? (The lesson in channel-integrity's
      post-archive corrections.)
- [x] 11.3 Full suite plus lint green. Record the pass count against the pre-change baseline.
      Tests this change knowingly touches: `test_channel_adapter.py` (recipient inspection,
      wire tokens, new transport tests), `test_app.py` (new tests only; existing ones
      untouched), `test_config.py`, `test_runtime.py`, and `conftest.py` (`FakeBridge`,
      `FakeChannel`). No other existing test may need modifying; if one does, re-run standing
      rule 6's grep rather than editing past it.
- [x] 11.4 `openspec validate owner-acknowledgement --strict`, then commit
      (publication-safe; the hook enforces it).

## 12. Deploy verification (owner-run on rp5, root shell)

All commands run as root on rp5. Set once per shell:

```bash
C="docker compose -p henk -f /home/pi/Coding/henk/docker-compose.yml"
```

- [ ] 12.1 **Before restart:** pull with the README's *Redeploying an existing install* recipe
      (backup first, `git pull` typed interactively), then `$C build henk`. Confirm the
      **effective** values through the new loader against the live file:
      ```bash
      $C run --rm --no-deps henk python -c 'from henk.config import Config; s=Config.load("/app/config.yaml").signal; print(s.acknowledge_owner, s.acknowledge_timeout_seconds, s.send_timeout_seconds)'
      ```
      Expect `True 5.0 10.0`. `grep -n acknowledge /home/pi/Coding/henk/config.yaml` should
      print nothing, which confirms the values come from the loader.
- [ ] 12.2 **Daemon auto-receipts (finding 4).** Confirm the signal-cli daemon is not sending
      read receipts on its own:
      ```bash
      $C exec signal-cli-rest-api sh -c 'for p in /proc/[0-9]*; do tr "\0" " " < $p/cmdline 2>/dev/null; echo; done' | grep -i signal-cli
      docker inspect "$($C ps -q signal-cli-rest-api)" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -i -E 'receipt|signal_cli'
      ```
      The daemon command line must not contain `--send-read-receipts`, and no environment
      variable may enable it. Record the daemon's arguments (not the account number) in the
      as-built notes.
- [ ] 12.3 **Bridge version.** Record what rp5 actually runs:
      ```bash
      docker inspect "$($C ps -q signal-cli-rest-api)" --format '{{.Image}}'
      $C exec henk python -c 'import httpx; print(httpx.get("http://signal-cli-rest-api:8080/v1/about", timeout=10).json())'
      ```
- [ ] 12.4 **Owner client settings.** In the owner's Signal/Molly client, confirm *Settings →
      Privacy → Read receipts* and *Typing indicators* are both **on**. They are reciprocal:
      with either off, nothing Henk sends is shown, and the checks below cannot be read.
- [ ] 12.5 Deploy: `$C up -d --build henk`, applying the README's three "silently did nothing"
      tells. Then, from the owner's phone:
      - send a short DM. It shows **read** within about a second, and "typing" appears until the
        reply lands. This also proves the endpoints accept the owner's **UUID** as recipient
        (finding 12);
      - ask something tool-heavy that takes well over 15 s. "Typing" stays up throughout,
        which proves the refresh;
      - send `/memories`. It is answered with no typing flash;
      - `$C logs henk --since 30m | grep -i -E 'acknowledg|typing|receipt'` prints nothing.
        Anything it prints is a failed or timed-out acknowledgement. Record it, with the
        observed latency if any is logged.
- [ ] 12.6 **Stranger, with acknowledgement enabled.** From the third Signal account, DM Henk's
      number. On that phone the message may show *delivered* (the accepted transport residual)
      but must **never** show *read* or "typing", and no reply arrives. Confirm the drop:
      `$C logs henk --since 10m | grep 'dropped message from non-owner'`. Wait a minute
      before concluding, in case an auto-receipt is delayed.
- [ ] 12.7 **Overlap check (design D7, finding 12).** Ask for something whose reply spans
      several chunks and whose turn is long enough for refreshes (for example, "list every tool
      you have and what each one does, in detail"). Confirm every chunk arrives in order with no
      gap or failure banner, "typing" is visible between the question and the first chunk, and
      the log grep from 12.5 plus `grep -E 'not delivered|send failed on chunk'` both print
      nothing.
- [ ] 12.8 **Approval suspension (optional; owner's call).** This needs a temporary
      `gate.demote_standing: true` in rp5's `config.yaml`, made and restored with the
      backup-first recipe, then `$C up -d henk`. Ask Henk to remember something: the approval
      prompt arrives, and within a second or two "typing" is **not** shown while it waits. A
      brief flash next to the prompt is the accepted residual in design D5. Answer `yes`;
      "typing" resumes within about a second, until the confirmation lands. Restore the file, `cmp` it
      against the backup, and `$C up -d henk`. If skipped, record that suspension is covered by
      6.1's integration test only.
- [ ] 12.9 **Shutdown.** `$C restart henk` while a long turn is running. The container stops
      within its grace period (no `Exited 137`), and any lingering "typing" on the phone clears
      on its own within about 15 s.
- [ ] 12.10 Record the results as an *As-built* section in this file, then `/opsx:archive`
      (`openspec archive owner-acknowledgement --yes`). No post-archive edit to
      `openspec/specs/` is needed: the Purpose stays as it is (design D9).
- [ ] 12.11 **Rollback, only if it is ever needed.** Add `acknowledge_owner: false` under
      `signal:` with the backup-first recipe. Then re-run 12.1's loader one-liner and **expect
      `False`** before `$C up -d henk`: a misspelt key is silently ignored (design D8). After
      restart, a DM shows no read ticks and no "typing".
