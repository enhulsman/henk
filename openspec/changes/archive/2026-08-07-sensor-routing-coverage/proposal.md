## Why

Henk's curated alert subset has a measured coverage hole, a **live dead rule**, and a maintenance trap. All three are verified against the deployed system, not inferred.

**The dead rule.** `HenkHealthEtl`'s fourth arm — the silent-metric detector — cannot fire. Grafana's rule template evaluates `reduce(last) → threshold(gt 0)`, so a rule fires on its expression's **value**. That arm is `(increase(health_etl_rows_total[48h]) == 0) and (max_over_time(...[7d]) > 0)`, and a `== 0` comparison returns the matching series with value **0** (`and` carries left-hand values). Measured live: 11 series, every one valued `0`. The rule has been structurally incapable of reporting a silent health metric since 2026-07-22, while reporting `health=ok`. The native `HealthEtlMetricSilent` it was transcribed from is fine — native Prometheus fires when an expression returns *any series*, value irrelevant. **The defect is the Grafana template, not the expression.**

**The hole.** Prometheus on the vps holds 23 native alert rules and no Alertmanager (`activeAlertmanagers: []`), so they deliver nowhere. That sounds like 23 missing alerts; it is not. 20 have Grafana twins that do deliver — 12 to `henk-events`, 8 to Discord (twin inventory confirmed against live Grafana state, 2026-08-06). Exactly **three** conditions reach nobody: `InstanceDown`, `ContainerRestarting`, `HighCPU`.

`HighCPU` is not worth routing (15d peak 67.8% against an 85% threshold). `InstanceDown` is, for a specific reason: every Prometheus target is host-down-covered by a Gatus check, but **none is exporter-down-covered** — the three node-exporter endpoints in Gatus are tier-3 with no `alerts:` block, and cadvisor ×2, adguard-exporter and pushgateway are not in Gatus at all. Meanwhile all four henk rules run `noDataState: OK`. So if an exporter dies while its host stays up, `InstanceDown` fires into the void *and* the rules depending on those metrics see noData and report healthy. The system does not merely go blind — it reports itself fine while blind. `ContainerRestarting` is the only rule watching a container crash-loop that no alerting Gatus endpoint fronts.

**The trap.** `~/.claude-config/provisioning/grafana-henk-events.sh` is non-idempotent (POSTs 409 partway under `set -euo pipefail`; its policy `jq` *prepends* the henk route every run) and **stale**: it still carries the pre-2026-08-02 `swap > 90%` fullness expression, where the deployment carries the pressure retune (`>95%` or `rate(pswpin+pswpout) > 50`). Confirmed 2026-08-06: live is correct, the script is wrong, so re-running it silently reverts a deliberate retune. `services/monitoring.md:602` nonetheless calls it "the idempotent provisioning script".

## What Changes

- **The rule template is fixed at source.** All six rules move from `reduce(last) → threshold(gt 0)` to **`reduce(count) → threshold(gt 0)`**, which fires iff the expression returns a series — native Prometheus semantics exactly. This kills the defect class rather than patching instances: arm 4 needs no rewrite, `up == 0` works as written and matches its native twin byte-for-byte, and `HenkDiskPressure`'s latent 0%-available blind spot disappears. Gated on an empirical probe (§2c) with the per-expression workaround recorded as the fallback.
- **Two new Grafana-managed rules** in folder `henk`: `HenkInstanceDown` (`up == 0`, `for: 2m`, critical) and `HenkContainerRestarting` (`changes(container_start_time_seconds{name!=""}[15m]) > 1`, `for: 5m`, warning). Curated subset goes 4 → 6.
- **Critical alerts stop being Henk-exclusive.** All six rules gain a `severity` label; the policy tree gains a sibling route matching `severity=critical` **and** `route=henk-events`, so `HenkInstanceDown` delivers to `henk-events` *and* `Discord-Grafana`. Today's single child route runs `continue: false`, which would make a consumer with a cooldown and a daily cap the sole path for a critical.
- **Henk's alert identity becomes opt-in scopeable.** `grafana:HenkInstanceDown` would otherwise be one identity for seven scrape targets, so a second host failing inside the 6h cooldown would be silently swallowed. Rules opt in via an `identity_scope: <label>` label that `_derive_grafana` honours; rules without it are unaffected.
- **`grafana-henk-events.sh` is rewritten as a convergent applier**: plans and diffs before writing, refuses unexplained drift, PUTs by uid, rebuilds the policy tree from a declaration, and records a credential-scrubbed state snapshot. It carries the retuned swap expression, so re-provisioning can no longer revert it, and it enforces declaration invariants (every rule carries `severity`; every critical is matched by a sibling route to a non-agent receiver; every rule sets `instant: true`).
- **`monitoring.md` corrected**: the false "idempotent" claim, the 6-rule table, the dual-delivery route, and the native-vs-Grafana redundancy map.
- **`HighCPU` deliberately stays unrouted**, recorded so its absence reads as a decision.
- **No Alertmanager.** Considered and rejected on measured grounds — see `design.md` D1.

## Capabilities

### New Capabilities

None. This extends existing routing rather than introducing a capability.

### Modified Capabilities

- `sensor-routing`: the curated Prometheus subset expands to include instance availability and container restarts; a new requirement that critical routed alerts retain a delivery path Henk cannot suppress; a new requirement that routing configuration is convergent and semantics-preserving on re-application.
- `event-intake`: alert identity gains an optional per-rule scoping dimension, so one alert name firing for several targets yields several identities rather than one.

## Impact

- **`~/.claude-config/provisioning/grafana-henk-events.sh`** — rewritten (separate repo; precedent: `henk-pickup`). `grafana-henk-swap-retune.sh` retires into it. Retires tooling-backlog item #3's seed.
- **Grafana on vps** — 2 new rules, 6 template migrations, 1 policy-tree edit. Every mutation is **owner-run**: `ssh vps` sudo needs a password and admin credentials live under `/opt`.
- **This repo** — `henk/events/identity.py` gains opt-in identity scoping (TDD'd from the existing fixture); `config.yaml` may gain a raised `cap_per_24h` and a cooldown override; new payload fixtures; the `sensor-routing` and `event-intake` spec deltas. No changes to the dispatcher, toolset, or security posture.
- **`~/Documents/homelab-docs-site`** — `services/monitoring.md` corrections.
- **Unaffected**: Prometheus's 23 native rules stay in place (consolidation is a separate decision, D6); ntfy topics, grants and credentials unchanged; no new always-on service; no new credential.

**Verification is mostly deploy-verify.** The spec scenarios describe vps-side infrastructure, discharged by observed end-to-end fires. The identity-scoping change is genuine Henk code and goes to the test suite first, per the project's TDD rule.

## Gate result (task 1.2, 2026-08-06)

The coverage map was documentation-derived at first drafting and is now measured. **It holds** — 3 of 23 uncovered, and the `HighMemory` twin exists, so D1's "20 of 23 are duplicates" argument survives. Three corrections came out of it, recorded in `design.md`: the notification policy reads back `continue: null` where the script wrote `false` (the applier must normalise, or every dry-run reports false drift); a Grafana-only rule `AuditShipStale` exists with no native twin, so the two rule sets are **not** in a subset relationship; and none of the four existing rules carries a `severity` label today.
