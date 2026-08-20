# Design — Channel Integrity

## Context

`henk/channel/` is the only module allowed to know Signal specifics, and it is the narrowest
part of the security posture: outputs are structurally owner-only, which is what lets Henk's
data circles widen safely (NORTH-STAR, "the theorem connecting the axes"). Everything in this
change stays inside that module plus three small call sites.

Current state, verified in source rather than assumed:

- `SignalAdapter.send` (`henk/channel/signal.py:96-113`) loops chunks, and on a permanent chunk
  failure logs, best-effort posts a banner, and **returns `None`** — the caller cannot
  distinguish any outcome. It also **returns on the first permanent failure**, so later chunks
  are never attempted; every chunk before a failure was delivered.
- `_send_chunk` (`signal.py:115-131`) retries `max_send_attempts` (default 3) with backoff,
  returning `bool` internally. That bool is discarded by `send`. Sleeps fall between attempts
  and not after the last, so the per-chunk budget is `3 × timeout + 3s` of backoff.
- `SignalCliRestBridge.send` wraps **every** exception into `SignalBridgeError`, so a lost
  response, a post-processing 5xx and an unsent request are indistinguishable — and it
  constructs `httpx.AsyncClient()` with no timeout, taking httpx's 5s default. That default is
  **per transport phase**, not total: connect, write and read each get 5s, so one POST can run
  to roughly three times the number a reader would assume.
- `SignalCliRestBridge.receive` passes `open_timeout=self._open_timeout`, whose 30s value is a
  constructor default that `henk/runtime.py:74` never supplies.
- `split_message` (`henk/channel/base.py:49-72`) measures `len(str)` — code points, not bytes —
  and validates only `limit <= 0`.
- `AllowlistFilter` runs in `Dispatcher.on_inbound` (`henk/app.py:38`), *above* the adapter.
- `ChannelAdapter` (`base.py:28`) is `@runtime_checkable`, but no `isinstance` check against it
  exists anywhere in `henk/` or `tests/`, and the repo configures no static type checker — so
  widening the Protocol has no conformance blast radius.

## Goals / Non-Goals

**Goals:**

- A caller can know whether its message reached the owner, and a partial delivery is never
  reported as success.
- The distinction between a reply and an agent-initiated message is carried by the contract, so
  the owner-facing notice on a failure is authored by the layer that knows what was sent.
- The safe-length limit is enforced in bytes, which bound both the wire size and any
  client-side character limit — the configured 2,000 is a chosen conservative value, not a
  measured Signal maximum.
- Timeouts are chosen, not inherited from a library default, and bound the whole request rather
  than each phase.

**Non-Goals:**

- Exactly-once delivery. Unobtainable: signal-cli offers no idempotency key, and D1 establishes
  the adapter cannot distinguish a lost response from an unsent request. This change makes the
  *ambiguity visible* to callers; it does not remove it.
- Retry policy changes beyond making the timeout explicit. `max_send_attempts` and its backoff
  stay as they are, as constructor defaults rather than config: a knob has to earn its place
  with a scenario, and nothing here reads them.
- Any change to the allowlist, the DM-only rule, or the owner identity resolution.
- **Serializing outbound sends.** See D5 — deferred to `reminder-delivery` with its rationale.
- **Acting on a non-delivered approval prompt.** See D6 — an explicit non-goal, with its
  rationale, because the obvious wiring is wrong.
- **Owner acknowledgement** (read receipts, working indicator). Deferred to
  `owner-acknowledgement`; it needs no part of this change beyond the outcome type.

## Decisions

### D1 — A three-valued outcome, not a boolean

`send()` returns `delivered` / `partial` / `failed`. A boolean would collapse the one case that
matters most: some chunks landed and the rest did not, which is neither success nor a clean
failure and which a caller may want to treat differently from either.

**`failed` means "delivery was not confirmed", never "nothing arrived."** `bridge.send` raises
on any transport fault, including a response lost after the server accepted and sent the
message, and `_send_chunk` then retries. So a `delivered` outcome is also
at-least-one-attempt-succeeded rather than exactly-once. Both facts are stated in the spec
because a caller that reads `failed` as "nothing happened" will double-send — and because D6
turns on exactly this point.

*Alternative considered.* Raising an exception on failure. Rejected: `send` is called from the
reply path, the triage path and the gate prompt, none of which want a raise, and the existing
contract explicitly says the adapter "must not raise on transport errors".

### D2 — Reply and proactive are separate operations, and the notice is caller-supplied

Today a failed send posts `"[⚠ part of this reply could not be delivered]"` regardless of what
was being sent. For a proactive send that text is simply wrong: it was not a reply.

**The distinction is carried by two methods, not a flag.** `send(text)` is the reply path;
`send_proactive(text, *, failure_notice=None)` is the agent-initiated path. A `reply: bool`
keyword was the obvious alternative and is ruled out by an existing test —
`tests/test_channel_adapter.py:191-199` asserts that `send`'s parameter list is exactly
`["text"]`, and that test protects a real property (no recipient may ever be passed), so it
should not be relaxed. Keeping the notice parameter on `send_proactive` alone preserves that
assertion unchanged for `send`.

**Two arguments carry this decision, and a third that used to be offered does not.** First,
baseline `agent-core`'s *Turns are typed and event turns carry triage framing* already binds
*"Event-turn output SHALL be routed through the channel adapter's proactive owner-directed
send"*, while `core.py:303` uses the reply path — so this closes a pre-existing spec-vs-code
divergence, recorded here as resolved rather than slipped in. Second, `reminders`' *Due
reminders are delivered verbatim without an agent turn* binds delivery through the proactive
send, making it a hard prerequisite for the next change.

The argument **not** to reuse: "an adapter-authored notice is delivered over the channel that
just failed." That applies identically to the reply path, where the notice is kept, and
`tests/test_channel_adapter.py` already demonstrates the notice succeeding after a chunk
failed. It is struck.

Every current call site is classified here, because two of them are not obvious:

| Call site | Class | Notice |
|---|---|---|
| `core.py:221` `/new` confirmation | reply | adapter's standing text |
| `core.py:228` command reply (`/inbox`, `/memories` — these do chunk) | reply | adapter's standing text |
| `core.py:240` error reply | reply | adapter's standing text |
| `core.py:247` agent reply | reply | adapter's standing text |
| `core.py:303` triage output | **proactive** | **caller-supplied** — the one proactive path that chunks |
| `core.py:325` degraded-durability notice | **proactive** — an unprompted operator alert | none (single chunk) |
| `runtime.py:195` since-rejected notice | **proactive** | none (single chunk, 212 chars) |
| `gate/approval.py:297` approval prompt | **reply** — the owner is present and waiting on it | adapter's standing text |

### D3 — The notice is emitted by one shared path, on any attempted non-`delivered` outcome

`send` and `send_proactive` are thin wrappers over one private
`_send_serialized(text, *, failure_notice)`. Sharing one path matters for two reasons: a wrapper
that called the other operation would re-enter it (and would deadlock outright once
`reminder-delivery` adds the mutex), and the notice must be emitted from inside the same send
sequence as the chunks it describes, not as a separate call that could be separated from them.

**The condition is "any outcome other than `delivered`, where at least one chunk was
attempted"** — not "partial". Today a permanent failure on chunk 1 of 1 still attempts the
notice, and a single-chunk reply that fails is the most common failure shape; conditioning on
`partial` would silently drop the notice exactly there. The attempted-a-chunk qualifier is what
keeps an empty send silent: `split_message("")` returns no chunks, the outcome is not
`delivered`, and a notice claiming a delivery failure would be false.

The notice gets a **single attempt** rather than the full retry budget. A bridge that just
refused three attempts will not take a fourth, and the notice's own failure is not worth
another 33 seconds of the caller's latency. Its outcome is never reported.

### D4 — Byte-measured splitting, character-boundary cuts, with a progress guard

`split_message` compares `len(chunk.encode("utf-8"))` against the limit while continuing to cut
at paragraph/line/word boundaries, shrinking the window until the encoded length fits. Code
points are never split, and concatenating the chunks reproduces the input exactly.

This is a latent bug fix for every outbound path: a long reply containing emoji can currently
produce a chunk whose encoded size is several times the limit the code believes it is enforcing.
The guarantee is over code points, not grapheme clusters — a ZWJ or skin-tone sequence can still
be divided — which is stated so no reader over-reads it.

**The two guarantees are jointly unsatisfiable below a 4-byte limit**, where no code point fits
and the shrink loop would find a zero-length cut and never advance — an unbounded loop on the
production send path. So the limit is floored at load. The floor and the byte measurement ship
together: the guard is what makes the measurement safe, and separating them would leave a
window where the loop is reachable.

### D5 — Serialization is deferred to `reminder-delivery`, not dropped

Chunks are sent with an `await` per chunk and no mutual exclusion, so two concurrent senders can
interleave their chunks. That is a real defect, and it is **not** fixed here.

The call-site topology says why. Of the eight `send` call sites, **seven run on the core worker
task** — six in `core.py` directly, and `gate/approval.py:297` transitively, since it fires
inside `session.run_turn` inside `_process_owner` inside `AgentCore.run` — and the core queue is
serial by construction. `runtime.py:195` is the only cross-task sender in the codebase, and it
sends `SINCE_REJECTED_NOTICE`: **212 characters, one chunk at any plausible safe length.** A
single-chunk message cannot have its own chunks interleaved. So the entire observable defect
today is that one short operator notice may land between two chunks of a long reply.

Against that, a mutex costs real head-of-line blocking that does not exist now. A waiting
sender's delay is `N × per-chunk latency` in every state, healthy or not, where `N` is the chunk
count of the message holding the lock; `N` is unbounded, since no reply-length cap exists in the
codebase. The failure ceiling is `N × 33 + 10` seconds — `max_send_attempts × send_timeout` plus
backoff per chunk, plus the notice's single attempt — i.e. roughly 670s at 20 chunks. And under
the elevated-latency state this change's Why documents, a long reply holds for tens of seconds to
minutes **with no failures at all**: an ~18-chunk `/memories` reply at 5–8s per chunk holds for
90–144s on a working bridge. `henk/agent/commands.py:146` calls `list_all()` unbounded, and the
store caps admit ~35KB, so that reply is reachable today.

Two mechanisms were designed and rejected before landing here, and both are recorded so the next
change does not re-derive them:

- **A bounded hold**, abandoning chunks past a time limit. Rejected: the bound's `failed`
  outcome is unreachable, because `send` returns on the first permanent failure and so any
  abandonment at a chunk boundary has already delivered something. Making it reachable requires
  starting the clock at lock-acquisition *wait*, which punishes the waiting message rather than
  the starving holder — inverting the mechanism's purpose.
- **A chunk cap**, truncating past *N* chunks. Rejected: it discards owner-requested content on
  the *healthy* path, where the hold bound only truncated under retry. At a cap of 10, roughly
  44% of a full `/memories` reply would be discarded permanently on a working bridge, under a
  notice claiming it could not be delivered.

**So serialization moves to `reminder-delivery`**, where the second cross-task sender actually
arrives (its scheduler runs as its own task and delivers directly through the adapter) and where
the latency measurement that would justify any bound can exist. The preferred mechanism there is
**delivery-path priority** — a second lock tier letting a reminder-class send pre-empt a queued
reply — rather than either rejected bound. If interleaving must be fixed without a bound,
**paginate rather than discard**: release the lock every *N* chunks and re-acquire for the
remainder, which bounds the hold and preserves content at the cost of one interleaving point per
long message.

**One consequence to carry forward, stated rather than left implicit.** `reminders`' scenario
*Delivery does not wait on a turn* is scoped by a requirement about the *serial turn queue*, and
remains true of the queue. Once a mutex exists, it stops being true of the send path: a reminder
due mid-multi-chunk-reply waits. `reminder-delivery` must either amend that scenario or implement
delivery-path priority — it cannot satisfy it with a plain mutex, and a test that appears to
prove otherwise is testing a cooperative double.

### D6 — Acting on a non-delivered approval prompt is an explicit non-goal

An approval prompt that never landed burns the full 300s timeout (`config.py:38`) with the owner
never having been asked. Wiring the gate to treat a non-`delivered` prompt as cancelled is about
three lines and looks like a clear improvement. **It is not, and D1 is the reason.**

`failed` means delivery was not confirmed. A prompt that reached the owner and whose
acknowledgement was lost — precisely the state the Why documents as common under load — would be
cancelled instantly. The owner's "yes" then arrives with no pending approval, so instead of
approving a tool call it enters the model's turn stream as ordinary conversational text. That
converts a working approval into a denial plus a stray keyword in the model's context. The
benefit case (a prompt truly undelivered) is the rarer state; the cost case is the documented
one.

The current behaviour already fails closed on timeout, so the cost of doing nothing is wasted
time, not a safety hole. Revisit when `owner-acknowledgement` lands and a read receipt can
corroborate delivery independently of the send outcome — corroboration is what makes the wiring
safe, and it does not exist yet.

## Risks / Trade-offs

- **`send()`'s return type changes and eight call sites use it.** → The outcome is additive:
  callers that ignore the return value behave exactly as before, and a contract scenario asserts
  that. Only the reply path and the three repointed proactive sites act on it in this change
  (logging), so the blast radius is four branches.
- **Byte measurement shrinks effective chunk size for non-ASCII text**, so a long reply with
  emoji becomes more messages than before. → Correct behaviour; the previous count was simply
  wrong about the limit it was enforcing.
- **The interleaving defect ships unfixed.** → D5. It is cosmetic today, by the single-chunk
  argument, and the fix has a real cost that wants measurement first.
- **The new signal has no durable consumer in this change.** Non-delivery reaches a log line and
  nothing else: no audit field, no handoff topic. NORTH-STAR's attention contract says
  informational-but-not-actionable belongs in the record, and this change does not put it there —
  so the event-triage audit record still asserts a triage was notified without recording whether
  the owner received it. → Accepted for one change, and the reason the deploy step includes a
  multi-day watch of `partial`/`failed` lines: that watch is the consumer until
  `reminder-delivery` gives the outcome a durable one. Recorded as a Non-Goal rather than counted
  as delivered value.
- **`send_timeout_seconds = 10.0` is a chosen number, not a measured one.** The Why says
  signal-cli latency "can exceed" 5s without saying by how much. → The value is deliberately
  generous against a container in the same compose network, and `reminder-delivery` is asked to
  measure before it designs any bound on top of it.

## Migration Plan

1. Land the change. Nothing owner-visible changes except that non-ASCII replies split into
   slightly more messages; the reply path begins logging non-`delivered` outcomes.
2. Confirm the new config keys' **effective** values on rp5 before restart. rp5's `config.yaml`
   is locally modified and will not carry them, so the values come from the loader's defaults,
   not from this repo's `config.yaml`.
3. Watch for `partial`/`failed` log lines for a few days. Any that appear are real and were
   previously invisible — that is the first thing this change buys, and until a later change
   gives the outcome a durable consumer, this watch is the only consumer.
4. **Rollback:** revert. There is no flag by design; these are strict corrections, and reverting
   would restore known bugs.

## Open Questions

- **Whether the total send budget should be decomposed across transport phases or enforced by
  cancelling the request.** Decomposition is specced: cancelling an in-flight POST creates a
  request that may already have been delivered, which is the ambiguity D1 exists to describe
  rather than to manufacture. If a phase allocation proves wrong in practice the allocation is
  the knob, not the mechanism.
