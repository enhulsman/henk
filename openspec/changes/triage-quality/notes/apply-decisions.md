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
