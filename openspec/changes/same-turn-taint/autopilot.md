# Autopilot — same-turn-taint

Implementation brief for the implementing agent. The proposal, design, delta specs and
tests are approved and committed; implement against them, tasks 2-5 in `tasks.md`.

## Base branch

`main`. Work on branch `spec/same-turn-taint`, which was cut from `main` at `f9e6c47`
(`main` has since moved on; do not rebase or merge unless the owner asks).

## Test command

```
uv run pytest -q
```

Done means the whole suite is green, including all 19 tests in
`tests/test_same_turn_taint.py`, with nothing under `tests/` changed.

## Paths you may edit

- `henk/**/*.py`

## Paths you must not touch

- `tests/`
- `pyproject.toml`
- `conftest.py` (any, including `tests/conftest.py`)
- `openspec/`

If a test looks wrong, stop and report it; do not edit it. Report the task 5.2 mutation
results in your final message: task ticks and `notes/apply-decisions.md` are recorded by
the coordinating session.

## The promise

- A tool call to a taint source (`raises_taint`, read from the registered tool instance)
  taints the turn when the gate authorizes it, before the tool runs, so any later
  `store_memory` or `capture` in that turn is refused `out-of-scope`, audited with
  `detail` `taint raised by <tool>`, with no channel message and the stores unchanged.
- The refusal gives the model the exact tool-taint reason from the approval-gate spec;
  incident-tainted sessions and event turns keep their existing reason and a null
  `detail`.
- The session stays tainted, with its source, for its lifetime and on every exit path,
  including a turn that errors; `/new`, idle expiry and an incident displacing the session
  clear it, and the incident session carries no tool source.
- Owner commands (`/remember`, `/capture`, `/remind`) still write in a tool-tainted session.
- Turns in which no taint source is called behave exactly as today, and no production tool
  declares `raises_taint`.
