## Context

The gate enforces turn scope from a `TurnContext` the agent core builds once, at turn entry,
in `_framed_turn` (`henk/agent/core.py:683-698`): `tainted=self._session_tainted`. The only
writer of `_session_tainted = True` is `_start_event_session` (`core.py:749-750`). Nothing
updates the context while the SDK runs the turn, so a tool result that brings untrusted text
into the model's context cannot affect a later authorization in that turn. The archived
session-awareness design (*Deferred: session titles*, "Invocation-time taint") names this as
the blocker for any free-text rendering, and roadmap row 4b makes it the first task.

**The production tool-call path.** For every Henk tool call the SDK consults `can_use_tool`
(`henk/agent/sdk_session.py:223-237`), which calls `decide_tool_permission`
(`henk/agent/permission.py:79`), which calls `ApprovalGate.authorize`
(`henk/gate/approval.py`). Read-only and notify-only tools get a permitting decision there
with no receipt. If allowed, the SDK then calls the MCP handler (`sdk_session.py:266-268`),
which calls `tool.run` directly. `gated_invoke` (`approval.py:421`) is **not** on this path:
nothing in `henk/` calls it, and its docstring's claim that "every tool call goes through" it
is wrong. The raise therefore attaches to `authorize`, the one function both paths share.

## Goals / Non-Goals

**Goals:**
- A tool call marked as a taint source makes every later owner-scoped mutating scope check
  in the same turn resolve `out-of-scope`, receipted, with no channel send.
- The session keeps that taint for its lifetime, like an incident, on every exit path.
- The refusal tells the model the true reason, and the receipt names the source.
- Turns with no taint source behave exactly as today: same contexts, outcomes, reasons and
  receipt fields.

**Non-Goals:**
- Marking any existing tool as a taint source (owner decision 2).
- Session titles, the publisher, any free-text rendering, the system prompt wording.
- An audit schema change or a session-record taint field (owner decision 3).

## Decisions

### D1 — The declaration: `raises_taint`, read from the registered tool

`Tool` gains `raises_taint: bool = False` beside `tool_class`, `authorization` and
`turn_scope`. The gate reads it from the registered **instance** (`tool.raises_taint`), so a
tool may set it on its class or in its constructor (owner decision 4). This lets
session-titles make `sessions_read` a taint source only when built with titles rendering
on, instead of ending writes on every sessions query.

The narrowing rule, the same one `effective_tier` applies to tiers (`approval.py:9-11`):
configuration may cause a tool to declare taint (by selecting the mode that renders outside
text), and nothing may clear a declaration the tool's code makes unconditionally. This is a
rule for tool authors and reviewers, not a runtime check.

**When a tool is a taint source** (the test for the next author): its result can carry free
text that someone other than the owner authored. Projections to fixed-shape values
(`homelab_health`, `homelab_query`, `sessions_read` today) and owner-authored text
(`todo_read`, `inbox_read`, `reminders_read`) are not. Event payloads meet the test, which is
why event turns are already tainted.

*Alternatives.* A new `ToolClass` value (`untrusted-read`) conflates the mutation axis with
the trust axis and would break every `tool_class in (READ_ONLY, NOTIFY_ONLY)` check. A
class-only flag forces session-titles into a separate tool (owner decision 4 (b)).

### D2 — Taint is raised inside `authorize`, on a permitting decision, before returning

When `authorize` is about to return a decision whose `permits` is true for a tool whose
`raises_taint` is true, it taints the gate's current turn context first. For read-only and
notify-only tools that is the early-return branch; for mutating tools it is after `_resolve`.
There is no `await` between the decision and the raise, so under concurrent `can_use_tool`
calls the order of authorizations on the event loop is the order that counts.

The raise always operates on the gate's context **at the moment of the raise**, never on a
copy captured earlier: the per-instance branch captures `context` before awaiting the
owner's reply (`approval.py:231`, `:265`). If the call was framed when authorization started
and no turn is framed at the moment of the raise, the raise does nothing (the turn is over;
the core already folded what it saw).

*Alternatives.* Raising in the MCP handler or in `gated_invoke` after the tool returns: the
handler is deploy-only code the suite cannot run, `gated_invoke` is not on the production
path at all, and both are later, so a write authorized while the source is still running
would pass. A `PostToolUse` hook depends on SDK behaviour, and the closed-toolset history
(`permission.py:51-63`) is the record of how that goes.

### D3 — "Later" means a scope check after the raise

- A write whose scope check ran before the raise stands, including one earlier in the same
  assistant message. Its arguments were fixed before the untrusted result existed. The
  consequence is that two calls in one assistant message (a taint source and a write) can
  resolve differently depending on the order the CLI authorizes them in. Both orders are
  safe: the write's arguments never saw the untrusted text.
- A write whose scope check runs while the taint source is still executing is refused: the
  raise happened at the source's authorization.
- The taint is raised whether the tool then succeeds or fails: an error string can carry the
  same outside text, and the gate never sees the result.
- A taint-source tool whose own authorization does not permit it (refused, timed out, out of
  scope) never ran, so it raises nothing.
- A per-instance write is scope-checked before its prompt and not again after the owner's
  answer. No per-instance tool is registered, and such a write, too, was composed before any
  taint raised while it waited. Re-checking after approval is left to the change that
  registers the first per-instance tool, together with the one case D2's "turn has exited"
  rule leaves open: a per-instance taint source approved after its turn errored, in a
  session that stays open, would run with no taint recorded. Dormant while no per-instance
  tool and no taint source exist.

### D4 — `TurnContext` carries the taint's source

`TurnContext` gains `taint_source: str | None = None`: the registered name of the tool that
tainted the session, `None` for an untainted session **and** for an incident-tainted one.
Raising taint on a context that is already tainted is a no-op, which keeps the two cases
apart (an incident session keeps `taint_source=None`) and keeps the first source when
several are called. Event turns always open a fresh session (`core.py:742-750`), so an
incident can never arrive in a session a tool already tainted. The dataclass stays frozen;
the gate swaps in a replaced copy, so snapshots a test double recorded at `enter_turn` never
change, and the new field's default keeps equality with existing recorded contexts.

### D5 — The core folds the turn's taint into the session in `_framed_turn`'s `finally`

The core keeps `_session_taint_source` beside `_session_tainted`, passes both into the
`TurnContext` it frames, and resets both wherever it resets `_session_tainted` today: the
new-session branch of `_ensure_session`, `_start_event_session`, and `_close_session`
(including its early return when there is no session). On the way out of a framed turn it
reads the context the gate held at exit and, if that is tainted and the session is not,
marks the session tainted with the context's source. Because this lives in the `finally`, a
turn that raises taint and then errors still taints the session.

The suggested seam is `ApprovalGate.exit_turn()` returning the context it held (or `None`).
The existing `RecordingGate` doubles return `None` from `exit_turn` and have no other
attributes, so the core must treat a `None` or non-`TurnContext` return as "nothing to fold".
The replay harness ignores `exit_turn`'s return. Any seam that keeps those passing is
acceptable; the tests drive the real gate through the real core and do not pin the seam.

*Alternatives.* A callback injected into the gate couples the gate to the core's session
state and gives the session two writers. Reading `gate.turn_context` before `exit_turn`
breaks the doubles, which lack the property.

### D6 — Reason and receipt detail

`_scope_denial_reason` keeps its order: event turn first, then a tainted context. For a
tainted context, `taint_source is None` keeps today's incident wording byte for byte; a set
source gets exactly this string, with `<tool>` replaced by the source's registered name and
no quoting around it:

```
a tool called earlier in this session (<tool>) returned content from outside the owner's control, so this session is now tainted for its lifetime and writes are out of scope in it; nothing was stored or scheduled. The owner can use /remember, /capture or /remind (which bypass this session entirely), or /new to start a clean session where writes work again.
```

The `out-of-scope` receipt for that refusal carries `detail` exactly `taint raised by <tool>`
through the existing field of the authorization record. Every other receipt the gate writes
keeps `detail=None`, as today. `approval_entry` (the session record's projection) is
unchanged, so the session record does not gain the field and no schema version moves. Tests
assert both strings exactly.

### D7 — An unframed taint-source call fails closed

With no framed turn at the start of authorization, the gate uses `_UNFRAMED_CONTEXT`. A
permitted taint-source call there must leave the gate tainted so later unframed owner-scoped
writes are refused `out-of-scope` until the next `enter_turn` or `exit_turn`. Whether that is
an installed `TurnContext` or a separate flag is the implementer's choice; the tests pin only
the refusal. Production frames every turn, so this matters only to direct callers.

## Test apparatus

The tests are written before the implementation and the implementer may not edit them, so
they drive the production boundary rather than a convenience wrapper:

- Tool calls are scripted through `decide_tool_permission(registry, gate,
  "mcp__henk__<name>", args)`, then `tool.run(**args)` only when allowed: the same shape as
  `DrivingClient._answer` (`tests/replay_fakes.py:152-166`) and production. `gated_invoke` is
  not used, so an implementation that raises there fails the suite.
- One probe authorizes the taint source and never runs it, then authorizes `store_memory`:
  it pins "before the tool runs" without naming a seam.
- One probe holds the taint source's `_run` on an `asyncio.Event` while `store_memory` is
  authorized: it pins D3's concurrent case, which a raise-after-run implementation passes
  sequentially and fails here.
- Every guard test (a write that should still execute) first proves the taint is present in
  the same test, so it cannot pass vacuously on today's code.

## Risks / Trade-offs

- **The path is dormant in production** → no production tool declares `raises_taint`, so
  only tests exercise it until session-titles lands. The tests run the real gate inside the
  real core at the production boundary, the same approach as the suppressed-turn path
  (`EventScopedTool`). session-titles owns the first live verification.
- **The design assumes `can_use_tool` fires for read-only Henk tools**, which no production
  artifact shows (the gate writes no receipt for reads). Confidence: moderate-high. The CLI
  has no input that tells a read-only Henk tool from a mutating one: `allowed_tools` is
  empty, `permission_mode` is `default`, `setting_sources=[]`, `strict_mcp_config=True`, and
  `_adapt_tool` sets no annotations. Mutating Henk tools are proven to reach `can_use_tool`
  by their receipts, so reads take the same path. `auto_approves_any` covers only the
  configured allow list, not anything the CLI approves internally, so it is not the
  guarantee here. Mitigation: session-titles' first live check confirms `can_use_tool` fires
  for a read-only `mcp__henk__` tool before any taint source ships.
- **A write in the same assistant message as the tainting read can still pass** if its
  authorization runs first → accepted (D3): it was composed without the untrusted text.
- **A tool-tainted turn with no attempted write leaves no taint-specific audit trace** →
  accepted under decision 3; the session record's `tool_calls` still show the call.
- **`ReplayStub` does not copy `raises_taint`** (`henk/replay/harness.py:321-328`) →
  harmless: replay frames every turn with the already-tainted `REPLAY_TURN`. A comment there
  records why.

## Migration Plan

None. No config, schema, stored data or deployment step. Rollback is a revert; with no
production taint source, deploying or reverting changes no observable behaviour.

## Open Questions

None blocking. Owner decisions 1-4 in the proposal carry recommended defaults that this
design implements.
