# Owner Acknowledgement

## Why

The owner has no way to tell that Henk received their message and is working. A reply can take
tens of seconds while tools run, and the conversation looks dead. Signal has exactly the right
primitives for this, and the channel-adapter spec already mentions both of them — in the
negative, as things a *stranger* must never receive. The positive case for the owner was never
specified.

There is a second, unobvious benefit. The adapter carries a standing `DEPLOY-VERIFY` note
(`henk/channel/signal.py:146-151`) about which identity Signal reports for the owner — UUID vs
E.164 — because a mismatch against `owner.id` makes the allowlist silently drop every owner
message. A read receipt is a live signal that the allowlist matched: its absence means a silent
drop. This makes that hazard diagnosable for the first time.

## What Changes

- **Henk acknowledges the owner** (config-gated): a **read receipt** when an owner message is
  accepted, and a **working indicator** for the duration of an owner turn, stopped on every exit
  path including errors. Both are owner-only by construction and are sent only *after* the
  allowlist passes — an unknown sender must never learn that anything is listening, which the
  existing spec already requires. Neither is a message: they carry no content and raise no
  notification, so the attention contract is untouched. Both are best-effort and bounded: a
  failure is logged and never fails the turn.
- **Inbound messages carry an opaque channel reference** so the adapter can acknowledge a message
  it previously emitted without exposing its wire format. This is a deliberate, argued exception
  to "no Signal-specific identifiers outside the Signal adapter", and it ships with the test that
  guards it.
- **Owner agent turns are bracketed by the working indicator** in the agent core, with the
  try/finally discipline the existing gate-framing context manager establishes.

## Capabilities

### New Capabilities

None. This change amends existing capabilities only.

### Modified Capabilities

- `channel-adapter`: the contract gains `acknowledge` and the working-indicator operations; a new
  requirement specifies owner-only acknowledgement of inbound messages; the Signal transport
  gains the receipt and typing endpoints and the opaque channel reference; and the **`Owner-only
  allowlist`** requirement plus the spec `## Purpose` are amended, because both currently state
  absolutely that a non-owner receives no read receipt.
- `agent-core`: an owner turn is bracketed by the working indicator, started before the turn and
  stopped on every exit path. Command turns and event turns are excluded.

## Impact

- **Depends on `channel-integrity`**, which must land first. That change narrows the `Swappable
  channel-adapter contract` requirement to remove the acknowledgement operations and the channel
  reference; this change MODIFIES the narrowed version to re-add them. Its `send_timeout_seconds`
  is not a dependency — the acknowledge timeout is its own key.
- **Code:** `henk/channel/base.py` (`channel_ref` on `InboundMessage`, the `acknowledge` and
  working-indicator contract), `henk/channel/signal.py` (receipt and typing calls),
  `henk/app.py` (the dispatcher gains the adapter as a dependency so it can acknowledge after the
  allowlist — a constructor change, with `henk/runtime.py` and `tests/test_app.py` following),
  `henk/agent/core.py` (bracket the owner turn), `henk/config.py` + `config.yaml`
  (acknowledge timeout, acknowledgement flag).
- **Dependencies:** none added. Both bridge endpoints are already provided by the deployed
  `bbernhard/signal-cli-rest-api` image and verified against its route table:
  `POST /v1/receipts/{number}` (`recipient`, `receipt_type`, `timestamp` as int64) and
  `PUT`/`DELETE /v1/typing-indicator/{number}` (`recipient`). Verified against current master,
  not against the `:latest` digest rp5 actually pulled — hence the deploy check.
- **Deployment:** no new volume, published port, listening socket, ACL grant or secret. Rollback
  is a config flag.

## Carried-over findings to fold in when this change is written

Recorded here so they are not rediscovered. Each was verified against source.

1. **`SHALL NOT block, delay or fail the turn` is unimplementable as written.** `App.run` awaits
   `on_inbound` inside the receive loop, one message at a time, so a hung acknowledge stalls every
   message behind it. Replace with: *best-effort **and bounded** — its own configured timeout,
   distinct from the send timeout; a failure or timeout is logged, does not fail the turn, does not
   propagate to the caller, and delays message handling by no more than that timeout.* Fixing this
   in the design and task list while leaving the spec sentence intact is the specific trap to
   avoid: the spec is the binding record.
2. **The flag's effective default is the inline literal in `from_dict`**, not the dataclass
   attribute and not this repo's `config.yaml`. `config.py:386` reads
   `int(signal_sec.get("safe_length", 2000))`; contrast `config.py:300`, which does use the
   dataclass attribute. rp5's `config.yaml` is locally modified and will not carry a new key. Pin
   `False` in **both** places and test that a config omitting every new key yields `False`, or the
   staged rollout deploys with acknowledgement already on.
3. **`Owner-only allowlist` and the `## Purpose` both state absolutely that a stranger gets no
   read receipt.** Amend both to scope the claim to acknowledgements Henk *originates*. The
   transport-level delivery receipts signal-cli emits in json-rpc mode are a pre-existing accepted
   residual — a registered Signal number reveals a recipient regardless of Henk. Note the receipt
   *types* differ: the residual concerns **delivery** receipts, the baseline enumerates **read**
   receipts, so the contradiction is conditional on the daemon check below.
4. **Confirm the signal-cli daemon is not configured to auto-send read receipts.** That would
   acknowledge strangers underneath the application, where no application-level test can observe
   it. `send_read_receipts` is a receive-endpoint query parameter the websocket path does not use —
   verify on the instance.
5. **A single typing `PUT` does not cover a turn.** Signal clients expire a typing indicator
   ~15s after the last TYPING START. One `PUT` would show typing for the first seconds of exactly
   the long tool-using turn the feature exists for, then look dead. Refresh on a cadence below the
   expiry (~8s), cancel the refresh in the same `finally` that sends the stop, and log refresh
   failures **once** per turn rather than per tick.
6. **`_framed_turn` is a *synchronous* `@contextmanager`.** The indicator needs `await` on both
   edges plus a refresh task, so it must be an `@asynccontextmanager`. Cite `_framed_turn` for its
   try/finally *discipline*, not as a shape to copy.
7. **The indicator would refresh through the approval gate's owner-wait.**
   `gate/approval.py:297-308` sends the prompt then awaits up to `approval_timeout_seconds`
   (300s) **inside** `session.run_turn` — inside the bracketed turn. The refresh would assert
   "Henk is typing" ~37 times while Henk is in fact blocked *on the owner*, who is the one being
   asked to act. Suspend the indicator while an approval is pending.
8. **The typing-stop lands on the shutdown critical path.** `__main__.serve` →
   `run_task.cancel()` → `App.run`'s `finally` → core worker cancelled → the turn's `finally`
   performs a network DELETE **inside a cancelled task**, where a fresh `await` is not reliably
   completable — inside the `docker stop` grace window `test_graceful_shutdown.py` protects. Bound
   the stop by the acknowledge timeout; cancel **and await** the refresh task with
   `CancelledError` suppressed; on cancellation rely on Signal's ~15s expiry rather than shielding
   a network call on the shutdown path.
9. **`channel_ref` will be `"0"` for timestamp-less envelopes.** `signal.py:153` reads
   `raw_ts = data.get("timestamp") or env.get("timestamp") or 0`, and `POST /v1/receipts` requires
   a valid timestamp — a guaranteed 400 that best-effort logging turns into per-message noise.
   Spec `acknowledge` as a no-op when the reference is absent.
10. **What keeps `channel_ref` out of the audit log is structural, and worth recording as such.**
    `Dispatcher.on_inbound` passes `message.text` to `core.submit(...)`; the `InboundMessage`
    object never crosses into the session or audit layer. A future change that queues
    `InboundMessage` instead of `str` breaks the prohibition without touching a line of
    `channel_ref` code. The repo has already shipped one leak of this shape — an audit `result_id`
    field carrying tool result text — so the no-log/no-audit prohibition ships with its test, not
    as an assumption.
11. **The receipt must take its recipient from the configured owner identity, never from
    `message.sender`.** That converts an ordering property into a structural one and is the
    strongest single decision in the original design. Keep it exactly.
12. **Deploy checks that are not covered by per-call verification.** Confirm the endpoints accept
    whatever form `owner.id` takes (UUID vs E.164) — the adapter already carries a DEPLOY-VERIFY
    note about this for the inbound identity and these endpoints inherit it; a 400 here degrades
    silently into a log line nobody reads. Confirm `sendReceipt` succeeds *at all* against a linked
    device with the owner as recipient and the owner's own message as target. And **overlap** a
    multi-chunk send with a typing refresh: this change creates the first concurrent multi-task use
    of one signal-cli-rest-api instance (receive websocket, send POST, ~8s typing PUT, receipt
    POST, all multiplexed over one daemon socket), and per-call verification does not test the only
    mode production runs in.
13. **`read`, not `viewed`.** `read` is what a chat client sends on seeing a message; `viewed` has
    media semantics Henk has no use for. Settled — record it as a decision, not an open question.

## Open Questions

- **Whether the working indicator should also cover long-running owner commands.** None currently
  take meaningfully long; if one does, the command-turn exclusion is the line to revisit.
