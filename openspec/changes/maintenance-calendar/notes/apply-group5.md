# maintenance-calendar — apply notes, group 5 (window provider)

Tasks 5.1 and 5.2. Files: `henk/calendar/windows.py` (new),
`tests/test_calendar_windows.py` (new, 61 tests). Nothing else changed; nothing is wired
into `henk/runtime.py` (task 7.3). Line numbers cite the working tree at the time of
writing.

## API for groups 6 and 7

`MaintenanceWindows(source, config, *, audit_path, events_enabled, clock=time.time, sleep=asyncio.sleep)`
(`henk/calendar/windows.py:96-124`). `source` is the `CalendarClient` (anything with
`async list_window(time_min, time_max)`); `audit_path` is the audit **log** path, and the
snapshot path is derived from it (`snapshot_path`, `:65`), so nothing can place it
elsewhere. `events_enabled` is `config.events.enabled`. The constructor does no I/O.

| Call | Use |
|---|---|
| `load() -> bool` (`:184`) | Once at startup, synchronous, **before intake starts** (7.3). True when a snapshot was read, even a stale one. Never raises on a bad file. |
| `await run()` (`:233`) | Background task: refresh at once, then every `refresh_seconds`. Only cancellation stops it; it logs `calendar window provider stopped` and re-raises. Cancel it with the other App tasks (`henk/app.py:104-131`). |
| `await refresh() -> bool` (`:261`) | One refresh; `run()` calls it. True when the calendar was read. Never raises except `CancelledError`. |
| `active(t) -> tuple[MaintenanceWindow, ...]` (`:141`) | Group 6's hold test at publish time: windows with `start <= t < end + grace`, in start order. Group 6 filters out released and in-flight windows itself and picks the latest `end`. |
| `active_window(window_id, t) -> MaintenanceWindow \| None` (`:152`) | D8's extension check, first two conditions: the current snapshot has this id **and** it is active at `t`. The returned `end` is the calendar's current end (the new frozen end); a moved window comes back None. Group 6 still checks `start <= earliest held published_at`. |
| `ended_within(t, seconds=86400) -> tuple[...]` (`:166`) | D10: windows whose hold ended in `(t - seconds, t]`, "ended" meaning past `end + grace`. See note 1 for its limit. |
| `grace_seconds` (`:129`), `fetched_at` (`:134`) | Grace in seconds; the snapshot's fetch instant (None without one). |

`MaintenanceWindow(id, title, start, end)` (`:71`): `start`/`end` epoch seconds, `end`
**without** the grace. Every query answers "nothing" while the snapshot is absent or
stale; staleness is judged against the clock now, never against `t`.

## Decisions (each the option easiest to undo)

1. **`ended_within` cannot see 24 hours back.** The spec's list window starts at
   `now - (max_window_hours + grace_minutes)` (`:272`), and Google's `timeMin` filters
   on an event's end, so the snapshot holds only windows that ended within about 8.5 h
   of its fetch. Widening the list window would contradict the spec's SHALL. Also, the
   agent-core delta says "released within the last 24 hours" while design D10 says
   "ended": a released window has `hold_windows` rows, which carry the counts the note
   needs, and a window that held nothing is never "released". **For group 7:** take
   ended windows from `hold_windows.released_at`, not from this snapshot; the
   D10/agent-core wording difference is the orchestrator's to settle. `ended_within`
   is exposed as asked, with the limit in its docstring.
2. **Staleness boundary:** stale when `age > stale_after_seconds` (`:334`); exactly the
   limit is still usable ("older than"). A `fetched_at` in the future counts as fresh
   (the strict reading). A future stamp needs a backwards clock step; a successful
   refresh overwrites it within 5 minutes anyway.
3. **One stale period:** starts at the first query (or failed refresh) that finds the
   snapshot absent or stale, and ends only with a successful refresh (`:293`). The
   WARNING names the age (`the calendar snapshot is 7.0 h old (limit 6.0 h): holding no
   alerts until a refresh succeeds`) or says `no calendar snapshot`.
4. **A failed refresh also checks staleness** (`:325`). Without that, a quiet system
   with no events would never log that holding stopped.
5. **Refresh-failure logging:** WARNING for the first failure in a streak, INFO for
   the rest, INFO on recovery (`:313-325`). A Google outage logs about 3 lines, not
   72 over 6 hours. Any exception from the source, or from using its answer, is a failed
   refresh. An unexpected type is also logged at ERROR with its traceback (`:288`).
6. **A read that succeeded but could not be written** still replaces the snapshot in
   memory and ends the stale period. The file keeps its previous, whole content, and
   the failure is logged at ERROR naming the path (`:302`). The alternative (treat it as
   a failed refresh) is a two-line move.
7. **`load()` re-applies `holds()` under the current configuration** (`:216`, `:348`).
   Without that, a snapshot written while holding was on would keep holding for up to
   6 h after a restart with `hold_alerts: false`, events disabled or a lower
   `max_window_hours`, as long as Google stayed unreachable. That would break *An event
   holds alerts only under one predicate*.
8. **A malformed file is all-or-nothing** (`:385-415`): one bad window makes the whole
   file absent. A title without the `Homelab` prefix counts as malformed, because
   admission could never have written it. Booleans, NaN/infinity, integers too large
   for a float, invalid UTF-8 and deep nesting (RecursionError) are all refused. Logs
   name the path and the exception class, never the content. A missing file is INFO
   at load; the stale WARNING comes from the first query.
9. **File format:** exactly `{"fetched_at": <epoch>, "windows": [{"id", "title",
   "start", "end"}]}`, with epoch-second floats, no version key and mode 600. If the
   shape ever changes, an old file reads as malformed, so it fails open and the next
   refresh rewrites it.
10. **Atomic write is local** (`_write_atomically`, `:418`): a sibling temp file, fsync,
    `os.replace`, and the temp file removed on any failure. It mirrors
    `henk/replay/recorder.py:384`, which is not imported because that module imports
    `henk.agent.triage` and so the agent package; the calendar package stays
    import-light (`henk/calendar/__init__.py`). Folding both into one shared helper is
    a follow-up.
11. **The snapshot path lives in `windows.py`**, not as an `AuditConfig` property. That
    mirrors `runtime._checkpoint_path` (`henk/runtime.py:72`) and kept this group out
    of `henk/config.py`. The alternative is a three-line property beside
    `triage_recordings_dir` (`henk/config.py:636`).
12. **The clock is read once per refresh**, before the request. `fetched_at` is when
    the read began, so a slow read never looks fresher than the calendar it reflects.
13. **Synchronous file I/O on the event loop**, once per refresh, the same as the
    recorder. Cancellation can land only at the `list_window` await or the sleep, so
    it never interrupts a write.
14. **Admitted titles are not logged either.** The provider logs counts and paths only.
    Non-admitted items never reach this module: the client drops them where it reads
    them (`henk/calendar/client.py:230-253`).

## Spec scenario → test

| Scenario / SHALL | Test(s) |
|---|---|
| Grace extends the window (22:29 active, 22:30 not) | `test_grace_extends_the_window`, `test_the_window_opens_at_its_start`, `test_the_grace_follows_the_configuration` |
| Snapshot survives a restart | `test_snapshot_survives_a_restart`, `test_load_runs_outside_an_event_loop` |
| Stale snapshot fails open, one WARNING naming the age | `test_stale_snapshot_fails_open_with_one_warning`, `test_a_restart_with_a_stale_snapshot_fails_open`, `test_staleness_starts_strictly_after_the_limit`, `test_the_staleness_limit_follows_the_configuration` |
| One WARNING per stale period; a successful refresh ends it | `test_a_successful_refresh_ends_the_stale_period`, `test_no_snapshot_holds_nothing_with_one_warning`, `test_the_refresh_loop_reports_staleness_without_any_query` |
| Failed refresh keeps the previous snapshot | `test_failed_refresh_keeps_the_previous_snapshot`, `test_any_failure_keeps_the_snapshot`, `test_a_failed_refresh_does_not_renew_the_fetch_time`, `test_a_garbled_answer_is_a_failed_refresh` |
| Refresh every `refresh_seconds` over the specified list window | `test_refresh_lists_from_max_window_plus_grace_ago_to_the_lookahead`, `test_the_list_window_follows_the_configured_values`, `test_run_refreshes_at_once_then_every_refresh_seconds`, `test_fetched_at_is_the_instant_the_read_began` |
| Written atomically beside the audit log | `test_the_snapshot_sits_beside_the_audit_log`, `test_a_refresh_writes_the_snapshot_beside_the_audit_log`, `test_the_snapshot_is_replaced_only_by_renaming_a_sibling`, `test_a_failed_write_leaves_the_previous_file_whole[replace,fsync]`, `test_the_audit_directory_is_created_if_missing` |
| Non-admitted never persisted or logged (Pam's events, stranger's invitation) | `test_non_admitted_titles_never_reach_the_file_or_the_log` (real client over `httpx.MockTransport`) |
| All-day and over-long never hold; hold switched off | `test_over_long_and_all_day_events_never_reach_the_snapshot`, `test_nothing_is_a_window_when_holding_is_off`, `test_the_loaded_snapshot_obeys_the_current_predicate` |
| Corrupt or unreadable snapshot never crashes startup | `test_a_corrupt_snapshot_is_treated_as_absent` (17 cases), `test_an_unreadable_snapshot_is_treated_as_absent`, `test_a_missing_snapshot_is_absent_without_a_load_warning` |
| Refresh cancels cleanly | `test_cancelling_a_refresh_in_flight_leaves_the_snapshot_alone`, `test_cancelling_between_refreshes_is_clean` |
| D8 / D10 consumers | `test_active_window_finds_a_window_by_id_only_while_active`, `test_an_extended_window_is_seen_with_its_new_end`, `test_a_moved_window_is_not_active_now`, `test_ended_within_lists_windows_whose_hold_ended_recently` |

## Mutation table (tests: `test_calendar_windows.py`; `PYTHONDONTWRITEBYTECODE=1 python -B`, `-x`)

| # | Mutation | Result |
|---|---|---|
| 1 | end edge `<` → `<=` | killed: `test_grace_extends_the_window` |
| 2 | forget the grace in `active()` | killed: same |
| 3 | start edge `<=` → `<` | killed: `test_the_window_opens_at_its_start` |
| 4 | grace fixed at 30 min | killed: `test_the_grace_follows_the_configuration[0-…]` |
| 5 | no staleness check | killed: `test_stale_snapshot_fails_open_with_one_warning` |
| 6 | staleness `>` → `>=` | killed: `test_staleness_starts_strictly_after_the_limit` |
| 7 | stale limit fixed at 6 h | killed: `test_the_staleness_limit_follows_the_configuration` |
| 8 | WARNING per call | killed: `test_stale_snapshot_fails_open_with_one_warning` |
| 9 | refresh does not end the stale period | killed: `test_a_successful_refresh_ends_the_stale_period` |
| 10 | failed refresh clears the snapshot | killed: `test_snapshot_survives_a_restart` |
| 11 | failed refresh renews `fetched_at` | killed: `test_the_refresh_loop_reports_staleness_without_any_query` |
| 12 | unexpected error not caught in refresh | killed: `test_any_failure_keeps_the_snapshot` |
| 13 | non-atomic write (`write_bytes` on the target) | killed: `test_a_refresh_writes_the_snapshot_beside_the_audit_log` (mode 644), and the failed-write test |
| 14 | no fsync | killed: `test_a_failed_write_leaves_the_previous_file_whole[fsync]` |
| 15 | temp file not removed on failure | killed: `…[replace]` |
| 16 | temp file outside the audit dir | killed: `test_the_snapshot_is_replaced_only_by_renaming_a_sibling` |
| 17 | keep non-holding events | killed: `test_over_long_and_all_day_events_never_reach_the_snapshot` |
| 18 | `events_enabled` ignored | killed: `test_nothing_is_a_window_when_holding_is_off[events-disabled]` |
| 19-22 | list window: no grace / only grace / from now / lookahead in hours | killed: `test_refresh_lists_from_max_window_plus_grace_ago_to_the_lookahead` (4 mutations) |
| 23 | list window: fixed 14 days | killed: `test_the_list_window_follows_the_configured_values` |
| 24 | memory updated only when the write succeeds | killed: `test_the_snapshot_is_replaced_only_by_renaming_a_sibling`; also `test_a_failed_write_leaves_the_previous_file_whole[replace]` (checked with `-k`) |
| 25 | `load()` ignores the current predicate | killed: `test_the_loaded_snapshot_obeys_the_current_predicate` |
| 26-30 | decode: no prefix check / bool as number / non-finite / skip one bad window / missing id | killed: `test_a_corrupt_snapshot_is_treated_as_absent[…]` (5 mutations) |
| 31 | decode: `RecursionError` not caught | **survived — equivalent**: `load()`'s catch-all still treats the file as absent |
| 32 | load: no catch-all around decode | **survived — equivalent**: decode handles every known shape |
| 33 | neither layer guards (31 + 32) | killed: `test_a_corrupt_snapshot_is_treated_as_absent[[[[…]` |
| 34 | load: `OSError` not caught | killed: `test_an_unreadable_snapshot_is_treated_as_absent` |
| 35 | load: missing file warns | killed: `test_a_missing_snapshot_is_absent_without_a_load_warning` |
| 36 | refresh-failure WARNING every time | killed: `test_refresh_failures_warn_once_per_streak` |
| 37 | failed refresh does not report staleness | killed: `test_the_refresh_loop_reports_staleness_without_any_query` |
| 38 | run: sleep a fixed 300 s | killed: `test_run_refreshes_at_once_then_every_refresh_seconds` |
| 39 | run: sleep before the first refresh | killed: same |
| 40 | run: stop on an unexpected error | **survived — equivalent**: `refresh()` catches everything but cancellation (made true by the fix for 49), so the loop's guard is a second layer, kept for parity with `ReminderScheduler.run` |
| 41 | `active_window` ignores activity | killed: `test_stale_snapshot_fails_open_with_one_warning`; also `test_active_window_finds_a_window_by_id_only_while_active` (checked with `-k`) |
| 42-44 | `ended_within`: end without grace / `< t` / inclusive look-back | killed: `test_ended_within_lists_windows_whose_hold_ended_recently` (3 mutations) |
| 45 | `ended_within` ignores staleness | killed: `test_stale_snapshot_fails_open_with_one_warning` |
| 46 | `fetched_at` read after the list call | **survived on first run** (the fake clock never moved during a read). Closed: `test_fetched_at_is_the_instant_the_read_began`; killed on rerun |
| 47 | log the window titles | killed: `test_non_admitted_titles_never_reach_the_file_or_the_log` |
| 48 | snapshot in a different directory | killed: `test_the_snapshot_sits_beside_the_audit_log` |
| 49 | answer processed outside the guarded block | **a real defect, found by a probe test**: `refresh()` raised `AttributeError` on an unusable answer, contrary to its docstring. Test `test_a_garbled_answer_is_a_failed_refresh` written first (failed), then fixed by moving the processing into the `try`; the pre-fix code is now this mutant, killed |

49 mutations: 46 killed, 3 equivalent survivors (31, 32, 40, all redundant guard
layers). One test gap (46) and one implementation defect (49) were found and closed.
Mutant 24 was first written so that it broke encoding instead of reordering the write;
it was rewritten and rerun.

## Suite

`pytest -q`: 3987 passed, 10 skipped, 12 deselected (baseline 3926 + these 61), run
with `--ignore=tests/test_tools_calendar.py`. That file is group 3's, in progress in
parallel: at the time of the run it failed to collect because `henk.tools.calendar` did
not exist yet. My files pass the repo's pre-commit patterns, `.githooks/local-checks.sh`
and `gitleaks dir`.

## Handed on

- **Group 7 (7.3 wiring):** construct the provider only when `calendar.enabled`. Call
  `load()` before the coordinator task starts, then run `run()` as a task cancelled with
  the others. `App.__init__` (`henk/app.py:89-102`) has no slot for a third background
  task yet.
- **Group 7 (D10):** see decision 1. Ended windows and their counts come from
  `hold_windows`; the snapshot only covers active windows reliably.
- **Group 6 (D8):** under a stale snapshot `active_window` returns None, so no
  extension happens and the window is released at its frozen end, which is the
  fail-open direction.
