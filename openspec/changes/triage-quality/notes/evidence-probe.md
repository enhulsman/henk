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

## 1b.5: the capture moved into place

Owner, 2026-09-23, from a root shell on rp5 (the `ssh -t` form failed first: `pi`'s
sudo does not allow `test`). `triage-cases/2026-09-23-capture/` is `10001:10001`, mode
`700`, with 588 files. `2026-09-23-raw/` is still `0:0`, mode `700`. The agent confirmed
the same day that `/home/pi/triage-capture-staging` no longer exists, so every transit
copy is gone. The `triage-cases/` directory's own ownership was set by the same chained
command. The agent cannot read it without sudo, so it is not verified independently.

## 1.1: cadvisor and systemd label sets (live, read-only)

Probed 2026-09-23 ~10:10Z against the vps Prometheus (3.9.1) over its HTTP query API from
the workstation. The probe printed label **names** and counts only; `instance` values are
addresses and were never printed. `container_label_*` below stands for the Docker compose
labels cadvisor copies onto named containers (ten or eleven of them, all
`container_label_com_docker_compose_*`).

**Label-name sets.** The three metrics share one shape per cgroup kind on each job:

| job | cgroup kind | series | label names |
|---|---|---|---|
| `cadvisor-pi5` | unnamed cgroup (host units, slices) | 88 | `__name__`, `id`, `instance`, `job` |
| `cadvisor-pi5` | named container, compose-managed | 16 | the above plus `image`, `name`, `container_label_*` |
| `cadvisor-pi5` | named container, not compose-managed | 3 | the above plus `image`, `name` |
| `cadvisor-vps` | unnamed cgroup | 41 | `__name__`, `id`, `instance`, `job` |
| `cadvisor-vps` | named container, compose-managed | 19 | the above plus `image`, `name`, `container_label_*` |

This holds for `container_memory_working_set_bytes`, `container_memory_swap` and
`container_cpu_usage_seconds_total` alike (107 series each on `cadvisor-pi5`, 60 each on
`cadvisor-vps`). The one difference: **`container_cpu_usage_seconds_total` carries a
`cpu` label on every series, and its only value is `total`** (107/107 and 60/60). There is
exactly one CPU series per container name on both jobs (19 names, multiplicity 1).

**What this means for D4.** cadvisor exports no per-CPU series here, so D4's
`max by (name)` (design.md:205) is a no-op as a collapse. It still drops `cpu`, `id`,
`image`, `instance` and the compose labels, leaving one `name`-labelled series per
container. D4's templates stay frozen as written (design.md:304-307), so nothing changes.

**Counts per job:**

| selector | `cadvisor-pi5` | `cadvisor-vps` |
|---|---|---|
| `name!=""` (each of the three metrics) | 19 | 19 |
| `id=~"/system\\.slice/.+\\.service"` (each of the three metrics) | 36 | 27 |

These agree with design.md:56 (19 named on both) and design.md:59 (27 and 36). Both were
measured on the same day, so nothing needed reconciling.

**`node_systemd_unit_state`.** `count by (job)` returns one job only:
`node-exporter-vps`, 1320 series. `node-exporter-pi5` and `node-exporter-pi2` are both up
(`up` has one series each) and have no `node_systemd_unit_state` series. The 1320 series
are **264 units x 5 states** (`activating`, `active`, `deactivating`, `failed`,
`inactive`, 264 each). Exactly 264 series are `== 1`, one per unit.

**Recorded against the design (standing rule 2):**
- **D5's `unit_count` counts series, not units.** The `host_service_state`/`unit_count`
  template (design.md:299) is `count(node_systemd_unit_state{job="<job>"})`, which returns
  1320 on vps today. The renderer's "the units counted" (design.md:331) would therefore
  overstate by the number of states, 5x. The template is byte-frozen by the D5 contract,
  so the fix belongs in the renderer (group 4): either render the count as series, or
  report units as the count divided by the distinct `state` values. That derivation must
  itself be tested, because node_exporter versions differ in their state set. The
  template still works as its stated purpose, proof that the collector ran.
- **D5's "58 series" for `memory_movers` depends on the time and the window**
  (design.md:247, :278). The same `max_over_time(<units>[w]) or max_over_time(<named>[w])`
  form counted on `cadvisor-vps` returns 50 (1h) and 55 (24h) at T=1790144998, and 46 (1h)
  and 56 (24h) at probe time. The claim that the selectors bound the series count still
  holds. The figure is one reading, not a constant, so no test should pin 58.
- **The 288/288 fraction presumes a 5-minute step** (design.md:72-73, :328;
  tasks.md:295-297). At rp5's effective `query_range_max_points` of 60, a 24h range steps
  at 1465 s and expects 59 points (`range_step_seconds`/`expected_point_count`,
  `henk/tools/query_registry.py:641-651`). A live `host_service_state` 24h result
  therefore reads "N/59", not "N/288". Group 4's fixtures may keep the 288-point shape as
  long as the denominator comes from the fixture's own step and is never hardcoded.

## 1.2: SDK ending surface (source read, no model call)

Read on 2026-09-23. The workstation `.venv` has only the base install: the SDK is the
`runtime` extra (`pyproject.toml:25`, installed in the image by `Dockerfile:38`). So the
read used the unpacked `claude_agent_sdk` 0.2.123 wheel in the local uv cache
(`_version.py`: `__version__ = "0.2.123"`; two cached copies, identical under
`diff -r`). Paths are relative to the package root.

**`AssistantMessage`** (`types.py:1025-1037`, a dataclass):
- `error: AssistantMessageError | None = None` (`types.py:1032`);
- `stop_reason: str | None = None` (`types.py:1035`);
- also present: `model: str` (`:1030`), `usage: dict[str, Any] | None` (`:1033`),
  `message_id: str | None` (`:1034`).

**`AssistantMessageError`** (`types.py:1005-1012`) is a `Literal` over exactly
`"authentication_failed"`, `"billing_error"`, `"rate_limit"`, `"invalid_request"`,
`"server_error"` and `"unknown"`. This matches design.md:619-620.

**`ResultMessage`** (`types.py:1200-1223`, a dataclass):
- `subtype: str` (`:1204`), not enumerated;
- `is_error: bool` (`:1207`), required;
- `stop_reason: str | None = None` (`:1210`);
- `errors: list[str] | None = None` (`:1218`), which holds **text** and must never be
  rendered into the notice;
- `api_error_status: int | None = None` (`:1222`). Its source comment (`:1219-1221`)
  says: the HTTP status of the failing API call "when `is_error` is True and `subtype` is
  "success"; None otherwise", emitted by the CLI since v2.1.110, with no message content.

**`stop_reason` is an unconstrained `str`** on both types. The string `refusal` does not
occur anywhere in the SDK's Python source, so `"refusal"` is an API value passed through,
not an SDK enum (design.md:78 records it as measured).

**Where each field is parsed** (`_internal/message_parser.py`):
- the assistant case constructs `AssistantMessage` at `:192-202`. `error` comes from the
  **top-level** frame, `data.get("error")` (`:196`); `stop_reason` comes from the
  **inner** message, `data["message"].get("stop_reason")` (`:199`);
- the result case constructs `ResultMessage` at `:293-317`, with
  `is_error=data["is_error"]` (`:297`), `stop_reason=data.get("stop_reason")` (`:300`),
  `errors=data.get("errors")` (`:314`) and `api_error_status=data.get("api_error_status")`
  (`:315`).
- **`stop_details` is never read.** No Python file in the package contains the string,
  so it is dropped at both construction sites (`:199` and `:300`, the lines that read
  `stop_reason` and nothing beside it). design.md:79 is correct.

**A stream behaviour group 9's fake must reproduce** (`_internal/query.py`):
- On a result frame with `is_error` true, the reader stores `"; ".join(errors)` (or the
  subtype) (`:304-308`), and still yields the `ResultMessage` (`:322`).
- If the CLI process then exits non-zero, the resulting `ProcessError` is replaced by an
  error frame carrying that text (`:334-353`). `receive_messages` raises it as a bare
  `Exception` (`:851-852`).

So one failing turn can produce both `ResultMessage.is_error` **and** a raise, in that
order. D12's table puts the `is_error` check ahead of "the turn raised", which gives the
right answer only if the accumulator records the result **as it streams**, before the
exception propagates. The exception's message holds `errors` text, so the notice may use
its class name only.

## 1.5: `HenkSwapPressure` label set against identity and node derivation

**What Prometheus can and cannot show.** `HenkSwapPressure` is a **Grafana-managed** rule
(folder `henk`, group `henk-events`; design.md:138-139 cites the henk-folder rule table),
not a Prometheus rule. Probed 2026-09-23:
- `/api/v1/rules` on the vps Prometheus lists six groups, and none contains
  `HenkSwapPressure`;
- `ALERTS{alertname="HenkSwapPressure"}`, `{alertname="HenkSwapPressure"}` of any
  metric, and `GRAFANA_ALERTS` all return no series over 05:40-06:50Z at 60 s steps;
- `count by (alertname) (ALERTS)` is empty over the same range.

So Prometheus's history cannot give the fired label set. It was established from three
sources instead, none of which prints a value that could be an address:
1. **Series labels, measured.** The rule expression, recorded verbatim at
   `openspec/changes/archive/2026-09-02-read-depth/notes/backend-probe.md:498`, was
   evaluated as a `query_range` over 06:00-07:00Z at 15 s. It returned exactly one series,
   label names `instance`, `job`, with `job="node-exporter-vps"`. It was true for 76
   consecutive points, 06:11:45Z to 06:30:30Z, which is consistent with a 15m `for`
   before the 06:27:58Z notification. Both `or` branches carry `{instance, job}`, so `or`
   yields one series with A's value, as already settled.
2. **Rule labels, recorded.** `route=henk-events` (archive
   `2026-08-02-henk-events/tasks.md:7`) and `severity=warning` (backend-probe.md:498;
   sensor-routing-coverage design D3). There is **no `identity_scope`** label
   (backend-probe.md:498, "extra label: —").
3. **Labels Grafana adds.** `alertname` and `grafana_folder`, as rendered in every
   captured Grafana payload (`tests/fixtures/ntfy_events/henk-events-live.jsonl` lines 3-5,
   label names only).

**Resulting label names:** `alertname` (`HenkSwapPressure`), `grafana_folder` (`henk`),
`instance` (an address, never recorded), `job` (`node-exporter-vps`), `route`
(`henk-events`), `severity` (`warning`). A resolve may add `grafana_state_reason`,
`datasource_uid` and `ref_id` (fixture line 5).

**Against the identity derivation.** The key is `grafana:HenkSwapPressure`:
- `_derive_grafana` takes the name from the `alertname = ` label (`henk/events/identity.py:93-95`)
  and builds `grafana:{name}` (`:101`);
- the scope suffix applies only when an `identity_scope` line exists (`:102-110`), and
  this rule has none;
- the same key appears in the existing tests (`tests/test_audit_log.py:108`).

D9's **same-rule** key `f"{source}:{name}"` (design.md:487-488) is therefore **identical**
to the identity key for this rule, so tiers 1 and 2 coincide here. That is harmless,
because each handoff appears once at its best rank. The root policy groups by
`[grafana_folder, alertname]` (sensor-routing-coverage design D3), so a simultaneous
swap alert on two nodes arrives as one `[FIRING:2]` notification, under one identity.
**Match: yes.**

**Against D9's node derivation** (design.md:489-491, whole-word `rp5`/`vps`/`rp2` plus
the `NODE_FOR_JOB` job names, `henk/tools/query_projection.py:30-34,46-49`):
- The only node-bearing label value is `job = node-exporter-vps`, which `NODE_FOR_JOB`
  maps to `vps`. The whole word `vps` also matches inside it at the hyphen, so both routes
  give `{vps}`.
- `instance` is an address on all three node-exporter jobs. Probed by shape only: no
  `instance` value contains a node token, so it adds nothing, which is also why the
  derivation must not rely on it.
- The Grafana notification title absorbs the grouped label values, `instance` included
  (fixtures README, "The title absorbs grouped label values"). So the title carries an
  address. D9 stores only the derived nodes, never the title, which is consistent with
  "No address is ever stored".
- The `Source:`/`Silence:` URLs in the message point at the Grafana host. The hostnames
  in the homelab docs matching `*grafana*` contain no whole-word node token (checked by
  shape). The real payload's host was redacted in the fixtures, so this remains an
  **assumption**. If group 8 wants it closed, it should match only the title and the
  `- <label> = <value>` lines, not the URLs.

**Match: yes, `{vps}`**, under that assumption.

**The limit, stated plainly.** Normally `ALERTS` carries the rule's labels plus the
series labels, which is what Alertmanager sends. This rule is Grafana-managed, so there
is no `ALERTS` record at all, and the label set above is reconstructed rather than read
from the fired payload. The fired ntfy payload itself is in the root-only preserved
material (task 1.9) and was not read.

The ntfy side can change what Henk sees. Grafana posts to ntfy's built-in
`?template=grafana` (archive `2026-08-02-henk-events/tasks.md:7`), and ntfy renders the
Grafana JSON into the title and the message. Henk's intake takes `title` and `message`
verbatim from the ntfy frame (`henk/events/intake.py:393-407`). Identity and node
derivation read only that rendered text (`identity.py:93,102-103`, `:44-58`). So a change
to the ntfy template, or a Grafana contact-point template override, could:
- add or drop the `- label = value` lines;
- or move labels between the title and the message.

Either would change both derivations without any change in Henk. The captured fixtures
show every label rendered on its own line (fixture lines 3-5).

## 1.6: restart signal on rp5 (owner measurement, 2026-09-23)

restart-signal cadvisor-pi5: verified

- Container: `wordle-web` on rp5 (`cadvisor-pi5`), restarted in place at ~09:58:55 CEST
  (~07:58:55Z) on 2026-09-23.
- `resets(container_cpu_usage_seconds_total{job="cadvisor-pi5",name="wordle-web"}[15m])`
  went from 0 to 1 within one scrape.
- The counter fell from ~2014 s to 1.9 s.
- `container_start_time_seconds` stayed flat, and so did `changes()` over it: it is the
  creation time and does not move on a restart.
- Scrape interval: ~30 s.

## 1.7: restart signal on vps (owner measurement, 2026-09-23)

restart-signal cadvisor-vps: verified

- Container: `taiga-docker-taiga-front-1` on the vps (`cadvisor-vps`), restarted with
  `docker restart` at ~10:15 CEST (~08:15Z) on 2026-09-23.
- `resets(...{job="cadvisor-vps",...}[15m])` went to 1.
- The counter fell to 0.61 s.
- `changes(container_start_time_seconds[15m])` stayed 0.

## 1.8: consolidated record

Every figure below is a name, a count or a shape. No label value that could be an
address, no payload text and no unit names beyond placeholders.

**Verdicts, one per cadvisor job** (read by test 3.8): the two `restart-signal` lines
under 1.6 and 1.7 above. Each appears exactly once in this file, on its own line.
`cadvisor-pi5` and `cadvisor-vps` are both verified, so no cadvisor job is unmeasured.

**Host units and systemd state** (from the findings note, 2026-09-23, re-measured under
1.1 the same day):
- `/system.slice/*.service` cgroups under cadvisor: **27** series on `cadvisor-vps` and
  **36** on `cadvisor-pi5`, with an empty `name`. The findings note's hand count
  (`notes/2026-09-23-vps-swap-incident-findings.md:36`) and the 1.1 count agree, so the
  design uses 27 and 36.
- `node_systemd_unit_state` exists only for `node-exporter-vps`: 264 units, 1320 series.
- Over 24 h on vps, at 5-minute samples (288 points):
  - `example-a.service` was `failed` in **288/288** samples;
  - `example-b.service` was `activating` in **281/288**. It was crash-looping, and the
    unit was removed on 2026-09-23.

  At rp5's default point budget the same window has 59 points (see 1.1).

**Measurements 1.1-1.7 in one place:**
- 1.1: the label sets and counts above. `cpu` is present with the single value `total`,
  so D4's `max by (name)` is a no-op collapse.
- 1.2: the ending surface above. There is no `stop_details`, and `stop_reason` is a free
  string.
- 1.3 and 1.4: pending owner runs; the commands are below.
- 1.5: `grafana:HenkSwapPressure`, nodes `{vps}`, reconstructed rather than read from the
  payload.
- 1.6 and 1.7: both restart signals verified.

**Contradictions and corrections recorded for the code** (design.md is unchanged, and
the code follows this record):
1. D5 `unit_count` returns series, which is units x states. It must not be rendered as a
   unit count (1.1).
2. D5's 58-series figure is one reading, not a constant (1.1).
3. The 288-point denominator is a 5-minute-step shape. The registry's default gives 59
   (1.1).
4. The D12 classifier must record `ResultMessage.is_error` as it streams, because a raise
   can follow it in the same turn (1.2).
5. `HenkSwapPressure` has no Prometheus `ALERTS` history, being Grafana-managed. The
   task 1.5 premise of reading it from `ALERTS` does not hold, and the label set was
   reconstructed instead (1.5).

## 1.3 / 1.4: owner commands (pending)

Both are drafted and have not been run. Each block is pasted into a root shell on rp5
(`sudo -i`). Neither contains `sudo` or `set -e`.

### 1.4: event-triage session records per UTC day

The audit log is one append-only, unrotated JSONL file at `audit.path`,
`/data/audit/henk-audit.jsonl` in the container (`henk/config.py:493`,
`config.yaml:219`). It lives at the root of the volume
(`henk/audit/logger.py:340-358`). Every schema version v1-v4 requires `record_type`, and
every version carries `trigger` (`audit-record.v4.schema.json`: `record_type` enum,
`trigger` enum `owner-message`/`event`, `at` a number). The writer always stamps `at`
with epoch seconds (`logger.py:347-350`).

The command reads only that file, not `triage-cases/`, which holds a copy of one day's
records and would double-count. It prints file names, `YYYY-MM-DD count` lines and
summary counts, never record content.

```
cd /var/lib/docker/volumes/henk_henk_audit/_data && ls -1 -- *.jsonl* 2>/dev/null; python3 - henk-audit.jsonl <<'PY'
import collections, datetime, json, sys, time
days = collections.Counter()
with_handoff = malformed = no_at = 0
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(r, dict) or r.get("record_type") != "session" or r.get("trigger") != "event":
            continue
        at = r.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            no_at += 1
            continue
        days[time.strftime("%Y-%m-%d", time.gmtime(at))] += 1
        if r.get("handoff_message_id"):
            with_handoff += 1
for d in sorted(days):
    print(d, days[d])
if days:
    first = datetime.date.fromisoformat(min(days))
    last = datetime.date.fromisoformat(max(days))
    span = (last - first).days + 1
    daily = [days.get((first + datetime.timedelta(i)).isoformat(), 0) for i in range(span)]
    def worst(w):
        return max(sum(daily[max(0, i - w + 1):i + 1]) for i in range(span))
    total = sum(daily)
    print("total", total)
    print("max_per_day", max(daily))
    print("span_days", span, "from", first, "to", last)
    print("mean_per_day %.2f" % (total / span))
    print("max_in_any_90d", worst(90), "(D8 row bound 500)")
    print("max_in_any_30d", worst(30), "(D13 recording bound 200)")
    print("with_handoff_id", with_handoff)
print("malformed_lines", malformed, "session_event_without_at", no_at)
PY
```

**What the answer decides:**
- D8 keeps **500 rows over 90 days** (design.md:477). That fits if
  `max_in_any_90d` is at most 500, a sustained average of at most **5.55 triages per day**
  (500/90).
- D13 keeps **200 recordings over 30 days** (design.md:651). That fits if
  `max_in_any_30d` is at most 200, at most **6.67 per day** (200/30).

D8's bound is the tighter one. D8 archives only handoffs published from event sessions,
so `with_handoff_id` is its closer measure, and the session count is an upper bound. If a
bound is exceeded, the count rather than the age becomes the real retention, and groups 6
and 10 must take corrected constants from this record. For scale: the cadence cap
(`cap_per_24h: 5`, `config.yaml`) gates only the Signal send, not triage
(`henk/events/pipeline.py:14-15`), so it bounds nothing here. Only the per-identity
cooldowns do.

### 1.3: is `claude-fable-5-1` available to the deployed credential?

How the credential reaches the container: `env_file: .env` (`docker-compose.yml:40`),
which carries `CLAUDE_CODE_OAUTH_TOKEN`. There is no mounted Claude config directory. The
bundled CLI keeps its state under `CLAUDE_CONFIG_DIR`, here pointed at `/tmp` inside the
throwaway container.

```
cd /home/pi/Coding/henk && docker compose run --rm --no-deps -T -e CLAUDE_CONFIG_DIR=/tmp/henk-probe henk python - <<'PY'
import asyncio
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

async def main():
    opts = ClaudeAgentOptions(
        model="claude-fable-5-1",
        tools=[], allowed_tools=[], mcp_servers={}, strict_mcp_config=True,
        setting_sources=[], max_turns=1, cwd="/tmp",
        system_prompt="Answer in one word.",
    )
    async for m in query(prompt="Reply with the single word: ok", options=opts):
        if isinstance(m, AssistantMessage):
            chars = sum(len(getattr(b, "text", "") or "") for b in m.content)
            print("assistant model=%s error=%s stop_reason=%s text_chars=%d" % (m.model, m.error, m.stop_reason, chars))
        elif isinstance(m, ResultMessage):
            print("result subtype=%s is_error=%s stop_reason=%s api_error_status=%s errors=%d" % (m.subtype, m.is_error, m.stop_reason, m.api_error_status, len(m.errors or [])))

try:
    asyncio.run(asyncio.wait_for(main(), 150))
    print("exception=none")
except BaseException as e:
    print("exception=" + type(e).__name__)
PY
```

**How to read the result:**
- **yes:** an `assistant` line whose `model` starts with `claude-fable-5-1`, with
  `error=None`, plus a `result` line with `is_error=False`.
- **no:** otherwise. Record the `error` value (the `AssistantMessageError` class), the
  `api_error_status` and the `exception` class name.
- **exception only:** if only an `exception=` line appears (for example
  `CLINotFoundError` or `TimeoutError`), record that; the question stays open.

**Effects on live Henk:**
- `run --rm` starts a separate container in its own cgroup, with its own 768m cap
  (`docker-compose.yml:71`). It never uses `exec` inside live Henk, so live memory is
  untouched (D14). One CLI plus Python stays well inside 768m.
- `--no-deps` starts nothing else. The run joins the running tailscale netns, as live
  Henk does, and publishes no ports.
- `-T` lets the heredoc reach `python -` on stdin.

**Writes:**
- The service definition still mounts `henk_audit` at `/data/audit` read-write, and
  `config.yaml` read-only. The script writes to neither. It does not import `henk`, and
  it runs the CLI with `cwd=/tmp`.
- The CLI's state goes to `/tmp/henk-probe` in the throwaway container's writable layer,
  which `--rm` deletes.
- **Nothing is written to Henk's live state or config directory.**

**Spend:** it uses the same OAuth token as live Henk, so one short turn is billed to the
owner's subscription. That is standing rule 5, which is why this is an owner step.
