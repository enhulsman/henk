# maintenance-calendar — apply notes, group 7 (agent core and triage)

Tasks 7.1–7.3. Not committed; `tasks.md` and `notes/apply-decisions.md` untouched.
Where a choice was open, the option easiest to undo was taken and is named here.
Line numbers cite the working tree at the time of writing.

## Files

New:
- `henk/agent/maintenance.py`: `release_line` (`:74`), `MaintenanceNote` (`:110`),
  `MAINTENANCE_BEGIN`/`MAINTENANCE_END` (`:40-41`), `RELEASED_NOTE_SECONDS` (`:44`).
- Tests: `tests/test_agent_core_maintenance.py`, `tests/test_maintenance_note.py`,
  `tests/test_hold_framing.py`, `tests/test_maintenance_runtime.py`,
  `tests/test_replay_calendar.py` (107 tests).

Changed:
- `henk/agent/core.py`: kwargs `release_sink`, `maintenance`, `owner_zone`
  (`:213-215`, stored `:300-306`); owner-turn composition (`:421-430`,
  `_with_maintenance` `:708`); the release line on the proactive send (`:572`,
  `_with_release_line` `:638`); the sink in `_flush_event_triage` (`:600-605`,
  `_mark_released` `:629`).
- `henk/agent/triage.py`: `STALE_HOLD_SECONDS` (`:274`), `_hold_note` (`:277`),
  appended after the method framing and before the recurrence note (`:345`).
- `henk/store/holds.py`: `HoldStore.released_since` (`:166`).
- `henk/runtime.py`: one calendar client (`:136`), the reference tap as the gate's
  recorder (`:137-140`, `:181`), provider built and `load()`-ed (`:255-262`), the
  ledger (`:267`), core kwargs (`:335-339`), coordinator with holds and windows
  (`:348`), the releaser (`:357`), App slots (`:388-389`), `_maintenance_note`
  (`:395`).
- `henk/app.py`: `windows` and `releaser` slots (`:100-101`), started and cancelled
  with the other background tasks (`:115`).
- `henk/replay/__main__.py`: the key popped right after the config is read, for
  every command (`:324`).

## Design citations re-checked

D8 cites `core.py:561`, `:563`, `:567-568`, `:457`, `:899-900`. Before this group they
were right except `:567-568` (group 6 already noted `:565-567`). After it: the early
return is `core.py:587`, `_write_audit_record` `:589`, the handoff sink `:591-593`,
the release sink `:600-605`, session setup `await self._start_event_session(turn)`
`:481` (was `:457`; it is outside any try, so a raise never reaches the flush), the
no-audit `(False, handoff_id)` `:991` (was `:900`). D10's composition order cites
`core.py:400-405`; it is now `:421-430`.

## Decisions (each the option easiest to undo)

1. **Calendar disabled, released windows in the store: they are shown.** The note is
   built whenever an owner zone is configured (`runtime.py:395-413`), with the
   provider only when the calendar is enabled. The agent-core delta's SHALL ("when
   ... one was released within the last 24 hours, every owner agent turn") has no
   calendar condition, and release itself runs with the calendar off (D8), so the
   owner who just got a release line can ask about it. Active windows need the
   provider, so none show while disabled. With no rows the note composes nothing and
   owner turns are byte-identical. To undo: return None from `_maintenance_note`
   when `windows is None` (mutant W14 shows the tests that pin this).
2. **No owner zone, no note.** The shared renderer needs a zone; the calendar cannot
   be enabled without one, so this only affects deploys that never enabled it.
3. **The release line without an owner zone is UTC and says so**
   (`(Held during "…", 19:00–21:30 UTC: 1 alert.)`, `maintenance.py:83-84`): rows
   held while enabled and released after a rollback that also dropped
   `owner.timezone`. An unlabelled UTC time would read as local.
4. **The block carries one framing sentence** after the BEGIN marker (what "held"
   means, that counts are distinct alerts, that it is a record), beyond D10's
   example, which shows only the window line. Without it the model sees "3 alerts
   held so far" with no idea that held alerts are untriaged. One constant,
   `maintenance.py:46`.
5. **Two states only.** A window past its grace but not yet released (catch-up
   pending, in flight, or the 15-minute fallback) is not in the block: the spec
   names "in progress" and "ended", and a third state is a new promise. Gap: for up
   to about 15 minutes after the grace, "how did the patch round go?" finds no
   block. Adding a "release pending" line is additive.
6. **A released window the calendar still lists as active reads as ended**
   (extended after its release; `maintenance.py:150-161`): it holds nothing any
   more, so "in progress; alerts held until …" would be false.
7. **"Within the last 24 hours" is strict**: `released_at > now − 86400`
   (`holds.py:166`); a release exactly 24 h ago is out.
8. **An active window that held nothing shows `0 alerts held so far`**, and one
   alert is singular. The spec requires the block whenever a window is active.
9. **Titles.** In the block and the hold framing: `neutralise_markers` then the
   calendar tool's `render_title` (delimiter strip until none remain, whitespace
   collapsed, 120-character cap). Admitted titles are owner-reviewed, but the
   block's own END marker and the event turn's markers must not be forgeable. In
   the release line: whitespace flattened and capped at 120, inside the spec's
   quotes, no delimiters (it is an owner-facing Signal line).
10. **Hold framing** sits after the method framing and before the recurrence note
    (`triage.py:337-348`), outside the untrusted block. Its times are UTC stamps in
    the incident headers' format (`_render_epoch`), so the session compares like
    with like; the spec requires the shared renderer only for the owner block. The
    age is whole hours, floored ("30 hours"); stale strictly above 24 h
    (`triage.py:300`), matching "exceeds".
11. **The release line's counts** use the plural helper in both variants. With
    recoveries, a turn exists only if at least one identity was forwarded, so
    `held ≥ 2` and the text is the spec's `<held> alerts`.
12. **The sink is guarded** (`core.py:629-636`): a raising sink is logged and the
    triage message still goes out. It fires for an errored triage whose record was
    written (D8 keys on the record, and an errored triage advances like any other,
    agent-core D1) and for a non-announceable one (nothing is sent; the record is
    durable). A release line that cannot be composed (an unrepresentable instant) is
    logged and left off; the message still goes out (`core.py:638-656`).
13. **Provider built only when `calendar.enabled` AND `events.enabled`**
    (orchestrator decision after review, replacing "whenever `calendar.enabled`";
    `runtime.py:255`). With events off `holds()` is false for every event, so the
    snapshot would always be empty and the provider would poll Google every 5
    minutes for nothing; `calendar_upcoming` reads live and does not need it. With
    the provider absent the owner-turn block shows no active windows (as with events
    off before), and the coordinator and releaser do not exist anyway. Tested by
    `test_calendar_on_events_off_builds_no_provider_but_keeps_the_tools`: no
    provider, no background task, no snapshot file, no request at startup, both
    tools still registered.
14. **Releaser started whenever events are enabled**, calendar on or off, rows or
    not (group 6 decision 12: always ticks). With the calendar disabled and no rows
    it is one `SELECT` on `hold_windows` per minute and does nothing.
15. **What "unchanged" covers** with `calendar.enabled` false and no held rows
    (`test_disabled_with_no_held_rows_changes_nothing_the_owner_or_google_sees`):
    registry tool names equal those built from the same config with no `calendar`
    section; the system prompt equals that config's and never says "calendar"; the
    gate records into the core's own `MutationReceipts` (no tap); no calendar client,
    no provider, no snapshot file, no window source on the coordinator; owner-turn
    content byte-identical; no request to any host during a release tick. **Not**
    unchanged: with events enabled, App runs one more background task (the
    releaser), which ticks every 60 s, finds nothing, submits, writes and marks
    nothing.
16. **The key pop.** `build_runtime` already popped first (`runtime.py:100`). Added a
    test that stands in a fake `claude_agent_sdk` module and observes the key absent
    both when `ClaudeSDKClient` is constructed and when it connects (where the real
    one spawns the CLI), for both factories, plus an enabled-calendar variant of the
    factory-construction spy. The replay pops right after `load_config()` and before
    the audit-log guard, for every command, including a refused one
    (`replay/__main__.py:324`); a test checks the key is still there when the loader
    runs, so the real loader can still decode it.
17. **`App` starts its background tasks in one loop** (`app.py:113-122`); behaviour
    for the coordinator and scheduler is unchanged.

## Open points handed on

- **Group 6's "events disabled" note stands**: with `events.enabled` false there is
  no ledger, coordinator or releaser (`runtime.py:267`, `:357`), so held rows wait.
- Decisions 1 (block with the calendar off: confirmed as built) and 13 (provider
  gated on events: changed as directed) were settled in the orchestrator's review.

## For task 8.4 (live checks on rp5)

- **Where to look for the key.** `/proc/<pid>/environ` of the Python process shows
  its *initial* environment, so it still lists `GOOGLE_CALENDAR_SA_KEY_B64` after
  the pop; that is not a leak. The check that matters is the CLI child the SDK
  spawns per session: count, never print, e.g.
  `tr '\0' '\n' < /proc/<cli pid>/environ | grep -c '^GOOGLE_CALENDAR_SA_KEY_B64='`
  must be 0. Same for a replay run.
- **Startup order.** Logs show `loaded the calendar snapshot: …` or `no calendar
  snapshot at … yet` before the first intake subscribe line, then `calendar window
  provider started`.
- **The release line** in the 10-minute test window: the triage message ends with a
  blank line and `(Held during "<title>", HH:MM–HH:MM: …)` in CET, the second time
  being end plus grace; nothing at all when everything recovered. The earliest
  release after a restart is the first tick after the first keepalive (~45 s).
- **The block**: during the window, ask "how many alerts are held so far?" (only
  the block carries counts; `calendar_upcoming` does not), and after the release
  "how did the patch round go?".
- **Audit**: one `hold` per held event, one `hold-release` per window, written after
  the release triage's session record.
- **Replay** with the calendar enabled: `python -m henk.replay list` starts (the
  config decodes the key in the replay container's environment).

## Spec scenario → test

| Scenario / SHALL | Test(s) |
|---|---|
| agent-core: Owner asks during a patch round | `test_maintenance_note.py::test_owner_asks_during_a_patch_round`, `::test_the_owner_turn_carries_the_block_between_the_header_and_the_text` (real core, real store) |
| agent-core: Ended window remembered for a day | `::test_ended_window_remembered_for_a_day`, `::test_an_ended_window_is_shown_for_24_hours[…]` |
| agent-core: Calendar trouble never blocks a turn | `test_agent_core_maintenance.py::test_calendar_trouble_never_blocks_a_turn`, `test_maintenance_note.py::test_read_failures_propagate_so_the_core_can_log_them` |
| block after the time header, before recall | `::test_the_block_sits_after_the_time_header_and_before_recall`, `::test_without_a_time_header_the_block_comes_first` |
| composed per turn | `::test_the_block_is_composed_per_turn` |
| does not taint / event turns never carry it | `::test_the_block_never_taints_the_session`, `::test_event_turns_never_carry_the_block` |
| no payload text; titles delimited | `test_maintenance_note.py::test_the_block_contains_no_alert_payload_text`, `::test_the_title_cannot_close_the_block_or_its_delimiters` |
| times through the shared renderer | `::test_times_render_through_the_shared_renderer_in_the_owner_zone` (hostile process zones) |
| agent-core: Release line with recoveries | `test_agent_core_maintenance.py::test_release_line_with_recoveries` |
| agent-core: Release line without recoveries (singular, plural) | `::test_release_line_without_recoveries_singular`, `::…_plural` |
| after the suppressed-count note | `::test_the_line_comes_after_the_suppressed_count_note` |
| agent-core: Incomplete triage still carries the line | `::test_incomplete_triage_still_carries_the_line`, `::test_incomplete_triage_carries_the_note_then_the_line` |
| non-announceable released turn sends nothing | `::test_a_non_announceable_released_turn_sends_nothing`, `::…_incomplete_…` |
| agent-core: Live turns carry no release line | `::test_live_turns_carry_no_release_line`, `::test_a_live_turn_with_a_suppressed_note_ends_with_the_note`, `::test_a_live_incomplete_notice_carries_no_line` |
| composed by code; HH:MM in the owner zone | `::test_the_line_is_composed_by_code_never_from_the_reply`, `::test_the_times_are_compact_hh_mm_in_the_owner_zone[…]` |
| D8 sink: on ok, on no audit; never on failure, live, twice, setup failure | `::test_the_sink_fires_once_the_triage_record_is_written`, `::…_does_not_fire_when_the_record_could_not_be_written`, `::…_fires_at_once_when_audit_is_not_configured`, `::…_never_fires_for_a_live_turn`, `::…_fires_at_most_once_per_triage`, `::…_never_fires_when_session_setup_failed` |
| incident-triage: Released triage is framed by its window | `test_hold_framing.py::test_released_triage_is_framed_by_its_window`, `::test_the_core_hands_the_session_the_window_framing` |
| incident-triage: Old hold is framed as possibly stale | `::test_an_old_hold_is_framed_as_possibly_stale[…]`, `::test_the_stale_note_states_the_age` |
| live framing unchanged | `::test_live_turns_are_framed_exactly_as_before` |
| maintenance-calendar: Registry unchanged when disabled | `test_maintenance_runtime.py::test_disabled_with_no_held_rows_changes_nothing_the_owner_or_google_sees` |
| event-intake: Release runs with the calendar disabled (wired) | `::test_disabled_with_held_rows_the_releaser_still_releases`, `::test_disabled_an_all_recovered_window_is_marked_released_by_the_releaser` |
| provider loaded before intake starts | `::test_the_snapshot_is_loaded_before_intake_starts` |
| no provider with events off; tools still registered | `::test_calendar_on_events_off_builds_no_provider_but_keeps_the_tools` |
| one client; one ledger; tap as recorder | `::test_one_calendar_client_serves_both_tools_and_the_provider`, `::test_the_hold_chain_runs_through_one_ledger`, `::test_the_gate_records_through_the_approval_reference_tap` |
| secure-deployment: key not inherited (runtime) | `::test_the_key_is_gone_before_any_sdk_client_exists`, `::test_the_key_is_gone_before_the_session_factories_are_built_when_enabled` |
| key not inherited (replay); replay works enabled | `test_replay_calendar.py` (4 tests) |
| App runs and cancels the new tasks | `test_maintenance_runtime.py::test_the_provider_and_the_releaser_run_for_the_apps_lifetime` and three isolation tests |

## Mutation table

Run in a scratch copy of the tree (`PYTHONDONTWRITEBYTECODE=1 python -B`, `pytest -x`
over the five new test files plus `test_calendar_config.py`, `test_runtime.py`,
`test_agent_core{,_events,_durability,_reminders,_turn_scope}.py`,
`test_event_turn_framing.py`, `test_app.py`, `test_reminders_runtime.py`,
`test_event_release.py`, `test_hold_store.py`, `test_replay_cli.py`). Two sanity
mutants first: a no-op survived and a broken import was killed, so the copy is what
ran. Every item on the brief's list is a row (S1, S2, S3, B1, B2, B3, B4, L1, L2, R1,
R2, L3, R3, N1, F1, F2, W1, W2, W3, W4, P1); the rest are additional.

| # | Mutation | Result |
|---|---|---|
| S1 | sink fires on audit failure | killed: `test_agent_core_maintenance.py::test_the_sink_does_not_fire_when_the_record_could_not_be_written` |
| S2 | sink fires for live turns | killed: `::test_the_sink_never_fires_for_a_live_turn` |
| S3 | sink fires twice (again on a second flush) | killed: `::test_the_sink_fires_at_most_once_per_triage` |
| S4 | sink skipped when audit is not configured | killed: `::test_the_sink_fires_at_once_when_audit_is_not_configured` |
| S5 | sink condition inverted (`not ok`) | killed: `::test_the_sink_fires_once_the_triage_record_is_written` |
| S6 | sink exception not caught | killed: `::test_a_failing_sink_never_stops_the_triage_message` |
| S7 | sink fires at turn start, before the record | killed: `::test_the_sink_fires_once_the_triage_record_is_written` |
| B1 | block placed after recall | killed: `::test_the_block_sits_after_the_time_header_and_before_recall` |
| B2 | block on event turns | killed: `::test_event_turns_never_carry_the_block` |
| B3 | block taints the session | killed: `::test_the_block_never_taints_the_session` |
| B4 | block failure blocks the turn | killed: `::test_calendar_trouble_never_blocks_a_turn` |
| B5 | block composed once per session (cached) | killed: `::test_the_block_is_composed_per_turn` |
| L1 | line before the suppressed-count note | killed: `::test_the_line_comes_after_the_suppressed_count_note` |
| L2 | line missing from the incomplete notice | killed: `::test_incomplete_triage_still_carries_the_line` |
| L3 | line sent for a non-announceable turn | killed: `::test_a_non_announceable_released_turn_sends_nothing` |
| L4 | line composition error not caught | killed: `::test_a_line_that_cannot_be_composed_never_stops_the_triage_message` |
| L5 | owner zone not passed to the line (UTC) | killed: `::test_release_line_with_recoveries` |
| R1 | singular/plural wrong (always plural) | killed: `::test_release_line_without_recoveries_singular` |
| R2 | singular/plural wrong (inverted) | killed: `::test_release_line_with_recoveries` |
| R3 | `render_instant` used for the line (must be HH:MM) | killed: `::test_release_line_with_recoveries` |
| R4 | line times in UTC, not the owner zone | killed: `::test_release_line_with_recoveries` |
| R5 | no `UTC` label without a zone | killed: `::test_without_an_owner_zone_the_times_are_utc_and_say_so` |
| R6 | title not flattened | killed: `::test_the_title_is_flattened_to_one_line` |
| R7 | recovered variant counts forwarded instead of held | killed: `::test_release_line_with_recoveries` |
| R8 | second time is the end without grace | killed: `::test_release_line_with_recoveries` |
| N1 | HH:MM used in the block (must be `render_instant`) | killed: `test_maintenance_note.py::test_owner_asks_during_a_patch_round[UTC]` |
| N2 | released look-back inclusive (`>=`) | killed: `::test_an_ended_window_is_shown_for_24_hours[86400-False]` |
| N3 | released window still on the calendar listed in progress | **first run: equivalent** (see below); redefined, killed: `::test_a_released_window_still_on_the_calendar_reads_ended` |
| N4 | counts are rows, not distinct identities | killed: `::test_owner_asks_during_a_patch_round[UTC]` |
| N5 | title not neutralised in the block | killed: `::test_the_title_cannot_close_the_block_or_its_delimiters` |
| N6 | block rendered in UTC, not the owner zone | killed: `::test_owner_asks_during_a_patch_round[UTC]` |
| N7 | block's grace hard-coded at 30 min | killed: `::test_the_held_until_instant_follows_the_configured_grace` |
| N8 | 24 h look-back ignored | killed: `::test_an_ended_window_is_shown_for_24_hours[86400-False]` |
| N9 | ended windows not shown without a provider | killed: `::test_an_ended_window_is_shown_with_the_calendar_disabled` |
| N10 | in-progress count always 0 | killed: `::test_owner_asks_during_a_patch_round[UTC]` |
| N11 | ended recovered/forwarded swapped | killed: `::test_ended_window_remembered_for_a_day[UTC]` |
| F1 | stale threshold off by one (`>=`) | killed: `test_hold_framing.py::test_an_old_hold_is_framed_as_possibly_stale[86400-False]` |
| F2 | stale threshold off by one second (`24 h + 1`) | killed: same test (`[86401-True]` fails too) |
| F3 | no stale framing | killed: `::test_an_old_hold_is_framed_as_possibly_stale[86401-True]` |
| F4 | hold note before the method framing | killed: `::test_the_window_note_follows_the_method_framing` |
| F5 | hold note never added | killed: `::test_released_triage_is_framed_by_its_window` |
| F6 | title not neutralised in the framing | killed: `::test_a_hostile_title_cannot_forge_a_block_marker` |
| F7 | no live-check direction | killed: `::test_released_triage_is_framed_by_its_window` |
| F8 | hold note after the recurrence note | killed: `::test_the_window_note_comes_before_the_recurrence_note` |
| W1 | provider `load()` after intake start (moved into `App.run`) | killed: `test_maintenance_runtime.py::test_the_snapshot_is_loaded_before_intake_starts` |
| W2 | second client built for the provider | killed: `::test_one_calendar_client_serves_both_tools_and_the_provider` |
| W3 | releaser not started when calendar disabled but rows exist | killed: `::test_disabled_with_no_held_rows_changes_nothing_the_owner_or_google_sees` (and the held-rows test) |
| W4 | env var popped after the SDK factories are built | killed: `::test_the_key_is_gone_before_the_session_factories_are_built_when_enabled` |
| W4b | env var never popped | killed: `::test_the_key_is_gone_before_any_sdk_client_exists` |
| W5 | gate recorder is the plain receipts when enabled | killed: `::test_the_gate_records_through_the_approval_reference_tap` |
| W6 | calendar receipts not handed to the registry | killed: same |
| W7 | registry builds its own client | killed: `::test_one_calendar_client_serves_both_tools_and_the_provider` |
| W8 | coordinator without the ledger | killed: same (the releaser refuses a foreign ledger) |
| W9 | coordinator without the provider | killed: `::test_the_snapshot_is_loaded_before_intake_starts` |
| W10 | releaser over a second ledger | killed: `::test_one_calendar_client_serves_both_tools_and_the_provider` |
| W11 | releaser grace hard-coded | **survived first run** (test gap: the default 30 min equals the constant). Closed: the runtime tests configure 45 min; killed: `::test_the_hold_chain_runs_through_one_ledger` |
| W12 | releaser without the provider when enabled | killed: `::test_the_hold_chain_runs_through_one_ledger` |
| W13 | core has no release sink | killed: same |
| W14 | note only when the calendar is enabled | killed: `::test_disabled_the_note_still_reports_a_recent_release` |
| W15 | note without the provider | killed: `::test_the_core_gets_the_maintenance_note_and_the_owner_zone` |
| W16 | owner zone not handed to the core | killed: same |
| W17 | provider not handed to the App | killed: `::test_one_calendar_client_serves_both_tools_and_the_provider` |
| W18 | releaser not handed to the App | killed: `::test_the_hold_chain_runs_through_one_ledger` |
| W19 | note grace hard-coded | **survived first run**, same gap as W11; closed the same way; killed: `::test_the_core_gets_the_maintenance_note_and_the_owner_zone` |
| W20 | provider built with events off (after the review change) | killed: `::test_calendar_on_events_off_builds_no_provider_but_keeps_the_tools` |
| A1 | App never starts the provider task | killed: `::test_the_provider_and_the_releaser_run_for_the_apps_lifetime` |
| A2 | App never starts the releaser task | killed: same |
| P1 | replay not popping | killed: `test_replay_calendar.py::test_a_replay_with_the_calendar_enabled_never_hands_on_the_key` |
| P2 | replay pops before the config is read | killed: `::test_the_key_is_still_there_when_the_config_is_read` |
| P3 | replay pops after the audit-log guard | killed: `::test_a_refused_command_removes_the_key_too` |

70 mutations (W20 added after the review change). First run of the original 69: 66 of 69: N3 as first written (drop the `released_ids`
filter) survived as a **provably equivalent** mutant, because the stored-row check
right after it already drops every released window; the redundant filter was
removed from `MaintenanceNote.block` and N3 redefined as "drop the stored-row
check", now killed. W11 and W19 were test gaps (default grace), closed as shown.
W20 was red on its first run. Final: 70 of 70 killed.

## Suite

`pytest -q`: 4368 passed, 11 skipped, 12 deselected (baseline 4261 + these 107).
The changed and new files pass the pre-commit hook's pattern layer and
`.githooks/local-checks.sh` over the added lines, and `gitleaks dir` reports nothing
in any of them.
