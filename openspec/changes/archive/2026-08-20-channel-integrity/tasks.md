# Tasks — Channel Integrity

TDD throughout: each group starts by writing tests derived from the delta spec scenarios
(each Given/When/Then → at least one test; each SHALL → at least one assertion), then
implements to green. Implementation happens in a fresh session via `/opsx:apply`.
**Hard stop before any deploy to rp5 — explicit owner go required.**

Two standing rules for this change:

- **Exercise the production path, not only a double.** The defect this change fixes is that
  `SignalAdapter.send` swallows a failure a cooperative double would report, so a test that only
  ever runs against a friendly fake proves nothing about the shipped code.
- **Before repointing any call site, grep for what observes it.** Not just the symbol —
  monkeypatches, test-double recording attributes (`grep -rn "\.sent\b" tests/`), `caplog`
  assertions. A test that patches the old symbol goes silently dead rather than failing, so
  nothing but the grep finds it. The enumerations in tasks 2.5 and 5.3 were produced this way and
  must be re-run when those tasks are written, since the suite moves.

## 1. Byte-measured splitting

- [x] 1.1 Tests for `split_message`: ASCII behaviour unchanged (the existing split tests must pass
      untouched — for ASCII, byte length equals character length, so they do); a chunk of
      multi-byte characters near the limit has an encoded length within the limit; no chunk splits
      a code point; concatenation reproduces the input exactly; a single token longer than the
      limit still hard-splits without corrupting a character; a limit below the longest single
      code point's encoding is refused at config load rather than looping at send time
- [x] 1.2 Implement byte measurement in `henk/channel/base.py` — boundary search stays
      character-based (paragraph → line → word), shrinking the window until
      `len(chunk.encode("utf-8"))` fits (design D4) — **together with** the progress guard
      (`ValueError` when no code point fits) and the `safe_length` floor validated in
      `henk/config.py`. Ship them in one task: the guard is what keeps the shrink loop from
      spinning on the production send path, so the measurement must not land without it

## 2. Delivery outcome and the reply/proactive split

- [x] 2.1 Tests from the contract scenarios: a healthy send reports `delivered`; a bridge that
      fails every attempt reports `failed`; one-of-three chunks failing reports `partial` and
      never success; a send whose text splits into no chunks is not `delivered`, sends nothing,
      and emits no notice; a caller that ignores the return value behaves exactly as before
- [x] 2.2 Implement `SendOutcome` in `henk/channel/base.py` and return it from `SignalAdapter.send`,
      plumbing `_send_chunk`'s existing bool rather than discarding it. Retype `send` on the
      `ChannelAdapter` Protocol from `-> None` to `-> SendOutcome` — the Protocol is what a reader
      takes as the contract, and no static checker in this repo would catch a stale annotation
- [x] 2.2a **Tests before the split is implemented.** The recipient-inspection test
      (`tests/test_channel_adapter.py:191-199`) covers **four** callables — `ChannelAdapter.send`,
      `SignalAdapter.send`, `ChannelAdapter.send_proactive`, `SignalAdapter.send_proactive` —
      asserting both the exact per-operation parameter list (`send` → `["text"]`,
      `send_proactive` → `["text", "failure_notice"]`) **and** that no parameter name matches a
      recipient denylist (`recipient`, `to`, `number`, `sender`, `phone`, `uuid`, `account`).
      Exact lists catch an added parameter of any name; the denylist catches a rename that keeps
      the arity; neither subsumes the other. Also: `test_proactive_send_reaches_owner_without_inbound`
      and `test_long_proactive_message_split_in_order` are repointed to `send_proactive` — they are
      filed under the proactive heading and currently call `send`. And assert the notice fires for
      `send` but not for a `send_proactive` whose caller supplied none
- [x] 2.3 Implement `send_proactive(text, *, failure_notice=None)` on the Protocol and the Signal
      adapter, both as thin wrappers over one private
      `_send_serialized(text, *, failure_notice)` (design D3). The shared path is not
      cosmetic: a wrapper that called the other operation would re-enter it, and would deadlock
      once `reminder-delivery` adds the mutex
- [x] 2.4 Test + implement the notice condition: it fires on **any** outcome other than
      `delivered` **where at least one chunk was attempted** — including a wholly-failed
      single-chunk reply, which is the most common failure shape and which a `partial`-only
      condition would silently drop. It is a single attempt, sent immediately after the delivered
      chunks, and never alters the reported outcome. Write the standing reply notice text and the
      triage notice text out in full here; "its triage note" is ambiguous against
      `_with_suppressed_note`, which is unrelated
- [x] 2.5 Repoint the three proactive call sites and fix what observes them. Sites: `core.py:303`
      (triage output — **supplies its own failure notice**), `core.py:325` (degraded durability,
      no notice), `runtime.py:195` (since-rejected, no notice). Log a distinguishable error at
      each on a non-`delivered` outcome. **Observers, enumerated by grep:**
      `tests/test_runtime.py::test_since_rejected_notice_reaches_the_channel` monkeypatches
      `app._adapter.send` on the instance while `runtime.py:194-195` is a closure resolving
      `channel.send` at call time — after the repoint the patch falls off the code path, the real
      bridge is hit, and the assertion fails after ~3s of real sleeps. Repoint the patch to
      `send_proactive` and assert both the text and that `failure_notice` is `None`, so the test
      keeps doing the job its own comment states. `core.py:303` and `core.py:325` are reached only
      through `process()` with `conftest.FakeChannel` (verified: no test names `_process_event` or
      `_flush_event_triage` directly, and no `caplog` assertion exists in
      `test_agent_core_events.py`, `test_incident_triage.py`, `test_agent_core_durability.py` or
      `test_runtime.py`), so task 2.6 covers them
- [x] 2.6 Widen the core's channel protocol and the shared double. `AgentCore._Sender`
      (`core.py:71-72`) gains `send_proactive`. `tests/conftest.py`'s `FakeChannel` gains
      `send_proactive`, and both send methods return `delivered` — without which the reply path's
      new outcome check fires on every existing test reply. **`FakeChannel.sent` stays the ordered,
      text-only list appended by both methods**, so all 62 `.sent` assertions across the suite pass
      untouched; add `calls: list[tuple[str, str, str | None]]` recording
      `(kind, text, failure_notice)` so the reply-vs-proactive distinction and the notice argument
      are assertable at the core level. Then test, using `calls`, that `core.py:303` passes its
      notice and the other two pass none
- [x] 2.7 Test + implement the core reply path logging a distinguishable error on a
      non-`delivered` outcome, so the new signal is exercised in production from day one

## 3. Explicit bridge timeouts

- [x] 3.1 Test: the Signal bridge's HTTP client is constructed with a timeout from configuration
      and no bridge code path constructs one without; the budget is a **total**, so a bridge that
      stalls within each per-phase limit is still cut off at the configured total; and the receive
      path's connection timeout comes from configuration
- [x] 3.2 Implement in `SignalCliRestBridge`, replacing httpx's implicit **per-phase** 5s default
      with an explicitly decomposed `httpx.Timeout` whose phases sum to the configured total —
      **naming all four phases including `pool`**, since an unnamed phase is an unbounded segment
      inside a guarantee whose whole point is totality. Do not use `asyncio.wait_for`: cancelling
      an in-flight POST manufactures the "may already have been delivered" ambiguity D1 exists to
      describe. Add `signal.send_timeout_seconds` (default **10.0**) and
      `signal.open_timeout_seconds` (default **30.0**, preserving today's constructor value) to
      `henk/config.py` + `config.yaml`, **pinning each value in both the dataclass and the
      `from_dict` literal** — the signal section reads inline literals
      (`config.py:386`), so the dataclass default alone does not determine what production gets.
      Thread all three timeouts through `runtime.py:74-80` so the bridge takes every timeout from
      config at construction. Add a test that a signal section carrying only the three existing
      keys loads with the intended effective values. Record in a comment why the implicit default
      was wrong and that its per-phase nature made the real ceiling ~3× the stated number. The
      shared tool client in `runtime.py` also lacks a timeout: out of scope for this change, noted
      rather than silently narrowing the scenario

## 4. Verification and close-out

- [x] 4.1 Read every requirement this change touches end to end, in final assembled form, against
      every other requirement it touches. Requirements touched more than once: *Swappable
      channel-adapter contract*, *Long replies are delivered intact*, *Proactive owner-directed
      sends*, *Signal transport*. This catches edits that individually pass review and disagree
      with each other — the failure mode that produced an unreachable outcome and a
      notice-on-empty-send in earlier drafts
- [x] 4.2 Run the full suite plus lint. Existing tests this change knowingly touches:
      `test_channel_adapter.py` (split-measure, recipient-inspection and notice assertions),
      `test_runtime.py` (the repointed since-rejected wiring), and every file using
      `conftest.FakeChannel`. No other test may be modified — and if one needs to be, that is a
      grep the standing rule above should have caught, so re-run it rather than editing past it
- [x] 4.3 `/opsx:sync` the deltas into `openspec/specs/` — **no separate sync step exists in
      this OpenSpec build** (the installed skills are propose/explore/apply/archive). The merge
      into `openspec/specs/channel-adapter/spec.md` is performed by `openspec archive`, i.e. by
      task 4.6. `openspec validate channel-integrity --strict` passes, so the delta is
      merge-ready
- [x] 4.4 Commit (publication-safe: no real numbers, no tailnet IPs — the pre-commit hook
      enforces it)
- [x] 4.5 **STOP — owner go required before deploying to rp5.** Then: confirm the **effective**
      value of `signal.send_timeout_seconds`, `signal.open_timeout_seconds` and the `safe_length`
      floor against rp5's live `config.yaml` **before restart** — that file is locally modified and
      will not carry the new keys, so the values come from the loader, not from this repo's
      `config.yaml`. Deploy, confirm nothing owner-visible changed beyond non-ASCII replies
      splitting slightly more, and watch for `partial`/`failed` log lines for a few days. That
      watch is the delivery outcome's only consumer until a later change gives it a durable one
      (design Risks), so it is a task rather than a suggestion. **Deployed 2026-08-20; single- and
      multi-chunk delivery both verified; the multi-day watch is deliberately left open — see
      As-built.**
- [x] 4.6 `/opsx:archive` with the deploy verification recorded

## As-built (deployed to rp5 2026-08-20, image rebuilt from `0bfcc5b`)

Deploy-verified results for task 4.5, recorded here because none of it is reproducible from
the test suite.

- **The new config keys' effective values come from the loader, as predicted.** rp5's live
  `config.yaml` was grepped before restart and carries `safe_length: 2000` and neither timeout
  key. Read back through the new loader against that same file
  (`compose run --rm --no-deps henk python -c '…Config.load("/app/config.yaml")…'`, which
  needs no restart): `send 10.0 open 30.0 safe 2000`. So production runs on the dataclass +
  `from_dict` literals this change pinned, which is exactly why task 3.2 required pinning both.
- **A real deploy, confirmed by the build rather than by the container line.** In the
  build-only step `COPY henk ./henk` was NOT cached and `pip install` re-ran (19.1s), so the
  new source is in image `db07bf85`; the container line then read `Started`. The follow-up
  `up -d --build` reported every layer `CACHED` — expected, since it reused the image built a
  minute earlier, and NOT the "silently did nothing" tell the README warns about. Startup
  logged `GET …/henk-events/json?since=<id>`, proving it attached to the real
  `henk_henk_audit` volume.
- **The reply path works under the new contract, single- AND multi-chunk.** A homelab-status
  question, `/memories` and `/inbox all` all replied normally and each fit one chunk; the log
  grep for `not delivered|send failed on chunk|Traceback` was empty. A deliberately long reply
  (Henk enumerating its seven tools) then exercised **multi-chunk delivery against the real
  bridge**: two Signal messages, in order, cut at a paragraph boundary, no gap at the seam and
  no failure banner. So splitting is no longer covered only by tests against a fake bridge.
- **The production split reproduces exactly under the shipped splitter.** Feeding that reply
  back through `split_message(text, 2000)` locally yields 2 chunks of 1907 and 373 bytes, and
  the first chunk is byte-identical to what Henk actually sent. Deployed behaviour and the
  unit-tested code agree.
- **That reply confirms the no-regression half, NOT the bug-fix half — stated so the evidence
  is not over-read.** It is 2252 characters but 2280 bytes (14 em-dashes at 3 bytes each), and
  the cut landed on a paragraph boundary at character 1881 — the same boundary the old
  character-measured code would have chosen, because paragraphs sit ~250 characters apart,
  far coarser than the 28-byte divergence. The overrun this change fixes only bites when a
  single paragraph or line is long enough that the window edge itself becomes the cut, which
  needs denser multi-byte text than prose with em-dashes. That case stays test-only, per the
  next bullet.
- **The byte-overrun case is not hand-observable at this limit.** Forcing a cut at the window
  edge with dense multi-byte text needs roughly 500 emoji in one reply, which cannot be
  reliably elicited from a model. For ASCII (and for prose with occasional em-dashes, as
  above) the boundary search reaches the same cut under either measurement, so there is
  nothing owner-visible to confirm — which is itself the "nothing changed beyond non-ASCII
  replies splitting slightly more" check passing.
- **The multi-day `partial`/`failed` watch is OPEN, by owner decision.** The change is archived
  without it. Rationale accepted: if the failure case arrives it is findable from these specs
  and reopenable. The watch command is
  `docker compose -p henk -f /home/pi/Coding/henk/docker-compose.yml logs henk --since 24h |
  grep -E 'not delivered|send failed on chunk'`. Anything it prints is real and was invisible
  before this change.

Three findings for a later change (none blocks anything):

1. **Henk emits Markdown in Signal replies** — bold markers and nested bullets — against
   `AgentConfig.system_prompt`'s explicit "avoid Markdown code blocks and tables". Observed in
   the smoke-test reply. Pre-existing, unrelated to this change, and a prompt-side fix.
2. **Node status is reported by raw tailnet IP rather than hostname.** The homelab-health tool
   output names nodes by address, so the owner-facing reply does too. Cosmetic for the owner,
   but it means owner-visible text (and any transcript of it) carries tailnet addresses — the
   same class of value the repo's commit hygiene rules keep out of the tree. Candidate fix:
   map addresses to `rp5`/`vps`/`rp2` in the tool's presentation layer.
3. **`owner-acknowledgement`'s proposal cites a line number this change moved.** The
   `DEPLOY-VERIFY` note it references as `henk/channel/signal.py:146-151` is now at line 192.
