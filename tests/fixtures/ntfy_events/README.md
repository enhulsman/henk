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

Not captured (synthesize for tests from the known formats):
- Gatus **resolved** ("An alert has been resolved after passing successfully N time(s) in a row…") — the smoke endpoint was removed while still failing.
- Grafana **resolved** (title flips to `[RESOLVED]`).
