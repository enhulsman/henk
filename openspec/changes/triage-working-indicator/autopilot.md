# Autopilot — triage-working-indicator

## Base branch

`spec/event-triage-typing`, branched from `main` at `f9e6c47`. The spec (`9afabf3`) and the
failing tests (`0148d89`) are committed on it. Work on this branch; do not push.

## Owner decisions (2026-09-27)

The proposal's three decisions, all on the recommended picks: (1) only triages that will be
sent to the owner show typing; (2) the existing `signal.acknowledge_owner` flag controls it,
with no new key; (3) it ships on its own, not with roadmap item 5.

## Test command

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q -p no:cacheprovider
```

Run from the repo root. At handover: 16 failed, 3610 passed, 4 skipped. Done means 0 failed,
with every pre-existing test still passing. The 16 failing tests are all in
`tests/test_agent_core_acknowledgement.py` (the event-turn section) and
`tests/test_acknowledgement_guards.py::test_the_alert_cap_decides_which_triage_shows_the_indicator`.

## Paths Codex may edit

- `henk/**/*.py`

The expected change is small: `henk/agent/core.py` (`_process_event` enters `self._working()`
when `turn.announceable`, else `contextlib.nullcontext()`, from before `_start_event_session`
to after the proactive send; the stale comments at the `_working_indicator` attribute and the
`_working` docstring) and one comment in `henk/runtime.py`. See `tasks.md` group 3 and
`design.md` D1-D3.

## Paths Codex must not touch

- `tests/`
- `pyproject.toml`
- `conftest.py` (including `tests/conftest.py`)
- `openspec/`

## The promise

- A triage the alert cap has cleared for sending shows the owner's "typing" indicator from
  before the incident session starts until after the triage message or incomplete-triage
  notice is sent; a cap-suppressed triage shows none.
- Every exit other than cancellation stops the indicator: a completed triage, one that did
  not complete (error, refusal or no reply), and any exception inside the bracket, including
  while starting the incident session. No indicator task is left running.
- Cancellation, including one delivered at the bracket's entry, propagates out of the core
  worker, the indicator task is cancelled and awaited, and no stop is sent.
- Inside a sent triage the indicator refreshes every 7 s, pauses while an approval is
  pending, and resumes after it. A hung stop holds the next queued turn by at most one
  acknowledge timeout.
- With acknowledgement disabled (`working_indicator=None`), event turns are unchanged: the
  same session content, the same sends, and no indicator requests.
