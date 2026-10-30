> **TDD is not optional here.** Every group writes its tests from the spec scenarios
> *before* its implementation, and each spec scenario maps to at least one test. Where a
> group's tests pass on first run, mutate the implementation to confirm the tests actually
> bind, and record each group's mutation table in `notes/apply-decisions.md`.
>
> **Standing rules for this change.**
> 1. **No real identifiers in the repo.** The calendar id, the owner's Google account,
>    Pam's service-account email and Henk's `client_email` live only in rp5's config and
>    `.env`. Fixtures use `henk@example-project.iam.gserviceaccount.com`,
>    `owner@hulsman.dev` and a placeholder calendar id. Test keys are generated per test
>    run and never committed.
> 2. **No live Google call in the suite.** The client is exercised against an `httpx` mock
>    transport. The live checks are §8 only.
> 3. **Every claim about an existing mechanism cites `path:line`** in notes and in amended
>    design paragraphs. Design D7/D8 cite the lines the review verified; re-read them
>    before relying on them.
> 4. **Coordination with `runbook-actions`.** Before archiving, check whether it has
>    archived. If it has, rebase this change's three shared MODIFIED requirements (audit-log
>    *Schema is versioned*, secure-deployment *Scoped secrets only*, approval-gate
>    *Mutating tools declare a turn scope, enforced per session*) onto the archived text,
>    carrying its version paragraph, secret entries and tool list, and renumber the audit
>    schema version.
> 5. **The approved review plan is `notes/scrutiny-fix-plan.md`.** Where the artifacts and
>    that plan disagree, raise it before building. Do not silently pick one.

## 1. Configuration and credential

- [x] 1.1 Tests from *One calendar through Henk's own scoped credential*: invalid key,
      missing key, pinned `token_uri`, key never printed, variable removed from the
      environment. Plus tests from *The calendar capability is disabled by default and
      inert when off*: config refusals and defaults.
- [x] 1.2 `CalendarConfig` and `personal_data.calendar_creator_allowlist` in
      `henk/config.py`. `Secrets.google_calendar_key` with `repr=False`. Decode and
      validate the key at load, only when enabled; pop the variable in `build_runtime`.
      Add commented, disabled `calendar:` and allowlist entries to `config.yaml`.
- [x] 1.3 Declare `pyjwt[crypto]` in `pyproject.toml`. `uv lock` must add no new package;
      check the package names in the `uv.lock` diff.

## 2. Calendar client

- [x] 2.1 Tests:
      - token assertion claims and scope;
      - token reuse until expiry minus 60 s;
      - the single 401 retry;
      - field mask and pagination bound on list (no `q`);
      - the insert body key set, `sendUpdates=none`, and RFC 3339 offsets for a normal, an
        ambiguous and a DST-boundary time;
      - the two-target request enumeration.
- [x] 2.2 `henk/calendar/token.py` and `henk/calendar/client.py` on the shared
      `httpx.AsyncClient`, with `iat` 30 s in the past.
- [x] 2.3 Tests from *Only owner-reviewed Homelab events are admitted* (including "never
      logged or persisted" for non-admitted titles) and *An event holds alerts only under
      one predicate*. Then implement `admit()` and `holds()`.

## 3. Tools

- [x] 3.1 Tests from *calendar_upcoming lists the next 14 days, titles and times only*,
      with times from `render_instant`.
- [x] 3.2 `CalendarUpcomingTool` in `henk/tools/calendar.py`.
- [x] 3.3 Tests from *calendar_create_event writes exactly what the owner approved* and
      *Calendar tools carry a declared tier and scope*. Cover:
      - the verbatim prompt via `ApprovalGate.format_prompt`;
      - `_render_argument` is the identity on every accepted title;
      - the weekday check under a non-English locale;
      - extra argument refused;
      - result text with and without the hold sentence.
- [x] 3.4 `CalendarCreateEventTool` (mutating, per-instance, owner-only), with
      parameter-schema patterns and execution-time validation using the fixed `WEEKDAYS`
      table. Register both tools in `build_production_registry` behind
      `calendar.enabled`, with the empty-allowlist WARNING.
- [x] 3.5 The system-prompt rule (events only at the owner's request), with the test from
      *Henk does not propose calendar events on his own initiative*.

## 4. Audit records

- [x] 4.1 Tests from the audit-log delta:
      - version 6;
      - `calendar`, `hold` and `hold-release` records validate;
      - no title or payload in them;
      - one `hold-release` per window;
      - no second `hold` on a duplicate;
      (These two rest on the group-6 row guards; group 4 ships the writers that
      leave uniqueness to the caller, and the behaviour is tested in 6.1/6.3. See
      notes/apply-decisions.md, group 4 item 6.)
      - rehydration is unchanged by hold records.
- [x] 4.2 Record builders in `henk/audit/logger.py`, `SCHEMA_VERSION = 6`, and the
      committed version-6 JSON Schema document. The create tool writes its `calendar`
      record only after a confirmed insert. (`CalendarReceipts` is in place; the call
      site is the create tool, 3.4.)

## 5. Window provider

- [x] 5.1 Tests from *Maintenance windows come from a persisted, fail-open snapshot*: grace
      edge, restart load, stale fail-open with one WARNING, failed refresh keeps the
      snapshot.
- [x] 5.2 `henk/calendar/windows.py`: refresh loop, atomic snapshot beside the audit log,
      load at startup before intake, `active(t)`.

## 6. Hold and release

- [x] 6.0 **Hard gate. No group-6 code before this.** On rp5, probe whether ntfy sends
      every cached `since=` message before the first `keepalive` frame on the events
      topic. Use a throwaway subscription with a recent `since` and log frame types only,
      never message bodies. Record the result in `notes/ntfy-keepalive-probe.md`. If the
      probe disagrees with design D8, the caught-up rule follows the probe (a full
      keepalive interval with no message frame after subscribe).
- [ ] 6.1 Tests from *Events published inside an open maintenance window are held*:
      - publish-time keying;
      - mixed batch;
      - held-only and duplicate-only batches advance;
      - fail-open on a storage failure;
      - late event for a released window, and event during an in-flight release;
      - no cooldown armed.
- [ ] 6.2 Implement holding:
      - `hold_windows` and `held_events` tables in `henk/store/db.py`, with a repository;
      - the classification in `EventCoordinator.dispatch_batch` ahead of
        `pipeline.evaluate`, never returning early;
      - publish time from the ntfy frame's `time`;
      - arrival time stored.
- [ ] 6.3 Tests from *Held events are released after the window, the grace, and intake
      catch-up*:
      - recovered sends nothing;
      - a still-firing alert is forwarded once;
      - the long-outage backlog case;
      - message frames alone are not catch-up;
      - dispatch in progress blocks release;
      - the 15-minute uptime fallback;
      - a crash before durability releases again with the checkpoint unchanged;
      - extension defers, a moved window does not;
      - cooldown and cap apply;
      - release with the calendar disabled.
- [ ] 6.4 Implement release:
      - intake `on_keepalive` callback, reset per subscribe and separate from proof of
        life;
      - debounce batch and `dispatch_in_progress` on the coordinator instance, the latter
        set at flush and cleared in `finally`;
      - the release task, run at startup and every 60 s;
      - an in-flight set;
      - a `HeldRelease` descriptor on `EventTurn`;
      - `EventPipeline.evaluate` accepts it;
      - suppression records for cooled-down forwarded identities.

## 7. Agent core and triage

- [ ] 7.1 Tests from *Owner turns carry the maintenance-window note*, *A released triage
      says what the window held* (both line variants, singular form, incomplete-notice
      path, non-announceable sends nothing, live turns unchanged) and *Held events are
      triaged only when released*: framing, and age over 24 h.
- [ ] 7.2 Implement:
      - `release_sink` beside `_handoff_sink`, firing on the audit write's `ok` or with no
        audit configured;
      - the maintenance block in owner-turn composition, after the time header and before
        recall;
      - hold framing in the event-turn framing;
      - the release line after the suppressed-count note and on the incomplete notice.
- [ ] 7.3 Wire the provider, release task and tools in `henk/runtime.py`. Confirm that with
      `calendar.enabled` false and no held rows, the registry and startup are unchanged.

## 8. Review, deploy and live checks

- [ ] 8.1 Full suite green. Run `/codex:adversarial-review` (new credential, new external
      write, new durability path) and record what it found and what was done about it.
- [ ] 8.2 On rp5, probe `oauth2.googleapis.com` from the henk netns, using the same
      one-liner as the 2026-10-30 probe. If it does not return 200, add the DOCKER-USER
      egress rule first.
- [ ] 8.3 The owner places the key, by hand, on rp5:
      1. `base64 -w0 key.json > key.b64` in a mode-700 directory, with the file at mode
         600.
      2. Append `GOOGLE_CALENDAR_SA_KEY_B64=` plus that value to henk's `.env` with an
         editor, never on a command line.
      3. Delete `key.json` and `key.b64`.
      4. Confirm in the GCP console that domain-wide delegation is off for the `henk`
         account.
      5. Set `calendar.calendar_id` and the creator allowlist in rp5's `config.yaml`.
      6. Deploy per the README runbook and flip `calendar.enabled`.
- [ ] 8.4 Live checks over Signal:
      - "what homelab work is planned?";
      - one create answered `no`;
      - one answered `yes` and seen on the calendar;
      - a 10-minute test window with a test alert inside it, observing hold, release and
        the release line (or no message when it recovered).
- [ ] 8.5 Run `/docs-update` for the Henk page (calendar, egress exception), then
      `openspec validate maintenance-calendar --strict` and archive.
