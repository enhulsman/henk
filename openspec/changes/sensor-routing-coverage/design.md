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
                        │  └─ route=henk-events, continue:null │
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

### D2 — The rule template is the defect; fix it there

**The bug.** Grafana rules here are built `A` (instant query) → `B` (`reduce`, `last`) → `C` (`threshold`, `gt 0`), so a rule fires on its expression's **value**. That works for comparisons whose surviving series carry a magnitude, and fails for comparisons that return zero. Measured on the live server:

```
(vector(0) == 0) and (vector(5) > 0)   =>  0     ← the arm-4 shape: dead
up == 0                                =>  EMPTY ← filter form returns value 0, then no series survives `gt 0`
```

`HenkHealthEtl` arm 4 evaluates to 11 series all valued `0` against real data. It is live and dead. The native `HealthEtlMetricSilent` is **not** broken, because native Prometheus fires on *series returned* regardless of value — so **transcribing a native expression into a Grafana rule is not mechanical**, and that is an honest cost of D1's choice.

**The remedy: `reduce(count)`.** An instant query yields one datapoint per series, so `count` = 1 per returned series and the condition becomes "a series exists" — native semantics exactly. Consequences: arm 4 needs no rewrite; `up == 0` works as written and matches its native twin byte-for-byte (the `up == bool 0` workaround becomes unnecessary); `HenkDiskPressure`'s latent 0%-available blind spot disappears rather than becoming permanent documentation; and the per-expression sign-probe mechanism this design previously carried is deleted along with its drift surface.

**The migration cannot silence a working rule — a static proof, not just staging.** Every declared expression filters a non-negative quantity (measured minima: disk-% 31.22, swap-% 1.02, swap I/O 0, backup age 49903, container changes 0). Under `last` a series fires iff value > 0; under `count` iff the series exists. Since no value can be negative, **count fires everywhere last fired, plus the value-0 cases**. It is a strict superset. The real risk is therefore the inverse — rules that were silently dead start firing — which is the change's payoff, and 3.4b checks for it explicitly.

**Costs, recorded.** `{{ $value }}` in a summary annotation would render 1 rather than the metric value. Narrower than it first appears: Grafana's default body renders per-node values and **node A still carries the real metric value** (only B and C become 1), so the magnitude stays in the payload. What is foreclosed is putting it in the summary annotation. Accepted — M1 templates summaries on `{{ $labels.* }}`, which is what identifies an incident.

**Gated, with a recorded fallback.** Confidence is moderate-high, not high, so §2c probes it before commitment (positive, negative, and multi-series cases — see Migration). If `count` misbehaves, the fallback is the per-expression approach: reorder arm 4 to terminate on a positive-valued operand (`(max_over_time(...[7d]) > 0) and (increase(...[48h]) == 0) and on() (...)` — verified to return positive values on live data), use `up == bool 0`, and add sign probes. Recorded as rejected-unless-needed, not deleted.

**A template invariant.** `reduce(count)` is only correct with `instant: true`; on a range query `count` becomes the number of samples in the window, silently turning "matches now" into "matched at any point in the last 10 minutes". All six rules set `instant: true` today, but the template's correctness now depends on a field set independently of it, so it joins the applier's declaration invariants (D5).

### D3 — Dual delivery via a sibling route matching severity *and* route

A notification-policy child route **consumes** the alert: the parent's receiver fires only when no child matches. So `continue: true` alone does not add Discord — it only permits evaluation of *sibling* routes. Dual delivery needs a real sibling.

All six rules gain `severity`. The tree becomes:

```
default receiver: Discord-Grafana   group_by: [grafana_folder, alertname]
routes:
  1. [route = henk-events]                          → henk-events      continue: true   ← flipped
  2. [severity = critical AND route = henk-events]  → Discord-Grafana  continue: false  ← new
```

Keying route 2 on `severity` rather than an invented label makes the requirement **enforceable**: any future critical is covered automatically, and D5's invariants can police it. The second matcher (`route = henk-events`) is load-bearing and now empirically justified: the measured state shows the four DNS critical rules carry `severity=critical` with **no** route label, so severity-alone matching would pull all four out of the parent and into route 2. Same receiver, so it would look harmless — but a child route carries its own grouping and timing, and the DNS path's behaviour would silently become dependent on route 2's config.

Flipping route 1 to `continue: true` is behaviour-preserving for the existing four: they match route 1, deliver, continue to route 2, fail its severity matcher, and stop — the parent stays unreachable because a child matched. Load-bearing enough to verify rather than reason about (task 4.1).

**Severity assignment.** `HenkInstanceDown` critical; the other five warning. `HenkBackupFreshness` combines seven natives of which `ObsidianBackupVerifyFailed` is critical, so a combined-OR rule **cannot carry an honest per-arm severity**; `warning` is the conservative choice (it changes no current behaviour) and the imprecision is further evidence for the deferred split.

Alternative rejected: adding a Discord integration to the `henk-events` contact point. Dual-delivers with no policy edit, but applies to every alert on that contact point, giving Discord the warning traffic with no per-rule control.

### D4 — The applier plans, diffs, and refuses drift

Rewrite as a convergent applier over a declared-state table (uid → title, expr, `for`, labels, annotations, `noDataState`, condition pipeline), plus the contact point and policy tree.

- **`--dry-run` is the default.** GET live state, print a per-object `create`/`update`/`unchanged` plan with a full diff. Mutating requires `--apply`.
- **Drift is a hard stop.** If a live rule's expression, condition pipeline, threshold or `for` differs from the declaration, abort naming the rule and printing the diff. Escape hatch: `ACCEPT_DRIFT=<uid>[,<uid>]`.
- **PUT-by-uid**, not POST, so re-runs converge instead of 409-ing.
- **The policy tree is rebuilt from the declaration**, matched on route identity, replacing the `jq` prepend that duplicated the henk route every run.
- **Normalise before diffing.** Measured: the API reads back `continue: null` where the script wrote `false`, and `group_by: null` on the child route (meaning "inherit"). Without normalising `null ↔ false` and `null ↔ inherited`, every dry-run reports false drift on route 1 and the drift guard becomes noise the operator learns to bypass — which would defeat the control entirely.

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
- **[Template migration touches four working rules]** → bounded by D2's superset proof (it cannot silence a working rule) plus staging: 3.4a ships the new rules on the count template, 3.4b migrates the existing four as a separate apply with its own verification. A template flaw cannot take out working rules in the operation that adds new ones.
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
4. **2c — probe the template.** In a **scratch folder, unlabelled** so nothing publishes and D5's invariant is not tripped, observed via Grafana's **rule-state API** (not ntfy delivery, which would test template, labels, policy and contact point in one probe and make failures unattributable):
   - *positive*: `vector(0) == 0` fires under `count`, does **not** fire under `last`;
   - *negative*: `vector(1) == 0` (no series) does **not** fire under `count` and lands NoData→OK — this is the half that protects the four working rules;
   - *multi-series*: `node_memory_SwapFree_bytes * 0 == 0` yields **3 separate instances each counting 1**, not one instance counting 3 — a global-count bug would pass the positive probe silently while destroying the per-instance labels D9 depends on.
   Delete the scratch rules after. If any probe fails, take D2's recorded fallback.
5. **3.4a — new rules + tree.** Order: contact point → **policy tree** → rules. Tree-first is safe at every instant (route 2's matchers match nothing until the rule exists); rules-first would leave `HenkInstanceDown` live at `severity=critical` under the old `continue: false` tree — Henk-only delivery for a critical, exactly what the new SHALL forbids, and the `ERR` trap restores only the tree, not rules.
6. **3.4b — migrate the four existing rules to the count template**, separate apply, own dry-run, and an immediate post-apply check for **newly-firing** rules (the payoff signal). Arm 4 is not expected to fire on migration: `sum(increase(health_etl_rows_total[48h]))` is currently 0, so its `on()` guard holds it at zero series.
7. **Verify** (section 4), **docs** (section 6).
8. **Rollback.** PUT the dated backup tree; DELETE the two new rules by uid; re-apply the 2a declaration to restore the `last` template. The four existing rules are untouched until 3.4b, so rollback before that point cannot disturb them. No Henk-side state to unwind — worst case `events.enabled: false` reverts to reactive-only.

## Open Questions

- Whether `cap_per_24h` rises to 5 is pre-committed *conditionally* on 4.3c's measurement; the arrival timestamps it records make the debounce margin a number rather than an estimate.
- Whether `HenkContainerRestarting` warrants a per-pattern cooldown override is deferred to deploy observation, as with the D6 defaults in `henk-events`.
- Whether the applier should generalise into the fleet-wide Grafana applier (tooling-backlog #3). This change builds the specific one; generalising on a single caller would be speculative.
- What `AuditShipStale` is for and whether it belongs in the curated subset — surfaced by the gate, deliberately not answered here. It is a pre-existing Discord-routed rule unrelated to Henk's audit log.
