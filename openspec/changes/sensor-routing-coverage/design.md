# Design: sensor-routing-coverage

## Context

Henk consumes events from one ntfy topic (`henk-events`) fed by Gatus (9 of 18 endpoints) and Grafana (4 managed rules in folder `henk`, labelled `route=henk-events`). Prometheus on the vps carries 23 native alert rules and no Alertmanager. `intake-liveness-watchdog` established that intake is alive; the open question was whether the pipeline is *fed* correctly.

State measured 2026-08-06 — Prometheus via its HTTP API, Grafana via an owner-run credentialed dump (task 1.2):

```
                        ┌──────────────────────────────────────┐
   metrics ────────────▶│ Prometheus (vps) · 23 native rules   │
                        │ alerting: <absent>                   │──▶ ✗ nowhere
                        └──────────────┬───────────────────────┘
                                       │ datasource
                        ┌──────────────▼───────────────────────┐
                        │ Grafana (vps)                        │
                        │ default: Discord-Grafana             │
                        │ group_by: [grafana_folder, alertname]│
                        │  └─ route=henk-events, continue absent│
                        └───┬──────────────────────────────┬───┘
                            │ 4 rules (folder henk)        │ 9 rules
                     ┌──────▼───────┐              ┌───────▼────────┐
                     │ ntfy         │              │ Discord-Grafana│
                     │ henk-events  │              └────────────────┘
                     └──────┬───────┘
  Gatus (9 endpoints) ─────▶│
                     ┌──────▼───────┐
                     │ Henk (rp5)   │
                     └──────────────┘
```

**Redundancy map (measured).** 20 of 23 natives have Grafana twins: 7 DNS + `HighMemory` → Discord; 4 `HealthEtl*` + 7 backup/obsidian + `DiskSpaceLow` → `henk-events`. Three natives are orphans: `InstanceDown`, `ContainerRestarting`, `HighCPU`. Two Grafana rules are orphans in the other direction: `HenkSwapPressure` (there is no native swap rule) and `AuditShipStale` (watches `homelab_audit_last_flush_timestamp` on vps; unrelated to Henk's own audit log, which lives on rp5). **The two rule sets are therefore not in a subset relationship** — a fact D6 needs.

**Nothing has fired in 15 days.** `/api/v1/label/alertname/values` is empty; no native rule has entered even the pending state across full retention. Backtesting the uncovered three: `HighCPU` peak 67.8%/85; `HighMemory` peak 56.2%/90; `ContainerRestarting` max 0; `up == 0` for exactly one 15-second scrape across three targets simultaneously (`avg_over_time(up[15d])` = 0.999988) — a network blip, never near 120s. The gap is **latent, not live**.

Two constraints shape everything: every Grafana mutation is **owner-run**, and the provisioning script is both non-idempotent and stale relative to the deployment.

## Goals / Non-Goals

**Goals:**

- Eliminate the rule-template defect class, of which one instance (`HenkHealthEtl` arm 4) is live.
- Close the exporter-down-while-host-up blind spot, where `noDataState: OK` converts "blind" into "fine".
- Give crash-looping containers no alerting Gatus endpoint fronts a path to triage.
- Make provisioning convergent and drift-refusing, so re-running can never revert a deliberate retune.
- Keep a critical alert's delivery independent of Henk's cooldown, cap, and liveness — enforceably, not by convention.
- Make deployed routing state readable from a recorded artifact.

**Non-Goals:**

- Alertmanager or any new always-on service (D1).
- Deleting or migrating the 23 native rules — consolidation is deliberately deferred (D6).
- Splitting the combined-OR rules (owner decision, deferred).
- Routing `HighCPU` (D7).
- Any change to Henk's toolset, security posture, ntfy topics, grants, or ACL tag.
- Closing the ntfy paradox (vps down → no delivery by any path); unchanged and unfixable from inside the vps.

## Decisions

### D1 — Grafana-managed rules, not Alertmanager

Two new Grafana rules on the established pattern; Prometheus stays Alertmanager-less.

The Alertmanager case is not weak. Three arguments are real: (a) the combined-OR rules destroy triage detail — `HenkBackupFreshness` collapses 7 conditions into *"a backup pipeline is stale or erroring"*, where the natives carry `{{ $labels.direction }}` and `{{ $labels.root }}`; (b) file-based rules are greppable where Grafana's live behind a credentialed API; (c) wiring `alerting:` costs no restart — this Prometheus runs without `--web.enable-lifecycle`, but SIGHUP reloads it with no scrape gap.

Rejected because: the measured gap is three rules, two of value — that does not justify a new always-on service against a standing constraint. The transport gains nothing; Alertmanager's last hop to Henk would still be webhook → ntfy. Henk already recovers collapsed detail by querying Prometheus mid-triage (observed in `henk-events` deploy-verify (d)). The file-based-IaC argument is aspirational: `/opt/monitoring` **is not a git repository**, and `rules/` holds four `.bak` files — both halves are edited in place with backups-by-copy, so neither is the well-managed one. And 20 of 23 natives are duplicates, so routing them through Alertmanager delivers to Henk **twice** unless one side is deleted first, making it a consolidation migration in disguise. The measured redundancy map adds a further cost the original framing missed: consolidating onto Prometheus would require **porting** `HenkSwapPressure` and `AuditShipStale`, not merely deleting duplicates.

### D2 — The firing bar is the defect; move the bar, not the reducer

**The bug, confirmed by direct evaluation.** Grafana rules here are built `A` (instant query) → `B` (`reduce`, `last`) → `C` (`threshold`, `gt 0`), so a rule fires on its expression's **value**. That works for comparisons whose surviving series carry a magnitude, and fails for comparisons that return zero. Evaluated through Grafana's own alerting eval endpoint on the live server (2026-08-06, Grafana 12.3.1):

```
expr `vector(0) == 0`            ->  A=0  B=0  C=0     a matching condition that cannot fire
expr `(increase(health_etl_rows_total[48h]) == 0)
      and (max_over_time(...[7d]) > 0)`  ->  11 frames, all C=0     ← arm 4's DETECTION CLAUSE,
                                                                       real metrics. Arm 4's third
                                                                       conjunct (the `on()` guard)
                                                                       is omitted: it is currently
                                                                       false, which is also why the
                                                                       migration fires nothing.
```

`HenkHealthEtl` arm 4 — the silent-metric detector — has therefore been live, `health=ok`, and structurally unable to report since 2026-07-22. The native `HealthEtlMetricSilent` it was transcribed from is **not** broken: native Prometheus fires on *series returned*, value irrelevant. **The defect is the Grafana template, so transcribing a native expression into a Grafana rule is not mechanical** — an honest cost of D1's choice.

**Rejected remedy: `reduce(count)`.** The obvious fix is to make node B count series rather than pass values, restoring native semantics. It does not work here, and the reason matters: **the reduce node is a no-op on instant queries.** Measured — against `vector(7)`, all six reducers (`last`, `count`, `sum`, `min`, `max`, `mean`) returned `B=7`, where `count` alone should have returned 1; the same `count` reducer on a *range* query correctly returned `B=601`, a sample count. `settings.mode` (`""` and `dropNN`) changed nothing. All six rules use `instant: true`, so a reducer migration would have been a no-op that *looked* like a fix — provisioned cleanly, diffed cleanly, changed nothing. This was adopted as the design's remedy on reasoning and only survived to here because it was gated on a probe.

**Adopted remedy: move the threshold to `gt -1`.** Leave the reducer alone; change the bar. A matching series with value 0 clears `-1` and fires; when nothing matches, the query returns no rows, so no alert instance exists and `noDataState: OK` keeps the rule silent. Verified end to end:

```
POSITIVE  gt 0   ->  C=[0]                  the defect, restated as a control
POSITIVE  gt -1  ->  C=[1]                  value-0 case rescued
NEGATIVE  gt -1  ->  1 frame, zero rows     still silent with no series
MULTI     gt -1  ->  C=[1,1,1]              per-series, labels preserved
CONTROL   gt -1  ->  C=[1]                  ordinary positive conditions unaffected
REAL-ARM4 gt -1  ->  11 frames, all C=[1]   same clause, now firing (see caveat above)
```

One field per rule, inside a template this deployment already runs, with no reducer semantics to trust.

**Why it is safe: the same superset proof, and its price.** Every declared expression filters a **non-negative** quantity (measured minima: disk-% 31.22, swap-% 1.02, swap I/O 0, backup age 49903, container changes 0, `up == 0` → 0). Under `gt 0` a series fires iff its value > 0; under `gt -1` iff its value > -1. Since no value can be negative, `gt -1` fires everywhere `gt 0` fired **plus** exactly the value-0 cases. It is a strict superset: **the migration cannot silence a rule that works today.** The real risk is the inverse — a rule that was silently dead starts firing — which is the change's payoff, and 3.6 checks for it explicitly.

The price is that correctness now *depends* on that non-negativity premise rather than merely being consistent with it. An expression that can return a negative value while matching would fire under `gt 0` and go silent under `gt -1`. So it stops being a one-time observation and becomes a standing obligation on the declaration (D5 invariant (e)).

**A consequence that inverts an earlier decision.** `HenkInstanceDown` must use the **filter** form `up == 0`, never `up == bool 0`. Under the old `gt 0` bar the filter form returns value 0 and never fires — that was the original D2 finding, and the `bool` modifier was the proposed fix. Under `gt -1` the `bool` form is actively catastrophic: it returns a series for *every* target (1 for down, 0 for up), and every one of them clears `-1`, so every scrape target would alert permanently. The remedy change flips this from "preferred" to "required in the opposite direction".

**Template invariant retained.** All six rules keep `instant: true`. It is no longer load-bearing for the reducer, but a range query would make node B meaningful again and shift what the bar is applied to, so the declaration keeps asserting it (D5 invariant (c)).

### D3 — Dual delivery via a sibling route matching severity *and* route

A notification-policy child route **consumes** the alert: the parent's receiver fires only when no child matches. So `continue: true` alone does not add Discord — it only permits evaluation of *sibling* routes. Dual delivery needs a real sibling.

All six rules gain `severity`. The tree becomes:

```
default receiver: Discord-Grafana   group_by: [grafana_folder, alertname]
routes:
  1. [severity=critical AND route=henk-events]  → Discord-Grafana  continue: TRUE
  2. [identity_scope=~"instance|name"
      AND route=henk-events]                    → henk-events      continue: false
                                                  group_by: [alertname, instance, name]
  3. [route=henk-events]                        → henk-events      continue: false
```

Ordering is load-bearing, and this shape was arrived at by tracing rather than by first instinct.
An earlier draft put the Henk route first with `continue: true` and Discord second; that breaks
`HenkInstanceDown`, which would match the Henk route, continue to the critical route, and **stop
before ever reaching its per-instance grouping route** — silently losing the D9 scoping the whole
identity change depends on. Putting the dumb Discord path first is also fail-safe: a critical
reaches the non-agent receiver before anything cleverer can go wrong.

| alert | r1 | r2 | r3 | lands |
|---|---|---|---|---|
| `HenkInstanceDown` (critical, scope=instance) | ✓ Discord, continue | ✓ Henk, per-instance, stop | — | **both** |
| `HenkContainerRestarting` (warning, scope=name) | ✗ | ✓ Henk, per-instance, stop | — | Henk only |
| the four pre-existing (warning, no scope) | ✗ | ✗ | ✓ Henk, root grouping | Henk only, unchanged |
| a DNS critical (`severity=critical`, no route label) | ✗ | ✗ | ✗ | parent → Discord, unchanged |

Route 2 exists because without per-instance grouping the root `group_by: [grafana_folder,
alertname]` collapses several down targets into **one** notification, from which Henk derives one
identity — defeating D9 entirely. It is kept off the catch-all so the four pre-existing rules keep
root grouping: per-instance grouping there would take Disk/Swap from one notification to three,
each still collapsing to a single unscoped identity, which is the identity bug made worse.

Keying route 2 on `severity` rather than an invented label makes the requirement **enforceable**: any future critical is covered automatically, and D5's invariants can police it. The second matcher (`route = henk-events`) is load-bearing and now empirically justified: the measured state shows the four DNS critical rules carry `severity=critical` with **no** route label, so severity-alone matching would pull all four out of the parent and into route 2. Same receiver, so it would look harmless — but a child route carries its own grouping and timing, and the DNS path's behaviour would silently become dependent on route 2's config.

The four pre-existing rules are untouched by this: carrying neither `severity=critical` nor an
`identity_scope`, they fall past routes 1 and 2 to the catch-all and deliver exactly as before.
**Verified rather than reasoned (task 4.1, 2026-08-06):** a temporary `severity=warning` rule
labelled `route=henk-events` arrived on ntfy and did *not* appear in Discord, while an unlabelled
rule still reached Discord via the parent.

**Severity assignment.** `HenkInstanceDown` critical; the other five warning. `HenkBackupFreshness` combines seven natives of which `ObsidianBackupVerifyFailed` is critical, so a combined-OR rule **cannot carry an honest per-arm severity**; `warning` is the conservative choice (it changes no current behaviour) and the imprecision is further evidence for the deferred split.

Alternative rejected: adding a Discord integration to the `henk-events` contact point. Dual-delivers with no policy edit, but applies to every alert on that contact point, giving Discord the warning traffic with no per-rule control.

### D4 — The applier plans, diffs, and refuses drift

Rewrite as a convergent applier over a declared-state table (uid → title, expr, `for`, labels, annotations, `noDataState`, condition pipeline), plus the contact point and policy tree.

- **`--dry-run` is the default.** GET live state, print a per-object `create`/`update`/`unchanged` plan with a full diff. Mutating requires `--apply`.
- **Drift is a hard stop.** If a live rule's expression, condition pipeline, threshold or `for` differs from the declaration, abort naming the rule and printing the diff. Escape hatch: `ACCEPT_DRIFT=<uid>[,<uid>]`.
- **PUT-by-uid**, not POST, so re-runs converge instead of 409-ing.
- **The policy tree is rebuilt from the declaration**, matched on route identity, replacing the `jq` prepend that duplicated the henk route every run.
- **Normalise before diffing.** Four representational differences, each found by running the thing, never by inspection. The API **omits** `continue` entirely on an unchanged child route (`has("continue")` is false — normalise with `(.continue // false)`, which covers absent and null alike; testing for `null` specifically misses it); returns `group_by: null` meaning "inherit"; returns `queryType: ""` on every data node, which the old script never sent; and **sorts a route's `object_matchers` alphabetically by label**, so a declared `[severity, route]` reads back `[route, severity]`. Matchers are a conjunction, so sorting both sides before comparison is safe. `id` and `updated` are server-managed and excluded. Without all five normalisations every dry-run reports false drift, and an operator who learns to bypass the drift guard does not have one.

This, not credentialed reads, is what prevents a repeat of the swap incident. The retune was a **staleness** failure, not an access failure: the script carried a wrong expression and applied it without looking. A drift-refusing planner would have printed `HenkSwapPressure: expr changing from <pressure> → <fullness>` and stopped. Reading the API would not have.

### D5 — Declaration invariants make the guarantees machine-checkable

Checked at plan time, failing the plan rather than warning:

1. Every declared rule in folder `henk` carries a `severity` label.
2. Every declared rule with `severity=critical` is matched by a declared sibling route to a non-agent receiver.
3. Every declared rule sets `instant: true` (D2's template invariant).
4. No undeclared rule exists in folder `henk` — **reported, not pruned** (see D6 rationale below).

(1)+(2) turn "convention says label your criticals" into a failing plan, closing the hole where a future critical omits the label and silently becomes Henk-exclusive. (4) is report-only because pruning would delete a rule added deliberately outside the declaration, contradicting the applier's own thesis of refusing to overwrite the unexpected; precedent is real — the old script provisions `henk-prov-smoke` and relies on a human to delete it.

### D6 — A recorded state snapshot, not a scoped read token

After a successful apply, write the live rule set and policy tree to a credential-scrubbed snapshot committed beside the script. Deployed state becomes readable without credentials and without inferring it from the provisioning source.

Minting a read-only Grafana service account was considered and rejected: the existing admin credentials already serve every read this change needs, every mutation is owner-run anyway, and a snapshot solves the actual problem for future readers with no new long-lived credential. The snapshot must never enter Henk's `.env` or container — operator tooling only; Henk's aperture does not widen.

### D7 — Native rules stay; consolidation is a separate decision

Leaving all 23 preserves today's invariant (natives deliver nowhere; Grafana is the delivery brain) and adds no owner-run deletion risk for zero behavioural gain. Which system should be the single alerting brain is a homelab decision independent of Henk, and letting Henk's roadmap drive an infra migration is the tail wagging the dog. What this change owes it is a factual basis: the redundancy map above — including that the relationship is not a subset in either direction — goes into `monitoring.md`.

### D8 — `HighCPU` stays unrouted, on the record

15d peak 67.8% against 85%, warning-class, and a busy CPU on a homelab box is rarely the incident. Recorded so its absence does not later read as an oversight and get "fixed".

### D9 — Alert identity gains opt-in scoping

`identity.py:70` keys Grafana alerts as `grafana:{alertname}`. For `HenkInstanceDown` that is **one identity for seven scrape targets**: pi5's exporter dies at 09:00 and is triaged; vps's dies at 11:00 and is *silently suppressed* by the 6h cooldown.

Rules opt in by carrying `identity_scope: <labelname>`; `_derive_grafana` appends that label's value when present and behaves exactly as today when absent. `HenkInstanceDown` → `identity_scope: instance`; `HenkContainerRestarting` → `identity_scope: name`. Grafana propagates custom labels into the notification body (confirmed in `tests/fixtures/ntfy_events/henk-events-live.jsonl`, which carries `route` and `grafana_folder`). Fire and resolve still pair, because the discriminator comes from labels, not state.

**Opt-in, not automatic.** An earlier draft appended `instance` whenever present; measurement killed it — all four existing metrics carry `instance`, so that would silently re-key all four. `HenkBackupFreshness`'s meaningful discriminator is `direction`, not `instance`, and its arms carry differing label sets, so opting it in is a semantics change belonging with the deferred split. Chosen over a Henk-side config allowlist so the scoping lives beside the rule it describes, where the drift guard can police it.

**Grouping follows the same opt-in.** `group_by: [alertname, instance]` on route 1 would re-group the existing four (Backup 1→2, Disk/Swap 1→3), so route 1 keeps root grouping and the instance grouping goes on a child route carrying only the two new rules.

## Risks / Trade-offs

- **[The policy-tree PUT is the riskiest mutation — a malformed tree could break Discord for every non-henk rule, including the DNS criticals]** → back up the tree first (task 1.3, dated file, not the reboot-volatile `/tmp` copy); `--dry-run` diff reviewed before `--apply`; deploy-verify checks a *non-henk* alert still reaches Discord, not only that the new ones work; rollback is a PUT of the backup.
- **[Template migration touches four working rules]** → bounded by D2's superset proof (`gt -1` fires everywhere `gt 0` fired, so it cannot silence a working rule) plus staging: step 5 ships the new rules on the `-1` bar while the existing four keep `gt 0`; step 6 moves the four as a separate apply with its own dry-run and verification. A template flaw cannot take out working rules in the operation that adds new ones.
- **[Flipping route 1 to `continue: true` could leak the existing four into Discord if Grafana's semantics differ from the reasoned model]** → verified empirically at deploy (4.1) with a test-fire asserting Discord receives nothing.
- **[Host-down produces duplicate signal against a daily cap]** → rp5 dying takes 3 exporters down *and* pages Gatus tier-1. Arrival times: Gatus ~T+60s (2×30s); `HenkInstanceDown` T+120s (`for`) + eval lag + `group_wait` ≈ T+150–210s. The gap is **90–150s against a 120s debounce** — marginal, so expect **two** conversations per host outage, not one, against `cap_per_24h: 3`. Pre-committed resolution if 4.3c confirms the split: **accept two-per-outage and raise the cap 3 → 5**, applied by editing rp5's locally-modified `config.yaml` **in place** (never `git checkout` — see Warnings in the change's operational notes). Widening debounce is rejected: it delays *every* triage to tidy a rare case that is genuinely two observations two minutes apart.
- **[`ContainerRestarting` coverage is narrower than assumed]** → measured: `henk-henk-1` was recreated 8× in 15d and `changes()` never reached 1, because a recreate mints a new container ID and a new cadvisor series. So `compose up -d --build` **cannot** trip it; the rule covers in-place restart loops with a stable container ID — which still includes the case it was chosen for, a crash-looping Henk under its restart policy. Deploy-verify must use `docker restart` ×2, not a rebuild, or it verifies nothing.
- **[Henk cannot report its own death]** → `HenkContainerRestarting` is Henk-only by decision, so a crash-looping Henk publishes an event only Henk can consume. Not fully lost — ntfy retains 72h and intake replays from the resume cursor — but a *permanently* dead Henk is unobserved, since Henk has no Gatus endpoint. Out of scope; belongs with change D or a Gatus endpoint for Henk.
- **[The new rules are unfalsifiable at deploy — nothing fires naturally]** → synthetic verification only. Stop a node exporter on **pi2 or pi5**; never `node-exporter-vps`, which carries the `health_etl_*` and `homelab_backup_*` textfile metrics — stopping it drives `HenkHealthEtl`/`HenkBackupFreshness` to noData → `noDataState: OK` → "healthy", injecting the change's headline failure mode into its own verification.
- **[Two-repo change]** → precedent exists (`henk-pickup`); tasks name the repo per item, and the change does not archive until both land.
- **[Owner-run bottleneck]** → every Grafana mutation needs the owner at a terminal. Mitigated by `--dry-run` producing a reviewable plan, and by task 1.1 capturing a live-state fixture so the planner is developable offline.

## Migration Plan

1. **Capture and gate.** Dump live Grafana state (done, 2026-08-06); reconcile against the script and docs; back up the policy tree to a dated file. Section 2 does not start until the coverage map is confirmed — **done: it holds**.
2. **2a — prove the planner.** Declaration reproduces live state exactly, including the *already-deployed* swap retune and today's `reduce(last)` pipeline. Dry-run must be `unchanged` for all four; any `update` means the declaration is wrong, not the deployment.
3. **2b — declared corrections.** Severity labels, templated summaries; retire the separate swap-retune *script* (a repo action, not a state change). Dry-run shows exactly these updates.
4. **Probe the template — DONE 2026-08-06.** Evaluated directly through Grafana's alerting eval endpoint, which creates nothing and notifies nobody, rather than the originally-planned scratch rules. Results in D2: `reduce(count)` rejected, `gt -1` adopted. The originally-planned approach, retained for reference — in a **scratch folder, unlabelled** so nothing publishes and D5's invariant is not tripped, observed via Grafana's **rule-state API** (not ntfy delivery, which would test template, labels, policy and contact point in one probe and make failures unattributable):
   - *positive*: `vector(0) == 0` fires under `count`, does **not** fire under `last`;
   - *negative*: `vector(1) == 0` (no series) does **not** fire under `count` and lands NoData→OK — this is the half that protects the four working rules;
   - *multi-series*: `node_memory_SwapFree_bytes * 0 == 0` yields **3 separate instances each counting 1**, not one instance counting 3 — a global-count bug would pass the positive probe silently while destroying the per-instance labels D9 depends on.
   Delete the scratch rules after. If any probe fails, take D2's recorded fallback.
5. **New rules + tree** (`STAGE=target LEGACY_THRESHOLD=0`). Order: contact point → **policy tree** → rules. Tree-first is safe at every instant (route 2's matchers match nothing until the rule exists); rules-first would leave `HenkInstanceDown` live at `severity=critical` under the old `continue: false` tree — Henk-only delivery for a critical, exactly what the new SHALL forbids, and the `ERR` trap restores only the tree, not rules.
6. **Move the four existing rules to the `gt -1` bar** (`STAGE=target`), separate apply, own dry-run, and an immediate post-apply check for **newly-firing** rules — that signal is the change's payoff. The drift guard will refuse this until the four uids are named in `ACCEPT_DRIFT`, which is correct: a threshold change is exactly the shape of the swap-retune revert, and it should require saying so out loud. Arm 4 is not expected to fire on migration: `sum(increase(health_etl_rows_total[48h]))` is currently 0, so its `on()` guard holds it at zero series.
7. **Verify** (section 4), **docs** (section 6).
8. **Rollback.** PUT the dated backup tree; DELETE the two new rules by uid; re-apply the baseline declaration to restore the `gt 0` bar. The four existing rules are untouched until step 6, so rollback before that point cannot disturb them. No Henk-side state to unwind — worst case `events.enabled: false` reverts to reactive-only.

## Open Questions

- Whether `cap_per_24h` rises to 5 is pre-committed *conditionally* on 4.3c's measurement; the arrival timestamps it records make the debounce margin a number rather than an estimate.
- Whether `HenkContainerRestarting` warrants a per-pattern cooldown override is deferred to deploy observation, as with the D6 defaults in `henk-events`.
- Whether the applier should generalise into the fleet-wide Grafana applier (tooling-backlog #3). This change builds the specific one; generalising on a single caller would be speculative.
- What `AuditShipStale` is for and whether it belongs in the curated subset — surfaced by the gate, deliberately not answered here. It is a pre-existing Discord-routed rule unrelated to Henk's audit log.
