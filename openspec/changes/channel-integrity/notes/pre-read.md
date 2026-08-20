# Pre-read findings — applied and deferred

A scrutiny pass was run on this change before its proper `/scrutinize` review. It was **primed**
— given a summary of the intended defects and design decisions — so it is strictly weaker than a
cold read, and it does **not** substitute for the scrutiny pass. It is recorded here so the real
pass can tell what was already caught from what it finds fresh.

## Applied to the artifacts

| # | Finding | Where fixed |
|---|---|---|
| F1 | Hold exhaustion was specced as `partial` even when **nothing** was delivered, contradicting the outcome's own definition. A downstream scheduler reading `partial` as "the owner saw part of it" would suppress a retry that should fire. | spec: `partial` if ≥1 chunk delivered, `failed` otherwise, + scenario |
| F2 | No mechanism specified for telling a reply from a proactive send, across **eight** call sites, two of them unclassifiable. And the obvious `reply: bool` keyword breaks an existing test asserting `send`'s params are exactly `["text"]`. | spec: `send_proactive` named in the contract's operation list; design: full call-site classification table |
| F3 | "SHALL NOT block or delay the turn" was unimplementable as written — `on_inbound` is awaited inside the receive loop, so a hung bridge stalls every message behind it. | design: awaited with its own ~2s bound, separate from the send timeout, with the delay explicitly accepted and detaching named as the fallback |
| F4 | A single typing `PUT` expires in ~15s, so it would cover only the first seconds of exactly the long tool-using turn the feature exists for — and the design leaned on that expiry as its stuck-indicator mitigation without noticing it cuts both ways. | design + task 5.6: ~8s refresh cadence, cancelled in the same `finally`, failures logged once per turn |
| F5 | Three unlisted wiring changes against a task promising the existing tests pass untouched: the `Dispatcher` constructor, the core's channel protocol, and `FakeChannel.send` returning `None`. | proposal Impact + tasks 5.5, 5.7, 6.3 (which now names the affected tests instead of using an escape hatch) |
| F6 | The binding spec text asserted bytes are "what the channel enforces" — but `base.py` says 2,000 is a self-chosen conservative value and Signal's client limit is counted in UTF-16 units. An unverified empirical claim in the binding record. | spec + proposal + design: re-grounded on bytes bounding both wire size and any character limit; the "4×" framing dropped |
| F7 | "Nothing reveals to a non-owner that a recipient exists" is unattainable — the bridge runs in `json-rpc` mode, so signal-cli sends protocol delivery receipts automatically, and a registered number reveals a recipient regardless. The stranger test would pass while the stated property was false. | spec: scoped to acknowledgements Henk *originates*, transport receipts recorded as an accepted residual, + task 6.9 checking the daemon isn't configured to send read receipts underneath the app |
| F8 | The hold bound had no arithmetic: one chunk's worst case is `max_send_attempts × send_timeout + backoff` ≈ 33s, so a smaller bound could never bite. | spec + design: validated at config load against `max_send_attempts × send_timeout_seconds` (the liveness-deadline precedent), evaluated at chunk boundaries; task 4.1 moved ahead of the mutex work |
| F9 | "No code path constructs a client without a timeout" is false in shipped code — `runtime.py`'s shared tool client has none. The test would have been quietly narrowed to fit. | spec scenario scoped to the Signal bridge; task 3.2 names the shared client as out of scope rather than hiding it |
| F10 | Group 5's tests depended on config and endpoints that arrived in a later task. | tasks: config + transport first (5.1) |
| F11 | **The design's justification for `channel_ref` was factually wrong.** It claimed the millisecond round-trip is lossy; it is not (verified over 200k samples), and `InboundMessage.timestamp` has no consumers at all. Since a stated exception to an encapsulation rule is precedent, a false premise underneath it is a real defect. | design D6: the claim is withdrawn in place and re-argued on message-identity grounds; `channel_ref` kept; no-log/no-audit prohibition added |
| F12 | Three undefined edges: whether the truncation banner alters the outcome or extends the hold, and what an empty send returns. | spec: two scenarios |
| F13 | The `channel-adapter` spec's `## Purpose` still enumerates the old operations and carries the "no read receipt" line. | task 6.1 |
| — | The receipt recipient should come from the configured owner constant, never `message.sender`. | spec + task 5.2 |
| — | Byte guarantee is over code points, not grapheme clusters. | spec + design |
| — | `acknowledge_inbound` defaulted `true` in config while the rollout deploys `false`. | task 5.1: repo default is `false` |
| — | Deploy-verify that the endpoints accept whatever form `owner.id` takes (UUID vs E.164). | task 6.8 |

## Deferred — for the owner, not for me

**F14 — the approval-gate is the one caller where non-delivery costs something.** An approval
prompt that never landed burns the full 300s timeout with the owner never having been asked. It
fails *closed*, so this is wasted time rather than a safety hole. Recorded as task 6.2, which
requires a decision (wire it, ~3 lines, or declare it a non-goal) rather than assuming one.

**The reviewer recommended splitting this change in two**, and that is a scope call for the
owner:

- `channel-integrity` = groups 1–4 (delivery outcome, mutex, bytes, timeout). Owner-invisible,
  no flag, no `channel_ref`, no agent-core amendment, and it is what reminders actually blocks on.
- `owner-acknowledgement` = group 5 + the agent-core delta + the `channel_ref` exception + the
  flag + the multi-day watch and the deploy gate.

The argument for splitting: the acknowledgement half carries the only new config, the only new
endpoints, the only encapsulation exception, the only rollback flag, the deploy gate, and four of
the pass's seven significant findings — while sitting on the critical path for reminders. The
argument against: both halves touch one file, so splitting means two passes over `signal.py`, and
the flag already contains the feature's risk.

**Not actioned.** The change is coherent as one unit and validates; splitting is cheap to do later
and this session had already been asked to stop widening scope on its own initiative.
