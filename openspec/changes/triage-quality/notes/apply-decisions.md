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
