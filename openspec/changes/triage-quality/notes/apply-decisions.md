# Apply decisions: triage-quality

Per-group decisions made during implementation, the tests this change deliberately
inverts (standing rule 6), and each group's mutation table. Counts and shapes only, per
standing rule 1.

## Group 1b: the first-case capture (`henk/replay/capture.py`)

**Decisions.**
- The capture is a plain synchronous `httpx.Client` script, `python -m henk.replay.capture`.
  It does not go through `HomelabQueryTool`, which cannot pin instant queries. Its
  routing, step and span are instead pinned against the tool's own `_prometheus_request`
  with the clock at `T`, for every registry plan
  (`test_every_registry_request_matches_what_the_live_tool_would_send`).
- The per-`T` count is pinned as a hand-derived literal: 155 static requests plus one
  named-container follow-up per name in that `T`'s own `container_state` results. The
  breakdown is in the test's `STATIC_REQUESTS_PER_T` comment.
- Named-container names are the union of the `name` labels across **all** of that node's
  `container_state` answers at `T`, not just `last_seen`. A name seen only in
  `restarts_24h`, for a container that ran in the last day, still gets its follow-up.
- A non-200 answer and a transport failure are recorded (status or error class, plus the
  body), and the capture continues. It never raises halfway through a `T`.
- Output files are `0600` in `0700` per-`T` directories named `YYYYMMDDTHHMMSSZ`, with a
  `manifest.json` per `T`. A repeated `T`, or an existing `T` directory, is refused
  before any request is issued.
- rp5's effective `homelab_query.query_range_max_points`: the key is absent from rp5's
  `config.yaml` (read-only grep, 2026-09-23), so the default of 60 applies. rp5's checkout
  (`ffb737e`) carries the same default and a byte-identical registry and tool.

**Mutation table** (20 mutants, each applied alone against `tests/test_replay_capture.py`).

| # | defect | result |
|---|---|---|
| M1 | instant query omits `time=` | killed |
| M2 | range `end` is `T - 1` | killed |
| M3 | step ignores `max_points` | killed |
| M4 | `range_roles` ignored (`node_mapping` becomes a range) | killed (by the strengthened parity test; the first version let it survive) |
| M5 | written-out copy used despite a registry role | killed |
| M6 | `host_service_state` rp5 not unavailable | killed |
| M7 | names from `last_seen` only | killed |
| M8 | `stat` follows symlinks | killed |
| M9 | mode check is a `& 0o077` mask (accepts sticky `1700`) | killed |
| M10 | owner check dropped | killed |
| M11 | host-unit regex with a single backslash | killed |
| M12 | range function over an `or` | killed |
| M13 | transport failure raises | killed |
| M14 | non-200 body dropped | killed |
| M15 | files world-readable | killed |
| M16 | per-`T` `mkdir(exist_ok=True)` | survived: **equivalent**, because the pre-existence check and the duplicate-`T` refusal both run first |
| M17 | existing-`T` pre-check dropped | killed |
| M18 | output path checked after planning | survived: **equivalent**, because planning issues no request |
| M19 | not-derivable combinations not skipped for written-out roles | killed (by the added "D3 domain before the D3 roles" test; the first version let it survive) |
| M20 | `restarts_24h` lookback spelled `1d` | killed |

M16 first survived as a real defect: a repeated `--at` value passed the pre-check, then
failed with a bare `FileExistsError` after 155 requests. The duplicate-`T` refusal and its
test were added for it.

**Review gate (fresh project-scrutinizer, 2026-09-23): APPROVED, four minor findings.**
- Follow-up completeness: fixed. Each manifest carries `named_followups` per node
  (`complete`, `failed_roles`, `names`). A failed `container_state` answer contributes no
  names, and the rebuild must not read that node's follow-ups as the whole set. Mutants
  M21 and M23 are killed.
- A private directory inside the repository checkout is now refused, because the output
  holds tailnet addresses. Mutant M22 is killed.
- `main()` is now tested: epoch and ISO-with-zone `--at`, a zoneless ISO time refused,
  `--max-points` reaching the records, and exit 2 on a refusal.
- Forward note for group 12b, not a 1b defect: the D15 drift check compares the
  expression, step and `max_points`, but not the kind. `rebuild` must also compare each
  record's `kind` against the current plan's range-ness, or a matrix could reach a
  renderer that expects a vector. Every record carries `kind`.
- rp5/rp2 `host_service_state` stays uncaptured, as D5 specifies. The closure costs
  nothing: the systemd collector was measured absent on both nodes, so there is no series
  to lose to retention.

## Group 5: audit schema v5

**Decisions.**
- v5 (`henk/audit/schema/audit-record.v5.schema.json`) was generated from v4 by script.
  The only changes are `$id`, the top-level description, the `schema_version` const 5,
  the `memory_hash` and `outcome` descriptions, and the four new properties.
  `test_no_v4_field_is_changed` enforces that.
- `outcome`'s `no-reply` value is documented in its description, not enforced as an
  enum. An enum would tighten a v4 field, which D16 forbids. `effort` has no enum either:
  D16 names no value set, and an enum would reject a future SDK level.
- The four new `session_record` keyword arguments default to null and are always
  present in the record, the same convention `memory_hash` uses. `[]` and null stay
  distinct. No caller is wired here: group 8 writes `prior_handoff_ids`, group 9
  `profile`/`effort`, and group 10 `recording_id`.
- **`prior_handoff_ids` holds the handoff archive's integer row ids** (decided after
  the review gate). D8's archive has an `id` and a nullable `message_id`, and the spec
  asks the record to list *every* handoff the digest showed. A handoff without a
  published id would be unrepresentable as a message id. The schema's `items` are
  `integer`, and the builder refuses a bare string and any non-`int` id, `bool`
  included. **Group 6 must create `handoffs.id` as `INTEGER PRIMARY KEY AUTOINCREMENT`**,
  so a row id is never reused after pruning. Known limit: a store rebuilt from nothing
  restarts at 1, which makes older audit references ambiguous. That is accepted as rare,
  and the recording carries the digest text.

**Inverted version pins** (standing rule 6, following cc2ed38's v3 → v4 precedent; none
weakened, per the review):
- `tests/test_audit_receipts.py`: `test_schema_version_is_four` became `_five`, and v4
  joined the prior-documents loop.
- `tests/test_audit_v4.py`: the current-version pin moved to `test_audit_v5.py`. The v4
  test now pins the v4 document's name and const. The closed-vocabulary test asserts the
  v4 reminder branch's `detail` enum. The v3-rejection test validates against v4
  explicitly.
- `tests/test_reminders_delivery_audit.py`: delivery records validate against the v4
  document when relabelled v4.

**Mutation table:** 25 mutants, all killed. The implementer's 22: version left at 4;
the current path still pointing at v4; the v4 path aliased; `profile` required;
`recording_id` non-nullable; the `profile` enum removed; `prior_handoff_ids` items
untyped; `effort` untyped; `additionalProperties` added; `memory_hash` non-null; the v4
`transition` enum narrowed; the `memory_hash` description not rewritten; `no-reply`
undocumented; the v4 document edited; the `recording_id` kwarg dropped; the `profile`
kwarg dropped; the id list aliased; `[]` collapsed to null; the `effort` key omitted; the
bare-string guard removed; rehydrate skipping `profile` records; the cap count reading
`recording_id`. Plus three for the integer-id fix: the builder's `type(...) is int`
loosened to `isinstance` (a `bool` would pass), the builder's id check removed, and the
schema `items` loosened to `number`.

Out of scope, and already stale before this change: `README.md:169` still says
`schema_version: 2`. It belongs to the group 13 close-out.

## Group 2: config surface

**Decisions.**
- **Absent versus null.** `None` already means "defer to the CLI" for the reasoning keys
  (`_require_choice`), so an `INHERIT_CHAT` sentinel plus `AgentConfig.__post_init__`
  carries "absent". A directly built `AgentConfig(model=...)` also inherits the chat
  values, so the default holds in both places (D17). The loader passes the resolved chat
  values explicitly.
- **`event_model` is validated only when present** (fixed after the review gate).
  Absent inherits `agent.model` exactly as loaded. That value is not validated and loaded
  before this change, so a null, blank or non-string chat model must not start refusing
  under a key the owner never wrote. Pinned by
  `test_an_absent_event_model_never_refuses_what_the_chat_model_accepted`.
- **Narrower readings than the tasks.** A present blank or non-string `event_model` is
  refused, and so are a null or blank `replay.judge_model` and a null
  `replay.judge_effort`. The last because a grade records the judge's effort, and a
  CLI-chosen one would make two grades of one case incomparable. Unknown keys inside
  `triage_recording` and `replay` are refused, so no key can seem to move a directory or a
  bound. Other sections still ignore theirs. The D14 `^claude-` pattern on `judge_model`
  is left to group 12a.
- **`replay.*` is validated on every `Config.from_dict`**, so a typo in that section
  stops live Henk from starting, although only the owner-run replay reads it. This is a
  conscious choice, consistent with the fail-fast house style, and harmless with the
  section absent, as on rp5.
- **Derived directories.** `AuditConfig.triage_recordings_dir`, `triage_cases_dir` and
  `triage_replays_dir` are properties, `Path(audit.path).parent / "triage-…"`, with the
  names D13 and D14 give. They follow the `events.audit_path` fallback that rp5 uses.
- **The sample `config.yaml`** carries the profile keys commented out under `agent`,
  plus `triage_recording` and `replay` sections. This changes the tracked file, which
  rp5 holds locally modified, so the next deploy's pull must merge it (group 13).

**For group 9.**
- **2.1 is deferred in part.** No event factory exists yet (`henk/runtime.py:135-142`
  builds one). So 2.1 asserts the resolved values on `Config`, plus a factory built by a
  test-local helper (`tests/test_config_triage.py`, `_factory`) the way `runtime.py`
  builds the chat one. That cannot catch a wiring bug, so group 9 must re-assert 2.1
  through the real event factory.
- **`dataclasses.replace` does not re-inherit.** `replace(cfg, model=..., effort=...)`
  passes the already-resolved event values back in (see the comment on
  `AgentConfig.__post_init__`). `tests/test_runtime.py:192` overrides the chat effort this
  way, and an "event follows chat" assertion written the same way would be wrong without
  anyone noticing.

**Mutation table:** 22 mutants from the implementer, all killed:
- M1–M3: an event key falling back to the class default instead of the resolved chat
  value;
- M4: null `event_model` accepted;
- M5: `judge_effort` not validated;
- M6: null `judge_effort` accepted;
- M7, M8: a dataclass default changed;
- M9, M10: a loader default changed with the dataclass unchanged;
- M11: the dataclass no longer inheriting;
- M12: the dataclass inheriting over an explicit None;
- M13: unknown keys ignored;
- M14: the recordings directory under the file path;
- M15: the replays directory misnamed;
- M16: event effort not validated;
- M17: event thinking checked against the effort levels;
- M18: the non-mapping section check dropped;
- M19: a `max_recordings` key added;
- M20: a live event key in the sample;
- M21: `judge_model` changed in both places together;
- M22: the recordings directory hardcoded.

The review-gate fix is pinned by three new parametrized cases, which fail against the
pre-fix code.

## Group 3: container and trend evidence

**Decisions.**
- **`for` values and their sources.** `HenkSwapPressure` 15m and `HenkDiskPressure` 15m
  come from the henk-folder rules table,
  `openspec/changes/archive/2026-09-02-read-depth/notes/backend-probe.md:498` and `:497`.
  The memory rule (`High memory usage`) is 5m, from its own section at `:525`.
- **DNS `for` values are pinned beyond D2's table**, from the DNS rules table at
  `backend-probe.md:341-347`: 5m warning and 2m critical per node, and 10m for the
  fleet-wide `DNSProcessingTimeCritical`. Without them, the test "none absent from the
  record" (`pinned_rule_for_windows`, `tests/test_query_registry.py:167`) would be
  vacuous for DNS.
- **rp2 sits in `container_state`'s own domain tuple, as not-derivable**
  (`CONTAINER_NODES`), and not in `tuple(CADVISOR_JOBS)`. The rp2 named-container
  follow-up raises `QueryRefused(..., NOT_DERIVABLE)`, which the capture skips.
- **`QueryPlan.unavailable_aspects`** carries each matching aspect hole's reason. The
  renderer reads it and prints the reason in the aspect's own column (3.7).
- **The rp5 `health_state` reason is shortened**, because it now prints in every row.
- **`restart_aspect_holes` is derived from `RESTART_VERIFIED_JOBS`**
  (`henk/tools/query_registry.py:116`, `:445`). The test parses that set from the
  evidence-probe verdict lines (3.8).
- **`homelab_health.py`'s `is_trigger` guard is dropped.** It only ever covered memory,
  disk and load, none of which is a swap branch, so behaviour is unchanged.
- **The auto-name regex is `[a-z]+_[a-z]+\d?`** (`henk/tools/query_renderers.py:651`).
  The annotation is hedged ("looks auto-generated"), because a hand-chosen
  `snake_case` name matches the same shape.
- **`container_state` points at `memory_movers`, which dangles until group 4.** This is
  accepted, because deploy is all-at-once in group 13. **Group 4's gate must add a test
  that every backticked query name in any caveat is registered.**
- **The four `container_state` rows are retired from `capture.WRITTEN_OUT_TEMPLATES`**
  (5 remain for group 4: three `memory_movers` rows and two `host_service_state` rows).
  They are pinned byte-equal in the registry. The total is still 155 static requests
  per `T`.
- **The window's end is the request's end (review-gate finding 1).** The first
  implementation set `_Summary.window_end = last_at`. A series that stopped 20 minutes
  early therefore rendered "Window ends at" its last sample, and read as current to the
  window's end. The fix:
  - `HomelabQueryTool._run` reads the clock **once** per invocation and attaches it via
    `with_range_end(plan, end, max_points)` (`henk/tools/query_registry.py`). This sets
    `QueryPlan.range_window`, a `RangeWindow(start, end, step)` holding exactly the
    parameters sent.
  - `_prometheus_request` uses `plan.range_window` when it is set. It falls back to the
    clock only for a plan that never went through `with_range_end`, which is how the
    capture's parity test calls it. That test is unmodified and green, with byte-identical
    params.
  - The renderers read the end from the plan, never from a clock. A group 12b rebuild
    passes `T` through the same `with_range_end`.
  - **A deliberate deviation from the brief's `range_end` field.** The plan carries
    start, end and step, not the end alone, because D1's "last point's evaluation time"
    is not `end`. Prometheus evaluates at `start + k*step <= end`, and with 60 points the
    step divides no supported window: the last point falls 4 s short of the end at
    15m/1h, 314 s at 6h, 1430 s at 24h, about 2.8 h at 7d and about 12.2 h at 30d. A gap
    judged against `end` would therefore flag every healthy 24h series as 24 minutes
    silent.
  - The rendered result says "Window ends at `<end>`". A series whose last sample falls
    more than half a step short of the last range point gets "No sample for the last N
    minutes of the window", with N measured from the last sample to `end`.
  - A series that is current to the last point, where that point lies a minute or more
    before `end`, gets "The last range point is at …" instead.
  - A plan with no window says it has no end time. It never falls back to the last
    sample.

**Inverted tests** (standing rule 6; the review found none weakened). Line numbers were
re-grepped in the staged files on 2026-09-23.
- **Rule 6's four:**
  - `tests/test_query_dispatch.py:157` (was :155), now
    `test_container_state_on_rp2_is_not_derivable_not_an_error_or_an_empty_list`;
  - `tests/test_query_dispatch.py:203-220` (was :196). The three-outcome test's refusal
    is now a `node_resource_trend` on `nas`, and `container_state(rp2)` lands on
    not-derivable at `:214`;
  - `tests/test_query_registry.py:604-617` (was :528-530). `is_trigger` became `branch`
    (`fullness` / `pressure`);
  - `tests/test_query_renderers.py:925-930` (was :918). "not observable" became the
    reset caveat.
- **Beyond rule 6's list:**
  - `tests/test_query_dispatch.py:96` and `:257`/`:261`: the out-of-domain example rp2
    became `nas`, and rp2 now asserts `NOT_DERIVABLE`;
  - `tests/test_query_registry.py:482`: the domain is {rp5, vps, rp2}, as its own tuple;
  - `tests/test_query_registry.py:445`: the domain test now parses the triage delta
    (`:449`), because the read-depth spec still says {rp5, vps};
  - `tests/test_query_renderers.py:371`: "primary" became "pressure branch";
  - `tests/test_query_renderers.py:386`: "not the rule's trigger" and "normal" are now
    absent, and the clear-bar line is present (`:396-398`);
  - `tests/test_query_renderers.py:976-978`: the rp2 follow-up is `NOT_DERIVABLE`;
  - `tests/test_replay_capture.py`: the instant count went from 23 to 31 (`:281`), and
    the expression count rose by 8 (`:333`). D5 byte-equality is split into 4 registry
    rows and 5 script rows (`RETIRED_TO_REGISTRY` `:344`, test `:354`). `source` is
    "registry" for the retired rows (`:452`).

**Mutation table:** 25 mutants from the implementer, all killed after one fix (M19's).
- M1–M3: the summary times (first occurrence, last occurrence, window end);
- M4–M5: a `for` dropped, or mistranscribed;
- M6, M20: rp2 back to out-of-domain, or its `Unavailable` removed;
- M7: `max by (name)` removed. The per-CPU evaluator test kills it;
- M8: the auto-name annotation unhedged;
- M9: `holes = {}`, the 3.7 demonstration;
- M10: the plan drops the aspect reasons;
- M11–M12: the verified set or the hole predicate diverging from the verdict lines;
- M13: the rp2 follow-up raised as `OUT_OF_DOMAIN`;
- M14–M15, M24: the swap branch lines;
- M16: the auto-name regex loosened;
- M17: the top-3 ordering reversed;
- M18: MiB computed as 10^6;
- M19: the 15m and 24h restart columns swapped. It first survived, because every fixture
  restart fell inside both windows. A container that restarted 2h ago was added
  (`test_a_restart_is_counted`), and that kills it;
- M21: a container row not retired from the capture;
- M22: the DNS `for` dropped;
- M23: an invented DNS `for`;
- M25: a missing health reading rendered as zero.

The implementer's first mutation run gave false results from stale `.pyc` files. It was
re-run with `python -B` / `PYTHONDONTWRITEBYTECODE=1`, and the results above come from
that run.

**The review-gate fix for finding 1 adds 8 more mutants**, each applied alone and run
with `python -B` against the triage-evidence, dispatch, renderer and capture tests. All
8 were killed:
- W1: the summary line renders `last_at` as the window end;
- W2: a plan without a window falls back to `last_at`;
- W3: the "no sample for the last N minutes" line is dropped;
- W4: the gap minutes are measured to the last range point, not to `end`;
- W5: the gap is judged against `end`, not the last range point;
- W6: the request ignores the plan's window and reads the clock again;
- W7: the tool never attaches the window to the plan;
- W8: the uneven-step note is dropped.

The fix also added a clear-bar `swap_io` case (`[3.0, 4.0]`) to the forbidden-wording
sweep (review finding 3).

**Contradictions with the design.**
- Rule 6's inversion list was incomplete. The additional inversions are listed above.
- The design's path for `backend-probe.md` has moved: the file is now under
  `openspec/changes/archive/2026-09-02-read-depth/notes/`.
- D1's window end was first implemented as the last sample. The review gate found it, and
  it is fixed as described above. D1's "the last point's evaluation time" is also not the
  request's `end` whenever the step does not divide the window, which at 60 points is
  every window. The result states `end` as the window's end, and uses the last range
  point only to decide whether a series has gone silent.
