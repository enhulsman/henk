# Tasks — Same-Turn Taint

> **Tests first, from the delta scenarios.** Each scenario gets at least one test, each
> SHALL/MUST at least one assertion. The tests live in `tests/test_same_turn_taint.py` and
> drive the real `ApprovalGate` (and, for the session scenarios, the real `AgentCore`) with
> test-only taint-source tools, because no production tool declares `raises_taint` (owner
> decision 2). Tests are written and committed before implementation; the implementer does
> not edit anything under `tests/` or `openspec/`. When a test fails, fix the
> implementation, not the test.
>
> The tasks assume the proposal's recommended picks: session-lifetime taint, no production
> tool marked, receipt `detail` only (no schema change), declaration read from the tool
> instance. A different pick changes the tasks before any code is written.
>
> **Coordinator-owned bookkeeping.** The implementer cannot edit `openspec/`, so ticking
> tasks 2-5 and recording 5.2's mutation results in `notes/apply-decisions.md` is done by
> the coordinating session from the implementer's final report.

## 1. Tests (`tests/test_same_turn_taint.py`)

- [x] 1.1 Test-only fixtures: read-only taint sources `untrusted_read` and `untrusted_read_2`
      (class-level `raises_taint = True`); a failing variant; a blocking variant whose `_run`
      waits on an `asyncio.Event`; a per-instance mutating taint source; a read-only tool
      whose class does not declare taint but whose instance sets it. A scripted session that
      replays the production boundary: each tool call goes through
      `decide_tool_permission(registry, gate, "mcp__henk__<name>", args)` and runs
      `tool.run(**args)` only when allowed (the `DrivingClient._answer` shape in
      `tests/replay_fakes.py`). `gated_invoke` is not used.
- [x] 1.2 approval-gate ADDED scenarios, one test each, all against the real gate and the
      real `AuditLog`/`MutationReceipts`: refuses a later `store_memory` (store unchanged, no
      approval prompt, reason and `detail` asserted exactly); refuses a later `capture`
      (inbox unchanged); raised before the tool runs (authorize, never run); a write
      authorized while the source is still running is refused; a write authorized before
      the taint stands (and a second write after it is refused); a failing source still
      taints; a not-permitted source raises nothing (denied by the owner, then taints once approved); an
      instance-level declaration is honoured; turns without a source are unchanged; an
      incident-tainted session keeps its reason and a null `detail` (the source executed);
      the first source is named; an unframed source fails closed; no production tool is a
      taint source (registry built with every optional tool enabled).
- [x] 1.3 approval-gate MODIFIED requirement: the tool-taint reason names the `/remember`,
      `/capture`, `/remind` and `/new` remedy (covered by the exact-reason assertions in
      1.2); the existing incident and event-turn tests stay unchanged and green.
- [x] 1.4 agent-core ADDED scenarios, one test each: the next turn of the session is tainted;
      a turn that errors after raising taint still taints the session; `/new` clears it;
      idle expiry clears it; an incident displacing a tool-tainted session carries no tool
      source; `/remember` still stores after it. Each clearing test first asserts a write is
      refused before the clearing step.
- [x] 1.5 Run `uv run pytest -q`. Every test whose scenario expects a refusal, a tool-taint
      reason or a named source fails on today's code with an assertion about the missing
      behaviour (not an import, fixture or attribute error in the fixtures themselves). Every
      guard test asserts its precondition in the same test, so it also fails on today's code.
      Tests that pass on today's code are listed in the commit report with the reason
      (expected only for "turns without a taint source are unchanged" and "an
      incident-tainted session keeps its reason", which pin current behaviour that the
      implementation must not break). "No production tool is a taint source" fails today on
      the missing `Tool.raises_taint` attribute, which is expected; it reads the attribute
      directly, never through `getattr` with a default. The displacement test first
      asserts a write in the tool-tainted owner session is refused. The existing suite
      passes.

## 2. Declaration (`henk/tools/base.py`)

- [x] 2.1 Add `raises_taint: bool = False` to `Tool`, documented beside `turn_scope` with the
      taint-source test from D1. Update the module docstring's list of declared axes.

## 3. Gate (`henk/gate/approval.py`)

- [x] 3.1 `TurnContext` gains `taint_source: str | None = None` (D4); the class and module
      docstrings describe tool-raised taint beside incident taint.
- [x] 3.2 In `authorize`, on every permitting decision for a tool whose instance declares
      `raises_taint`, taint the gate's current context before returning, with no `await` in
      between; a no-op on an already-tainted context; a no-op if the call was framed and the
      turn has since exited (D2). Fail closed when unframed (D7).
- [x] 3.3 `_scope_denial_reason`: keep the event-turn and incident reasons byte for byte; add
      the tool-taint reason for a tainted context with a `taint_source` (D6, exact string).
- [x] 3.4 Pass `detail="taint raised by <tool>"` to the receipt for that denial only; every
      other gate receipt keeps `detail=None` (D6).
- [x] 3.5 Make the context held at exit available to the core (suggested: `exit_turn()`
      returns it) (D5).
- [x] 3.6 Correct `gated_invoke`'s docstring: it is a convenience for callers outside the SDK
      path, not the production path, and taint is raised in `authorize`.

## 4. Core (`henk/agent/core.py`)

- [x] 4.1 Track `_session_taint_source` beside `_session_tainted`; frame it into every
      `TurnContext`; reset it in the new-session branch of `_ensure_session`, in
      `_start_event_session` and in `_close_session`, including its no-session early return
      (D5).
- [x] 4.2 In `_framed_turn`'s `finally`, fold a tainted exit context into the session taint
      and source, tolerating gate doubles whose `exit_turn` returns `None` (D5).

## 5. Verification

- [x] 5.1 `uv run pytest -q` fully green, with nothing under `tests/` edited.
- [x] 5.2 Mutation-check the new tests; for each mutation at least one new test must fail:
      drop the raise in 3.2; move the raise from `authorize` to after `tool.run` in
      `gated_invoke`; drop the tool-taint reason (3.3); drop the `detail` (3.4); drop the
      fold in 4.2; drop the source resets in `_close_session` and `_start_event_session`
      together (each alone is covered by the other: displacement runs both, and a stale
      source is observable only through displacement, because a source is read only while
      the session is tainted). Report the results for the coordinator to record.
- [x] 5.3 In `henk/replay/harness.py`, add a comment at `ReplayStub` saying why
      `raises_taint` is not copied (replay frames every turn with the tainted `REPLAY_TURN`).

## 6. Close-out (coordinator)

- [x] 6.1 `openspec validate same-turn-taint --strict` passes.
- [x] 6.2 At archive, update NORTH-STAR row 4b: the taint prerequisite has landed; the
      session-titles obligations are the system-prompt wording for tool-raised taint, the
      live check that `can_use_tool` fires for a read-only `mcp__henk__` tool, and marking
      `sessions_read` only when built with titles rendering on. Replace the stale
      `core.py:542-548` citation.
