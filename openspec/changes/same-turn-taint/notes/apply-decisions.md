# Apply decisions — same-turn-taint

Owner decisions, how the change was applied, and the task 5.2 mutation results. Codex
implemented tasks 2-5 on `autopilot/2026-09-27-taint` (`cb421b6`) from the committed tests
and `autopilot.md`. The coordinating session brought the branch into
`spec/same-turn-taint`, rebased it onto `main` (`7746e2e`) and recorded this file, because
the implementer may not edit `openspec/`.

## Owner decisions (2026-09-27)

All four proposal decisions went to the recommended option, and the refusal wording was
approved as written.

1. **1a: a mid-turn taint lasts for the rest of the session**, like incident taint. The
   owner accepted the stated cost: once session titles ship, asking about running sessions
   switches off Henk's own memory, capture and reminder writes until `/new` or idle expiry.
   Owner commands keep working.
2. **2a: no existing tool is marked.** The test for tool authors is "free text someone other
   than the owner authored". The last test in `tests/test_same_turn_taint.py` pins an empty
   list of declaring tools, over a registry with every optional tool enabled.
3. **3a: the refusal is recorded in the existing receipt `detail`**
   (`taint raised by <tool>`), with no audit schema change and no session-record field.
4. **4a: the declaration is read from the tool as built**, on its class or its instance.
   Session-titles can make `sessions_read` a taint source only when titles rendering is on.

The model-facing refusal text is the exact string in the approval-gate delta spec.

## Implementation notes

- **Seam (D5).** `ApprovalGate.exit_turn()` returns the context it held. The core folds a
  tainted exit context into `_session_tainted` / `_session_taint_source` in
  `_framed_turn`'s `finally`, and ignores a non-`TurnContext` return, so the existing
  `RecordingGate` doubles pass unchanged.
- **Resets (4.1).** `_close_session` now clears both taint fields *before* its no-session
  early return. Previously it cleared `_session_tainted` only after that return, so the
  reset now runs on every path, as D5 asks. There is no behaviour change for today's flows,
  because a tainted session with `_session is None` cannot arise.
- **Unframed (D7).** A permitted taint source with no framed turn installs a tainted copy of
  `_UNFRAMED_CONTEXT` as the gate's context, which is the installed-context option D7 left
  open. `turn_context` is then non-None until the next `enter_turn` / `exit_turn`. Nothing
  in `henk/` reads it outside a turn.
- **Receipt `detail`.** `_resolve` passes `detail` only when set. Every other gate receipt
  keeps the recorder's `None` default, byte for byte as before.
- **3.6 / 5.3.** `gated_invoke`'s docstring now says the SDK path calls `authorize`
  directly and that taint is raised there. `ReplayStub` carries a one-line comment on why
  `raises_taint` is not copied.

## Verification

- After the implementation, on the Codex branch: 3627 passed (owner-reviewed, Go).
- After the rebase onto `main` (`7746e2e`, which adds `tests/test_builtin_host_tools.py`):
  `uv run pytest -q` gives 3631 passed, 6 skipped, 12 deselected. Nothing under `tests/`
  changed after the tests commit.

### Task 5.2 mutation results

Each mutation was applied alone, and new tests failed for every one of them. Counts are
failing tests, as the owner reported them, in the order the task lists the mutations:

| Mutation | Failing tests |
|---|---|
| Drop the raise in `authorize` (3.2) | 16 |
| Move the raise from `authorize` to after `tool.run` in `gated_invoke` | 16 |
| Drop the tool-taint reason (3.3) | 8 |
| Drop the receipt `detail` (3.4) | 4 |
| Drop the fold in `_framed_turn`'s `finally` (4.2) | 6 |
| Drop the source resets in `_close_session` and `_start_event_session` together (4.1) | 1 |

The last row is caught only by the displacement test, as the corrected task 5.2 predicts:
a stale source is observable only when an incident re-taints a session, and each reset
alone is covered by the other. The gap was found while the tests were being written. A
throwaway implementation (never committed) showed that dropping the
`_start_event_session` reset alone survives.

## Handed to session-titles

These are recorded in NORTH-STAR row 4b (task 6.2):
- system-prompt wording for tool-raised taint;
- a live check that `can_use_tool` fires for a read-only `mcp__henk__` tool before any taint
  source ships;
- marking `sessions_read` only when it is built with titles rendering on;
- re-checking scope after a per-instance approval, including a taint source approved after
  its turn errored (design D3).
