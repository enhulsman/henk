# maintenance-calendar — apply notes, group 6 (hold and release)

Tasks 6.1–6.4. Not committed; `tasks.md` and `notes/apply-decisions.md` untouched.
Nothing is wired into `henk/runtime.py` (task 7.3) and `henk/agent/core.py` is
unchanged (group 7). Line numbers cite the working tree at the time of writing.

## Files

New:
- `henk/store/holds.py`: `HoldStore` over `hold_windows` and `held_events`.
- `henk/events/holds.py`: `publish_time`, `group_held`, `rebuild_event`, `HoldLedger`
  (holding, the in-flight set, `mark_released`).
- `henk/events/release.py`: `HoldReleaser` (the release task), `RELEASE_TICK_SECONDS`,
  `CATCH_UP_FALLBACK_SECONDS`.
- Tests: `tests/test_hold_store.py`, `tests/test_event_hold.py`,
  `tests/test_event_release.py`, `tests/test_event_catch_up.py`, and the shared rig
  `tests/hold_fixtures.py` (108 tests).

Changed:
- `henk/store/db.py`: the two tables (`:117-150`), `HOLD_WINDOW_COLUMNS` /
  `HELD_EVENT_COLUMNS` (`:185-206`) and the drift check for both (`:325-326`).
- `henk/store/factory.py`: `HenkStores.holds`, built whether or not the calendar is
  enabled (`:38`, `:76`); `henk/store/__init__.py` exports.
- `henk/events/types.py`: `Event.published_at` (default None).
- `henk/events/intake.py`: `events(on_subscribe=, on_keepalive=, on_unsubscribe=)`
  (`:163`; calls at `:196`, `:252`, and `:270` in a `finally` around the frame loop),
  `_notify` (`:321`), `published_at` from the frame's `time` (`:446`, `_frame_time`
  `:450`).
- `henk/events/coordinator.py`: classification (`_partition_holds` `:185`), catch-up
  state, `note_subscribe` / `note_keepalive` / `note_unsubscribe` and `caught_up`
  (`:84-143`), instance-level debounce batch and `dispatch_in_progress` in `run()`
  (`:272-331`), `submit_release` (`:243`).
- `henk/events/pipeline.py`: `evaluate(..., held_release=None)` passes it onto the
  turn (`:182-244`).
- `henk/agent/turns.py`: `HeldRelease` (`:38`) and `EventTurn.held_release` (`:89`).

## What group 7 must call

**The release sink.** `HoldLedger.mark_released(window_id: str) -> bool`
(`henk/events/holds.py:228`). Synchronous, never raises. True when this call marked
the window; False when it was already released (no record written) or the store
failed (window stays in flight until restart).

In `AgentCore._flush_event_triage` (`henk/agent/core.py:551`), beside the
`_handoff_sink` call (`core.py:565-567`, not `:567-568` as D8 cites):

```python
if turn.held_release is not None and self._release_sink is not None and (
    ok or self._audit is None
):
    self._release_sink(turn.held_release.window_id)
```

`ok or self._audit is None` because `_write_audit_record` returns `(False, …)` when
audit is not configured (`core.py:899-900`). The early return at `core.py:561` keeps it
from firing twice. A session-setup failure (`core.py:457`) never reaches the flush,
so the window stays in flight and unreleased until restart, as D8 requires.

**The descriptor.** `EventTurn.held_release: HeldRelease | None`
(`henk/agent/turns.py:38-89`): `window_id`, `title`, `start` (epoch), `end_with_grace`
(epoch: frozen end plus grace), `held`, `recovered` (distinct identities), a
`forwarded` property (`held - recovered`), and `hold_age_seconds` (release time minus
the frozen end, without grace; see decision 5). The release line uses `title`, `start`,
`end_with_grace`, `held`, `recovered`; the framing's >24 h rule uses
`hold_age_seconds`. Live turns carry None.

**D10 data.** In-progress windows with holds: `HoldStore.unreleased_windows()`
(`henk/store/holds.py:154`), counts via `group_held(HoldStore.held_events(id))`
(`henk/events/holds.py:70`, `henk/store/holds.py:166`). A "released within 24 h"
query does not exist yet: group 7 adds one (`SELECT … FROM hold_windows WHERE
released_at >= ?`; no index, the table is tiny).

## Constructors for 7.3

Only when `events.enabled` (the coordinator owns the pipeline):

```python
ledger = HoldLedger(stores.holds, HoldReceipts(audit))          # before AgentCore
core = AgentCore(..., release_sink=ledger.mark_released)        # group 7's new kwarg
windows = MaintenanceWindows(...) if config.calendar.enabled else None  # load() first
coordinator = EventCoordinator(intake, pipeline, core,
                               debounce_seconds=..., audit=audit,
                               holds=ledger, windows=windows)
releaser = HoldReleaser(ledger, coordinator,
                        grace_seconds=config.calendar.grace_minutes * 60,
                        windows=windows)
# App runs releaser.run() (and windows.run() when enabled) beside the coordinator.
```

`HoldLedger(store, receipts, *, clock=time.time)`;
`EventCoordinator(..., holds=None, windows=None, ...)` (both optional; neither means
the old behaviour exactly); `HoldReleaser(ledger, coordinator, *, grace_seconds,
windows=None, clock=time.time, sleep=asyncio.sleep, tick_seconds=60.0,
fallback_seconds=900.0)`. `MaintenanceWindows` applies `holds()` (hold_alerts,
events.enabled, max length) to its snapshot (group 5 decision 7), so it can be passed
whenever the calendar is enabled. Nothing needs wiring for catch-up: the
coordinator's pump hands intake its own callbacks (`coordinator.py:333-351`).

## Decisions (each the option easiest to undo)

1. **The keepalive signal is three callbacks on `EventIntake.events()`, passed by the
   coordinator's pump**, not constructor arguments. Design D8 names one callback,
   `on_keepalive`; the reset "on every (re)subscribe" and "this process's first
   subscribe attempt" also need a subscribe signal, hence `on_subscribe`, and the
   reset when a subscription ends needs `on_unsubscribe` (decision 2). Passing them
   from `_pump` means the runtime cannot miss or mis-wire them (a missed callback would
   silently degrade every release to the 15-minute fallback), and avoids a forward
   reference: intake is built before the coordinator (`runtime.py:412-421`). The
   ordering property D8 relies on holds: the generator reads the next frame only after
   the pump put the previous event on the queue, so `on_keepalive` runs after every
   earlier message was handed on. A callback that raises is logged and ignored.
2. **The keepalive evidence is cleared the moment a subscription ends, and again at
   the next subscribe** (orchestrator review; stricter than D8's "since the latest
   subscribe"). `on_unsubscribe` fires from a `finally` around the frame loop
   (`intake.py:270`), so it runs for every ending (stream error, liveness trip, clean
   end, since-rejection, consumer close, cancellation) and before the backoff sleep.
   Without it, `caught_up` stayed true through the reconnect backoff while the
   reconnect's `since=` backlog was unread: a release could fire early, and a
   `resolved` in that backlog would then arrive after its identity was triaged, a page
   for something that had recovered. **Remaining gap:** a half-open connection before
   the liveness trip (up to 135 s) is undetectable; until intake trips it, the last
   keepalive still counts, and an event published in that span for a window released
   in it is triaged live when it arrives (not lost; it can page).
3. **The in-flight set lives on `HoldLedger`**, which the coordinator consults, not on
   the coordinator object. The core's sink must exist before the coordinator does (the
   core is built at `runtime.py:221`, the coordinator at `:287`), so the object the
   sink calls has to be constructible first. Behaviour is the design's.
   `HoldReleaser` refuses a ledger its coordinator does not use (`release.py:74-80`):
   with two in-flight sets a submitted window would be released on every tick.
4. **Column names follow the design** (`start`, `end`), with `"end"` quoted in every
   statement since it is an SQL keyword. Because there is no migration mechanism, both
   tables get the same drift check as `reminders` and `handoffs` (`db.py:325-326`).
5. **`hold_age_seconds` = release time − frozen end** (without grace). That makes the
   framing's threshold literally D8's "a release more than 24 h after the window's end"
   and the incident-triage scenario "released 30 hours after its end". The alternative
   (since the earliest held publish time) is a one-line change in `release.py:135`.
6. **A failed release submission keeps the window in flight** (`coordinator.py:243-268`).
   Not covered by D8. Found by a test: `evaluate` has already armed the identities'
   cooldown, so a same-process retry would suppress them and mark the window released
   with nothing triaged. In flight, it is released again after a restart. In practice
   the submission is an unbounded queue put that only cancellation can interrupt.
7. **A release's suppression records do not gate marking released.** `dispatch_batch`
   withholds its marker when a suppression record fails, so the events replay; a
   release has no checkpoint to withhold, and not marking would re-evaluate every
   60 s. The record failure is logged by `AuditLog.write`.
8. **`hold-release` counts are recomputed from the rows inside the marking
   transaction** (`HoldStore.mark_released` returns them, `henk/store/holds.py:253`),
   not carried in memory from the release. An in-flight window takes no new rows, so
   they are the same, and the sink stays `mark_released(window_id)`; after a restart
   there is no memory to carry anyway.
9. **The duplicate check runs first inside the hold transaction too**
   (`henk/store/holds.py:178-238`), so a replay never creates a window row with no held
   events, and a released window takes nothing (`closed`).
10. **An event with an empty id is never held** and never counts as a duplicate: an
    empty primary key would make every later id-less event a "duplicate".
11. **Every read failure fails open**: the duplicate pre-check (replays then go on as
    new; the in-transaction check still drops them when a window is open), the window
    source's `active()`, the window rows, and the insert.
12. **The release task always ticks** (one cheap query) rather than starting and
    stopping with the rows; that satisfies "whenever held events are unreleased". One
    window's failure is logged and the others proceed; a whole tick's failure is
    logged and the loop continues.
13. **The startup tick cannot release anything**: before intake's first subscribe
    attempt neither `caught_up` nor the fallback can hold. The earliest release after a
    restart is the first tick after the first keepalive (about 45 s, per the probe).
14. **Grace comes from the current config at release time**, also with the calendar
    disabled (its defaults then). A grace changed between hold and release applies.
15. **"Latest" is publish time, ties in insertion order** (`ORDER BY published_at,
    rowid`): ntfy's `time` is whole seconds, and insertion order is its delivery order.
16. **Logs carry ids and identity keys only**, never a title or message, matching the
    audit records.

## Found and fixed outside the brief

- **`EventCoordinator.run` discarded a completed debounce batch** (before this change,
  `coordinator.py:119` ignored `Debouncer.feed`'s return value). `feed` closes its batch
  when an event's wall-clock arrival is more than the window past the batch's first
  arrival, while `run`'s deadline is monotonic and starts when the first event leaves
  the queue; queue latency or a clock step made them disagree, and the earlier events
  were silently dropped while the checkpoint advanced past them. Now kept and dispatched
  (`coordinator.py:316-331`); test
  `test_event_catch_up.py::test_a_debounce_split_never_drops_the_completed_batch`
  (fails on the old behaviour, mutation 37).

## For the orchestrator

- **Events disabled.** Release needs the coordinator's pipeline, which exists only
  when `events.enabled` (`runtime.py:287-291`). With events off nothing is held, and
  rows held earlier wait until events are enabled again. The spec says "whatever
  `calendar.enabled` says" and is silent on `events.enabled`.
- **No retention** for `held_events` / `hold_windows`. The duplicate check relies on
  rows only while ntfy can replay them; pruning released rows older than ntfy's cache
  is a follow-up.
- **Design citations re-checked.** D7/D8's coordinator lines (`:58-99`, `:96-99`,
  `:107-120`, `:140-151`) and `pipeline.py:242-252` were right before this change and
  have moved (now `coordinator.py:147-183`, `:178-183`, `:272-331`, `:333-351`;
  `pipeline.py:255`). `intake.py:227-228` and `:393-408` are now `:249-252` and
  `:432-447`. D8's `core.py:567-568` is `core.py:565-567`; `:561`, `:563`, `:457`,
  `:899-900` are right.

## Spec scenario → test

| Scenario | Test(s) |
|---|---|
| Alert during a window is held silently | `test_event_hold.py::test_alert_during_a_window_is_held_silently`, `test_the_hold_record_carries_no_payload_and_no_window_title` |
| Publish time decides after a reboot | `test_publish_time_decides_after_a_reboot`, `test_an_event_published_before_the_window_is_not_held_however_late_it_arrives`, `test_an_event_without_a_publish_time_is_keyed_on_arrival`, `test_event_catch_up.py::test_publish_time_comes_from_the_frame_time` (9 cases) |
| Mixed batch splits | `test_mixed_batch_splits` |
| Held-only batch advances the checkpoint | `test_held_only_batch_advances_the_checkpoint`, `test_held_only_batch_moves_the_real_checkpoint_past_its_last_event` |
| A hold that cannot be stored fails open | `test_a_hold_that_cannot_be_stored_fails_open`, `test_an_unusable_store_fails_open_for_every_event`, `test_a_failed_hold_record_keeps_the_hold` |
| Duplicate replay is dropped and still advances | `test_duplicate_replay_is_dropped_and_still_advances`, `..._after_release_...`, `test_a_duplicate_inside_one_batch_is_held_once`, `test_duplicates_are_dropped_with_the_calendar_disabled` |
| Late event for a released window is triaged live | `test_late_event_for_a_released_window_is_triaged_live`, `test_a_released_window_does_not_take_a_hold_from_an_overlapping_open_one` |
| Event during an in-flight release is triaged live | `test_event_during_an_in_flight_release_is_triaged_live` |
| No cooldown armed / no cap (SHALL NOT) | `test_alert_during_a_window_is_held_silently`, `test_held_events_charge_no_cap` |
| incident-triage: No triage while held | `test_no_triage_session_exists_while_held` (real `AgentCore`) |
| Recovered alerts send nothing | `test_event_release.py::test_recovered_alerts_send_nothing`, `..._through_the_real_core` |
| Still-firing alert is triaged after the window | `test_still_firing_alert_is_triaged_after_the_window`, `test_forwarded_event_is_the_latest_non_resolved_one`, `test_forwarded_event_is_rebuilt_from_the_stored_fields`, `test_forwarded_events_reach_the_pipeline_in_publish_order` |
| Release waits for the backlog after a long outage | `test_release_waits_for_the_backlog_after_a_long_outage` (coordinator `run()` driven step by step) |
| Message frames alone do not mean caught up | `test_message_frames_alone_do_not_mean_caught_up`, `test_event_catch_up.py::test_on_keepalive_fires_for_keepalive_frames_only`, `test_real_intake_message_frames_alone_are_not_caught_up` |
| Dispatch in progress blocks release | `test_dispatch_in_progress_blocks_release`, `test_dispatch_in_progress_is_cleared_when_dispatch_raises` |
| Unreachable ntfy cannot hold alerts forever | `test_unreachable_ntfy_cannot_hold_alerts_forever[900, 300]`, `test_the_fallback_counts_from_the_end_plus_grace_when_that_is_later[900, 300]`, `test_the_default_fallback_is_fifteen_minutes`, `test_without_any_subscribe_attempt_the_fallback_never_fires` |
| Crash before the triage is durable releases again | `test_crash_before_the_triage_is_durable_releases_again` (real core and `OffsetCheckpoint`; checkpoint bytes compared), `test_a_failed_submission_keeps_the_window_for_the_next_restart` |
| Release survives a restart | `test_release_survives_a_restart` |
| Extended window defers release | `test_extended_window_defers_release`, `test_extension_is_seen_through_the_real_window_provider`, `test_a_window_starting_exactly_at_the_earliest_hold_is_an_extension` |
| Moved window is not an extension | `test_moved_window_is_not_an_extension`, `test_a_window_now_starting_after_its_earliest_hold_is_not_an_extension` |
| Release obeys cooldown and the daily cap | `test_release_obeys_cooldown_and_the_daily_cap`, `test_a_release_whose_every_identity_cools_down_marks_itself` |
| Release runs with the calendar disabled | `test_release_runs_with_the_calendar_disabled`, `test_nothing_is_released_before_the_stored_end_plus_grace[disabled-*]` |
| Evaluated at startup and every 60 s | `test_the_release_task_ticks_at_startup_then_every_interval[60, 17]` (sleep spy), `test_a_failing_tick_does_not_stop_the_release_task` |
| Caught up resets per subscribe | `test_a_keepalive_before_the_latest_subscribe_does_not_count`, `test_event_catch_up.py::test_real_intake_a_reconnect_voids_the_earlier_keepalive`, `test_on_subscribe_runs_before_every_subscribe_attempt` |
| Caught up is void once a subscription ends (review) | `test_event_catch_up.py::test_on_unsubscribe_runs_when_a_subscription_ends_before_any_backoff[error, eof, liveness, since-rejected]`, `test_a_release_tick_during_the_backoff_after_a_stream_error_finds_nothing` (real intake, gated backoff; a keepalive on the new subscription restores it), `test_note_unsubscribe_clears_catch_up`, `test_event_release.py::test_an_ended_subscription_blocks_release_until_the_next_keepalive` |
| audit-log: one `hold-release` per window; no second `hold` | `test_a_second_mark_writes_no_second_hold_release`, `test_hold_store.py::test_marking_released_is_guarded_and_returns_the_rows_once`, `test_a_duplicate_event_id_inserts_nothing` |

Bounds are asserted on the configured value, not "eventually": the fallback at
`first + fallback - 1` (not released) and `first + fallback` (released) for 900 s and
a configured 300 s; the grace at `end + grace - 1` / `end + grace` for 30 and 10 min;
the tick via a sleep spy recording `[60, 60, 60]` and, configured, `[17, 17, 17]`.

## Mutation table

Run in a scratch copy of the tree (`PYTHONDONTWRITEBYTECODE=1 python -B`, `pytest -x`
over the four new test files plus `test_event_coordinator.py`, `test_event_intake.py`,
`test_event_pipeline.py`). Rows 1-24 cover every item on the brief's list, some split
into variants (10, 13, 17, 20 are such variants); 25-62 are additional. Rows 1-62 ran
in full before the review change (subscription-end reset); the catch-up rows 11-13
and the new 63-66 ran after it.

| # | Mutation | Result |
|---|---|---|
| 1 | return early on a held-only batch | killed: `test_event_hold.py::test_held_only_batch_advances_the_checkpoint` |
| 2 | key on arrival time instead of publish time | killed: `test_event_hold.py::test_publish_time_decides_after_a_reboot` |
| 3 | hold against the earliest-ending window | killed: `test_event_hold.py::test_held_against_the_open_window_with_the_latest_end` |
| 4 | treat released windows as open | killed: `test_event_hold.py::test_a_released_window_does_not_take_a_hold_from_an_overlapping_open_one` |
| 5 | treat in-flight windows as open | killed: `test_event_hold.py::test_event_during_an_in_flight_release_is_triaged_live` |
| 6 | skip the duplicate check (coordinator) | killed: `test_event_hold.py::test_duplicate_replay_after_release_is_dropped_and_still_advances` |
| 7 | skip the duplicate check (store) | killed: `test_hold_store.py::test_a_duplicate_event_id_inserts_nothing` |
| 8 | arm cooldown / charge cap for held events | killed: `test_event_hold.py::test_alert_during_a_window_is_held_silently` |
| 9 | fail closed on a hold storage failure | killed: `test_event_hold.py::test_a_hold_that_cannot_be_stored_fails_open` |
| 10 | fail closed on a duplicate-read failure | killed: `test_event_hold.py::test_an_unusable_store_fails_open_for_every_event` |
| 11 | count message frames as keepalives | killed: `test_event_catch_up.py::test_on_keepalive_fires_for_keepalive_frames_only` |
| 12 | not reset the keepalive on resubscribe (coordinator) | killed: `test_event_release.py::test_a_keepalive_before_the_latest_subscribe_does_not_count` |
| 13 | not signal resubscribes (intake: first subscribe only) | killed: `test_event_catch_up.py::test_on_subscribe_runs_before_every_subscribe_attempt` |
| 14 | ignore dispatch_in_progress | killed: `test_event_release.py::test_dispatch_in_progress_blocks_release` |
| 15 | clear dispatch_in_progress before the submit await | killed: `test_event_release.py::test_dispatch_in_progress_blocks_release` |
| 16 | fallback measured from the window end only | killed: `test_event_release.py::test_unreachable_ntfy_cannot_hold_alerts_forever[900.0]` |
| 17 | fallback measured from the first subscribe only | killed: `test_event_release.py::test_the_fallback_counts_from_the_end_plus_grace_when_that_is_later[900.0]` |
| 18 | extension without the start <= earliest-published check | killed: `test_event_release.py::test_a_window_now_starting_after_its_earliest_hold_is_not_an_extension` |
| 19 | forward/judge an identity by its first event, not its latest | killed: `test_event_release.py::test_recovered_alerts_send_nothing` |
| 20 | forwarded events not in publish order | killed: `test_event_release.py::test_forwarded_events_reach_the_pipeline_in_publish_order` |
| 21 | pass an offset on the release batch | killed: `test_event_release.py::test_still_firing_alert_is_triaged_after_the_window` |
| 22 | mark released at submission | killed: `test_event_hold.py::test_late_event_for_a_released_window_is_triaged_live` |
| 23 | allow a second hold-release (no released_at guard) | killed: `test_hold_store.py::test_marking_released_is_guarded_and_returns_the_rows_once` |
| 24 | release only when the calendar is enabled | killed: `test_event_release.py::test_nothing_is_released_before_the_stored_end_plus_grace[disabled-1800.0]` |
| 25 | release before end + grace (end only) | killed: `test_event_release.py::test_nothing_is_released_before_the_stored_end_plus_grace[event deleted-1800.0]` |
| 26 | due boundary < -> <= | killed: `test_event_hold.py::test_late_event_for_a_released_window_is_triaged_live` |
| 27 | fallback boundary <= -> < | killed: `test_event_release.py::test_unreachable_ntfy_cannot_hold_alerts_forever[900.0]` |
| 28 | fallback hard-coded at 15 min | killed: `test_event_release.py::test_unreachable_ntfy_cannot_hold_alerts_forever[300.0]` |
| 29 | fallback without a subscribe attempt fires | killed: `test_event_release.py::test_release_waits_for_the_backlog_after_a_long_outage` |
| 30 | tick interval hard-coded at 60 s | killed: `test_event_release.py::test_the_release_task_ticks_at_startup_then_every_interval[17.0]` |
| 31 | sleep before the first tick | killed: `test_event_release.py::test_the_release_task_ticks_at_startup_then_every_interval[60.0]` |
| 32 | a failing tick stops the release task | killed: `test_event_release.py::test_a_failing_tick_does_not_stop_the_release_task` |
| 33 | one failing window stops the tick | killed: `test_event_release.py::test_one_failing_window_does_not_block_another` |
| 34 | caught_up ignores the pump queue | killed: `test_event_catch_up.py::test_an_event_waiting_in_the_pump_queue_blocks_catch_up` |
| 35 | caught_up ignores the open debounce batch | killed: `test_event_catch_up.py::test_an_open_debounce_batch_blocks_catch_up` |
| 36 | caught_up ignores the keepalive | killed: `test_event_release.py::test_release_waits_for_the_backlog_after_a_long_outage` |
| 37 | debounce split drops the completed batch | killed: `test_event_catch_up.py::test_a_debounce_split_never_drops_the_completed_batch` |
| 38 | release suppressions not recorded | killed: `test_event_release.py::test_release_obeys_cooldown_and_the_daily_cap` |
| 39 | in-flight not set at submission | killed: `test_event_hold.py::test_event_during_an_in_flight_release_is_triaged_live` |
| 40 | tick ignores the in-flight set | killed: `test_event_release.py::test_a_failed_submission_keeps_the_window_for_the_next_restart` |
| 41 | mark_released keeps the window in flight | killed: `test_event_release.py::test_still_firing_alert_is_triaged_after_the_window` |
| 42 | a failed mark takes the window out of flight | killed: `test_event_release.py::test_a_failed_mark_keeps_the_window_in_flight` |
| 43 | hold record not written | killed: `test_event_hold.py::test_alert_during_a_window_is_held_silently` |
| 44 | duplicate treated as not held | killed: `test_event_hold.py::test_a_duplicate_inside_one_batch_is_held_once` |
| 45 | recovered if any held event resolved | killed: `test_event_release.py::test_forwarded_event_is_the_latest_non_resolved_one` |
| 46 | frozen end not frozen (INSERT OR REPLACE) | killed: `test_hold_store.py::test_first_hold_inserts_the_window_with_its_end_frozen` |
| 47 | extension does not persist the new end | killed: `test_event_release.py::test_extended_window_defers_release` |
| 48 | hold age measured from end + grace | killed: `test_event_release.py::test_still_firing_alert_is_triaged_after_the_window` |
| 49 | no-turn release never marked | killed: `test_event_release.py::test_recovered_alerts_send_nothing` |
| 50 | intake never sets published_at | killed: `test_event_catch_up.py::test_publish_time_comes_from_the_frame_time[1794078000-1794078000.0]` |
| 51 | frame time accepts booleans | killed: `test_event_catch_up.py::test_publish_time_comes_from_the_frame_time[True-None]` |
| 52 | frame time accepts non-finite | killed: `test_event_catch_up.py::test_publish_time_comes_from_the_frame_time[nan-None]` |
| 53 | event without an id can be held | killed: `test_event_hold.py::test_an_event_without_an_id_is_never_held` |
| 54 | callback exception kills intake | killed: `test_event_catch_up.py::test_a_failing_callback_never_kills_intake` |
| 55 | unreleased_windows includes released ones | killed: `test_hold_store.py::test_unreleased_windows_lists_only_windows_with_held_rows` |
| 56 | pipeline drops held_release | killed: `test_event_hold.py::test_event_during_an_in_flight_release_is_triaged_live` |
| 57 | release extension uses the snapshot even when not active | killed: `test_event_hold.py::test_late_event_for_a_released_window_is_triaged_live` |
| 58 | held rows ordered by arrival only (rowid) | killed: `test_hold_store.py::test_held_events_are_ordered_by_publish_time_then_arrival_order` |
| 59 | held rows ordered by publish time only, newest tie first | killed: `test_hold_store.py::test_held_events_are_ordered_by_publish_time_then_arrival_order` |
| 60 | held_events not part of the drift check | killed: `test_hold_store.py::test_a_drifted_table_refuses_to_start_naming_the_column[held_events-columns1]` |
| 61 | release turn not routed through the in-flight close (dispatch sees released only) | killed: `test_event_hold.py::test_event_during_an_in_flight_release_is_triaged_live` |
| 62 | releaser accepts a ledger the coordinator does not use | killed: `test_event_release.py::test_the_releaser_refuses_a_ledger_the_coordinator_does_not_use` |
| 63 | drop the reset when a subscription ends (intake) | killed: `test_event_catch_up.py::test_on_unsubscribe_runs_when_a_subscription_ends_before_any_backoff[error]` |
| 64 | `note_unsubscribe` is a no-op | killed: `test_event_release.py::test_an_ended_subscription_blocks_release_until_the_next_keepalive` |
| 65 | `on_unsubscribe` not wired in the pump | killed: `test_event_release.py::test_an_ended_subscription_blocks_release_until_the_next_keepalive` |
| 66 | reset fired after the backoff (outer `finally` instead of the frame loop's) | killed: `test_event_catch_up.py::test_on_unsubscribe_runs_when_a_subscription_ends_before_any_backoff[error]` |

66 of 66 killed (rows 63-66 added with the review change; 11-13 rerun after it, still
killed). History, all closed before the final run:

- **First run, 58 of 60.** *Forward the latest event instead of the latest non-resolved*
  survived as a provably equivalent mutant: an identity is forwarded only when its latest
  event is not `resolved`, so its latest event IS its latest non-resolved one. The redundant
  branch was removed from `group_held` (`henk/events/holds.py:70`); rows 19-20 are the
  meaningful mutants of what remains (judge by the first event; reverse the order).
- *Release due at the window end instead of end + grace* survived: the extension check
  masked it whenever the window was still in the snapshot. A real gap (calendar disabled,
  or the event deleted mid-window). Closed by
  `test_nothing_is_released_before_the_stored_end_plus_grace[unchanged|event deleted|disabled]` (row 25).
- **Second run.** *Forwarded events not in publish order* survived; closed by
  `test_forwarded_events_reach_the_pipeline_in_publish_order` (row 20).
- A test written before the mutation runs (`test_a_failed_submission_...`) exposed decision 6:
  a same-process retry of a failed submission would have marked the window released with
  nothing triaged.

## Suite

`pytest -q`: 4261 passed, 11 skipped, 12 deselected (baseline 4153 + these 108). New
files pass the pre-commit hook's pattern layer and `.githooks/local-checks.sh` over the
added lines, and `gitleaks dir` reports nothing in them.
