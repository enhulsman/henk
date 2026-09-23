> **TDD is not optional here.** Every group writes its tests from the spec scenarios
> *before* its implementation. Each spec scenario maps to at least one test, and the task
> that names a test names the scenario. Where a group's tests pass on first run, mutate the
> implementation to confirm they actually bind: a green suite that survives a deliberate
> defect is not evidence. Record each group's mutation table in `notes/apply-decisions.md`.
>
> **Standing rules for this change.**
> 1. **No live data enters the repo.** Recordings, handoffs, rule payloads, audit
>    records and memory content from rp5 never appear in fixtures or `notes/`. Fixtures
>    use placeholder hostnames (`host-a.example`), placeholder unit names
>    (`example-a.service`) and RFC 5737 addresses (`192.0.2.x`). Probe notes record label
>    names, counts and shapes. Run `.githooks/pre-commit` deliberately over `notes/*.md`,
>    including `notes/2026-09-23-vps-swap-incident-findings.md`.
> 2. **The probe wins.** Where a group-1 record contradicts `design.md`, the record
>    goes into `notes/evidence-probe.md` and the code follows the record.
> 3. **Every claim about an existing mechanism cites the `path:line` read when the claim
>    was written.** The `design.md` line numbers were read on 2026-09-23. Re-grep
>    `henk/agent/core.py` before relying on them, because `owner-acknowledgement` edits it
>    too.
> 4. **Test defaults through `Config.from_dict` with the key absent**, never against a
>    dataclass attribute. rp5's `config.yaml` carries none of the new keys.
> 5. **No spend without the owner.** Only the owner runs `replay run`/`grade` against a
>    real model. Tests use fake sessions and fake message streams.
> 6. **Tests this change deliberately inverts.** The spec changed, so update these, don't
>    weaken them, and record each one in `notes/apply-decisions.md`:
>    - `tests/test_recall.py:208` (`test_event_turns_never_carry_the_block`) and `:217`
>      (`test_owner_followup_in_an_event_started_session_gets_the_block`), in group 7;
>    - `tests/test_query_dispatch.py:155` (`test_container_state_rejects_rp2_with_an_error_not_an_empty_list`)
>      and `:196` (the `container_state` rp2 refusal inside the three-outcome test), in
>      group 3;
>    - `tests/test_query_registry.py:528-530` (`is_trigger` on the swap thresholds), in
>      group 3;
>    - `tests/test_query_renderers.py:918` (asserts "not observable"), in group 3.
>
> **Group dependencies.** Each group is one sub-agent, with a review gate between groups.
>
> | group | what | needs | notes |
> |---|---|---|---|
> | 1 | measure | — | owner step 1.3; 1.9 is a record of what was already preserved |
> | 1b | capture for the first case | — | agent-run; **run it soon, hard limit 2026-10-07 06:30Z** (Prometheus retention of the 24h windows); independent of groups 2–11 |
> | 2 | config | — | |
> | 3 | container and trend evidence | 1.1 | parallel with 2, 5 and 6 |
> | 4 | host-coverage queries | 3 | same files as 3 |
> | 5 | audit v5 | — | parallel with 2 and 3 |
> | 6 | handoff archive | 2 | |
> | 7 | event-turn framing, times, recall, markers | 2, 5 | |
> | 8 | digest and recurrence | 6, 7 | |
> | 9 | profile and ending classifier | 2, 5 | edits `core.py`; runs after 8, or in a worktree rebased onto 8 |
> | 10 | recording | 5, 7, 9 | |
> | 11 | replay | 10 | |
> | 12a | rubric, judge, `compare`, `grade` | 11, 1.3 | |
> | 12b | `cases`, `rebuild`, targets seam, fixture, first case | 3, 4, 11, 1b, 1.9 | 12b's owner step 12.6 also needs 12a for its final `grade`; nothing else in 12b does |
> | 13 | deploy and close-out | everything | |

## 1. Measure first (probes; owner steps marked)

- [ ] 1.1 Re-probe on the live Prometheus, read-only, and record label names and counts:
  - the label sets of `container_memory_working_set_bytes`, `container_memory_swap` and
    `container_cpu_usage_seconds_total` on both cadvisor jobs, and whether the CPU counter
    carries a per-`cpu` label. This decides D4's `max by (name)`;
  - how many `name!=""` series and how many `id=~"/system\\.slice/.+\\.service"` series
    each job has;
  - that `node_systemd_unit_state` exists only for `node-exporter-vps`.

  The `High memory usage` `for` (5m) is already pinned at `backend-probe.md:525` and is
  not re-probed.
- [ ] 1.2 Record the SDK ending surface from the installed `claude_agent_sdk` 0.2.123, so
  that group 9 can build its fake stream from the real field paths:
  - the `AssistantMessage.error`/`.stop_reason` paths (`types.py:1026-1037`);
  - the `ResultMessage.stop_reason`/`.is_error`/`.api_error_status` paths
    (`types.py:1201-1225`);
  - the dropped `stop_details` (`_internal/message_parser.py:199,300`).

  This is a source read, with no model call.
- [ ] 1.3 **Owner:** in a `compose run` container on rp5, run one zero-tool session
  confirming that `claude-fable-5-1` is available to the credential. Record yes/no and the
  error class.
- [ ] 1.4 From rp5's audit log, count event-triage records per day (counts only). Confirm
  that 90 days fits in 500 archived handoffs and that 30 days fits in 200 recordings.
  If not, record the corrected constants for groups 6 and 10.
- [ ] 1.5 Confirm that the label set of a fired `HenkSwapPressure` payload matches the
  identity derivation and D9's node derivation, recording labels only. The value block is
  settled: `A or B` returns A's value.
- [ ] 1.6 Transcribe the rp5 restart measurement into `notes/evidence-probe.md` under the
  verdict line `restart-signal cadvisor-pi5: verified`: `wordle-web`, ~09:58:55 CEST
  2026-09-23, `resets()` 0→1 within one scrape, the counter from ~2014 s to 1.9 s,
  `container_start_time_seconds` and `changes()` flat, a scrape interval of ~30 s.
- [x] 1.7 **Recorded result (owner, 2026-09-23 ~10:15 CEST):** `docker restart
  taiga-docker-taiga-front-1` on the vps. `resets(...{job="cadvisor-vps",...}[15m])` went
  to 1, the counter fell to 0.61 s, and `changes(container_start_time_seconds[15m])`
  stayed 0. Verdict: `restart-signal cadvisor-vps: verified`. It is transcribed in 1.8.
- [ ] 1.8 Write 1.1–1.7 into `notes/evidence-probe.md` under standing rule 1, with one
  machine-readable verdict line per cadvisor job (read by test 3.8). Add the host-unit
  and systemd-state measurements from the findings notes: 27 and 36 unit series; the
  288/288 `failed` and 281/288 `activating` shapes, with placeholder unit names.
- [x] 1.9 **Preserved (owner, 2026-09-23).** Everything is in root-owned (`root:root 700`)
  `/var/lib/docker/volumes/henk_henk_audit/_data/triage-cases/2026-09-23-raw/` on rp5,
  which is `/data/audit/triage-cases/2026-09-23-raw/` in the container:
  - 10 Prometheus `query_range` exports covering 05:30–07:30 UTC at 15 s, with
    `prom-index.tsv`:
    - vps SwapTotal/SwapFree;
    - the pswpout/pswpin rates;
    - MemAvailable/MemTotal/Cached;
    - cadvisor-vps working set and swap for all 74 series;
    - `node_systemd_unit_state`.
  - The `henk-events` ntfy cache (all cached messages).
  - The `henk-handoffs` cache, fetched with the owner's henk-pickup token. Henk's own
    token is publish-only there and gets a 403.
  - The audit records for 2026-09-23 local time, filtered on epoch `at`.

  The last three were being completed as this was written. The main session corrects
  this entry if any of them failed.

  Two limits apply. The range export cannot answer the 6h/24h windows, and it is not
  served as query results. The v4 audit records hold tool **names** only, with no
  arguments or results (`audit-record.v4.schema.json:37-94`). Group 1b exists because of
  both.

## 1b. Capture for the first graded case (agent-run; run it soon, hard limit 2026-10-07 06:30Z)

Independent of groups 2–11: the script carries its own template list. It follows
**route (a)**, decided by the main session (design D15):
1. The script runs on the workstation, into a mode-700 scratch directory.
2. The output is streamed into a mode-700 staging directory in `pi`'s home on rp5, and
   the workstation copy is deleted.
3. One owner sudo step moves the staging directory into place and chowns it.

The capture is persisted only on rp5's audit volume.

- [x] 1b.1 Tests first, `henk/replay/capture.py`, with a fake transport:
  - *Instant captures are pinned to the evaluation time*: `time=T` on instant queries,
    and `end=T` plus the registry step on range queries;
  - *The capture covers every argument combination*: every PromQL expression of every
    registry entry, in every in-domain combination, including `scrape_targets`' `up` and
    `up_over_window`, plus the D3/D4/D5 canonical templates (design D5 table). There is
    no targets-API request and no Gatus request.

    The count is taken over the **union of the registry and the written-out templates,
    deduplicated by (query, role, arguments)**. The script uses a written-out template
    only for a role the registry does not yet have. So the count is unchanged, never
    doubled, once groups 3 and 4 land, and a test pins exactly that;
  - named-container follow-ups (`query_registry.py:781-814`) are captured per name at `T`,
    for every name present in that `T`'s own `container_state` results;
  - range requests use rp5's effective `homelab_query.query_range_max_points` (default 60,
    `henk/config.py:402`), and every captured file records `max_points` and the step;
  - the D5 `memory_movers` templates use the per-selector
    `max_over_time(a[w]) or max_over_time(b[w])` form, never a range over an `or`;
  - each saved response records `T`, the exact expression sent, the role, the arguments,
    `max_points`, the step, the HTTP status and the body;
  - the written-out templates are byte-equal to the D5 literals **hardcoded in the test**
    (never parsed from `design.md`, which moves at archive), including the
    single spelling `/system\\.slice/.+\\.service`.
  - *The capture refuses an unsafe output path*: the output path must be an existing
    mode-700 directory owned by the invoking user.
- [x] 1b.2 Implement the capture script from the registry's templates, with the D5 table's
  canonical templates written out for the roles the registry lacks. **D4 stays exactly as
  in that table, whatever 1.1 finds**, so this does not wait on 1.1 (design D5). Read
  rp5's effective `query_range_max_points` from its `config.yaml`, read-only. `HomelabQueryTool`
  cannot pin instant queries (`henk/tools/homelab_query.py:293`); its only clock seam is
  the range `end` (`:299`).
- [x] 1b.3 From the preserved ntfy and audit material, read the interval
  [notification time + 120 s debounce, the triage record's audit `at`]. Choose at least
  three `T` values spanning it, including both ends. Record the interval and the chosen
  `T` values, as times only, in `notes/evidence-probe.md`.
- [x] 1b.4 **Agent-run, soon; hard limit 2026-10-07 06:30Z:**
  - on the workstation, from the repo checkout, run the capture at each `T` into a
    mode-700 scratch directory, one `<T>/` subdirectory per `T`;
  - stream it to rp5 with
    `tar -C <scratch> -cf - . | ssh rp5 'umask 077; mkdir -p /home/pi/<staging>; tar -x -C /home/pi/<staging>'`;
  - verify the file count on rp5, then delete the workstation scratch;
  - in `notes/evidence-probe.md`, record the transfer (date, file count, both transit
    paths deleted or pending), the request and response counts per `T`, and any non-200
    statuses.
- [x] 1b.5 **Owner, one sudo step on rp5:**
  - `sudo mv /home/pi/<staging> /var/lib/docker/volumes/henk_henk_audit/_data/triage-cases/2026-09-23-capture`;
  - `cd /var/lib/docker/volumes/henk_henk_audit/_data && sudo chown 10001:10001 triage-cases && sudo chown -R 10001:10001 triage-cases/2026-09-23-capture`,
    leaving the root-only `2026-09-23-raw/` untouched.

  The agent then confirms that the staging directory is gone and records it in
  `notes/evidence-probe.md`.

## 2. Config surface

- [ ] 2.1 Tests first, *Defaults change nothing*. A config omitting `agent.event_model`,
  `agent.event_effort` and `agent.event_thinking` yields event values equal to the
  **resolved** chat values, including overridden chat values. Assert this through the
  built event factory's config.
- [ ] 2.2 Tests first, the explicit values:
  - `event_effort: null` and `event_thinking: null` defer to the CLI, which is not the
    same as absent;
  - `event_model: null` is refused, naming the key;
  - both keys are validated by the same `_require_choice` path as the chat keys.
- [ ] 2.3 Tests first:
  - `triage_recording.enabled` defaults to true;
  - `replay.judge_model` defaults to `claude-fable-5-1`;
  - `replay.judge_effort` defaults to `high` and is validated against `EFFORT_LEVELS`;
  - each default holds through `Config.from_dict` with its section absent.
- [ ] 2.4 Test the invariants:
  - `Secrets.from_env` is unchanged;
  - no new key names a path, token or URL;
  - no key configures an archive, digest or recording bound, because those are module
    constants;
  - the recording and replay-output directories are derived from `audit.path`
    (*Recording paths are derived from the audit path*).
- [ ] 2.5 Add the keys to the sample `config.yaml`, with the agreement test. The profile
  keys go in commented out, because absent is their default.
- [ ] 2.6 Implement the profile resolution in the agent section of `Config.from_dict`
  (today at `henk/config.py:847-854`), plus `TriageRecordingConfig` and `ReplayConfig`,
  with each default pinned in both places.

## 3. Container and trend evidence (homelab-tools)

- [ ] 3.1 Tests first, for `node_resource_trend` and `dns_performance`:
  - *Extremes carry their times*;
  - *A repeated extreme reports first and last occurrence*;
  - *DNS summaries carry the same times*.
- [ ] 3.2 Tests first:
  - *A comparison states the rule's for window* (15m swap, 5m memory);
  - the extended *Registry thresholds match the pinned record*;
  - the "not in the pinned record" sentence for a threshold with no recorded `for`.
- [ ] 3.3 Tests first, the swap wording:
  - *Swap fullness below its bar …*;
  - *Swap fullness above its bar …*;
  - *Swap results say both branches need checking*.

  Invert `test_query_registry.py:528-530` to `branch` (`fullness` / `pressure`). Assert
  that "practically fires on pressure", "anti-correlated", "primary" and "secondary"
  appear in no rendered swap result, and that "not the rule's
  trigger" appears in no rendered result.
- [ ] 3.4 Tests first:
  - *Memory and swap are reported per container*;
  - *Container memory carries no bar*;
  - *An auto-generated name is annotated* (it is hedged);
  - *Host units are pointed elsewhere, not silently missing*.
- [ ] 3.5 Tests first:
  - *rp2 container state is not available, and says so*;
  - *A named-container follow-up on rp2 is not derivable*;
  - the rewritten *An out-of-domain value is rejected, not answered emptily*.

  Invert `test_query_dispatch.py:155` and `:196`. Add a test that `container_state`'s node
  domain is its own tuple, not `tuple(CADVISOR_JOBS)`.
- [ ] 3.6 Tests first:
  - *A restart is counted*, on both nodes;
  - *Per-CPU series count one restart once*, from a fixture with several per-CPU series
    each resetting once;
  - *The restart caveat describes the measured behaviour*.

  Invert `test_query_renderers.py:918`.
- [ ] 3.7 Tests first, *An unavailable aspect is rendered in its place*. rp5's
  `health_state` hole renders its reason in the column via the plan, and a mutation that
  removes the renderer's read of the plan fails the test.
- [ ] 3.8 Tests first, *The restart aspect is traceable to a measurement*. Parse
  `notes/evidence-probe.md`'s verdict lines and assert the aspect exists for exactly the
  verified jobs.
- [ ] 3.9 Implement:
  - `_Summary` times (`henk/tools/query_renderers.py:137-173`);
  - `Threshold.for_window` and `branch`, with the swap line;
  - the container expressions, columns and annotation;
  - `CONTAINER_NODES`, with rp2's `Unavailable` and `named_container_expression`'s
    not-derivable branch (~`query_registry.py:797`);
  - the restart roles;
  - aspect holes rendered in place.

  Test first that the new `container_state` expressions and role names (`memory_working_set`,
  `swap`, `restarts_15m`, `restarts_24h`) are **byte-equal** to `henk/replay/capture.py`'s
  written-out templates and to the D5 template literals **hardcoded in the test**. The
  test never parses `design.md`, which moves at archive. In the D5 table, `\|` is
  Markdown escaping, not part of any template. Then retire those roles' copies from
  the script, which reads them from the registry from then on.

  Every template stays address-free.

## 4. Host-coverage queries (homelab-tools)

- [ ] 4.1 Tests first:
  - *The host-coverage queries are enumerated*;
  - the rewritten *No rule-state query is registered*: no entry reads `ALERTS` or a rule
    API;
  - the domain scenarios for `memory_movers` and `host_service_state`, including *Host
    service state is not available where the collector is missing*.
- [ ] 4.2 Tests first, `memory_movers`:
  - *A host unit's page-cache burst is named with its peak time* (a placeholder unit
    moving ~100 MB → ~1 GB);
  - *Range functions wrap only vector selectors*: no `_over_time(` whose argument is a
    parenthesised expression;
  - *Results join on the cgroup id*: fixtures in which the instant results lack
    `__name__` and the range results carry `__name__` and `container_label_*`;
  - *A cgroup gone at the window's end is flagged*: modelled on the 2026-09-23 shape,
    with a placeholder unit ranked first on 1h and not present at `T`;
  - *Containers and host units rank together*;
  - *Peak-time resolution is stated*;
  - *No series is not an empty ranking*;
  - the point budget holds at `24h`.
- [ ] 4.3 Tests first, `host_service_state`:
  - *A persistently failed unit is reported with its fraction* (288/288, placeholder
    unit);
  - *A crash-looping unit is flagged* (281/288 `activating`);
  - *A healthy host proves the query ran*;
  - *A silent collector is not health*.
- [ ] 4.4 Test first that the `memory_movers` (`movers_max`, `movers_min`, `movers_series`)
  and `host_service_state` (`bad_states`, `unit_count`) expressions and role names are
  **byte-equal** to `capture.py`'s written-out templates and to the D5 literals
  **hardcoded in the test**, which never parses `design.md`. The `bad_states` literal is
  `activating|failed`: the table's `\|` is Markdown escaping. Then
  implement both registry entries, their renderers and their caveats, and retire the
  script's remaining written-out copies, so it reads every template from the registry. Check that
  the system prompt's tool summary and the `homelab_query` description derive the enum
  from the registry, so neither is hand-edited.

## 5. Audit schema v5

- [ ] 5.1 Tests first:
  - *New records declare the new version*;
  - *Old records remain valid* (v1–v4);
  - every v5 field is optional and nullable;
  - an event record with a non-null `memory_hash` validates.
- [ ] 5.2 Tests first, *A mixed-version log rehydrates identically*.
- [ ] 5.3 Implement `audit-record.v5.schema.json`, `SCHEMA_VERSION = 5`, the path
  constant, the new `session_record` keyword arguments, and the version-history comment.

## 6. Handoff archive (triage-handoff)

- [ ] 6.1 Tests first, store:
  - a round-trip;
  - *Retention holds its bounds* (the 500/90-day constants, oldest first, in the insert's
    transaction);
  - the 32 KB marker and flag;
  - `_check_handoffs_columns`.
- [ ] 6.2 Tests first, tool:
  - *A published handoff is retained with its incidents*;
  - *An owner-session handoff is not retained*;
  - *A failed publish is not retained*;
  - *A retention failure does not fail the publish*;
  - *Incident context cannot come from the model*.
- [ ] 6.3 Tests first:
  - message-id parsing, both forms;
  - an empty id stored as `NULL`;
  - rule-key and node derivation: whole-word matching, and no address ever produced.
- [ ] 6.4 Implement the `handoffs` table and repository, `IncidentContext` and its
  provider, and retention in `PublishHandoffTool` after a 2xx when the context is
  non-empty.

## 7. Event-turn framing, times, recall and markers (agent-core, incident-triage, memory-store)

- [ ] 7.1 Tests first:
  - *Event turn framed for triage* (the order: recall, block, framing);
  - *Event turn with an empty store carries no recall block*;
  - *Framing follows the untrusted block*.
- [ ] 7.2 Tests first. Invert `test_recall.py:208` and `:217`.
  - *Event turns get memory*;
  - *Recall given at the event turn is not repeated*;
  - *Owner follow-up gets recall when the event turn could not read it*;
  - *Recall in an event turn cannot be written back*;
  - *Event turns stay tainted with recall*;
  - the continuation record inherits `memory_hash`.
- [ ] 7.3 Tests first:
  - *Framing directs branches, window, and missing evidence*;
  - *The handoff instruction asks for times*;
  - *Memory is framed as context only when present*;
  - *Docs are named only when registered* (no numbering gap);
  - *The arc and its check are unchanged*: the existing `test_triage_framing.py` arc
    tests stay green unmodified.
- [ ] 7.4 Tests first:
  - *Both times are present*;
  - *The notification time is not presented as onset*;
  - *A missing notification time is not invented*.
- [ ] 7.5 Tests first:
  - *A payload cannot close the block early*: exactly one begin and one end marker, and a
    recall-block marker placed in a payload is neutralised;
  - *A memory cannot open or close a block*: a stored memory containing the recall end
    marker, and one containing the untrusted begin marker;
  - *A memory cannot close the recall block*: the recall hash is computed over the
    neutralised render, and the stored memory is unchanged.
- [ ] 7.6 Tests first, *Owner turn unaffected* and *Handoff history does not enter
  recall*.
- [ ] 7.7 Implement:
  - `compose_event_turn_content` taking the recall block, the registered tool names and
    the incident times;
  - the D6 framing;
  - the D10 marker neutraliser, applied to every string placed inside the block and to
    memory content in `render_recall_block` (`henk/agent/recall.py:92-95`);
  - event-turn recall in `_process_event`/`_start_event_session` (`core.py:324-350`,
    `580-590`);
  - incident-context publication.

## 8. Digest and recurrence (triage-handoff, incident-triage)

- [ ] 8.1 Tests first:
  - *A same-identity handoff is shown without a recurrence*;
  - *Same rule, different subject relates*;
  - *Ranking and bounds hold*;
  - *The recurrence reference counts toward the total*;
  - *The digest is labelled and delimited as untrusted*;
  - *No related history means no digest*;
  - *The record names what history was shown*.
- [ ] 8.2 Tests first, *A retained handoff cannot close the block early*.
- [ ] 8.3 Tests first:
  - *Recurrence of a recently triaged incident*;
  - *Recurrence whose prior handoff is not retained*;
  - *Recurrence framing survives a restart*.
- [ ] 8.4 Tests first:
  - *Owner sessions never see handoff history*;
  - *No tool exposes the archive*.
- [ ] 8.5 Implement the digest renderer (with the constants), the recurrence reference and
  note, and `prior_handoff_ids` on the record.

## 9. Triage profile and ending classifier (agent-core, incident-triage)

- [ ] 9.1 Tests first:
  - *A configured triage profile applies to event sessions only*;
  - *Follow-ups stay on the triage profile*;
  - *Reset returns to the chat profile*;
  - *Profiles share the boundary* (the same registry and gate objects);
  - *The record names the profile*;
  - *A follow-up record names the factory's profile, not its trigger*;
  - *A plain owner session records the chat profile*.
- [ ] 9.2 Tests first, with a fake SDK message stream built from 1.2's field paths:
  - *An API error rendered as text is not delivered as the triage*: an
    `AssistantMessage.error` together with "API Error: …" text;
  - *A refusal produces an honest notice*: `stop_reason="refusal"` on the assistant
    message, and separately on the result message;
  - *A result-level API error is reported with its status*: `is_error` with
    `api_error_status=529`;
  - *An empty reply is no longer silent*;
  - *An errored triage is reported*;
  - *A suppressed incomplete triage stays silent*;
  - *The notice carries the suppressed count*: the notice goes through
    `_with_suppressed_note` (`core.py:377,420`);
  - *The incomplete-triage notice carries a pickup path and no invented diagnosis*.

  Assert that the classifier's order is structured signals first, that the notice
  carries no category and no model text, and that it is length-bounded.
- [ ] 9.3 Tests first: a fake session with no `ending()` method is treated as reporting
  no signal, so existing fakes keep working. `_SdkAgentSession.ending()` reflects the
  last turn only.
- [ ] 9.4 Tests first: owner-acknowledgement's *Event turns are not bracketed* still holds
  for event-factory sessions, if that change has landed.
- [ ] 9.5 Implement:
  - `ending()` on the `AgentSession` protocol, as an optional method (`session.py:50-62`),
    and in `_SdkAgentSession`;
  - the classifier;
  - the notice, replacing `core.py:375`'s send;
  - `event_factory`, and the second factory in `henk/runtime.py`;
  - factory-carried `profile` and `effort`, stamped on the accumulator.

## 10. Triage recording (triage-replay)

- [ ] 10.1 Tests first:
  - the recording schema validates the fixtures;
  - *A triage leaves a recording*;
  - *Denied calls are recorded too*;
  - *An errored triage is still recorded*.
- [ ] 10.2 Tests first:
  - *Recording does not widen audit capture* (`RESULT_CAPTURING_TOOLS` unchanged);
  - *A recording failure does not disturb the triage*;
  - *Recording can be turned off*;
  - *A failed recording leaves a null link, not a false one*;
  - *A triage record links its recording and history*;
  - *Non-triage records carry null evidence links*;
  - *References carry no content*.
- [ ] 10.3 Tests first:
  - *Reference cases survive recording retention* (the count bound of 20, refusal past
    it, no eviction);
  - *An oversized recording is marked, not silently cut*;
  - *Retention holds its bounds*;
  - *Rehydration ignores recordings*;
  - an atomic write.
- [ ] 10.4 Implement:
  - `_TranscriptAccumulator` and `transcript()`;
  - the per-turn slice;
  - `henk/replay/recorder.py`;
  - `triage-recording.v1.schema.json`, with `reconstructed` and the optional `reference`;
  - the derived `triage-recordings/` and `triage-cases/` directories;
  - pruning, which never touches `triage-cases/`;
  - `recording_id` on the record.

## 11. Replay (triage-replay, secure-deployment)

- [ ] 11.1 Tests first:
  - *A recorded call is served its recorded result*;
  - *An unrecorded call is answered honestly*;
  - *A handoff in replay is captured, not published*.
- [ ] 11.2 Tests first, *A replay that tries everything reaches nothing*:
  - a refusing transport records zero tool-originated requests;
  - `Store` is patched to fail the test if constructed;
  - no audit line and no send.

  Also *The hook still blocks built-ins*.
- [ ] 11.3 Tests first:
  - *A replay runs a recording on another model*;
  - *Drift is reported, not hidden*;
  - *An invalid effort spends nothing*;
  - *A malformed model identifier spends nothing*;
  - *A wrong compose project is refused*: an absent audit log means exit before any model
    call;
  - the replay module imports no channel, intake or ntfy module.
- [ ] 11.4 Tests first:
  - *A reconstructed case says what it is*;
  - *Any in-domain query is answerable from the capture*, for Prometheus-backed queries
    only;
  - *scrape_targets is rendered from up, with the targets part unavailable*;
  - *A Gatus-backed query is unavailable*: `endpoint_history`;
  - *A drifted template is not served*;
  - *Only case.json directories are cases*;
  - *Uncaptured tools are unavailable, not guessed*, including `homelab_docs`;
  - *The case cannot leak the answer through memory or history*;
  - *Original arguments are not invented*.
- [ ] 11.5 Implement `henk/replay/`:
  - the registry and stubs;
  - the refusing transport;
  - the null channel;
  - the run writer;
  - the audit-log guard;
  - `__main__` with `list` and `run`. `compare`, `grade`, `cases` and `rebuild` belong to
    group 12.

  Document the exact `docker compose run --rm --no-deps -e CLAUDE_CONFIG_DIR=… henk`
  invocation, and the must-run-from-the-checkout rule, in the README.

## 12a. Rubric, judge, compare and grade (triage-replay)

- [ ] 12.1 Write `henk/replay/rubric/triage-rubric.v1.md`: five criteria with 0–3
  anchors, the harness-limit instruction, and how to use a verified reference. Test *A
  rubric change is a new version*.
- [ ] 12.2 Tests first:
  - *Side-by-side comparison*;
  - *The judge has no tools*;
  - *The judge is blind to models*.
- [ ] 12.3 Tests first:
  - *The judge scores against the committed rubric*;
  - *A verified reference sharpens the grade*;
  - *Unparseable judge output is not a score*;
  - *A judge refusal is recorded as refused*.
- [ ] 12.3a Implement `compare` and `grade`: the judge session over an empty
  `ToolRegistry`, `replay.judge_model`/`judge_effort`, and thinking unset. If 1.3 found
  Fable unavailable, stop at the judge and record the blocker.

## 12b. Cases, rebuild, targets seam, fixture and the first case (triage-replay)

- [ ] 12.4 Commit a placeholder-safe fixture of the 2026-09-23 case to
  `tests/fixtures/replay/`, with the same shape as the real case:
  - captured-response files for a few `T` values;
  - placeholder hosts and units, and RFC 5737 addresses;
  - the original call sequence with names only;
  - a paraphrased reference: the fullness branch fired; the culprit was a host unit's
    page-cache burst triggered by a package upgrade; not a leak; recurs on upgrade.

  Add the test for *Fixtures pass the publication checks*.
- [ ] 12.5 Implement `cases`. Then implement `rebuild`, which
  builds one case per captured `T`, each as `triage-cases/2026-09-23-swap-T<HHMMSSZ>/case.json`,
  from four inputs:
  - the preserved ntfy event, composed with no recall and no digest;
  - the 1b capture, rendered through the current renderers;
  - the audit record's tool names, with arguments `unknown`;
  - the original handoff from the preserved `henk-handoffs` cache, plus the audit
    record's diagnosis and confidence, as the original candidate.

  Every non-Prometheus tool, `endpoint_history`, the targets part of `scrape_targets`,
  and `homelab_docs` return "unavailable in reconstruction". Add the
  `targets_unavailable` renderer seam, so that a reconstructed `scrape_targets` never
  prints "no scrape error recorded by the backend" (`query_renderers.py:337,355`). Test
  *A missing targets payload is not read as "no error"*. Before serving each captured
  response, check it for drift against the registry's current expression. The case's output states that it grades the current renderers. The
  reference comes from an owner-written file.

  Treat a mismatch in `max_points` or step against the current configuration as drift.
- [ ] 12.6 **Owner, on rp5:**
  - `triage-cases/` and the capture are already owned by `10001:10001` (task 1b.5). `sudo`
    copy `2026-09-23-raw/` to a directory owned by `10001:10001` (the henk uid,
    `Dockerfile:31,46`), mode 700, leaving the root-owned original untouched;
  - write the reference file from the findings notes' ground truth: the fullness branch
    (A=98.39) with the I/O branch also true; the `dmesg.service` page-cache burst from the
    rsyslog upgrade; not a leak, recurs on every upgrade; the applied fix;
  - run `rebuild`, which prints one case id per `T` (`2026-09-23-swap-T<HHMMSSZ>`). Check
    that `cases` lists exactly those ids;
  - for each case id, `replay run` it on the chat profile, then `grade` it. Grading needs
    group 12a.

  This is the first graded case. It stays on rp5.

## 13. Deploy, live checks, close-out

- [ ] 13.1 Full suite green. Record the pass count against the pre-change baseline.
- [ ] 13.2 Deploy to rp5 with no `config.yaml` edit. Confirm:
  - the container starts;
  - the `handoffs` table exists;
  - `docker compose run --rm --no-deps … henk python -m henk.replay list` runs from the
    checkout directory.
- [ ] 13.3 During one `compose run` replay with live Henk up, `docker inspect` the run
  container:
  - confirm `HostConfig.Memory` is 768m (805306368);
  - confirm `HostConfig.RestartPolicy` is empty or `no`, because compose's handling of
    `restart:` under `run --rm` is unverified;
  - confirm its `CLAUDE_CONFIG_DIR` differs from the live container's.

  Record rp5's host memory peak during the run.
- [ ] 13.4 On the next real triage, confirm:
  - a recording exists;
  - the audit record is v5, with `profile: event` and the matching `recording_id`;
  - the handoff is archived.

  On a related triage, confirm the digest appears, checked from the recording. Over
  Signal, confirm:
  - `container_state` shows memory, swap and restarts on rp5 and the vps, and "not
    available" on rp2;
  - `memory_movers` names host units;
  - `host_service_state` answers on the vps and reports "not available" on rp5 and rp2.
- [ ] 13.5 Record these homelab follow-ups in `~/.claude-config/docs/tooling-backlog.md`:
  - `HenkContainerRestarting`, using the measured `resets()` expression;
  - enabling the systemd collector on rp5 and rp2;
  - `--collector.systemd.enable-restarts-metrics` on the vps, plus its node_exporter
    0.18.1 upgrade;
  - an owner-side purge command for the `handoffs` table (D7 residual 2).

  Record the Opus comparison as the owner's next step.
- [ ] 13.6 Add a row to `operations/backup-recovery.md` via `/docs-update`:
  - `triage-recordings/` and `triage-replays/` on `henk_henk_audit`;
  - the archive inside `henk-store.db`;
  - effective retention: 30 days live plus the 4-week snapshot rotation.

  Present the doc diff before any commit. Update the README.
- [ ] 13.7 Run `openspec validate triage-quality --strict`, then archive. After archiving:
  - update every test that reads this change's `notes/` by path (task 3.8's
    `evidence-probe.md` reader), from `openspec/changes/triage-quality/` to
    `openspec/changes/archive/<date>-triage-quality/`, following the hardcoded pattern at
    `tests/test_query_registry.py:53`, and re-run the suite;
  - fill the new `openspec/specs/triage-replay/spec.md` Purpose from `design.md`;
  - confirm that no touched spec's Purpose is a placeholder;
  - re-grep the cited lines in `owner-acknowledgement/proposal.md`.
