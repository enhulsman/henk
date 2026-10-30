# maintenance-calendar — apply notes, group 3 (tools)

Tasks 3.1–3.5, plus task 4.2's last sentence (the create tool writes its `calendar`
record only after Google confirms the insert). Where a choice was open, the option
easiest to undo was taken and is named here. Line numbers cite the working tree at the
time of writing.

Files: `henk/tools/calendar.py` (new), `henk/tools/__init__.py`, `henk/config.py`,
`tests/test_tools_calendar.py` (new, 140 passed + 1 skipped),
`tests/test_calendar_registration.py` (new, 26 passed). Full suite after the
orchestrator's portable-pattern change: see the end of this file.

## Decisions and ambiguities

1. **How the approval reference reaches the tool.** The `calendar` record must carry
   the authorization's reference (audit-log delta, *A calendar write is receipted
   twice*). The gate writes the reference into its decision record
   (`henk/gate/approval.py:351-375`), but the SDK runs the tool with the model's
   arguments only (`henk/agent/sdk_session.py:313-314`, after
   `henk/agent/permission.py:97`), and changing gate code is a non-goal (design,
   Non-Goals). So `ApprovalReferences` (`henk/tools/calendar.py:325-361`) wraps the
   gate's decision recorder: it forwards every decision unchanged and keeps the
   reference of the latest *approved* `calendar_create_event` for the tool to take once.
   - Latest-wins, single slot, cleared on take. A FIFO would let one approval whose
     run never happened (the turn died between permission and execution) shift every
     later link by one until restart. The gate allows one pending approval at a time.
   - The tool takes the reference first on every run (`calendar.py:434`). With
     receipts wired and no reference, it refuses before any call (`calendar.py:448`):
     an unlinkable `calendar` record would break the meaning of the receipt pair.
   - To undo: a gate-level hook (the deferred `resolve_for_approval` of D4) could carry
     the reference once runbook-actions has landed.
2. **How `build_production_registry` gets a client.** The resolver pattern: a
   `calendar_client` keyword, built by the new `build_calendar_client(config, http)`
   when omitted (`henk/tools/__init__.py:105-136`, registration at `:347-375`). Also
   `calendar_receipts` (as `reminder_receipts`) and `calendar_references`. With
   `calendar.enabled` false nothing calendar-related is built, registered or logged.
   Building a client contacts nobody, so the replay's refusing transport is fine.
3. **Title rules stricter than the spec's list**, because the identity property
   demands it. A single no-break space or em space is neither a run nor
   leading/trailing whitespace, yet `_render_argument` turns it into a space
   (`approval.py:446`), so the prompt would differ from the written title. Accepted
   whitespace is therefore single ASCII spaces only. Also refused: format characters
   (Cf: bidi overrides, zero-width) and lone surrogates (Cs), beyond control characters
   (Cc), and any `<` or `>` at all (item 4). One combined message names whitespace and
   the angle brackets together, quoting `<<< >>>`, per the scenario *Title that the
   prompt would alter is refused*.
4. **The title's schema `pattern` has no lookaround** (orchestrator review,
   replacing a lookahead version whose acceptance by the CLI's validator and the API
   was going to be a live check). It is
   `^Homelab(?: ?[^\s<>\x00-\x1f\x7f-\x9f])*$` (`calendar.py:101`): every character
   after the prefix may be preceded by one space, so no run and no trailing space, and
   none is whitespace, `<`, `>` or a C0/C1 control character.
   - **Execution matches it, stricter than the spec:** `title_problem` refuses any
     `<` or `>` (`calendar.py:105`, `:209`), not only `<<<`/`>>>`.
   - **Where the two differ, the schema is the more permissive one, and execution
     decides.** Format characters (a zero-width space) are refused at execution only.
     So is a trailing newline, which the schema's `$` admits under JSON Schema's
     `search` semantics.
   - **Tests:** `test_the_schema_patterns_use_no_lookaround` checks the pattern is
     free of `(?=`, `(?!`, `(?<=` and `(?<!`.
     `test_the_schema_pattern_and_execution_agree_on_pinned_titles` pins 15 titles
     and both verdicts, including the two documented differences.
   - **Binding confirmed:** allowing space runs (` *`) and dropping `<>` from the
     class each fail the pinned test. Reverting execution to sequences only fails
     five tests.
   - Execution decides with `fullmatch`, so the pattern only advises the model.
5. **Wall-time parsing uses `fullmatch` and `[0-9]`** (`calendar.py:88-91`), not the
   schema's `\d` and `$`: `$` matches before a trailing newline, and `\d` matches
   other scripts' digits, which `int()` then accepts. Both mutants were killed.
6. **"Starting in the next 14 days" is read as the period queried.**
   `list_window(now, now + lookahead)` (`calendar.py:288`) returns events that
   overlap the period, so a window already running is listed too. Dropping it would
   hide the most relevant line. To undo: filter `start >= now` after the read.
7. **All-day rendering keeps the date.** `(all day)` replaces the time part:
   `Sunday 8 November (all day)`, or `Thursday 12 November – Saturday 14 November (all
   day)` (Google's end date is exclusive). The dates come from the fixed
   `WEEKDAYS`/`MONTHS` tables. `(all day)` alone would have lost the date.
8. **`calendar_upcoming` beyond the spec's minimum:**
   - it refuses any argument, so the model cannot believe it widened the period;
   - a header line names the period;
   - `...and N later events not shown.` when over 50;
   - it sorts in code rather than trusting `orderBy` across pages.
9. **Titles in the list** have delimiters stripped until none remain, then whitespace
   collapsed, then a cap of 119 characters plus `…` (120 in all).
   - **Observed, outside this change:** the gate strips in a single pass
     (`approval.py:447`), so an argument `<<>>><` renders as `<<<`, a reassembled
     opening delimiter in a prompt.
   - Not reachable through `calendar_create_event`: since item 4, its accepted titles
     hold no `<` or `>` at all, so nothing can be reassembled. Still a follow-up for
     the gate, for its other tools.
10. **Honest failure texts** (`calendar.py:525-542`):
    - 403: "Henk's account cannot write to the calendar".
    - Any other 4xx: "was not created".
    - A timeout, a transport failure, a 5xx, or a 200 with no id: "could not confirm …
      may have been created anyway: check the calendar". The request may have reached
      Google.
    - A sign-in failure falls in the last group, which is cautious but never false:
      `CalendarError` does not say whether a request left, and telling them apart
      would need a client change.
11. **A failed `calendar` record write after a confirmed insert** still returns
    `Created …`, because the event exists and saying otherwise invites a duplicate
    approval. It is logged at ERROR with the reference and never the title
    (`calendar.py:467-479`).
12. **Validation measures instants with `fold=0`**, the same instants the client sends
    (`henk/calendar/client.py:170-184`). The duration is elapsed time: 01:30–03:30 on
    the fall-back night lasts 3 hours, and an ambiguous 02:30 start counts from its
    first occurrence.
13. **Boundaries are inclusive where the spec says "more than" or "exceeds".**
    Accepted: a start exactly 15 minutes past, exactly 90 days ahead, and a duration
    of exactly `max_window_hours`.
14. **System prompt** (`henk/config.py:139-153`, `:184`, `:218`, `:256-266`, `:1140`):
    - `CALENDAR_TOOL_SUMMARIES` is appended last, matching registration order.
    - `COUNT_WORDS` gains 14 and 15.
    - The rule paragraph appears only when the calendar is enabled; the prompt is
      byte-identical when off (tested over all 16 other flag combinations).
    - "no scheduling" is dropped from the exclusions when the calendar is on, by the
      same reasoning that drops it for reminders and "no files" for docs.
    - The rule omits "name the weekday first" (fix plan B1) and asks for the weekday
      that matches the date.
    - The summary names no period, so it cannot go stale the way the query count did.
15. **Two gate constants restated** in `calendar.py:68-69` (the delimiters, and
    `"approved"`). Importing `henk.gate` from `henk.tools` is circular:
    `approval.py:40` imports `henk.tools.base`, which runs `henk/tools/__init__`, which
    imports this module. A test pins both constants to the gate's own values.
16. **The empty-allowlist WARNING** is logged once, in `build_production_registry`
    beside `todo_read`'s, and only when the calendar is enabled
    (`tools/__init__.py:350-355`). This settles group 1's open point.
17. **Locale test.** `test_weekday_check_ignores_the_locale` skips on this host, which
    has only C, C.utf8, POSIX and en_US.utf8. The binding checks are:
    - a structural test: no `strftime`, `strptime`, `%a`/`%A`/`%b`/`%B` or
      `import locale` in the module;
    - a table-rotation test, which kills a `strftime('%a')` mutant by behaviour.
18. **"No scheduler, event turn, or release path invokes the create tool"** is tested
    two ways:
    - at the gate seam: an event turn and a tainted session are refused `out-of-scope`,
      with no prompt and no request;
    - structurally: nothing under `henk/events/` or `henk/reminders/` names the tool.
      Group 6 must keep it that way.

## Handed on

- **Group 7 wiring:**
  - Build the client once with `build_calendar_client(config, client)` and pass the
    same instance to `build_production_registry(calendar_client=...)` and to the
    window provider: one token cache.
  - When the calendar is enabled: `refs = ApprovalReferences(receipts)` as the gate's
    `recorder` (today `recorder=receipts`, `henk/runtime.py:155`), plus
    `calendar_receipts=CalendarReceipts(audit)` and `calendar_references=refs`.
  - Receipts without references: every create refuses. No receipts: no `calendar`
    record (the reminder precedent).
- **Replay:** with the calendar enabled, `build_definition_registry`
  (`henk/replay/harness.py:337-354`) builds both tools and a client over the refusing
  transport. It needs the decoded key; that is the existing group-7 replay item.
- **Task 8.4:** confirm that the title schema pattern is accepted (item 4).

## Mutation table (tests: `test_tools_calendar.py`, `test_calendar_registration.py`)

Run against a copy of the tree, with `PYTHONDONTWRITEBYTECODE=1 python -B`, `-x`.

| Mutation | Result |
|---|---|
| drop the weekday refusal | killed: `test_wrong_weekday_refused` |
| invert the weekday check | killed: `test_approved_event_created_verbatim` |
| weekday via `strftime('%a')` | killed: `test_the_weekday_check_reads_the_fixed_table` |
| drop the end<=start refusal | killed: `test_an_end_not_after_start_is_refused[equal]` |
| end==start accepted | killed: same |
| drop the duration limit | killed: `test_over_long_window_refused` |
| duration bound exclusive | killed: `test_the_duration_bound_is_inclusive_and_configured` |
| duration by wall clock | killed: `test_the_duration_is_real_elapsed_time_across_the_fall_back` |
| duration limit hardcoded 8 | killed: `test_the_duration_bound_is_inclusive_and_configured` |
| drop the past refusal | killed: `test_a_start_more_than_fifteen_minutes_past_is_refused` |
| past bound inclusive | killed: same |
| drop the 90-day refusal | killed: `test_a_start_more_than_ninety_days_ahead_is_refused` |
| 90-day bound inclusive | killed: same |
| drop the DST-gap refusal | **survived first run**: the client's own guard still refused, so nothing reached Google, but the result no longer named the argument or the rule. Closed: `test_nonexistent_local_time_refused` asserts the tool's own text; killed on rerun |
| ambiguous time takes fold=1 in validation | **survived first run**: no test had an ambiguous start near the limit. Closed: `test_an_ambiguous_start_is_validated_as_its_first_occurrence`; killed on rerun |
| allow extra arguments | killed: `test_extra_argument_refused` |
| drop the missing-argument check | killed: `test_a_missing_argument_is_refused[title]` |
| drop the title prefix rule | killed: `test_the_title_schema_pattern_refuses_what_execution_refuses[patch round]` |
| drop the title length cap | killed: `test_every_title_rule_refuses_without_a_call[…-100]` |
| title length cap off by one | killed: `test_the_title_schema_pattern_accepts_what_execution_accepts[…]` |
| drop the control-character rule | killed: `…refuses_what_execution_refuses[Homelab: \x00]` |
| format characters (Cf) accepted | killed: `test_every_title_rule_refuses_without_a_call[…‮…]` |
| drop the leading/trailing-space clause | killed: `…refuses_what_execution_refuses[Homelab: patch ]` |
| drop the double-space clause | killed: `…refuses_what_execution_refuses[Homelab:  patch]` |
| drop the non-space-whitespace clause | killed: `…refuses_what_execution_refuses[Homelab:\xa0patch]` |
| drop the delimiter clause (then `<<<`/`>>>`, now any `<` or `>`) | killed: `…refuses_what_execution_refuses[Homelab: <<<patch]` |
| non-string title accepted | killed: `test_every_title_rule_refuses_without_a_call[42-text]` |
| wall time via `match`, not `fullmatch` | killed: `test_a_malformed_start_is_refused[…\n]` |
| wall-time digits via `\d` | killed: `test_a_malformed_start_is_refused[Sat ٢٠٢٦-…]` |
| schema allows additional properties | killed: `test_the_parameter_schema_expresses_the_formats` |
| create declared standing | killed: `test_create_is_mutating_per_instance_owner_only` |
| create scoped for event turns | killed: same |
| upcoming declared a taint source | killed: `test_upcoming_is_read_only_takes_no_arguments_and_raises_no_taint` |
| upcoming declared mutating | killed: same |
| record written before the insert | killed: `test_approved_event_created_verbatim` |
| record carries the model's strings | killed: same |
| reference never taken | killed: `test_approved_event_created_verbatim` (and 8 more) |
| write without an approval reference | killed: `test_without_an_approval_reference_nothing_is_written` |
| tap keeps the reference after take | killed: `test_the_reference_tap_forwards_every_decision_and_notes_approved_creates` |
| tap notes every outcome | killed: same |
| tap does not forward | killed: same |
| 403 not named | killed: `test_write_permission_missing_is_honest` |
| 5xx claimed not created | killed: `test_an_unanswered_insert_says_its_outcome_is_unknown[unavailable]` |
| unknown outcome claimed not created | killed: `…outcome_is_unknown[timeout]` |
| hold sentence without `holds()` | killed: `test_no_hold_sentence_when_holding_is_off` |
| hold sentence ignores `calendar.enabled` | killed: same |
| hold sentence ignores the grace | killed: `test_the_hold_sentence_names_end_plus_the_configured_grace` |
| marker without `holds()` | killed: `test_all_day_and_over_long_events_listed_without_the_marker` |
| drop the title cap in the list | killed: `test_titles_are_delimited_flattened_and_capped` |
| drop the delimiters in the list | killed: `test_planned_work_is_listed_in_the_owners_zone` |
| titles not stripped of delimiters | killed: `test_titles_are_delimited_flattened_and_capped` |
| single-pass delimiter strip (the gate's) | killed: same |
| titles not flattened | killed: same |
| list descriptions (field mask) | killed: `test_description_never_rendered` |
| failure reported as an empty calendar | killed: `test_unreachable_calendar_is_not_nothing_planned[timeout]` |
| upcoming caches its first read | killed: `test_upcoming_reads_live_from_now_over_the_lookahead` |
| drop the 50-event cap | killed: `test_at_most_fifty_events_ordered_by_start` |
| not ordered by start | killed: same |
| upcoming ignores extra arguments | killed: `test_upcoming_refuses_arguments_it_does_not_take` |
| all-day without its date | killed: `test_all_day_and_over_long_events_listed_without_the_marker` |
| all-day end date read inclusive | killed: same |
| upcoming period hardcoded | killed: `test_upcoming_reads_live_from_now_over_the_lookahead` |
| register when disabled | killed: `test_registry_unchanged_when_disabled[absent]` |
| events flag hardcoded in the registry | killed: `test_the_registry_hands_the_tools_the_events_flag[False]` |
| drop the empty-allowlist warning | killed: `test_an_empty_creator_allowlist_warns_at_startup` |
| warning inverted | killed: same |
| receipts not handed to the tool | killed: `test_the_runtime_receipts_and_references_reach_the_create_tool` |
| references not handed to the tool | killed: same |
| passed client ignored | killed: `test_a_passed_client_is_the_one_both_tools_use` |
| client built with an empty allowlist | **survived first run**: no test read through a registry-built client. Closed: `test_the_registry_built_client_reads_and_writes_the_configured_calendar`; killed on rerun |
| client built for the wrong calendar | **survived first run**, same gap; closed by the same test; killed on rerun |
| client built in UTC | **survived first run**, same gap; closed by the same test; killed on rerun |
| tools render in UTC | **survived first run**, same gap; closed by the same test; killed on rerun |
| drop the prompt rule | killed: `test_the_system_prompt_carries_the_rule_when_enabled` |
| calendar summaries not enumerated | killed: same |
| loader does not wire the flag | killed: `test_the_loader_wires_the_flag_from_the_calendar_section` |
| "no scheduling" kept with the calendar | killed: `test_the_calendar_drops_no_scheduling_from_the_exclusions` |
| count table lacks fifteen | killed: `test_the_full_capability_set_with_the_calendar_counts_fifteen` |

78 mutations: 72 killed on the first run, 6 survivors (test gaps, none equivalent)
closed. 78 of 78 killed.

After the portable-pattern change (item 4), three more:

| Mutation | Result |
|---|---|
| schema allows space runs (`(?: *[^…])`) | killed: `test_the_schema_pattern_and_execution_agree_on_pinned_titles['Homelab:  patch']` |
| schema allows `<` and `>` | killed: same test, four titles |
| execution refuses only `<<<`/`>>>` again | killed: `test_every_title_rule_refuses_without_a_call[Homelab: a < b-…]` and four more |

Full suite after the portable-pattern change: 4153 passed, 11 skipped, 12 deselected
(the one new skip is the locale test, item 17).
