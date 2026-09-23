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
