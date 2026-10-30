# maintenance-calendar — apply decisions

Ambiguities, design disagreements and mutation tables recorded during `/opsx:apply`.
Where a choice was open, the option easiest to undo was taken and is named here.
Line numbers cite the working tree at the time of writing.

## Group 1 — configuration and credential

### Decisions

1. **Range refusals only when enabled.** The spec scopes the refusals to "while it is
   true" (maintenance-calendar spec, *disabled by default and inert when off*); the
   repo's precedent validates bounds unconditionally (`henk/config.py:1208`, `:1303`,
   `:1411`). Followed the spec. To undo: drop the `if not enabled:` early return in
   `_calendar_settings` (`henk/config.py:1476`). A disabled section whose numbers do
   not coerce falls back to the defaults rather than refusing.
2. **Refusals beyond the spec's list**, each cheap to remove:
   - positivity of `lookahead_days`, `refresh_seconds`, `stale_after_seconds`,
     `timeout_seconds` (enabled only);
   - `calendar.enabled` and `calendar.hold_alerts` must be real booleans, via the
     existing `_require_strict_bool` (`henk/config.py:1707`): `bool("false")` is True,
     so a quoted rollback would otherwise enable the calendar;
   - unknown `calendar.*` keys are refused, via the existing `_optional_section`
     (`henk/config.py:1662`), so a hand-typed `calendar.scope` cannot look effective.
3. **The variable is popped unconditionally**, not only when enabled
   (`henk/runtime.py:89`, first statement of `build_runtime`). The rollback keeps the
   `.env` line (design Migration Plan step 6), so a disabled runtime must not hand it to
   the SDK subprocess either.
4. **`Secrets.google_calendar_key` holds the decoded `ServiceAccountKey`**, not the
   base64 string (`henk/config.py:788`, `repr=False`). `Secrets.from_env` gains a
   keyword `calendar_enabled` (default False) and decodes only then, so the base64 value
   is never retained and a disabled deploy never reads the variable.
5. **Key validation is stricter than the spec's list**: an absent `token_uri` is refused
   (absent is not the pinned value), and the PEM must parse and be RSA (RS256), so a
   broken key fails at startup rather than at the first refresh. `ServiceAccountKey`
   keeps only `client_email`, `private_key`, `token_uri`; its `repr`/`str` are redacted
   (`henk/calendar/token.py:57-73`). Every refusal is raised `from None`.
6. **Creator emails are case-folded** (`normalise_email_allowlist`,
   `henk/config.py:1392`), and `admit()` folds the item's creator too. Google accounts
   are case-insensitive, so this cannot widen the set; exact matching would make a
   hand-typed `Owner@...` admit nothing. `normalise_label_allowlist` gained `setting`
   and `kind` keywords; the session allowlist's messages are unchanged.
7. **`config.yaml`: "commented, disabled" read as an active `calendar:` section with
   `enabled: false`** plus an active empty `calendar_creator_allowlist`, following the
   `sessions:` / `homelab_docs:` precedent in the same file. The alternative,
   commented-out lines, is a two-minute change.
8. **Three inventory tests amended**, because they enumerate fields by design so that
   an addition is deliberate, and this change adds one field each on purpose (spec
   secure-deployment, *The calendar key is the only new secret*):
   `tests/test_config_sessions.py::test_the_secret_set_is_unchanged`,
   `tests/test_config_triage.py::test_secrets_from_env_is_unchanged`,
   `tests/test_config_read_depth.py::test_the_corpus_allowlist_sits_with_the_other_tier_w_allowlists`.
9. **`pyjwt[crypto]>=2.8`** added to `[project] dependencies`. `uv lock`: the diff is
   two lines, the `henk` package's dependency edge and its `requires-dist` entry. The
   sorted list of `name = ` entries is identical before and after (43 packages); no
   package was added. `uv sync --inexact` installed `pyjwt 2.13.0`, `cryptography
   50.0.0`, `cffi`, `pycparser` into the dev venv (they were locked but only reachable
   through the `runtime` extra before).

### Open points handed on

- **Empty-allowlist startup WARNING.** The spec puts it under the admission requirement;
  tasks.md puts it in 3.4 beside `todo_read`'s warning (`henk/tools/__init__.py:160-164`).
  Left for 3.4.
- **Design citation `sdk_session.py:335-347`.** Those lines build `ClaudeAgentOptions`
  with no `env`; that the CLI then inherits `os.environ` is the SDK's default, not
  verified against SDK source here (the `runtime` extra is not installed in the dev
  venv). The unconditional pop makes the question moot.
- **Replay CLI.** `henk/replay/__main__.py:89` loads `Config` from `os.environ` in its
  own process. With the calendar enabled on rp5, a replay run decodes the key too, and
  refuses to start if its environment lacks the variable. For group 7 to decide.
- **Observation, outside this change:** `Secrets`' other four fields are not
  `repr=False`, so `repr(config)` prints the Anthropic, Taiga, todo and ntfy tokens.
  The calendar key is excluded. Worth a follow-up.

### Mutation table (tests: `test_calendar_config.py`, `test_config.py`, `test_runtime.py`)

| Mutation | Result |
|---|---|
| drop the `token_uri` pin | killed: `test_invalid_token_uri_refuses_startup_without_echoing_the_key` |
| pin as a prefix match | killed: `test_only_the_exact_pinned_token_uri_is_accepted[.../token/]` |
| keep the key's `token_uri` instead of the constant | **survived — equivalent**: the pin guarantees equality; the token flow uses the constant anyway (group 2 kills the flow-level version) |
| drop `repr=False` on the Secrets field | killed: `test_secrets_field_is_declared_repr_false` |
| drop the key's redacted `repr` | killed: `test_service_account_key_repr_is_redacted` |
| forget to pop the env var | killed: `test_build_runtime_pops_the_variable_when_enabled` |
| pop only when enabled | killed: `test_build_runtime_pops_the_variable_when_disabled` |
| chain the parser exception (`from exc`) | killed: `test_errors_do_not_chain_the_underlying_exception` |
| echo the value in the error | killed: `test_invalid_token_uri_refuses_startup_without_echoing_the_key` |
| decode the key even when disabled | killed: `test_disabled_calendar_never_reads_the_variable` |
| accept an empty `calendar_id` | killed: `test_enabled_without_a_calendar_id_refused` |
| grace range exclusive at the top | killed: `test_grace_bounds_are_inclusive[240]` |
| drop the range check | killed: `test_grace_outside_0_to_240_refused[-1]` |
| zone not required by the calendar | killed: `test_enabled_without_an_owner_timezone_refused` |
| `bool()` instead of the strict flag | killed: `test_enabled_flag_must_be_a_real_boolean[None]` |
| allowlist not case-folded | killed: `test_creator_allowlist_is_parsed_stripped_and_case_folded` |
| accept a non-RSA key | killed: `test_a_non_rsa_private_key_is_refused` |
| unknown calendar keys ignored | killed: `test_unknown_calendar_key_refused` |
| positivity check `<= 0` → `< 0` | killed: `test_other_durations_must_be_positive_when_enabled[lookahead_days]` |

18 of 19 killed; the survivor is an equivalent mutant.

## Group 2 — calendar client, admit(), holds()

### Decisions

1. **`list_window` applies `admit()` itself** and returns only admitted
   `CalendarEvent`s (`henk/calendar/client.py:230-253`). Design D2 lists `list_window`
   and `admit` as separate calls; folding them makes "dropped where it is read" a
   property of the client, so neither consumer (tool, window provider) can forget the
   filter. The client takes the allowlist; Henk's own email comes from the token
   source. `admit()` stays public for direct tests.
2. **`admit()` and `holds()` live in `client.py`**, where D2 places the admission rule.
   `holds(event, calendar_config, *, events_enabled)` (`client.py:156`); a reversed
   event (end before start) never holds.
3. **`insert()` takes naive wall-clock datetimes** and resolves them in the owner's zone
   (`rfc3339_local`, `client.py:170`): ambiguous → first occurrence (fold forced to 0
   even if the caller passed 1), DST gap → `NonexistentLocalTime`, aware datetime →
   `TypeError`. The tool (group 3) still validates and names each rule for the owner;
   the client's refusals are defence in depth. It also refuses a title not starting
   `Homelab` (an event Henk could never read back).
4. **The token request and the assertion's `aud` always use the pinned constant**, never
   the key object's `token_uri`.
5. **The single 401 retry applies to every Calendar call**, whether or not the token was
   freshly issued; no other status is retried. Token reuse: `now < expiry - 60`, so at
   exactly 60 s before expiry a new token is fetched. Concurrent callers share one
   sign-in (an `asyncio.Lock`).
6. **Errors never quote Google's body** (token or API). `CalendarError.status` carries
   the HTTP status (None for timeouts and transport failures); `.forbidden` is the 403
   case the create tool reports honestly.
7. **Unreadable items are dropped silently**: a missing id, an offset-less or
   unparsable `dateTime`, or a missing creator. All-day events are admitted with their
   dates (`start_date`, exclusive `end_date`) and `start`/`end` None.

### Conflicts handed on (standing rule 5: raise before building group 3)

- **Renderer form.** The spec and proposal show `Sat 7 Nov at 20:00 CET`, but the shared
  renderer `render_instant` returns `Saturday 7 November at 20:00 CET`
  (`henk/reminders/timeparse.py:74-96` full-name tables, `:191-194` the format; checked
  by running it). The calendar spec requires the shared renderer, so the scenario text
  and the renderer cannot both hold. Group 3 must pick before writing its tests.
- **Weekday table.** The tool's argument uses `Mon`..`Sun`, and the spec requires the
  check against "a fixed weekday table"; `timeparse.WEEKDAYS` is full names. Group 3
  needs a short-name table (or slices of the full one), never `strftime`.
- **Calendar id in logs.** httpx logs every request URL at INFO and `__main__` runs at
  INFO (`henk/__main__.py:80-83`), so the calendar id will appear in rp5's logs. Not a
  secret under the spec (its list is email, key header, base64), but it is a real
  identifier. Group 7 may want the `httpx` logger at WARNING.
- **"Never persisted"** for non-admitted titles: logging is covered here
  (`test_list_window_returns_only_admitted_events_and_logs_no_title`); the persistence
  half needs the window snapshot (group 5).

### Mutation table (tests: `test_calendar_client.py`)

| Mutation | Result |
|---|---|
| `iat` not backdated | killed: `test_token_assertion_carries_the_events_scope_only` |
| `exp = iat + 7200` | killed: same |
| wider scope | killed: same |
| add a `sub` claim | killed: same |
| `aud` from the key | killed: `test_token_request_goes_to_the_pinned_uri_even_if_the_key_says_otherwise` |
| post to the key's `token_uri` | killed: same |
| forget the 60 s expiry margin | killed: `test_token_reused_until_60_seconds_before_expiry` |
| margin boundary `<` → `<=` | killed: same |
| never cache the token | killed: same |
| no lock around sign-in | **survived first run** (the mock transport never yielded, so callers ran one after another). Closed: `test_concurrent_callers_share_one_sign_in` now holds the token request in flight with an async handler; killed on rerun |
| quote the token error body | killed: `test_token_endpoint_refusal_is_a_token_error_without_its_body` |
| retry 401 twice | killed: `test_a_second_401_fails_without_a_third_attempt` |
| no 401 retry | killed: `test_expired_token_retried_exactly_once` |
| retry without dropping the token | killed: same |
| retry every error status | killed: `test_other_errors_are_not_retried` |
| accept `q` | killed: `test_list_requests_exactly_the_field_mask_and_no_q` |
| ask for the description | killed: same |
| drop the pagination bound | killed: `test_a_fifth_page_fails_loudly_with_no_partial_result` |
| partial result past the bound | killed: same |
| pages not followed | killed: `test_list_follows_page_tokens` |
| include `attendees` in the insert body | killed: `test_insert_body_is_exactly_summary_start_end` |
| include an empty description | killed: same |
| `sendUpdates=all` / no `sendUpdates` | killed: same (two mutations) |
| drop `timeZone` | killed: same |
| ambiguous takes `fold=1` | killed: `test_rfc3339_offsets[ambiguous]` |
| caller's fold honoured | killed: `test_rfc3339_offsets[ambiguous fold=1]` |
| gap time not refused | killed: `test_rfc3339_refuses_a_time_in_the_dst_gap` |
| calendar id not percent-encoded | killed: `test_list_requests_exactly_the_field_mask_and_no_q` |
| calendar id quoted with `/` safe | killed: `test_a_calendar_id_cannot_leave_the_events_path` |
| invert the creator check | killed: `test_expired_token_retried_exactly_once` (first to fail under `-x`) |
| own account not admitted | killed: `test_henks_own_events_are_admitted_without_configuration` |
| creator compare case-sensitive | killed: `test_creator_comparison_ignores_case` |
| prefix case-insensitive | killed: `test_prefix_is_case_sensitive_and_leading[homelab patch]` |
| prefix after strip / prefix anywhere | killed: `test_prefix_is_case_sensitive_and_leading[ Homelab: patch]` (two mutations) |
| cancelled admitted | killed: `test_cancelled_events_are_ignored` |
| admission bypassed in `list_window` | killed: `test_list_window_returns_only_admitted_events_and_logs_no_title` |
| offset-less time accepted | killed: `test_unreadable_or_offsetless_times_are_not_admitted` |
| insert accepts any title | killed: `test_insert_refuses_a_title_admission_would_drop` |
| `holds` ignores `hold_alerts` | killed: `test_hold_switched_off` |
| `holds` ignores `events.enabled` / `calendar.enabled` | killed: `test_no_hold_with_the_calendar_or_events_disabled` (two mutations) |
| `holds` exclusive at the maximum | killed: `test_the_eight_hour_bound_is_inclusive` |
| `holds` ignores the maximum | killed: `test_all_day_and_over_long_events_never_hold` |
| `holds` uses a fixed 8 h | killed: `test_the_bound_follows_the_configured_maximum` |
| all-day events hold | killed: `test_all_day_and_over_long_events_never_hold` |
| quote Google's error body | killed: `test_other_errors_are_not_retried` |
| insert without a confirmed id | killed: `test_insert_answer_without_an_id_is_a_failure` |

50 mutations: 49 killed on first run, 1 survivor (test gap) closed; 50 of 50 killed.

## Group 4 — audit records

### Decisions

1. **`transition` widened, reminder branch re-pinned.** The spec's calendar record
   carries `transition: "created"`, but v5's top-level `transition` is the reminder
   enum. v6 adds `created` at the top level and the reminder branch pins reminder
   records to exactly v5's seven values, so no v5 record type changes meaning
   (`tests/test_audit_v6.py::test_no_v5_field_or_branch_is_changed_except_the_transition_union`).
   The alternative was a differently named field (`calendar_transition`), which departs
   from the spec text.
2. **The three new types are closed with `propertyNames` allowlists** in their branches,
   a positive list rather than the reminder branch's `not anyOf required` deny-list, so
   the document refuses any title, message, payload or `attendees` slot.
3. **Field names and types.** `calendar`: `transition` (const `created`), `event_id`
   (Google's id, non-empty), `start`/`end` (RFC 3339 strings with offset, pattern-checked),
   `initiated_by` (const `model`), `reference` (non-empty string). `hold`:
   `identity_key`, `state`, `event_id` (ntfy id), `window_id`, `window_end` (frozen end,
   epoch, without grace), `published_at` (epoch). `hold-release`: `window_id`, `held`,
   `recovered`, `forwarded` (non-negative integers).
4. **`hold_release_record` refuses counts that do not add up** (`held = recovered +
   forwarded`): per the event-intake release requirement every identity that did not
   recover is forwarded.
5. **Writers return whether they wrote**: `CalendarReceipts.created` and
   `HoldReceipts.held` / `.released` return `AuditLog.write`'s result (True when audit is
   not configured), so a failed `hold` record can be logged loud while the hold stands.
6. **Deferred to the code that owns the mechanism** (each is also listed in that group's
   tasks): "no second `hold` on a duplicate" is the `INSERT OR IGNORE` on `held_events`
   and "one `hold-release` per window" the `released_at IS NULL` guard (group 6); "the
   create tool writes its `calendar` record only after a confirmed insert" is the tool's
   call site (3.4, tested by 3.3's 403 scenario). `HoldReceipts`' docstring states that
   the caller owns uniqueness. **The last sentence of task 4.2 is therefore not done
   here.**
7. **Prior version-pinned tests amended**, following the v5 bump's precedent:
   `test_audit_receipts.py::test_schema_version_is_five` → `_six`; `test_audit_v4.py`
   pin 5 → 6 and the reminder transition set now asserted on the reminder branch;
   `test_audit_v5.py`: the current-version test became "the v5 document stays committed",
   new records assert `SCHEMA_VERSION`, the no-v4-field-changed test compares v4 with the
   v5 document, and the mixed-log helpers relabel their records to version 5.
8. **Standing rule 4**: if `runbook-actions` archives first with its own schema bump,
   this change's v6 must be renumbered and its document rebased.

### Mutation table (tests: `test_audit_v6.py`, `_v5`, `_v4`, `test_audit_receipts.py`, `test_audit_log.py`)

| Mutation | Result |
|---|---|
| wrong schema version (stay at 5) | killed: `test_the_current_version_is_six_and_its_document_is_committed` |
| current document still v5 | killed: same |
| document pins version 5 | killed: same |
| title leaking into the calendar record (`summary`) | killed: `test_new_records_declare_the_new_version_and_validate` |
| title leaking into the hold record (`window_title`) | killed: same |
| payload slot in the hold-release record | killed: same |
| calendar branch not closed | killed: `test_the_document_refuses_a_title_or_payload_on_a_calendar_record[title]` |
| hold branch not closed | killed: `test_the_document_refuses_a_payload_on_hold_records[title]` |
| hold-release branch not closed | killed: same |
| reminder branch not re-pinned | killed: `test_no_v5_field_or_branch_is_changed_except_the_transition_union` |
| calendar initiator unpinned | killed: `test_calendar_record_fields_are_typed[initiated_by-owner-command]` |
| calendar transition unpinned | killed: `test_calendar_record_fields_are_typed[transition-scheduled]` |
| calendar start without offset accepted | killed: `test_calendar_record_fields_are_typed[start-...]` |
| calendar reference nullable | killed: `test_calendar_record_fields_are_typed[reference-None]` |
| hold requires no `window_end` | killed: `test_hold_record_requires_every_field[window_end]` |
| release counts may be negative | killed: `test_release_counts_are_non_negative_integers[held--1]` |
| release counts unchecked in the builder | killed: `test_release_counts_must_add_up` |
| new type missing from `record_type` | killed: `test_the_current_version_is_six_and_its_document_is_committed` |
| calendar receipt reports success on failure | killed: `test_a_failed_calendar_write_is_reported_not_raised` |
| hold receipt reports success on failure | killed: `test_a_failed_hold_write_is_reported_not_raised` |
| calendar record initiator changed | killed: `test_new_records_declare_the_new_version_and_validate` |

21 of 21 killed.

## Orchestrator decisions after groups 1, 2, 4

- **Renderer form: the shared renderer wins.** The spec requires `render_instant`, so the
  six example strings in `proposal.md`, `design.md` (D3, D10),
  `specs/maintenance-calendar/spec.md` and `specs/agent-core/spec.md` now read
  `Saturday 7 November at 20:00 CET`, the renderer's actual output (checked by running
  it). The release line keeps its compact `HH:MM` (D8, unaffected).
- **Weekday table for `start`/`end`:** group 3 derives the short names from
  `timeparse.WEEKDAYS` (`name[:3]`), never `strftime`/`strptime`.
- **Calendar id in httpx INFO logs:** left as is. The id is not a secret under the spec,
  grants nothing without the key, and the logs stay on rp5. Silencing `httpx` would also
  hide the ntfy and Signal request lines used for diagnosis.
- **Replay CLI:** carried to group 7. Replay must not hand the key to its SDK subprocess
  either: it removes the variable the same way `build_runtime` does.
- **`Secrets` repr of the other tokens:** out of scope; a follow-up, not this change.
- **Task 6.0:** probe run by the owner on rp5, agrees with D8
  (`notes/ntfy-keepalive-probe.md`).
- **D10 "ended" vs agent-core "released" (from group 5):** the spec governs. The
  owner-turn note covers active windows plus windows *released* within 24 h, taken from
  `hold_windows.released_at` with their counts; `MaintenanceWindows.ended_within` is not
  used for it (the snapshot only reaches back ~8.5 h). A window that held nothing is never
  released and gets no "ended" note. Design D10's wording aligned to the spec.

## Groups 3 and 5 (run in parallel)

Full notes and mutation tables: `notes/apply-group3.md` (78 + 3 mutations; 6 first-run
survivors were test gaps, all closed) and `notes/apply-group5.md` (49 mutations, 46
killed; 3 equivalent, 2 gaps closed, one of them a real defect in `refresh()`).

Orchestrator review:
- **Title schema pattern made lookaround-free** before commit (group 3 notes 4 and 9):
  `^Homelab(?: ?[^\s<>\x00-\x1f\x7f-\x9f])*$`, and execution refuses any `<` or `>`.
  A JSON Schema engine without lookaround support can no longer reject the tool, and the
  gate's one-pass delimiter strip cannot be reached through this tool. Stricter than the
  spec's "no `<<<` or `>>>`".
- **Approval reference reaches the tool through `ApprovalReferences`**, a tap on the
  gate's recorder (no gate change, per design). Verified end to end through the real gate
  in `test_an_approved_create_is_receipted_twice_with_one_reference`.
- **Group 7 carries:** runtime wiring (one shared `CalendarClient`, the tap as the gate's
  recorder, `CalendarReceipts`), replay's key handling, the provider's `load()` before
  intake and a third background task in `App`.

## Group 6 (hold and release)

Full notes and mutation table: `notes/apply-group6.md` (62 + 4 mutations, all killed on
the final code; one equivalent mutant led to a dead branch being removed, two real gaps
closed).

Orchestrator review:
- **Caught-up now also resets when a subscription ends** (`on_unsubscribe`, fired from a
  `finally` around intake's frame loop), not only at the next subscribe. Without it a
  release could fire during the reconnect backoff, before the reconnect's `since=`
  backlog, and page the owner for an identity whose `resolved` was still unread. The
  half-open gap before the liveness deadline remains, undetectable by design.
- **Pre-existing defect fixed:** `EventCoordinator.run` discarded the batch
  `Debouncer.feed` returns when an event arrives more than `debounce_seconds` (arrival
  wall clock) after its batch opened, which needs queue lag or a forward clock step
  while the loop's monotonic deadline has not passed. Those alerts were never
  dispatched. Live in production today; fixed by this change's deploy.
- **Accepted as recorded:** with `events.enabled` false nothing is released (there is no
  coordinator, and nothing new is held either, since `holds()` requires events); held
  rows have no retention (tiny, one row per held alert; a follow-up).
