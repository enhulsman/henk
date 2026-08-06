# Live ntfy event fixtures (captured 2026-07-22, henk-events prep)

`henk-events-live.jsonl` — verbatim `GET /henk-events/json?poll=1` output from the
vps ntfy instance during infra prep. One JSON object per line, exactly as the
event-intake subscriber will receive them. Use these for the event-intake TDD
(payload parsing, per-source identity derivation, fallback key).

| Line | Source | Notes |
|---|---|---|
| 1 | manual curl | conforming title (`Gatus \| smoke-test \| firing`) but hand-published — exercises the fallback/identity path for arbitrary publishers |
| 2 | **Gatus** (real) | native format: title `Gatus: {group}/{endpoint}`, state only in the message body ("An alert has been triggered…"), `priority`, `tags`, `click` present. Does NOT match the idealized title contract — per-source rules must key on the `Gatus: ` prefix + group/endpoint |
| 3 | **Grafana** (real, via ntfy `?template=grafana`) | title `🚨 [FIRING:1] {alertname} {folder} (…)`; message carries Labels (`alertname`, `grafana_folder`, `route`) and Annotations (`summary` follows the `Grafana \| name \| detail` convention) |
| 4 | **Grafana `HenkInstanceDown`** (real, captured 2026-08-06 during `sensor-routing-coverage` task 4.3b) | First capture of a rule carrying `identity_scope`. Labels: `alertname`, `grafana_folder`, `identity_scope=instance`, `instance=cadvisor:8080`, `job`, `route`, `severity=critical`; `summary` rendered from the `{{ $labels.* }}` template. Derives `grafana:HenkInstanceDown/cadvisor:8080` |
| 5 | **Grafana `HenkInstanceDown` RESOLVED** (real, same incident as line 4) | The resolve for line 4. Carries `grafana_state_reason = NoData` plus `datasource_uid`/`ref_id` that the firing payload lacks. Must derive the **same** key as line 4 |

Lines 4–5 use the `cadvisor:8080` target deliberately, not the pi2 one that was also
captured: pi2's `instance` label is a **tailnet address**, whereas `cadvisor:8080` is a
Docker-internal name that commits as-is.

**One redaction, in the URLs only:** the `Source:`/`Silence:` links in these two payloads
pointed at the real Grafana hostname, which the pre-commit hook correctly refuses. The host
was rewritten to `grafana.hulsman.dev`; **every label, annotation and value is untouched.**
Identity derivation reads the `- <label> = <value>` lines, never the URLs, so the redaction
cannot affect what these fixtures test — but they are "verbatim apart from the Grafana host",
not byte-for-byte, and a future capture should expect the same edit.

**Line 5 records a boundary condition worth keeping:** a NoData resolve renders as
`Value: A=-1, B=-1, C=-1`. Since the condition is `threshold gt -1` and `-1 > -1` is false,
the no-series case does **not** clear the bar — which is precisely why moving the bar to `-1`
restores Prometheus semantics without making every rule fire permanently. That safety
argument was previously established only by probe; this payload is production evidence of it.

**Two format notes worth knowing before writing a parser against these:**

- **The title absorbs grouped label values.** Line 4's title is
  `🚨 [FIRING:1] HenkInstanceDown cadvisor:8080 (henk instance cadvisor-vps henk-events critical)`
  — the bare word `instance` there is the `identity_scope` label *value*, not a key. Adding
  labels to a rule changes its title shape, so identity derivation reads the
  `- <label> = <value>` body lines, never the title.
- **A recovery arrives as NoData, not as a value-based resolve.** `up == 0` returns no
  series once the target is back, so Grafana resolves through `noDataState: OK`. That is why
  line 5 carries `grafana_state_reason = NoData`.

Not captured (synthesize for tests from the known formats):
- Gatus **resolved** ("An alert has been resolved after passing successfully N time(s) in a row…") — the smoke endpoint was removed while still failing.
- ~~Grafana **resolved**~~ — **captured 2026-08-06**, line 5.
- Grafana **`HenkContainerRestarting`** — **not capturable at all.** Task 4.6 ran the prescribed
  `docker restart` ×2 on 2026-08-06 and the rule did not fire, because cadvisor's
  `container_start_time_seconds` is the container's *creation* time: it does not move on restart,
  and a recreate starts a brand-new series. So `changes()` is 0 on every path, including the
  crash-loop case the rule was added for. There is no payload to capture until the rule is rebuilt
  on a real restart counter (e.g. Docker `RestartCount` via the textfile collector). Do **not**
  synthesize one — a fixture for an event that cannot occur would encode the bug as expected
  behaviour.
