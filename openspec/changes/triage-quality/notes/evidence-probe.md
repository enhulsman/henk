# Evidence probe: recorded results

Measurements and owner-run checks for `triage-quality`, recorded as they happen. Times and
counts only: no payload text, tool output or addresses (the raw material stays on rp5).

## 1b.3: evaluation window of the 2026-09-23 HenkSwapPressure triage

Read on 2026-09-23 from the preserved material in
`/data/audit/triage-cases/2026-09-23-raw/` on rp5 (ntfy `henk-events` cache and the day's
audit records), by printing timestamps and record types only.

| Epoch | UTC | What |
|---|---|---|
| 1790144878 | 06:27:58 | ntfy notification time of the FIRING message (not onset) |
| 1790144998 | 06:29:58 | notification + 120 s debounce; an owner-message session record flushed at the same instant (the open chat session displaced by the event session) |
| 1790145026 | 06:30:26 | `session` record, trigger `event`, `triage_arc_complete: true`, confidence `moderate` |
| 1790145178 | 06:32:58 | ntfy notification time of the RESOLVED message |
| 1790145298 | 06:34:58 | `suppression` record (the resolved notification, not triaged) |

**Interval:** [1790144998, 1790145026], i.e. 06:29:58 to 06:30:26 UTC, 28 s. The displaced
owner session flushing at the debounce instant pins the start tightly. The end is when the
triage record was written, after the last tool call.

**Chosen `T` values:**
- 1790144998 (06:29:58Z, start)
- 1790145012 (06:30:12Z, midpoint)
- 1790145026 (06:30:26Z, end)

At a ~30 s scrape interval these span at most two samples, so the three captures should differ
little; a large difference between them is itself a finding to record under 1b.4.

## 1b.4: the capture at the three `T` values

Run on 2026-09-23 from the workstation checkout with
`python -m henk.replay.capture --prometheus-url <vps Prometheus> --max-points 60`. rp5's
`config.yaml` has no `homelab_query` section, so its effective `query_range_max_points` is
the default of 60. The output went into a mode-700 scratch directory.

| `T` | directory | requests | statuses | named follow-ups (rp5 / vps) |
|---|---|---|---|---|
| 1790144998 | `20260923T062958Z/` | 195 | 195 x 200 | 19 / 21, both complete |
| 1790145012 | `20260923T063012Z/` | 195 | 195 x 200 | 19 / 21, both complete |
| 1790145026 | `20260923T063026Z/` | 195 | 195 x 200 | 19 / 21, both complete |

Each `T` has 155 static requests plus 40 named-container follow-ups. vps has 21 names
against 19 current series because `restarts_24h` also sees containers that ran within the
day. There are no non-200 statuses. The only empty result is rp5 `container_state`
`health_state`, the registry's declared hole. Every 24h range reaches a full 24 h back
from `T`.

**Spread across `T`** (values only, sample timestamps stripped; a range is compared on
each series' last point):
- 062958Z → 063012Z: 132 of 195 results differ: `node_resource_trend` 64,
  `container_state` 43, `memory_movers` 21, `dns_performance` 3, `host_service_state` 1.
- 063012Z → 063026Z: 115 of 195 results differ: `node_resource_trend` 65,
  `container_state` 24, `memory_movers` 19, `dns_performance` 6, `host_service_state` 1.

Most of this is mechanical. `last_seen` moves every scrape. Each range's step grid is
anchored at its own `end=T`, so its last point moves with `T`. A ~30 s scrape lands inside
the 28 s interval. Whether the spread changes a triage conclusion is for the graded
cases to show, not for this note.

**Transfer.** The output was streamed with
`tar -C <scratch> -cf - . | ssh rp5 'umask 077; mkdir -p /home/pi/triage-capture-staging; tar -x -C /home/pi/triage-capture-staging'`.
On rp5 there are 588 files (3 x (195 + manifest)); every directory is `700 pi` and every
file `600`. The sha256 over all files matched the workstation copy. The workstation
scratch was then deleted (done, 2026-09-23). The rp5 staging directory remains
**pending** the owner's move in 1b.5.
