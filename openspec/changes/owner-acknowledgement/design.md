# Design — Owner Acknowledgement

## Context

The owner messages Henk and sees nothing until the reply lands, which for a tool-using turn is
tens of seconds. Signal has two content-free primitives for exactly this, a read receipt and a
typing indicator, and the channel-adapter spec names both only as things a stranger must never
get. This change specifies the owner's side of them.

Current state, read in source on 2026-09-26 rather than assumed:

- `Dispatcher.on_inbound` (`henk/app.py:37-49`) runs the allowlist, then either classifies the
  text against a pending approval or calls `core.submit(message.text)`. It is awaited **inside**
  `App.run`'s receive loop (`app.py:90-91`), one message at a time, so anything slow in it
  stalls every message behind it (proposal finding 1).
- `InboundMessage` (`henk/channel/base.py:53-60`) carries `sender`, `text`, `timestamp`,
  `is_group`. `SignalAdapter._convert` (`henk/channel/signal.py:207-231`) builds it and reads
  `raw_ts = data.get("timestamp") or env.get("timestamp") or 0` (`:224`), so a timestamp-less
  envelope yields `0` (finding 9). The standing `DEPLOY-VERIFY` note about UUID vs E.164
  owner identity is at `:217-222`.
- `SignalAdapter` holds one `asyncio.Lock` (`signal.py:90`) around `_send_serialized`, and
  `tests/test_channel_adapter.py:842-883` (`test_the_lock_is_the_whole_mechanism_and_nothing_more`)
  AST-scans the **whole module** for exactly one `Lock` call and **zero** attribute calls named
  `wait_for` or `timeout`, and greps its source for `priority`, `max_chunks`, `chunk_cap`,
  `hold_timeout`. `test_no_bridge_code_path_constructs_a_client_without_a_timeout` (`:616-634`)
  requires exactly one `AsyncClient(...)` construction, in `_build_client` (`signal.py:282`).
- `SignalCliRestBridge._build_client` gives every httpx phase the configured `send_timeout` in
  full. Those are per-phase, per-socket-operation bounds: nothing bounds a whole request
  (the post-archive correction recorded in `archive/2026-08-20-channel-integrity/tasks.md`).
- `AgentCore._process_owner` (`henk/agent/core.py:356-393`) handles `/new`, then app-side
  commands, then `_ensure_session`, composes the content (time header ⟵ recall ⟵ deliveries),
  runs the turn inside `with self._framed_turn(TurnType.OWNER)`, and sends the reply or the
  error reply. `_framed_turn` (`core.py:653-676`) is a **synchronous** `@contextmanager`
  (finding 6).
- `ApprovalGate._prompt_and_wait` (`henk/gate/approval.py:288-308`) sets `self._pending`
  (`:294`) **before** sending the prompt (`:297`), then awaits the owner for up to
  `approval_timeout_seconds` (`:298`, 300 s) **inside** `session.run_turn`, which is inside the
  bracketed turn (finding 7). `has_pending()` (`:187-188`) is public.
- `__main__.serve` (`henk/__main__.py:42-70`) cancels `App.run` on SIGTERM; `App.run`'s
  `finally` cancels the core worker; an owner turn in flight unwinds under cancellation inside
  `docker stop`'s grace window (finding 8).
- `OwnerTurn` (`henk/agent/turns.py:20-24`) has one field, `text`. The `InboundMessage` object
  never crosses into the core, session or audit layer today; that is structural, not enforced
  (finding 10).
- Owner identity on rp5 is a Signal **UUID** (the deployed `owner.id`). Henk runs on a
  **dedicated registered number**, not a linked device (README "Signal registration"), which
  corrects finding 12's "linked device" wording.

## Goals / Non-Goals

**Goals:**

- The owner sees that a message was received, and sees that Henk is working for as long as a
  turn actually runs, including long tool-using turns.
- A stranger gets nothing more than before: no receipt, no indicator, no reply.
- A broken or hung bridge endpoint can never fail a turn, and delays message handling by a
  known, configured, small amount at most.
- A missing read receipt becomes a usable diagnostic for the silent-allowlist-drop hazard.

**Non-Goals:**

- **Reading the owner's receipts for Henk's messages.** Channel-integrity D6 deferred acting on
  a non-delivered approval prompt until a read receipt could corroborate delivery. That needs
  *inbound* receipt parsing (`receiptMessage` envelopes, which `_convert` discards today). This
  change *sends* acknowledgements; it does not consume them. D6 stays deferred.
- An indicator for owner commands or event turns (agent-core delta; the proposal's open
  question stays open).
- Any change to the allowlist's matching, the DM-only rule or owner identity resolution.
- Retrying an acknowledgement. One attempt, bounded; a lost receipt is not worth a second.

## Decisions

### D1 — The receipt is sent dispatcher-side, after the allowlist and after routing

`Dispatcher.on_inbound` sends the receipt, and only on the path where `allowlist.allows()`
returned true. Sequence: allowlist → route (gate classification or `core.submit`) → receipt.
Every allowlisted message is acknowledged, including an approval keyword routed to the gate and
an unrelated message that fails a pending approval closed: each was received and accepted.

*Why the dispatcher, not the adapter.* The allowlist lives in the dispatcher (`app.py:38`),
above the adapter. An adapter that acknowledged on receipt would acknowledge **before** the
allowlist had run, which is precisely the stranger leak the spec forbids. The only place where
"this message passed the allowlist" is a fact is the line after the check.

*Why after routing, not before.* The receipt is awaited inside the receive loop. Sent before
`core.submit`, a hung receipt delays the message it acknowledges; sent after, the message is
already queued and the core worker can start the turn while the receipt is in flight. Only the
*next* inbound message can wait, and by at most the bound (D6).

*Alternatives considered.* A fire-and-forget task per receipt: no delay at all, but untracked
tasks that outlive shutdown and swallow their exceptions, for a saving of at most one bound on
a degraded bridge. Rejected. Acknowledging in the core when the turn starts: a queued message
would look unread until the turn ahead of it finishes, which misreports what "read" means here
(Henk has it), and approval replies never become turns at all.

**The receipt's recipient is the configured owner identity, never `message.sender`**
(finding 11). This is made structural rather than conventional: `acknowledge` accepts only the
channel reference (D2), so the adapter has no sender to use and addresses the receipt to the
`owner` it was constructed with, the same identity the allowlist matched. A future edit that
moved the receipt above the allowlist would still address only the owner, never a stranger.
That converts an ordering property into a structural one, and the ordering is tested too.

**`read`, not `viewed`** (finding 13). `read` is what a chat client sends on seeing a message;
`viewed` has media semantics Henk has no use for. Settled.

### D2 — An opaque channel reference, and its one argued encapsulation exception

A Signal read receipt names the message by its sender timestamp. The neutral layer has to carry
*something* from the inbound message to `acknowledge` so the adapter can name the message it
emitted. `InboundMessage` gains `channel_ref: str | None = None`: minted by the adapter, carried
through, handed back only to `acknowledge(channel_ref)`.

At runtime this is a Signal identifier outside the Signal adapter, which runs against *"No
Signal-specific types, identifiers, or API details SHALL appear outside the Signal adapter
implementation."* The exception is argued, not slipped in:

- **It is a value, not a reference in code.** Neutral code receives a `str` and hands it back.
  No neutral module knows it is a timestamp. The code-level scan (`Signal specifics stay
  encapsulated`) is unaffected and is extended (D10). The spec does not add a "never parsed or
  compared" rule, since nothing could enforce it beyond what the scan already does.
- **Every alternative is worse.** An adapter-side cache from message to timestamp needs a key
  the neutral layer would have to carry anyway, plus eviction and a memory bound. Acknowledging
  inside the adapter on receipt runs before the allowlist (D1). Handing the adapter the whole
  `InboundMessage` hands it `sender`, reopening finding 11.
- **A second adapter needs nothing.** A channel without per-message receipts leaves it `None`.

**The exception is bounded by two rules, each with a test**: the reference never enters an
agent turn, and it is never persisted or written to an audit record. The reason is not secrecy.
The same value already crosses the boundary as `InboundMessage.timestamp`, and it is not
sensitive. The reason is scope: channel specifics stay out of what the model reads and out of
the durable record, so a second channel never has to reason about the first one's
identifiers in turns or audit history. Logging is deliberately not restricted. A log line may
name a timestamp as it may today, and a repr-hiding field would have protected nothing.

**What keeps it out of the core is structural, and is pinned as such** (finding 10). The core
receives `message.text`, a `str`; the `InboundMessage` never crosses. A future change that
queued `InboundMessage` instead of `str` would break the rule without touching a line of
`channel_ref` code, and the repo has shipped a leak of this exact shape before (an audit
`result_id` field carrying tool result text). So two guard tests ship with the field:

1. `OwnerTurn`'s dataclass fields are exactly `("text",)` and `AgentCore.submit`'s parameters
   are exactly `["text"]`.
2. A sentinel scan: a full owner turn through `App.run` with a real `SignalAdapter` over a fake
   bridge, whose envelope timestamp is a distinctive sentinel value. The sentinel reaches the
   bridge's receipt call. It appears in no audit or store file, no session turn content and no
   outbound message. The harness is specified in task 8.2.

**A timestamp-less envelope mints no reference** (finding 9). `_convert` sets
`channel_ref=None` when `raw_ts` is falsy, rather than `"0"`, which `POST /v1/receipts` would
reject with a guaranteed 400 on every such message. `acknowledge(None)` is a no-op with no
bridge request, and the dispatcher skips the call entirely, so no failure is logged. A malformed
reference (not an integer) is refused by the adapter without a bridge request and reported as a
failure, since that one is a bug.

### D3 — The working indicator is an `@asynccontextmanager` bracket around the whole owner wait

The indicator needs `await` on both edges and a task between them, so it is an
`@asynccontextmanager`, not a copy of `_framed_turn` (finding 6). It borrows `_framed_turn`'s
try/finally *discipline*: every exit path goes through one `finally`.

**Placement in `_process_owner`, decided:**

```
/new                      → not bracketed (command)
app-side command          → not bracketed (command)
async with working():     ← enters here
    _ensure_session(...)
    compose: time header ⟵ recall ⟵ deliveries ⟵ text
    try:
        with _framed_turn(OWNER):
            reply = await run_turn(content)
    except Exception:
        await _reply(error_reply); return      ← still inside
    if reply: await _reply(reply)             ← still inside
                          ← exits here
```

*Why it opens before `_ensure_session`.* Idle expiry closes the previous session there (an audit
flush and an SDK client close) and constructs the next. That is owner-visible wait, and an
exception raised there today propagates out of `_process_owner` to `AgentCore.run`'s logger.
Inside the bracket, that path also passes through its `finally`, so no exit path escapes it.

*Why it closes after the reply or error send, not before.* The owner's wait ends when the reply
lands, not when the model finishes. Signal clients typically clear a sender's typing indicator
when a message from that sender arrives. So stopping *after* the send is mostly invisible (the
first chunk has already cleared it), and it costs one request. Stopping *before* the send
removes the indicator for the whole send: about 1.1 s per chunk healthy, and up to
`max_send_attempts × send_timeout` per chunk degraded, which is when the owner most needs to
see that something is happening. Between chunks of a long reply, a refresh may briefly
re-assert typing. That is accurate, since more is coming. Closing after the send also puts the
three outcomes (reply, error reply, empty reply) on one exit edge.

*Relative to `_framed_turn`.* The gate framing is inner and synchronous; it exits before the
reply is sent, as today. The bracket is outer. Nothing about gate framing changes.

*Alternatives considered.* Bracketing only `run_turn`: misses session setup and the send, and
splits the stop across the three outcome branches. Bracketing all of `_process_owner` including
commands: contradicts the agent-core delta; a command replies within one round trip.

**Start without waiting.** Entering the bracket creates the indicator task (D4) and yields once
with `await asyncio.sleep(0)`, so the task issues its first start before the session receives
the turn's content. The turn never waits on that start's response. The yield sits **inside**
the bracket's `try`, so a cancellation delivered during entry still runs the `finally` and
cannot orphan the task.

**Wiring.** `AgentCore` gains `working_indicator: Callable[[], AbstractAsyncContextManager[None]]
| None = None`, and the runtime passes the bound `working` method of the acknowledgement object
(D6). `None` (every existing unit test, and `acknowledge_owner: false`) runs `_process_owner`
exactly as today. `AgentCore._Sender` (`core.py:120-126`) is **deliberately not widened**: the
core never calls an acknowledgement operation itself. Widening it would oblige every core test
double to implement operations the core does not use.

### D4 — One task owns every typing request; the close is one bounded await

Signal clients expire a typing indicator about 15 s after the last start. One start would show
typing for the first seconds of exactly the long turn this feature exists for (finding 5). The
indicator re-asserts the start every **7 s**, measured from each start's **issue** time,
whether that start succeeded or not. With a 7 s interval, one lost refresh leaves at most a
14 s gap between the last successful start and the next one issued, still under the 15 s
expiry. At 8 s the same loss would leave 16 s, and the indicator would drop. The margin is
about a second less the request's latency. It is tight, but it holds, because of the
scheduling rule below. The cadence is a property of Signal's clients, so it is a
constant in `henk/channel/signal.py` (`TYPING_REFRESH_SECONDS = 7.0`) that the runtime passes
in. It is not config: a knob has to earn its place with a scenario, and this one has none.

**One task issues every typing request for the turn**: the first start, each refresh, the
pause stop and resume start (D5), and the final stop. Its loop:

```
start, and set next_due = issue time + interval
until exit_event is set:
    wait for exit_event OR sleep(max(0, min(PAUSE_POLL_SECONDS, next_due − clock()))),
        whichever first (asyncio.wait FIRST_COMPLETED over two tasks; the loser is
        cancelled and awaited)
    on a pause transition: stop / start (the resume start sets next_due as above)
    else, if clock() ≥ next_due: start, and set next_due = issue time + interval
stop                    ← only on this normal exit path
```

**Why the wait is not a fixed poll.** The next wait starts only after the previous request
returns. With a fixed 1 s poll, a refresh issued at t = 7 that hangs until its bound expires at
t = 14 would be followed by one more poll, and the next start would go out at about t = 15,
at the expiry. Waiting at most `next_due − clock()`, clamped at 0, means an overdue refresh
fires as soon as the hung one times out: at t = 14, 2 × 7 s after the last successful start.

Every request inside the loop is bounded by the acknowledge timeout (D6). Because one task
issues the requests **sequentially**, a start can never land after the stop: the stop is only
issued once the previous request's response has been received. This holds by construction,
with no drain step to get right. A 2xx from signal-cli-rest-api in json-rpc mode means
signal-cli has handled the request, so the order holds at the bridge too.

**Closing sets `exit_event` and awaits the task inside one `asyncio.timeout(T)`.** The loop
wakes on the event immediately. It is never sleeping out a poll, so on a healthy bridge the close
costs one stop round trip and nothing else. If a request is in flight, the close waits for it
and then for the stop, all within the same single bound T.

**When the bound is exhausted before the stop is sent,** because an in-flight refresh or the
stop itself hangs, the bracket cancels the task and awaits it. No stop is sent, or the one in
flight is abandoned, and the indicator is left to Signal's ~15 s client expiry. One line is
logged. The turn's exit is delayed by at most T. The task is always finished before the
`finally` completes, so it never outlives the turn.

**Awaiting a cancelled task never swallows an outer cancellation.** The bracket awaits the task
with `await asyncio.wait({task})`, which returns when the task is done and never raises the
child's `CancelledError`. The obvious alternative, `try: await task / except CancelledError:
pass`, cannot tell the child's cancellation from a cancellation of the *bracket itself*. At
shutdown it would swallow the latter: the core worker would continue to `queue.get()`, and
`App.run`'s `finally` would hang waiting for it.

**On cancellation, no stop** (finding 8). When the bracket exits with `CancelledError` (shutdown
via `serve` → `App.run` → core worker), the `finally` cancels the task without setting
`exit_event`, awaits it with `asyncio.wait({task})`, sends nothing, and re-raises. A fresh
network `await` inside a cancelled task is not reliably completable, and it would sit on the
`docker stop` critical path that `test_graceful_shutdown.py` protects. Shielding it would trade
a cosmetic 15 s indicator for shutdown latency. Signal's client-side expiry clears the
indicator. That is the designed behaviour on this path, and the agent-core delta states it
rather than promising a stop it will not send.

**Failures are logged once per turn** (finding 5). The loop keeps going after a failure, since a
transient fault recovers, but it logs only the first failure of a start or refresh in a turn.
The close gets at most one line of its own. So one turn on a dead endpoint produces at most two
lines, never one per tick.

### D5 — The indicator is suspended while an approval is pending

Inside a bracketed turn the gate can send an approval prompt and then wait up to 300 s on the
**owner** (finding 7). Refreshing through that wait would assert "Henk is typing" about 40 times
while Henk is blocked on the one person being asked to act. That inverts the indicator's
meaning. Three options:

| Option | During the wait | Verdict |
|---|---|---|
| Keep refreshing | "typing" next to a question addressed to the owner | Wrong signal; rejected |
| Stop for the rest of the turn | Dead after the owner approves, while the approved tool runs | Loses the indicator for exactly the part of the turn that follows the approval |
| **Suspend, resume** | Nothing shown while the owner decides; typing resumes when the gate resolves | **Chosen** |

**Mechanism: a pause predicate, polled by the same task.** The acknowledgement object is
constructed with `paused=gate.has_pending`. The D4 loop checks it at every wake-up
(`PAUSE_POLL_SECONDS = 1.0`). On the transition to paused it sends one stop and no refreshes
while paused. On the transition back it sends a start at once rather than waiting out the
interval.

**What this does not guarantee, stated.** The gate sets `_pending` before it sends the prompt
(`approval.py:294` vs `:297`), so no *new* refresh starts once the predicate is true. A refresh
already in flight when `_pending` is set is not ordered against the prompt: the prompt goes
through the send lock, and the refresh does not (D7). So "typing" can briefly be shown next to
the prompt, or re-appear after it, for up to about one poll plus T, until the pause stop goes
out. That is acceptable. It is brief and cosmetic, and ordering it would mean putting typing
through the send lock, which D7 rejects.

*Why polling and not an event from the gate.* An event needs a new gate hook, gate code that
knows about channel indicators, and a change to approval-gate code, which the contract's
"adding a second channel" scenario says is never needed. `has_pending()` already exists and is
public. The poll costs one wake-up per second during a turn and adds no latency to the close,
because the loop also wakes on `exit_event` (D4). The predicate is only ever true during this
turn's own wait, because the core queue is serial.

### D6 — Best-effort and bounded, with the bound outside `signal.py`, enforced by cancellation

Finding 1 is binding: *"SHALL NOT block, delay or fail the turn"* is unimplementable, because
the receipt is awaited in the receive loop. The spec states instead that each acknowledgement
operation is **best-effort and bounded**: its own configured timeout, distinct from the send
timeout. A failure or timeout is logged, never fails the turn, never propagates, and delays
message handling by no more than that timeout.

**The bound is enforced by cancellation**, `asyncio.timeout(acknowledge_timeout)` around the
whole operation, because nothing else bounds a whole request. httpx phase timeouts bound each
socket operation, and a response trickling in many small reads is never cut off
(channel-integrity post-archive correction 1). The bounded operations are:

- a receipt;
- each start, refresh and pause stop inside the D4 task;
- the close as a whole (set the event, await the task, including its final stop).

**Why cancelling these requests is safe when cancelling a send is not.** Channel-integrity ruled
cancellation out for sends because a cancelled POST may still have been delivered: it
*manufactures* the "may already have been delivered" ambiguity `SendOutcome` exists to describe,
and a caller that retries duplicates a message. Here the same ambiguity is harmless:

- a read receipt is idempotent. A receipt that was processed despite the cancel marks read a
  message that was in fact read, and a duplicate is deduplicated by the client;
- a typing start or stop sets state rather than adding content. Processed or not, the worst
  case is an indicator that expires on its own within about 15 s;
- nothing retries on a cancelled acknowledgement, so nothing can be duplicated.

**The bound lives outside `henk/channel/signal.py`.** `test_the_lock_is_the_whole_mechanism_and_nothing_more`
forbids any `wait_for` or `timeout` attribute call anywhere in that module, deliberately, so a
hold timer on the send lock has to argue with the decision. Relaxing the test to admit an
acknowledgement timeout would weaken a guard that protects something else. So the adapter's
acknowledgement operations are unbounded single attempts, and the bound lives in a new
channel-neutral module, **`henk/channel/acknowledge.py`**, holding `OwnerAcknowledgement`:

- constructed with the adapter, `timeout`, `refresh_seconds` and `paused`, plus an injectable
  `sleep` **and `clock`**. An injected sleep needs an injected clock, or "the interval has
  elapsed" cannot be tested;
- `async receipt(channel_ref) -> None`, the bounded receipt the dispatcher calls;
- `@asynccontextmanager working()`, the D3/D4/D5 bracket the core enters.

Both catch `Exception` (which includes `TimeoutError`), log and return. **They do not catch
`BaseException`**: an outer cancellation (shutdown) must propagate out of the receive loop and
the core worker exactly as it does today. The module holds no wire tokens and is covered by the
encapsulation scan.

**The adapter operations never raise on transport errors**, consistent with the rest of the
contract. `acknowledge`, `start_working` and `stop_working` return `bool` (accepted by the
bridge or not) and do not log. Only the caller knows the once-per-turn scope, so the caller
owns the log line.

**What the bound costs, stated.** On a healthy bridge nothing waits beyond one round trip. The
receipt follows routing, so the message it acknowledges is never held. Entering the bracket does
not await the start. Closing it costs the stop's round trip, and never a poll interval. On a
hung endpoint, the next inbound message waits at most T behind a receipt, and the next queued
turn waits at most T behind a close. Refreshes run on the indicator task and delay nothing.

### D7 — Acknowledgements bypass the send lock

The adapter's `_send_lock` serializes **send sequences** so a message's chunks stay contiguous.
Acknowledgements carry no content, and nothing orders them against chunks in any way the owner
can see (D5's in-flight refresh next to a prompt is the one cosmetic exception, accepted there).
Routing them through the lock would:

- make a receipt, which runs in the receive loop, wait behind any multi-chunk reply or reminder
  in flight. That would reliably hit the bound on a long send and turn the receive loop's
  worst case from "one timeout on a broken endpoint" into "one timeout whenever Henk is
  talking";
- starve the refresh during a long multi-chunk send, which is exactly when the indicator
  should stay up;
- put a second kind of holder on a lock whose spec and guard test describe it as the whole
  serialization mechanism for sends and nothing more.

So acknowledgement operations call the bridge directly, without the lock. No second lock is
added (the lock test still counts exactly one), and each request goes through `_build_client`
(the single-construction test still counts exactly one).

**The cost is new concurrency at the bridge, and it gets a deploy check** (finding 12). This
change creates the first overlap of a send POST with a typing PUT on one
signal-cli-rest-api instance, multiplexed over one json-rpc daemon socket alongside the receive
websocket and possibly a receipt POST. Per-call verification does not exercise the only mode
production runs in, so task 12.7 **overlaps a multi-chunk reply with typing refreshes** on the
instance and confirms that every chunk arrives in order, no acknowledgement error is logged,
and no send is slowed beyond its normal latency. If signal-cli serializes json-rpc requests
internally, the observable effect is an acknowledgement waiting behind a chunk, which the bound
already absorbs.

### D8 — Config: one flag, default on, and a separate timeout

Two keys under `signal:`:

| key | default | meaning |
|---|---|---|
| `acknowledge_owner` | **`true`** | Send the owner a read receipt and a working indicator. `false` sends neither, and handling is byte-identical to before this change |
| `acknowledge_timeout_seconds` | `5.0` | Whole-operation bound on each acknowledgement operation, enforced by cancellation (D6). Not a phase timeout, and unrelated to `send_timeout_seconds`. At most the typing refresh interval (7 s) |

**One flag, not two.** A receipt without an indicator, or the reverse, has no scenario. One
flag is one rollback.

**Default on, by owner decision 2026-09-26.** This supersedes the *value* in proposal finding 2,
which asked for `False` and a staged rollout (annotated there). The *mechanism* in finding 2
stands, and it matters more with the default on. rp5's `config.yaml` is locally modified and
will not carry either key, so the effective values there are whatever the loader produces for
an absent key. The default is pinned in **both** places: the `SignalConfig` dataclass attribute,
and the `from_dict` builder, which reads each key with an explicit fallback exactly as the
adjacent `send_timeout_seconds` and `open_timeout_seconds` do (`config.py:1014-1027`). Two
tests pin it, because each catches a different trap:

- a signal section omitting both keys loads `acknowledge_owner is True` and
  `acknowledge_timeout_seconds == 5.0`. This catches a wrong fallback;
- an explicit `acknowledge_owner: false` loads `False`. This catches a builder that never reads
  the key, which passes the first test on the dataclass default alone. That is the trap
  finding 2 names.

**Validation at load.**
- The flag must be a real boolean. A quoted `"false"`, a `1`, and a blank value (YAML `null`)
  are refused, rather than read by `bool()`.
- The timeout must be a positive number, and not a boolean: `true` passes `float()` as `1.0`.
- The timeout must not exceed `TYPING_REFRESH_SECONDS`, imported from `henk/channel/signal.py`.
  A timeout above the interval would let one hung refresh push the gap between starts past
  interval + T > 15 s. At T ≤ 7 s, with the interval measured from issue time and an overdue
  refresh fired immediately (D4), the next start after one lost refresh is issued at most
  14 s after the last successful one.

**A mistyped rollback key is silently ignored.** The `signal:` section does not reject unknown
keys; only the `_optional_section` sections do (`config.py:1441-1465`). So
`acknowledge_owenr: false` would leave acknowledgement on. Unknown-key rejection for `signal:`
is out of scope. The rollback step instead re-runs the effective-value one-liner and expects
`False` (Migration step 4, task 12.11).

**Consequence, stated.** The deploy turns acknowledgement on with no `config.yaml` edit. Rollback
therefore needs an edit to rp5's locally-modified file (`acknowledge_owner: false` under
`signal:`), done with the README's backup-first recipe. The timeout value 5.0 is chosen, not
measured. It is over four times the worst observed per-chunk send latency on rp5 (~1.1 s,
reminder-delivery), within the 7 s cap, and short enough that a dead endpoint costs a noticeable
but tolerable pause. Task 12.5 records the observed acknowledgement latency.

**Wording.** The new keys' comments say what they are: a whole-operation bound enforced by
cancellation. They must not copy `send_timeout_seconds`' comment, which still says *"TOTAL
budget for one bridge HTTP request, decomposed across httpx's four transport phases"*
(`config.py:352-356`, and `config.yaml:31-33`). That has been false since channel-integrity's
post-archive correction replaced the decomposition with the full value per phase. Those lines
are corrected in the same edit (task 1.4), since this change touches them anyway.

### D9 — The `## Purpose` stays as it is

Finding 3 asked to amend both `Owner-only allowlist` and the spec's `## Purpose`, since both say
absolutely that a stranger gets no read receipt. On review, only the requirement needs amending.

The Purpose (`openspec/specs/channel-adapter/spec.md:4-9`) says *"only the configured owner
identity is ever processed, nothing else gets a reply, a read receipt, or even a typing
indicator."* That stays true after this change: only the owner gets them. The one way it could
become false is if the signal-cli daemon auto-sent **read** receipts to strangers. The
`Owner-only allowlist` delta forbids that, and task 12.2 verifies it on the instance
(finding 4). The **delivery** receipts signal-cli emits at the transport level are a
pre-existing, accepted residual. They reveal only that a registered number exists, which is a
property of registering a Signal number at all. They are not a read receipt, and they are not
new.

So no edit to `openspec/specs/` is needed outside the delta. The standing rule, never
hand-edit `openspec/specs/`, holds without exception. (OpenSpec 1.3.1 deltas cannot carry a
Purpose anyway; the parser recognises only the four requirement sections.)

*Optional polish, not part of this change's close-out.* If the owner ever wants the Purpose to
mention acknowledgement, a candidate sentence is: *"The owner, and only the owner, is
acknowledged: a read receipt once a message has passed the allowlist and a working indicator
while a turn runs, both content-free, best-effort and bounded."*

### D10 — Guards: what each one pins

| Guard | Pins | Extends |
|---|---|---|
| `OwnerTurn` fields `== ("text",)`; `AgentCore.submit` params `== ["text"]` | the reference never enters the core (D2) | new |
| Sentinel scan through `App.run` | never audited, persisted, sent or put in a turn (D2) | new |
| Recipient inspection | `acknowledge` → `["channel_ref"]`; `start_working`/`stop_working` → `[]`; recipient denylist on all three, on the Protocol and the Signal adapter | `test_no_send_operation_exposes_an_arbitrary_recipient` |
| Wire-token scan | adds `receipt_type`, `typing-indicator`, `/v1/receipts`, and the existing routes `/v2/send`, `/v1/receive`, which the scan's comment claims but its list lacks | `test_signal_wire_format_stays_encapsulated` |
| Stranger gets nothing, **acknowledgement enabled** | no receipt, typing or reply for a stranger or a group message, through a real `SignalAdapter` over `FakeBridge` | `test_stranger_dropped_before_reaching_core` (re-run enabled) |
| Lock and client-construction tests | still exactly one `Lock`, no `wait_for`/`timeout`, one `AsyncClient` | unchanged; they must pass untouched |

`typing` alone cannot be a wire token: `from typing import …` appears throughout `henk/`.

## Risks / Trade-offs

- **UUID vs E.164, now diagnosable and also a new failure surface.** The owner's `owner.id` is a
  UUID. The receipt and typing endpoints take the recipient as a string. Whether they accept a
  UUID, where `/v2/send` already does, is verified against current master, not against the
  `:latest` digest rp5 pulled. A 400 there degrades into a log line nobody reads. → Task 12.5
  checks it live. The upside is the reason for this change: **a missing read receipt now
  diagnoses a silent allowlist drop.** If the owner's message shows delivered but never read,
  either the allowlist did not match or the receipt failed, and the log says which. The
  `DEPLOY-VERIFY` note in `_convert` is updated to name this check.
- **The diagnostic is only as good as the owner's client settings.** Signal's read receipts and
  typing indicators are reciprocal privacy settings. With either off in the owner's own client
  (Signal or Molly), the owner sees none from Henk, whatever Henk sends, and a missing receipt
  then means nothing. → Task 12.4 confirms both settings before any live check is read.
- **Bridge endpoint availability on the deployed image.** Both endpoints exist on
  `bbernhard/signal-cli-rest-api` master; rp5 runs `:latest` as pulled. → Task 12.3 records the
  image's version from `/v1/about`, and 12.5 exercises both endpoints for real. If either is
  missing, every call fails inside the bound, one line per turn, with no owner-visible effect;
  the rollback is the flag.
- **Daemon-level auto read receipts** would acknowledge strangers beneath the application, where
  no test can see it (finding 4). → Task 12.2 inspects the daemon's command line and the
  container environment, and 12.6 repeats the stranger test from a third account with
  acknowledgement enabled and watches for read ticks.
- **Typing briefly next to an approval prompt** (D5). → Cosmetic, at most about one poll plus T.
- **A lingering indicator when the close's bound is exhausted or at shutdown** (D4). → Cleared by
  the client's ~15 s expiry. Cosmetic.
- **Default on means no staged rollout.** → Owner decision. The flag is the rollback. It needs a
  backup-first edit of rp5's `config.yaml`, and a loader check afterwards because a misspelt key
  is silently ignored (D8).
- **Concurrency at the bridge** (D7). → Task 12.7's overlap check.
- **The poll adds one wake-up per second per running owner turn.** → Negligible. It stops with
  the bracket and never delays the close.

## Migration Plan

1. Land config, transport, contract, bracket, wiring and guards with the defaults, so
   acknowledgement is on.
2. Before restart on rp5, confirm the **effective** values through the loader against the live
   `config.yaml` (task 12.1). Confirm the daemon is not auto-sending read receipts (12.2) and
   record the bridge version (12.3).
3. Deploy with no `config.yaml` edit. Run the live checks in group 12.
4. **Rollback:** add `acknowledge_owner: false` under `signal:` in rp5's `config.yaml` with the
   README's backup-first recipe. Re-run task 12.1's loader one-liner and **expect `False`**,
   since a misspelt key is silently ignored (D8). Then `up -d henk`. Nothing is stored, so there
   is nothing to unwind. Rolling back the image also works.

## Open Questions

- **Whether the working indicator should also cover long-running owner commands.** Carried over
  from the proposal. None currently take meaningfully long; if one does, the command exclusion
  in `_process_owner` is the line to revisit.
