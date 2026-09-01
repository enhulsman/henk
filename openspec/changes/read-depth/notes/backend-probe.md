# Backend probe — pinned live facts for `read-depth`

**Probed:** 2026-09-01 22:00–22:30 UTC (task group §1, tasks 1.1–1.7)
**Method:** read-only HTTP over `ssh rp5` / `ssh vps` to Gatus `localhost:8080`,
Prometheus `localhost:9090`, Grafana `localhost:3000`. Nothing was written, restarted, or
reconfigured on either host.

**Publication gate.** This file records metric names, job names, label *names*, value
*shapes*, and measured numbers. It contains no tailnet address, no `instance` label value,
no `server` label value, no scrape URL, no Gatus endpoint name or key, and no raw response
body. Raw bodies were kept only in an untracked scratch directory outside the repo.

**Standing rule this file exists to enforce:** every value below is measured. Nothing here
was transcribed from `~/Documents/homelab-docs-site/`, from `design.md`, or from the
existing `henk/` code. Where the live reading contradicts one of those, the contradiction
is called out rather than smoothed over — see **Contradictions found** at the end.

---

## 1.1 Gatus — per-endpoint route and uptime-window vocabulary

**Resolves `APPLY-RESOLVED:gatus-window`.**

### Pinned: the uptime window domain is `{1h, 24h, 7d, 30d}`

The deployed Gatus states its own vocabulary in the 400 body, so this is the server's
answer rather than an inference from which probes happened to succeed:

```
ssh rp5 "curl -s -w '|%{http_code}' 'http://localhost:8080/api/v1/endpoints/<KEY>/uptimes/15m'"
→ Durations supported: 30d, 7d, 24h, 1h|400
```

| probed token | `/uptimes/{d}` | `/uptimes/{d}/badge.svg` | `/response-times/{d}/badge.svg` | `/response-times/{d}/chart.svg` |
|---|---|---|---|---|
| `1h`  | 200 | 200 | 200 | **400** |
| `24h` | 200 | 200 | 200 | 200 |
| `7d`  | 200 | 200 | 200 | 200 |
| `30d` | 200 | 200 | 200 | 200 |
| `15m` | 400 | 400 | 400 | 400 |
| `6h`  | 400 | 400 | 400 | 400 |
| `12h` | 400 | 400 | 400 | 400 |
| `1y`  | 400 | 400 | 400 | 400 |

Command shape (run once per token):
`ssh rp5 "curl -s -o /dev/null -w '%{http_code}' 'http://localhost:8080/api/v1/endpoints/<KEY>/uptimes/<TOKEN>/badge.svg'"`

`chart.svg` carries a **narrower** vocabulary than the rest — its 400 body reads
`Durations supported: 30d, 7d, 24h` (no `1h`). Irrelevant to `endpoint_history` as specced
(it needs no SVG), recorded so nobody widens the window enum on the strength of one route.

**Consequence for the spec:** `endpoint_history`'s `window` domain is
**`{1h, 24h, 7d, 30d}`**, and it does **not** share `node_resource_trend`'s
`{15m, 1h, 6h, 24h}`. Two of the four Prometheus windows (`15m`, `6h`) are rejected by
Gatus; two of the four Gatus windows (`7d`, `30d`) have no Prometheus counterpart. The
design's suspicion that the vocabularies differ is confirmed — they overlap on exactly
`{1h, 24h}`.

### Pinned: a per-endpoint status route exists — no in-process filter fallback needed

| route | code | response shape |
|---|---|---|
| `GET /api/v1/endpoints/{key}/statuses` | **200** | object `{name, group, key, results[], events[]}` |
| `GET /api/v1/endpoints/{key}/uptimes/{d}` | **200** | bare float in `[0,1]`, e.g. `0.999283` |
| `GET /api/v1/endpoints/{key}/response-times/{d}` | **200** | bare integer (ms), e.g. `43` |
| `GET /api/v1/endpoints/{key}` | 404 | — |
| `GET /api/v1/endpoints/statuses` (bulk) | 200 | array of the same objects, minus `events` |
| `GET /health` | 200 | `{"status":"UP"}` |
| `GET /api/v1/config` | 200 | `{announcements, authenticated, oidc}` — no version field |

So D7's fallback ("filter the bulk response in process") is **not required**. The bulk
route remains useful for endpoint-key *discovery*, which is what D7 actually needs it for.

Unknown key → `endpoint not found|404`. An encoded-traversal key (`..%2F..%2Fhealth`,
`--path-as-is`) → 404, no escape observed.

### Pinned: response shapes (field names only)

- Per-endpoint object keys: `name`, `group`, `key`, `results`, `events`.
- Bulk array element keys: `name`, `group`, `key`, `results` — **no `events`**.
- `results[]` element keys: `timestamp`, `success` (bool), `duration` (int ns),
  `status` (int), `hostname` (str), `conditionResults[]`.
- `conditionResults[]` element keys: `condition` (str), `success` (bool).
- `events[]` element keys: `timestamp`, `type`. Observed `type` values across the probed
  endpoint: `START`, `HEALTHY`, `UNHEALTHY`.
- `timestamp` is RFC3339 with nanoseconds and a `Z` suffix (30 characters).

**Projection note:** `results[].hostname` is a target identifier and must be dropped from
tool output for the same reason as `instance`. On the probed endpoint it was not
address-shaped, but that is a property of one endpoint's config, not a guarantee.

### Pinned: history depth and paging — `results` is ~1.7 h, `events` is ~20 days

Measured on one endpoint, `?page=1&pageSize=100`:

- 100 results spanning **1 h 39 min** (60 s check interval), ordered **oldest-first**.
- `page=2` → **4** further results. Total stored ≈ **104** checks ≈ **1.7 hours**.
- `events` list on the same endpoint spanned **2026-08-12 → 2026-09-01** — ~20 days — in
  **6** entries.

`pageSize` is **capped at 100**: `pageSize=101 / 200 / 1000` all returned exactly 100.
A page past the end returns `"results": null` — **JSON null, not an empty array**. An
implementation doing `len(body["results"])` will raise there; treat null as empty.

**Consequence for `endpoint_history`:** "since when did this endpoint start failing"
cannot come from `results` — that is only the last ~1.7 h. It must come from `events`
(transition log, ~weeks) with `uptimes/{window}` for the ratio. Three sources, three
purposes:

| question | source |
|---|---|
| current state, failing condition, latency of recent checks | `…/statuses` → `results[]` (~1.7 h only) |
| since when / how many transitions | `…/statuses` → `events[]` |
| uptime over `1h`/`24h`/`7d`/`30d` | `…/uptimes/{window}` |

### Pinned: the endpoint-key format, as a shape

19 endpoints across 7 distinct groups (counts only; no names recorded).

`key = sanitize(lower(group)) + "_" + sanitize(lower(name))`, where `sanitize` is a
**1:1 character substitution** — the key length always equals
`len(lower(group) + "_" + lower(name))`. Verified across all 19 endpoints:

```
entries with length preserved: 19 of 19
character substitutions observed (src → key): {' ' → '-': 30 occurrences, '.' → '-': 1}
```

Observed key character classes: lowercase letters, digits, `-`, `_`. Key lengths 15–30.
**0 of 19 keys** contain a character requiring percent-encoding as a URL path segment
(`[^A-Za-z0-9._~-]`). The spec's percent-encode-or-reject requirement is therefore not
currently exercised by live data — it must still be implemented and unit-tested against a
synthetic key, because the input is owner free text and the substitution set above is not
guaranteed to be exhaustive.

### NOT MEASURED

- **The Gatus release version.** Image is `ghcr.io/twin/gatus` with tag/label `latest`;
  `docker inspect` is outside rp5's read-only NOPASSWD sudo allowlist (`sudo: a password
  is required`), and neither `/api/v1/config` nor `/health` nor the response headers carry
  a version. Recorded as unknown rather than guessed. The route and window facts above
  were measured directly against the running instance, so nothing depends on the version
  string.

---

## 1.2 Freshness metric families — exact `__name__` values

Command:
`ssh vps 'curl -s "http://localhost:9090/api/v1/label/__name__/values"'`
→ `{"status":"success","data":[…]}`, **478** metric names total.

Label names and timestamp-gauge classification from:
`ssh vps 'curl -s --get --data-urlencode "query={__name__=~\"homelab_backup_.*|homelab_dump_.*|health_etl_.*|obsidian_.*\"}" http://localhost:9090/api/v1/query'`
→ instant-vector result, **44** series.

"Epoch gauge" below means every current sample fell in the Unix-seconds band
(1.4e9 < v < 2.0e9), i.e. the metric is a raw timestamp, not a precomputed age.

| metric | series | label names (beyond `__name__`) | epoch gauge | age at probe (h) |
|---|---|---|---|---|
| `homelab_backup_duration_seconds` | 2 | `direction`, `instance`, `job` | no | — |
| `homelab_backup_errors_total` | 2 | `direction`, `instance`, `job` | no | — |
| `homelab_backup_last_run_timestamp` | 2 | `direction`, `instance`, `job` | **yes** | 20.76 – 21.17 |
| `homelab_backup_last_success_timestamp` | 2 | `direction`, `instance`, `job` | **yes** | 20.76 – 21.17 |
| `homelab_backup_rotate_errors_total` | 2 | `instance`, `job`, `root` | no | — |
| `homelab_backup_rotate_last_run_timestamp` | 2 | `instance`, `job`, `root` | **yes** | 66.26 – 67.26 |
| `homelab_dump_mtime_timestamp` | 1 | `dump`, `instance`, `job` | **yes** | 21.51 |
| `health_etl_duration_seconds` | 1 | `instance`, `job` | no | — |
| `health_etl_errors_total` | 1 | `instance`, `job` | no | — |
| `health_etl_last_success_timestamp_seconds` | 1 | `instance`, `job` | **yes** | 4.08 |
| `health_etl_rows_last_run` | 11 | `instance`, `job`, `metric` | no | — |
| `health_etl_rows_total` | 11 | `instance`, `job`, `metric` | no | — |
| `obsidian_backup_push_errors_total` | 1 | `instance`, `job` | no | — |
| `obsidian_backup_push_last_run_timestamp` | 1 | `instance`, `job` | **yes** | 4.75 |
| `obsidian_backup_push_last_success_timestamp` | 1 | `instance`, `job` | **yes** | 4.75 |
| `obsidian_backup_verify_failed_total` | 1 | `instance`, `job` | no | — |
| `obsidian_backup_verify_last_run_timestamp` | 1 | `instance`, `job` | **yes** | 5.75 |
| `obsidian_backup_verify_ok_total` | 1 | `instance`, `job` | no | — |

**8 timestamp gauges**, which are `freshness_check`'s raw-timestamp inputs:

```
homelab_backup_last_run_timestamp
homelab_backup_last_success_timestamp
homelab_backup_rotate_last_run_timestamp
homelab_dump_mtime_timestamp
health_etl_last_success_timestamp_seconds
obsidian_backup_push_last_run_timestamp
obsidian_backup_push_last_success_timestamp
obsidian_backup_verify_last_run_timestamp
```

Note the naming is **not** uniform: `health_etl_*` uses the `_seconds` suffix, every other
family does not. A `*_timestamp` glob would silently miss the health-ETL metric. Select on
the explicit list, not on a suffix pattern.

**Family-name corrections against the task text**, both measured:

- The `obsidian_backup_verify_*` family named in task 1.2 is only half of it. There is a
  sibling **`obsidian_backup_push_*`** family (3 metrics, 2 of them timestamps), and it
  backs a live alert (`ObsidianPushStale`). Selecting on `obsidian_backup_verify_*` alone
  would drop it. Use `obsidian_*` (6 metrics).
- `homelab_dump_*` is a single metric, `homelab_dump_mtime_timestamp`, and it is the
  raw-mtime pattern D9 cites as the fleet's own freshness idiom.

**Non-`job` label value shapes** (values not recorded; shape only):

| label | distinct values | IPv4-shaped | path-shaped | max length |
|---|---|---|---|---|
| `direction` | 2 | no | no | 10 |
| `dump` | 1 | no | no | 8 |
| `metric` | 11 | no | no | 12 |
| `root` | 2 | no | **yes** (leading `/`) | 12 |

None is address-shaped, so all four are safe as distinguishing labels under the
"`job` plus a non-address label" projection rule. `root` is a filesystem path and should be
weighed on that basis rather than on the address test alone.

**Where the metrics are scraped from** (they arrive through node-exporter's textfile
collector, not through the pushgateway):

- `job="node-exporter-vps"`: all `health_etl_*`, all `obsidian_backup_push_*`
- `job="node-exporter-pi5"`: `homelab_dump_*`, all `obsidian_backup_verify_*`
- both: all `homelab_backup_*`

---

## 1.3 Job label values and `up` series counts

Command: `ssh vps 'curl -s "http://localhost:9090/api/v1/label/job/values"'`

**Seven** job values, not six:

```
adguard-exporter
cadvisor-pi5
cadvisor-vps
node-exporter-pi2
node-exporter-pi5
node-exporter-vps
pushgateway
```

Command: `ssh vps 'curl -s --get --data-urlencode "query=up" http://localhost:9090/api/v1/query'`

| `job` | `up{job=…}` series | value | exactly-one expected |
|---|---|---|---|
| `adguard-exporter` | 1 | 1 | yes |
| `cadvisor-pi5` | 1 | 1 | yes |
| `cadvisor-vps` | 1 | 1 | yes |
| `node-exporter-pi2` | 1 | 1 | yes |
| `node-exporter-pi5` | 1 | 1 | yes |
| `node-exporter-vps` | 1 | 1 | yes |
| `pushgateway` | 1 | 1 | yes |

Total `up` series: **7**. Every job yields exactly one series, so the spec's
"unexpected multiplicity is reported" branch is currently unexercised by live data and must
be unit-tested against a synthetic two-series fixture.

The `up` series carry exactly `{__name__, instance, job}`.

### `pushgateway` is a real seventh scrape target

`{job="pushgateway"}` returns **50** series, all of them the pushgateway's own Go/process
self-metrics plus `up` and the four `scrape_*` metrics. **No application metric is
currently pushed through it.** It is nonetheless a live scrape target that
`InstanceDown` / `HenkInstanceDown` will alert on.

**Consequence:** the design (D3) and tasks 1.3 / 4.4 say "six jobs" / "all six targets".
Live it is **seven**. `scrape_targets` must enumerate 7 targets, and any test asserting a
count of 6 will fail against production. `pushgateway` has no node in the `node` enum, the
same as `adguard-exporter` — so **two** of seven jobs are node-less, not one.

### `/api/v1/targets` — fields available to `scrape_targets`

Command: `ssh vps 'curl -s "http://localhost:9090/api/v1/targets"'`

`activeTargets`: **7**. `droppedTargets`: **0**.
Element fields: `discoveredLabels`, `globalUrl`, `health`, `labels`, `lastError`,
`lastScrape`, `lastScrapeDuration`, `scrapeInterval`, `scrapePool`, `scrapeTimeout`,
`scrapeUrl`.

- `labels` keys: `instance`, `job`. `discoveredLabels` keys: `__address__`,
  `__metrics_path__`, `__scheme__`, `__scrape_interval__`, `__scrape_timeout__`, `job`.
- **Four fields carry addresses and must never be rendered:** `scrapeUrl`, `globalUrl`,
  `labels.instance`, `discoveredLabels.__address__`. The spec names `scrapeUrl` and
  `instance`; `globalUrl` and `__address__` are additional and equally unsafe.
- `lastError` is present on every entry, `""` when healthy. All 7 healthy at probe time,
  so the populated-error branch is **NOT MEASURED** live and must be fixture-tested.
- `scrapeInterval` is `15s` on all 7 targets. `lastScrapeDuration` ranged 0.0024–0.473 s.

### Prometheus server facts

`ssh vps 'curl -s "http://localhost:9090/api/v1/status/buildinfo"'` → version **3.9.1**.
`ssh vps 'curl -s "http://localhost:9090/api/v1/status/flags"'`:

| flag | value |
|---|---|
| `storage.tsdb.retention.time` | `15d` |
| `storage.tsdb.retention.size` | `0B` (unlimited) |
| `query.lookback-delta` | `5m` |
| `query.timeout` | `2m` |
| `query.max-samples` | `50000000` |
| `query.max-concurrency` | `20` |

15-day retention confirmed, so all four `node_resource_trend` windows and all four Gatus
windows except `30d` sit inside it. **`endpoint_history(window=30d)` exceeds Prometheus
retention — but it is served by Gatus, which keeps its own history, so this is not a
conflict.** Recorded because the two 15-day/30-day numbers invite the wrong inference.

---

## 1.4 All native Prometheus alert rules

Command: `ssh vps 'curl -s "http://localhost:9090/api/v1/rules"'`

**Actual count: 23 alerting rules in 6 groups across 3 rule files.** This matches the
expected 23. There are **zero** recording rules.

| group | rule file | rules |
|---|---|---|
| `dns_performance` | `adguard.rules.yml` | 7 |
| `health-pipeline` | `health.rules.yml` | 4 |
| `backup_monitoring` | `infrastructure.rules.yml` | 7 |
| `container_health` | `infrastructure.rules.yml` | 1 |
| `instance_health` | `infrastructure.rules.yml` | 1 |
| `system_resources` | `infrastructure.rules.yml` | 3 |

All 23 were in state `inactive` at probe time.

### Enumeration and mapping to the query measuring each rule's input

Threshold values are quoted verbatim from the live `query` field. DNS rule selectors are
per-server and each contains a tailnet address, so the selector is shown as
`{server=<addr>}` — the addresses are deliberately not recorded (see 1.6 for why a
`server` selector must never enter the registry).

| # | rule | group | key metric(s) | threshold | `for` | query measuring its input |
|---|---|---|---|---|---|---|
| 1 | `Pi5HighDNSProcessingTime` | dns_performance | `adguard_avg_processing_time_seconds{server=<addr>}` | `> 0.06` | 5m | `dns_performance` |
| 2 | `Pi5CriticalDNSProcessingTime` | dns_performance | same | `> 0.15` | 2m | `dns_performance` |
| 3 | `VPSHighDNSProcessingTime` | dns_performance | same | `> 0.08` | 5m | `dns_performance` |
| 4 | `VPSCriticalDNSProcessingTime` | dns_performance | same | `> 0.2` | 2m | `dns_performance` |
| 5 | `Pi2HighDNSProcessingTime` | dns_performance | same | `> 0.2` | 5m | `dns_performance` |
| 6 | `Pi2CriticalDNSProcessingTime` | dns_performance | same | `> 0.5` | 2m | `dns_performance` |
| 7 | `DNSProcessingTimeCritical` | dns_performance | `adguard_avg_processing_time_seconds` (unselected) | `> 0.3` | 10m | `dns_performance` |
| 8 | `HealthEtlStale` | health-pipeline | `health_etl_last_success_timestamp_seconds` | `> 0` and age `> 13*3600` | 10m | `freshness_check` |
| 9 | `HealthEtlErrors` | health-pipeline | `health_etl_errors_total` | `increase(…[1h]) > 0` | 5m | **accepted gap** |
| 10 | `HealthEtlSlow` | health-pipeline | `health_etl_duration_seconds` | `> 120` | 5m | **accepted gap** |
| 11 | `HealthEtlMetricSilent` | health-pipeline | `health_etl_rows_total` | `increase([2d])==0` ∧ `max_over_time([1w])>0` ∧ `on() sum(increase([2d]))>0` | 60m | **accepted gap** |
| 12 | `BackupStale` | backup_monitoring | `homelab_backup_last_success_timestamp` | age `> 93600` (26 h) | 0s | `freshness_check` |
| 13 | `BackupErrors` | backup_monitoring | `homelab_backup_errors_total` | `> 0` | 0s | **accepted gap** |
| 14 | `BackupRotateStale` | backup_monitoring | `homelab_backup_rotate_last_run_timestamp` | age `> 691200` (8 d) | 0s | `freshness_check` |
| 15 | `BackupRotateErrors` | backup_monitoring | `homelab_backup_rotate_errors_total` | `> 0` | 0s | **accepted gap** |
| 16 | `ObsidianBackupVerifyFailed` | backup_monitoring | `obsidian_backup_verify_failed_total` | `> 0` | 0s | **accepted gap** |
| 17 | `ObsidianVerifyStale` | backup_monitoring | `obsidian_backup_verify_last_run_timestamp` | age `> 172800` (48 h) | 0s | `freshness_check` |
| 18 | `ObsidianPushStale` | backup_monitoring | `obsidian_backup_push_last_success_timestamp` | age `> 50400` (14 h) | 0s | `freshness_check` |
| 19 | `ContainerRestarting` | container_health | `container_start_time_seconds{name!=""}` | `changes(…[15m]) > 1` | 5m | `container_state` |
| 20 | `InstanceDown` | instance_health | `up` | `== 0` | 2m | `scrape_targets` |
| 21 | `HighCPU` | system_resources | `node_cpu_seconds_total{mode="idle"}` | `100 - avg by (instance)(rate(…[5m]))*100 > 85` | 5m | `node_resource_trend(cpu)` |
| 22 | `HighMemory` | system_resources | `node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes` | `(1-…)*100 > 90` | 5m | `node_resource_trend(memory)` |
| 23 | `DiskSpaceLow` | system_resources | `node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}` | `*100 < 15` | 5m | `node_resource_trend(disk)` |

**Covered: 17 of 23. Accepted gaps: 6 of 23.**

The six gaps are one coherent family, not six independent misses: they are the
**counter and duration** rules of the freshness pipelines (`*_errors_total`,
`*_failed_total`, `*_duration_seconds`, `*_rows_total`), whereas `freshness_check` as
specced reports **raw timestamps and derived ages**. One-line reasons:

- #9 `HealthEtlErrors` — an error counter; `freshness_check` measures recency, not errors.
- #10 `HealthEtlSlow` — a run-duration gauge; no query in v1 reports durations.
- #11 `HealthEtlMetricSilent` — row-count silence over a 2-day window; not a timestamp.
- #13 `BackupErrors`, #15 `BackupRotateErrors`, #16 `ObsidianBackupVerifyFailed` — error /
  failure counters, same reason as #9.

This gap is **cheap to close and worth flagging to §4.7**: all six inputs are already in
the metric families `freshness_check` selects over (see 1.2), so widening its projection to
carry each pipeline's error/failure counter alongside its timestamp would take the mapping
to 23 of 23 without a new query, a new parameter, or a new backend call. Recorded as a gap
rather than a spec change, because the spec delta is binding and this is not §1's call.

**Zero rules are un-mapped for reasons of "nobody considered this family"** — every gap
above is a deliberate scope statement about `freshness_check`, not an unconsidered rule.

### Cross-check of D4's Grafana-twin claim

Measured twin coverage of the 23 natives by the 18 Grafana-managed rules (1.5):

- 7 DNS natives → 7 individual twins in the `AdGuard Home` folder, thresholds identical.
- 4 `health-pipeline` natives → folded into the **single** `HenkHealthEtl` rule as a
  four-term `or` expression; every term and threshold matches.
- 7 `backup_monitoring` natives → folded into the **single** `HenkBackupFreshness` rule as
  a seven-term `or` expression; every term and threshold matches.
- `ContainerRestarting` → `HenkContainerRestarting`, expression byte-identical.
- `InstanceDown` → `HenkInstanceDown`, expression byte-identical.
- `DiskSpaceLow` → `HenkDiskPressure`, expression byte-identical.
- `HighMemory` → `High memory usage` (folder `NodeExporter`), **same expression, different
  threshold** — native `> 90`, Grafana `> 75`. See 1.5.
- `HighCPU` → **no Grafana rule anywhere.**

So **22 of 23** natives have a Grafana counterpart, or **21 of 23** if `HighMemory` is not
counted as twinned because its bar differs. `design.md` D4 says 21 of 23; the live reading
supports that number only under the second reading, and the reason (a 15-point threshold
divergence) is itself worth recording.

`HenkSwapPressure` has **no native twin** — confirmed: no native rule references
`node_memory_SwapFree_bytes`, `node_memory_SwapTotal_bytes`, `node_vmstat_pswpin`, or
`node_vmstat_pswpout`. D4's claim holds.

### Input-metric availability (measured, because a mapping to an absent metric is not a mapping)

`ssh vps "curl -s --get --data-urlencode 'query=count by (job) (<METRIC>)' http://localhost:9090/api/v1/query"`

| metric | `node-exporter-pi5` | `node-exporter-vps` | `node-exporter-pi2` |
|---|---|---|---|
| `node_memory_MemAvailable_bytes` | 1 | 1 | 1 |
| `node_memory_SwapTotal_bytes` | 1 | 1 | 1 |
| `node_filesystem_avail_bytes{mountpoint="/"}` | 1 | 1 | 1 |
| `node_load1` | 1 | 1 | 1 |
| `node_thermal_zone_temp` | 1 | **0 — absent** | 1 |
| `node_hwmon_temp_celsius` | 6 | **0 — absent** | 2 |

| metric (`{name!=""}`) | `cadvisor-pi5` | `cadvisor-vps` |
|---|---|---|
| `container_last_seen` | 19 | 19 |
| `container_start_time_seconds` | 19 | 19 |
| `container_oom_events_total` | 19 | 19 |
| `container_health_state` | **0 — absent** | 19 |

Two measured availability holes the spec's uniform domains do not anticipate:

1. **`temperature` has no data on `vps`.** `node_resource_trend`'s `node` domain is
   `{rp5, vps, rp2}` for every resource, but neither temperature metric exists on the vps.
   `node_resource_trend(node=vps, resource=temperature)` is **in domain and not derivable**
   — exactly D2's third outcome — and must produce that message rather than an empty
   summary. It is not an argument for narrowing the domain: a per-(node,resource) domain
   matrix would be a worse trade than one honest runtime message.
2. **`container_health_state` exists only on `cadvisor-vps`.** `container_state(node=rp5)`
   can report last-seen, OOM events, and creation time but **not** health state. The result
   must say the field is unavailable on that node rather than omitting it silently — an
   omitted health column reads as "no container is unhealthy".

Also measured: **zero** metric names in this Prometheus contain the substring `restart`
(`478` names scanned). D6's and `sensor-routing`'s claim that no restart counter exists is
confirmed live, not inherited.

Temperature label names (no addresses): `node_thermal_zone_temp{instance, job, type, zone}`
— observed `type="cpu-thermal"`, `zone="0"`; `node_hwmon_temp_celsius{chip, instance, job,
sensor}` — 3 distinct `chip` values, 4 distinct `sensor` values (`temp0`–`temp3`), none
address-shaped. Current readings: pi2 44.4 °C (both metrics agree); pi5 48.5 °C from
`node_thermal_zone_temp`, and 45.9–53.7 °C across 6 `node_hwmon_temp_celsius` series. The
two metrics do **not** agree per-series on pi5 — `node_hwmon_temp_celsius` covers several
chips including an NVMe sensor. Pick one and label it; a bare "temperature" over
`node_hwmon_temp_celsius` would silently report a disk sensor as the CPU.

---

## 1.5 Grafana alert rules — live expressions and thresholds

**Resolves `APPLY-RESOLVED:memory-bar`.**

Source: the **live Grafana provisioning API** on the host that runs Grafana (vps),
authenticated with the existing `alerting-applier` service-account token already present on
that host. The token was passed by command substitution and never printed.

```
ssh vps 'curl -s -H "Authorization: Bearer $(cat ~/.config/grafana-applier/token)" \
          http://localhost:3000/api/v1/provisioning/alert-rules'
```
→ JSON array, **18** rules. Folders from `/api/folders`.

Grafana rules are **API-provisioned, not file-provisioned** — there is no rules YAML on
disk for these, so the API is the provisioning artifact.

| folder | rules |
|---|---|
| `henk` | 6 |
| `AdGuard Home` | 7 |
| `Homelab` | 3 |
| `NodeExporter` | 2 |

### The six `henk`-folder rules (group `henk-events`)

Every one has `noDataState: OK`, `execErrState: Error`, `keep_firing_for: 0s`,
`isPaused: false`, label `route=henk-events`, and **no** `notification_settings` (routing
is by the `route` label through the notification policy, not per-rule). Each is a
three-node pipeline: `A` = a Prometheus instant query over `relativeTimeRange {from: 600,
to: 0}`, `B` = `reduce(last)` of `A`, `C` = `threshold(B) evaluator gt -1` — the
"fire on any returned series" idiom `sensor-routing` requires.

| rule | `for` | severity | extra label | expression (refId A), verbatim |
|---|---|---|---|---|
| `HenkHealthEtl` | 10m | warning | — | `(health_etl_last_success_timestamp_seconds > 0 and (time() - health_etl_last_success_timestamp_seconds) > 13*3600) or (increase(health_etl_errors_total[1h]) > 0) or (health_etl_duration_seconds > 120) or ((increase(health_etl_rows_total[48h]) == 0) and (max_over_time(health_etl_rows_total[7d]) > 0) and on() (sum(increase(health_etl_rows_total[48h])) > 0))` |
| `HenkBackupFreshness` | 5m | warning | — | `(time() - homelab_backup_last_success_timestamp > 93600) or (homelab_backup_errors_total > 0) or (time() - homelab_backup_rotate_last_run_timestamp > 691200) or (homelab_backup_rotate_errors_total > 0) or (obsidian_backup_verify_failed_total > 0) or (time() - obsidian_backup_verify_last_run_timestamp > 172800) or (time() - obsidian_backup_push_last_success_timestamp > 50400)` |
| `HenkDiskPressure` | 15m | warning | — | `(node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100 < 15` |
| `HenkSwapPressure` | 15m | warning | — | `((1 - node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes) * 100 > 95) or (rate(node_vmstat_pswpin[5m]) + rate(node_vmstat_pswpout[5m]) > 50)` |
| `HenkInstanceDown` | 2m | critical | `identity_scope=instance` | `up == 0` |
| `HenkContainerRestarting` | 5m | warning | `identity_scope=name` | `changes(container_start_time_seconds{name!=""}[15m]) > 1` |

**Pinned thresholds for the registry, from the above:**

| resource | bar | form | source rule |
|---|---|---|---|
| `disk` | **15** | percent **free** on `mountpoint="/"`, `< 15` | `HenkDiskPressure` |
| `swap_io` | **50** | `rate(pswpin[5m]) + rate(pswpout[5m]) > 50` pages/s | `HenkSwapPressure`, second term |
| `swap_used` | **95** | percent full, `> 95` — **not** the rule's practical trigger | `HenkSwapPressure`, first term |
| `memory` | **75** | percent used, `> 75` | `High memory usage` (below) |
| `cpu` | none in `henk` folder; native bar is 85 | — | see below |
| `load` | none — no rule exists in either system | — | — |
| `temperature` | none — no rule exists in either system | — | — |

`load` and `temperature` confirmed absent from **all 18** Grafana rules and **all 23**
native rules. The spec's "no bar" for those two is measured, not assumed.

### `APPLY-RESOLVED:memory-bar` → **75** (percent used)

The memory rule is **not in the `henk` folder**. It is:

- **title:** `High memory usage`
- **folder:** `NodeExporter` (uid `af2p9d1r0d6v4a`), group `Eval every minute`
- **expr (refId A):** `(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100`
- **condition (refId C):** `threshold`, `evaluator {type: "gt", params: [75]}`
- **`for`:** `5m`; **`noDataState`:** `NoData`; **`execErrState`:** `Error`
- **`notification_settings.receiver`:** `Discord-Grafana`
- **`labels`:** `null` — this rule carries **no** `severity` label at all
- **`updated`:** `2025-11-13T10:37:41Z`

So the pinned memory bar is **75 %, not 90 %**, and the design's refusal to accept
`homelab_health`'s 90 was correct. The rule delivers to Discord, never to `henk-events` —
also confirmed (it is the only rule outside `AdGuard Home`/`Homelab`/`henk` with an
explicit receiver, alongside `AuditShipStale`).

**Three different memory bars are live simultaneously:** Grafana `> 75`, Prometheus-native
`HighMemory > 90`, and `homelab_health`'s constructor default `90.0`. The spec's
"the two tools cannot disagree about a threshold" scenario is currently **violated** by the
deployed system; §10's amendment resolves it toward **75**.

### The other 12 Grafana rules

| folder | title | expression / condition | threshold | receiver |
|---|---|---|---|---|
| `AdGuard Home` | `Pi5 DNS Processing Time Warning` | `adguard_avg_processing_time_seconds{server=<addr>}`, `last` | `gt 0.06`, `for 5m` | policy |
| `AdGuard Home` | `Pi5 DNS Processing Time Critical` | same | `gt 0.15`, `for 2m` | policy |
| `AdGuard Home` | `VPS DNS Processing Time Warning` | same | `gt 0.08`, `for 5m` | policy |
| `AdGuard Home` | `VPS DNS Processing Time Critical` | same | `gt 0.2`, `for 2m` | policy |
| `AdGuard Home` | `Pi2 DNS Processing Time Warning` | same | `gt 0.2`, `for 5m` | policy |
| `AdGuard Home` | `Pi2 DNS Processing Time Critical` | same | `gt 0.5`, `for 2m` | policy |
| `AdGuard Home` | `DNS Processing Time Critical (Any Device)` | `adguard_avg_processing_time_seconds` (unselected) | `gt 0.3`, `for 10m` | policy |
| `Homelab` | `DawarichDumpStale` | `max(time() - homelab_dump_mtime_timestamp{dump="dawarich"}) or vector(1e9)` | `gt 108000` (30 h), `for 30m`, `noData: Alerting` | policy |
| `Homelab` | `MollySocketLiveness` | `max(time() - container_last_seen{name="mollysocket"}) or vector(1e9)` | `gt 300` (5 min), `for 5m`, `noData: Alerting` | policy |
| `Homelab` | `HealthPipelineSilence` | **Postgres datasource** (`health-postgres`), raw SQL over a `samples` table | `gt 172800` (48 h), `for 30m` | policy |
| `NodeExporter` | `High memory usage` | see above | `gt 75` | `Discord-Grafana` |
| `NodeExporter` | `AuditShipStale` | `time() - homelab_audit_last_flush_timestamp` | `gt 900` (15 min), `for 0s`, `noData: Alerting` | `Discord-Grafana` |

Two live confirmations worth recording:

- **The `or vector(…)` guard D6 prescribes is already the fleet's idiom.** Both
  `DawarichDumpStale` and `MollySocketLiveness` use `max(time() - <metric>{name=…}) or
  vector(1e9)` precisely so an absent series yields a huge age instead of no series.
  `container_state`'s named-container form should copy this shape, not invent one.
- **`HealthPipelineSilence` reads Postgres, not Prometheus.** No `homelab_query` entry can
  measure its input; it is outside the query half's reach by construction. Recorded so it
  is not later mistaken for a coverage miss.

### What `homelab_health` hardcodes today (for §10's delta)

`henk/tools/homelab_health.py`, constructor defaults — and `henk/tools/__init__.py`
registers the tool passing only `client`, `gatus_url`, `prometheus_url`, and `timeout`, so
**these defaults are the deployed values**:

| constructor parameter | current value | live bar it should track | delta |
|---|---|---|---|
| `memory_threshold_pct` | `90.0` | **75** (`High memory usage`) | **−15** |
| `disk_threshold_pct` | `90.0` **percent used** | **15 percent free**, i.e. 85 percent used (`HenkDiskPressure`) | **−5 in used terms, and the polarity is inverted** |
| `load_threshold` | `8.0` | **no rule exists in either system** | the bar is unsourced entirely |

Three further deltas §10 must handle, all read from the live code rather than assumed:

- Nodes are keyed by the raw `instance` label (`_fetch_prometheus` uses
  `sample["metric"]["instance"]`) and rendered into the summary verbatim — the projection
  the spec requires is currently absent.
- Gatus endpoints are named by `entry["name"]`, not by the composed `key`, so a name
  collision across two groups would merge silently.
- The tool reports memory / disk / load only. Swap and temperature — both now first-class
  in `node_resource_trend` — are outside it, so no threshold conflict arises there.

Live headroom against each bar at probe time, so the §10 change can be assessed for
whether it will make anything newly "DEGRADED":

| node | memory used % (now / 7 d max) | disk free % (now) | load1 (now) |
|---|---|---|---|
| `rp5` | 52.38 / 57.41 | 54.71 | 1.10 |
| `vps` | 54.67 / 58.11 | 30.24 | 1.14 |
| `rp2` | 48.47 / 49.99 | 41.03 | 0.70 |

Moving the memory bar 90 → 75 changes **no** current verdict — the 7-day maximum across
the fleet is 58.11 %. The change is a correctness fix, not a new-alert-noise risk.

Additional measured baselines for the per-resource comparison rules (§4.3 needs these; all
`*_over_time` over the stated window, `[…:5m]` subquery where the expression is derived):

| measurement | `rp5` | `vps` | `rp2` |
|---|---|---|---|
| `swap_used` %, avg over 24 h | 6.50 | **77.36** | 8.00 |
| `swap_used` %, max over 7 d | 6.70 | **89.70** | 8.13 |
| `swap_io` pages/s, max over 7 d | 36.27 | **83.42** | 0.97 |
| cpu busy %, max over 7 d | 13.31 | **70.58** | 13.32 |

These confirm the spec's anti-correlation claim with fresh numbers: the vps sits chronically
at 77 % swap **fullness** against a 95 % bar and is the fleet's only device whose swap
**pressure** has crossed the 50 pages/s bar in the last 7 days (peak 83.42) — while rp2, at
8 % fullness, peaked at 0.97 pages/s. Fullness and pressure genuinely move in opposite
directions here. Note also that vps's 7-day swap-fullness maximum of **89.70 %** is higher
than the 86.3 % the design records; the chronic range should be described as **64–90 %**,
and the "approaching the bar" phrasing the spec forbids is now only 5 points from true, so
the prohibition matters more, not less. CPU peak on the vps is **70.58 %** over 7 days
against the native 85 bar — headroom, but less than the 67.8 % the design cites.

---

## 1.6 `dns_performance`

**Resolves `APPLY-RESOLVED:dns-baselines`.** *(This slug appears in `tasks.md` 1.6 only; no
`APPLY-RESOLVED:dns-baselines` token exists in `specs/`, so task 9.7's grep is unaffected
by it.)*

### Pinned: the metric

**`adguard_avg_processing_time_seconds`** — this is the metric all **14** live DNS alert
rules evaluate (7 native + 7 Grafana, each set being 6 per-device plus one fleet-wide), so
it is the metric that satisfies D4's "measure the rule's input" criterion. Counted
programmatically over both rule sets, not by eye.

```
ssh vps "curl -s --get --data-urlencode 'query=adguard_avg_processing_time_seconds' \
         http://localhost:9090/api/v1/query"
```
→ instant vector, **3** series.

| label name | distinct values | IPv4-shaped | URL-scheme prefix | `:port` suffix | max length |
|---|---|---|---|---|---|
| `instance` | **1** | **yes** | no | **yes** | 17 |
| `job` | 1 (`adguard-exporter`) | no | no | no | 16 |
| `server` | **3** | **yes** | **yes** | **yes** | 23 |

So the label set is exactly `{__name__, instance, job, server}`, as D5 states. `instance`
has **one** value across all three series (the single exporter), `job` is uniform, and
`server` is the only discriminator — and it is an **`http://<addr>:<port>` URL**. Both
discriminators are address-bearing, confirmed live. A `server` selector in the registry
would put three tailnet addresses in this repo.

**A second, different metric also exists and is not the one to use:**
`adguard_top_upstreams_avg_response_time_seconds` (3 series) carries an **additional
`upstream` label** which is *also* address-shaped (1 distinct value, IPv4, `:port`
suffix). Task 1.6's phrase "per-upstream response time" reads onto this metric literally;
the alert rules read the former. Decision recorded in **Decisions made alone**. If the
upstream metric is ever used, note it carries a **fourth** address-bearing label the spec's
projection rule does not currently name.

### Pinned: the node↔series derivation is still 1:1

```
ssh vps "curl -s --get --data-urlencode 'query=up{job=~\"node-exporter.*\"}' \
         http://localhost:9090/api/v1/query"
```

Derivation: host = `instance.rsplit(":",1)[0]` for the node-exporter series; host =
the authority of the `server` URL for the AdGuard series; match on host.

| check | result |
|---|---|
| node-exporter jobs matching `node-exporter.*` | 3 (`node-exporter-pi5`, `node-exporter-vps`, `node-exporter-pi2`) |
| distinct node-exporter instance hosts | 3 |
| distinct AdGuard `server` hosts | 3 |
| matched hosts | **3** |
| unmatched AdGuard hosts | **0** |
| unmatched node-exporter hosts | **0** |

**1:1 with no ambiguity and no unmatched value — re-verified 2026-09-01.** D5's claim
holds. Address strings existed only in the probe process; none is recorded here.

### Pinned: measured per-device baselines

```
ssh vps "curl -s --get --data-urlencode \
  'query=<avg|min|max|quantile 0.95>_over_time(adguard_avg_processing_time_seconds[<W>])' \
  http://localhost:9090/api/v1/query"
```
→ instant vector, 3 series per call; each mapped to its node enum via the derivation above.

**All figures in milliseconds.**

| node | window | avg | min | p95 | max |
|---|---|---|---|---|---|
| `rp5` | 15m | 2.439 | 2.439 | 2.439 | 2.439 |
| `rp5` | **1h** | **2.439** | 2.438 | 2.440 | 2.440 |
| `rp5` | 6h | 2.432 | 2.413 | 2.440 | 2.441 |
| `rp5` | **24h** | **2.399** | 2.361 | 2.439 | 2.441 |
| `rp5` | 7d | 2.322 | 2.150 | 2.412 | 2.441 |
| `vps` | 15m | 2.771 | 2.771 | 2.772 | 2.772 |
| `vps` | **1h** | **2.772** | 2.770 | 2.775 | 2.776 |
| `vps` | 6h | 2.766 | 2.757 | 2.773 | 2.776 |
| `vps` | **24h** | **2.739** | 2.715 | 2.771 | 2.787 |
| `vps` | 7d | 2.631 | 2.428 | 2.749 | 2.787 |
| `rp2` | 15m | 77.306 | 76.804 | 77.536 | 77.591 |
| `rp2` | **1h** | **77.017** | 76.804 | 77.406 | 77.591 |
| `rp2` | 6h | 76.396 | 75.809 | 77.222 | 77.591 |
| `rp2` | **24h** | **74.724** | 72.493 | 76.807 | 77.591 |
| `rp2` | 7d | 66.862 | 60.142 | 75.474 | 77.591 |

**Headroom against each node's own live bars** (from 1.4 / 1.5), using the 24 h average:

| node | 24 h avg | warning bar | critical bar | fleet-wide critical bar |
|---|---|---|---|---|
| `rp5` | 2.40 ms | 60 ms | 150 ms | 300 ms |
| `vps` | 2.74 ms | 80 ms | 200 ms | 300 ms |
| `rp2` | 74.72 ms | **200 ms** | 500 ms | 300 ms |

### The metric is a slow-moving cumulative average — a caveat the trend summary must carry

Every window's min and max are within ~1 % of the mean, and the 15 m window is flat to
three decimal places. `adguard_avg_processing_time_seconds` is AdGuard's own running
statistic over its internal stats period, **not** an instantaneous latency. Consequences
for `dns_performance`'s summary:

- "Direction of travel" over `15m` or `1h` is close to meaningless — the series barely
  moves inside those windows. Over `24h` and multi-day spans it is genuinely informative
  (see the 15-day trajectory below).
- A first/last/min/max summary will look suspiciously constant on the short windows. The
  result should say the metric is a rolling average rather than let the flatness read as a
  broken query.

### 15-day trajectory — rp2 is climbing, and it is a real signal

```
ssh vps "curl -s --get --data-urlencode 'query=adguard_avg_processing_time_seconds' \
  --data-urlencode 'start=<now-15d>' --data-urlencode 'end=<now>' \
  --data-urlencode 'step=21600' http://localhost:9090/api/v1/query_range"
```
→ 3 series × 61 points at a 6-hour step.

| node | 2026-08-18 | 2026-09-02 | 15-day min / max |
|---|---|---|---|
| `rp5` | 2.17 ms | 2.44 ms | 2.09 / 2.44 |
| `vps` | 2.32 ms | 2.77 ms | 2.25 / 2.77 |
| `rp2` | **56.44 ms** | **77.24 ms** | 56.41 / 77.24 |

rp2 has risen **monotonically, ~37 %, over 15 days**, with no plateau. It is still well
under its 200 ms warning bar, so nothing has fired and nothing will for a while — which is
exactly the class of question `dns_performance` exists to answer and the alerting brains
cannot. Recorded as the change's first genuine live finding rather than as a fault.

---

## 1.7 Cross-cutting record

### APPLY-RESOLVED markers

| marker | where it appears | pinned value | evidence |
|---|---|---|---|
| `APPLY-RESOLVED:gatus-window` | `specs/homelab-tools/spec.md:52`, `design.md:214`, `design.md:584` | **`{1h, 24h, 7d, 30d}`** | 1.1 — the server's own 400 body: `Durations supported: 30d, 7d, 24h, 1h` |
| `APPLY-RESOLVED:memory-bar` | `specs/homelab-tools/spec.md:127`, `design.md:235`, `design.md:586` | **75 % used** (`> 75`) | 1.5 — live Grafana provisioning API, rule `High memory usage`, evaluator `{gt, [75]}` |
| `APPLY-RESOLVED:dns-baselines` | `tasks.md:24` only — **not present in `specs/`** | 24 h avg: `rp5` 2.40 ms, `vps` 2.74 ms, `rp2` 74.72 ms | 1.6 — `avg_over_time(adguard_avg_processing_time_seconds[24h])`, node mapping derived live |

No spec file was edited by this task group. Marker replacement is task **9.7**.

### Values the registry must take from this record, not from prose

| registry value | pinned | section |
|---|---|---|
| `endpoint_history` window domain | `1h`, `24h`, `7d`, `30d` | 1.1 |
| `node_resource_trend` disk bar | 15 % **free** on `mountpoint="/"` | 1.5 |
| `node_resource_trend` swap_io bar | 50 pages/s (`rate(pswpin[5m]) + rate(pswpout[5m])`) | 1.5 |
| `node_resource_trend` swap_used bar | 95 % full — reported, labelled not-the-trigger | 1.5 |
| `node_resource_trend` memory bar | 75 % used | 1.5 |
| `node_resource_trend` cpu / load / temperature bars | none | 1.5 |
| `scrape_targets` target count | **7** | 1.3 |
| `freshness_check` timestamp metrics | the 8 names listed | 1.2 |
| `dns_performance` metric | `adguard_avg_processing_time_seconds` | 1.6 |
| `dns_performance` baselines | table in 1.6 | 1.6 |
| `dns_performance` per-node bars | 60/150, 80/200, 200/500 ms + 300 ms fleet-wide | 1.4, 1.5 |

### Contradictions found — live reading versus the written record

1. **Seven scrape targets, not six.** `design.md` D3 and `tasks.md` 1.3 / 4.4 say six jobs.
   `pushgateway` is a seventh live target that `InstanceDown` and `HenkInstanceDown` both
   alert on. A test asserting six will fail in production.
2. **`APPLY-RESOLVED:memory-bar` is 75, not 90.** Three memory bars are simultaneously
   live — Grafana 75, Prometheus-native 90, `homelab_health` 90. The design's refusal to
   copy the 90 was correct; the divergence is larger than it suspected.
3. **`design.md` D9's own "corrected" DNS baselines have `vps` and `rp2` swapped.** D9
   records the live 2026-08 reading as Pi5 / VPS / Pi2 = 2.3 / 57 / 2.1 ms. The 15-day
   range query above shows that on 2026-08-18 the fleet read rp5 2.17, **vps 2.32**,
   **rp2 56.44** — so the 57 ms figure belongs to **rp2**, not the vps, and the 2.1/2.3
   pair is rp5/vps rather than vps/rp2. This is a third instance of the transcribe defect
   the change was structured to prevent, and it sits inside the very passage written to
   prevent it. **`design.md` D9 needs correcting at task 9.7 alongside the marker
   replacement.** The docs' six-month-old figures (17 / 41 / 134 ms) are wrong in a
   different way again and are separately due a `docs-update`.
4. **The vps swap-fullness chronic ceiling is 89.70 %, not 86.3 %** (7-day max, measured).
   Five points from the 95 % bar.
5. **The vps 7-day CPU peak is 70.58 %**, above the 67.8 % the design cites for a 15-day
   window.
6. **`temperature` has no data on `vps`** and **`container_health_state` has no data on
   `rp5`** — two availability holes inside domains the spec declares uniform. Both must
   surface as D2's third outcome, not as empty results.
7. **A per-endpoint Gatus status route exists**, so D7's in-process-filter fallback is not
   needed. The design treats it as version-dependent and unknown; it is now measured.
8. **Gatus stores only ~1.7 hours of check `results`.** "Since when" for an endpoint must
   come from the `events` list, not from `results`. Nothing in the design or spec says this,
   and an implementation reaching for `results` would answer "since when" wrongly and
   confidently.
9. **`obsidian_backup_push_*` is a fourth freshness family** that task 1.2's list omits and
   that backs a live alert.
10. **6 of 23 native rules have no query measuring their input** — the error-counter and
    duration family. Recorded above as accepted gaps with a cheap closure path.

### Decisions made alone

- **`dns_performance`'s metric.** Task 1.6 says "per-upstream response time", which reads
  literally onto `adguard_top_upstreams_avg_response_time_seconds`. I pinned
  **`adguard_avg_processing_time_seconds`** instead, because the binding spec delta requires
  the registry to measure the input of the live rules (D4's admission criterion, and the
  `homelab-tools` requirement that thresholds be traceable to live rule expressions), and
  all 14 DNS rules read that metric. Both metrics' shapes are recorded above so the choice
  can be revisited without re-probing.
- **Which Grafana folder is "Henk's".** Task 1.5 says "every Grafana alert rule in the Henk
  folder (or whatever folder the docs say Henk's rules live in)". The `henk` folder holds
  6 rules and **contains no memory rule**, so pinning `memory-bar` required going outside
  it. I enumerated **all 18** Grafana rules across all 4 folders and recorded every one,
  rather than stopping at the folder boundary and reporting `memory-bar` as unmeasurable.
- **Baseline statistic and windows.** The task says "e.g. avg over 1h and 24h". I recorded
  avg / min / p95 / max over `15m`, `1h`, `6h`, `24h`, `7d` — the four spec windows plus
  `7d` — because a single average would have hidden that the metric barely moves inside the
  short windows, which is itself a design-relevant fact.
- **Rule-to-query mapping granularity.** I mapped each of the 23 natives individually
  rather than by group, so that the six counter/duration gaps inside otherwise-covered
  groups are visible instead of being absorbed by a group-level "covered".
- **Gatus version.** Recorded as NOT MEASURED rather than inferred from the image tag
  `latest`, which is not a version.
- **The `server`/`instance`/`upstream`/`hostname`/`globalUrl`/`__address__` values.** Every
  one was handled in-process and never written to this file, including into the mapping
  table, which is keyed by job name instead.

### Publication-safety check on this file

```
$ grep -nE '100\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.|10\.[0-9]+\.[0-9]+\.[0-9]+' notes/backend-probe.md
(no output — 0 hits)

$ gitleaks detect --no-git --source openspec/changes/read-depth/notes/
(0 leaks found)
```

Raw probe bodies — which do carry addresses, `instance` values, `server` URLs, scrape URLs,
and Gatus endpoint names — were written only to an untracked scratch directory outside the
repository and are not part of this change.
