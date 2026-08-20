# Channel Integrity

## Why

The channel adapter cannot tell its callers whether a message arrived. `send()` returns
`None`: on a permanent failure it logs, posts `"[⚠ part of this reply could not be
delivered]"`, and returns normally — so every caller observes success. That is tolerable for a
chat reply (a missing answer is visible to the owner) and disqualifying for anything that must
*know* it delivered. Two further defects sit in the same module, both present today and both
independent of any future feature:

- **Characters counted where bytes are what travel.** `split_message` measures `len(str)`
  against a 2,000 limit, so a chunk of accented or emoji text can encode to several times the
  size the code believes it is enforcing. (The 2,000 is a deliberately conservative chosen
  value, not a measured Signal maximum — bytes are the right unit because they bound both the
  wire size and any client-side character limit.)
- **An implicit send timeout, and it is per-phase.** The bridge builds `httpx.AsyncClient()`
  with no timeout, taking httpx's default of 5s. That default applies to *each* transport
  phase — connect, write, read — so a single POST can consume roughly three times it, and
  signal-cli send latency can exceed that under load. A message the bridge accepted and sent is
  then reported as failed and retried: duplicate delivery, from a value nobody chose and a
  ceiling nobody computed.

## What Changes

- **`send()` reports its outcome.** The contract returns `delivered` (every chunk
  acknowledged), `partial` (some delivered, the rest abandoned) or `failed` (nothing delivered
  — meaning *delivery was not confirmed*, never that nothing arrived). A partial is never
  reported as success. Existing callers keep working unchanged; the reply path logs a
  distinguishable error on a non-`delivered` outcome, so the new signal is exercised in
  production from day one rather than only by tests.
- **Replies and proactive sends become separate operations.** `send()` is the reply path;
  `send_proactive()` is the agent-initiated path. This closes a standing divergence: the
  `agent-core` spec already requires event-turn output to be routed through a proactive send,
  while the code uses the reply path. A proactive send accepts an **optional caller-supplied
  failure notice**, because the adapter's standing banner says "part of this reply" for
  something that was never a reply — and the caller is the only layer that knows what was
  being sent. The one proactive path that chunks (triage output) supplies its own notice, so
  nothing owner-visible is lost.
- **Splitting measures UTF-8 bytes**, never splitting a code point, so the safe-length limit
  means what it claims for non-ASCII text.
- **The bridge timeouts are explicit and configurable**, replacing httpx's implicit per-phase
  default with a chosen *total* request budget, and passing the receive path's connection
  timeout from configuration rather than leaving it at a constructor default the wiring never
  supplies.

## Capabilities

### New Capabilities

None. This change amends existing capabilities only.

### Modified Capabilities

- `channel-adapter`: `send()` gains a delivery outcome and partial is never success; a separate
  `send_proactive()` carries an optional caller-supplied failure notice; splitting measures
  bytes with a validated minimum limit; and the Signal transport gains an explicit total send
  timeout plus a configured receive connection timeout.

## Impact

- **Code:** `henk/channel/base.py` (the `SendOutcome` type, `send`'s and `send_proactive`'s
  contract on the `ChannelAdapter` Protocol, byte-measured `split_message` with a progress
  guard), `henk/channel/signal.py` (outcome plumbing, `send_proactive`, the shared serialized
  send path, explicit client timeouts), `henk/agent/core.py` (`_Sender` gains
  `send_proactive`; the reply path logs a non-`delivered` outcome; the triage and
  degraded-durability sends repoint to `send_proactive`, triage supplying its own notice),
  `henk/runtime.py` (the since-rejected notice repoints to `send_proactive`; the adapter and
  bridge take their timeouts from config), `henk/config.py` + `config.yaml`
  (`signal.send_timeout_seconds`, `signal.open_timeout_seconds`, and the `safe_length` floor).
  `SendOutcome` is imported from `henk.channel.base`; `henk/channel/__init__.py`'s curated
  `__all__` is unchanged.
- **Known test changes**, enumerated rather than promised. `tests/conftest.py`'s `FakeChannel`
  gains `send_proactive` and both send methods return `delivered` — without which the reply
  path's new outcome check fires on every existing test reply. `FakeChannel.sent` stays the
  ordered text-only list appended by both methods, so all 62 `.sent` assertions across the
  suite pass untouched; a new `calls` list records `(kind, text, failure_notice)` so the
  reply-vs-proactive distinction is assertable. `tests/test_runtime.py`'s since-rejected test
  monkeypatches `send` on the adapter instance, so it must be repointed to `send_proactive` or
  it silently stops covering the wiring it exists to guard.
  `tests/test_channel_adapter.py` gains `send_proactive` coverage on its recipient-inspection
  and split-measure assertions.
- **Dependencies:** none added, none removed. No new bridge endpoint.
- **Deployment:** no new volume, published port, listening socket, ACL grant or secret, and no
  config flag — the delivery-outcome, splitting and timeout fixes are strict corrections with
  no plausible reason to disable. The new config keys must be confirmed against rp5's live
  `config.yaml`, which is locally modified and will not carry them: their effective values come
  from the loader's defaults, not from this repo's `config.yaml`.
- **Blocked work unblocked:** anything that must know whether a message landed. The reminders
  capability depends on both the delivery outcome and `send_proactive`, and is specified
  separately.
- **Deferred to `owner-acknowledgement`:** read receipts, the working indicator, the opaque
  channel reference, the agent-core turn-bracketing amendment, and their config flag and deploy
  gate. That half carries the only new endpoints, the only encapsulation exception and the only
  rollback flag, and it is not on the critical path for reminders.
- **Deferred to `reminder-delivery`:** serializing outbound sends behind a mutex. See the design
  Non-Goals — the only cross-task sender today emits a single-chunk message, which cannot be
  interleaved, so serialization buys a cosmetic fix at the cost of real head-of-line blocking.
  It lands where the second cross-task sender and the latency measurement both exist.
