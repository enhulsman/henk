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
