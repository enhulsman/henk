# Tasks: sensor-routing-coverage

> **Repo split.** Tasks are tagged `[claude-config]`, `[docs-site]`, or `[henk]`. The applier lives
> in `~/.claude-config/provisioning/`; docs corrections in `~/Documents/homelab-docs-site/`; spec,
> code and config changes here. Precedent: `henk-pickup` (`henk-events` task 4.1). Do not archive
> until all three have landed.
>
> **Owner-run constraint.** Every Grafana read *and* write needs credentials under `/opt` on the
> vps behind an interactive sudo password. Steps marked **[owner]** are executed by the owner from
> exact prepared commands. Task 1.1's live-state fixture exists so the applier can be developed and
> unit-tested offline between owner sessions.

## 1. Capture and gate

- [x] 1.1 **[owner]** Dump live Grafana state — all alert rules (uid, title, expr, `for`, labels,
      annotations, `noDataState`, condition pipeline, `instant`), the full notification policy tree,
      and contact points. **DONE 2026-08-06T15:50:13Z**, saved on the vps under
      `/root/henk-grafana-state-<stamp>/`
- [x] 1.2 **GATE — reconcile the dump against the script and the docs. DONE 2026-08-06; the
      coverage map HOLDS** (3 of 23 natives uncovered; the `HighMemory` twin exists, so D1's
      duplicate argument survives). Findings that change the work:
      - `HenkSwapPressure` live carries the **pressure retune**; the script is stale. Trap confirmed
      - All four henk rules run `reduce(last) → threshold(gt [0])`, `instant: true` — D2 confirmed
        against deployed state, and `HenkHealthEtl` arm 4 is dead in production
      - The policy API reads back `continue: null` where the script wrote `false`, and
        `group_by: null` on the child route → **the applier must normalise** (task 2.6)
      - **None** of the four rules carries a `severity` label → C3 must add it to all four (2b)
      - New rule discovered: `AuditShipStale` (Grafana-only, no native twin, watches
        `homelab_audit_last_flush_timestamp` on vps; unrelated to Henk's audit log). With
        `HenkSwapPressure` it means the two rule sets are **not** a subset relationship either way
      - Docs place "High memory usage" in the DNS Performance group; it is in another folder and
        carries no labels at all
- [ ] 1.3 **[owner]** Back up the policy tree to a **dated** file on the vps (not the reboot-volatile
      `/tmp/grafana-policies.backup.json`, which is 2026-07-22 vintage). This is the rollback point
- [ ] 1.4 `[claude-config]` Commit a credential-scrubbed copy of 1.1's dump as the applier's
      offline development fixture — no tokens, no admin user, no tailnet IPs (the contact-point URL
      contains one)

## 2. Applier rewrite — make it convergent before adding anything to it

- [ ] 2.1 `[claude-config]` Restructure `grafana-henk-events.sh` around a **declared-state table**
      (uid → title, expr, `for`, labels, annotations, `noDataState`, condition pipeline), plus the
      contact point and policy tree, reproducing 1.1's live state **exactly** — including the
      already-deployed swap **pressure** retune and today's `reduce(last)` pipeline. At this point
      the declaration describes the current deployment with zero changes
- [ ] 2.2 `[claude-config]` Implement the planner: GET live state, compare per object, print a
      `create`/`update`/`unchanged` plan with a full diff per change. `--dry-run` is the **default**;
      mutation requires an explicit `--apply`
- [ ] 2.3 `[claude-config]` Implement the drift guard: abort naming the rule and printing the diff
      when a live rule's expr, condition pipeline, threshold or `for` differs from the declaration.
      Escape hatch `ACCEPT_DRIFT=<uid>[,<uid>]`. **Verify it works** by pointing a scratch
      declaration at the stale `>90%` fullness expression and confirming it refuses — this is the
      control that would have caught the swap revert
- [ ] 2.4 `[claude-config]` Convert writes from POST to **PUT-by-uid** so re-runs converge instead
      of 409-ing under `set -euo pipefail`. Preserve `X-Disable-Provenance: true` on every
      provisioning write — without it, PUTs to provisioned rules can 403
- [ ] 2.5 `[claude-config]` Rebuild the policy tree from the declaration matched on route identity,
      replacing the `jq` prepend that duplicated the henk route every run
- [ ] 2.6 `[claude-config]` **Normalise before diffing**: `continue: null ↔ false`, `group_by: null ↔
      inherited`, per 1.2's measurement. Without this every dry-run reports false drift on route 1
      and the operator learns to bypass the guard, defeating it entirely
- [ ] 2.7 `[claude-config]` Implement declaration invariants, failing the plan rather than warning:
      (a) every rule in folder `henk` carries `severity`; (b) every `severity=critical` rule is
      matched by a declared sibling route to a non-agent receiver; (c) every rule sets
      `instant: true` (the count template is wrong on a range query — it would silently become
      "matched at any point in the window"); (d) no undeclared rule in folder `henk` — **reported
      and blocking, never pruned** (precedent: the old script's `henk-prov-smoke`)
- [ ] 2.8 `[claude-config]` Post-apply state snapshot: live rules + policy tree to a committed file,
      credential-scrubbed
- [ ] 2.9 `[claude-config]` Correct the script header (it documents its own non-idempotency, obsolete
      once 2.2–2.6 land); `shellcheck` clean
- [ ] 2.10 **[owner]** `--dry-run` against live. **Expected: `unchanged` for every object.** Any
      `update` here means the declaration is wrong, not the deployment — fix the declaration

### 2b — declared corrections

- [ ] 2.11 `[claude-config]` Add `severity` labels to all four existing rules (all `warning`).
      Note in the declaration why `HenkBackupFreshness` is `warning` despite combining a critical
      native (`ObsidianBackupVerifyFailed`) — a combined-OR rule cannot carry an honest per-arm
      severity; evidence for the deferred split
- [ ] 2.12 `[claude-config]` Template the summary annotations on `{{ $labels.* }}`
- [ ] 2.13 `[claude-config]` Retire `grafana-henk-swap-retune.sh` into the applier, leaving a pointer
      note in its place. **This is a repo action, not a state change** — the retune is already
      deployed and therefore belongs in 2.1's declaration
- [ ] 2.14 **[owner]** `--dry-run`: shows exactly these updates and nothing else, then `--apply`

### 2c — probe the count template before committing to it

- [ ] 2.15 **[owner]** Provision three probe rules in a **scratch folder, unlabelled** (so nothing
      publishes and 2.7d is not tripped), observed via Grafana's **rule-state API** — not ntfy
      delivery, which would test template, labels, policy tree and contact point at once and make a
      failure unattributable:
      - **positive**: `vector(0) == 0` fires under `reduce(count)`, does **not** fire under
        `reduce(last)`. Proves the bug and the fix together
      - **negative**: `vector(1) == 0` (returns no series) does **not** fire under `count` and lands
        NoData → OK. **This is the half that protects the four working rules**
      - **multi-series**: `node_memory_SwapFree_bytes * 0 == 0` yields **3 separate alert instances
        each counting 1**, not one instance counting 3. A global-count bug would pass the positive
        probe silently while destroying the per-instance labels task 5.1 depends on
      Delete the scratch rules after. **If any probe fails, take design D2's recorded fallback**
      (arm-4 reorder + `up == bool 0` + sign probes) and revise 3.1 accordingly

## 3. New rules, dual delivery, template migration

- [ ] 3.1 `[claude-config]` Declare `HenkInstanceDown`: uid `henk-instancedown`, expr **`up == 0`**
      (plain — the count template makes `bool` unnecessary and keeps it byte-identical to its native
      twin), `for: 2m`, `noDataState: OK`, `reduce(count)`, `instant: true`, labels
      `route=henk-events`, `severity=critical`, `identity_scope=instance`; summary templated on
      `{{ $labels.instance }}`
- [ ] 3.2 `[claude-config]` Declare `HenkContainerRestarting`: uid `henk-container`, expr
      `changes(container_start_time_seconds{name!=""}[15m]) > 1`, `for: 5m`, `noDataState: OK`,
      `reduce(count)`, `instant: true`, labels `route=henk-events`, `severity=warning`,
      `identity_scope=name`; summary templated on `{{ $labels.name }}`
- [ ] 3.3 `[claude-config]` Declare the policy tree: route 1 `[route=henk-events]` → `henk-events`,
      **`continue: true`**, root grouping retained; route 2 `[severity=critical AND
      route=henk-events]` → `Discord-Grafana`, `continue: false`, **no** group_by override; plus a
      child route carrying instance grouping for the two new rules only. Route 2's second matcher is
      load-bearing — 1.2 measured that the four DNS criticals carry `severity=critical` with no
      route label, so severity-alone matching would pull them out of the parent
- [ ] 3.4 `[claude-config]` Implement apply ordering: **contact point → policy tree → rules**, with
      an `ERR` trap restoring the policy backup. Tree-first is safe at every instant; rules-first
      would leave a critical rule live under the old `continue: false` tree with no non-agent path
- [ ] 3.5 **[owner]** Apply 3.4a — new rules + tree. Dry-run (expect `create` ×2, policy `update`,
      `unchanged` ×4), review, `--apply`. Commit the snapshot
- [ ] 3.6 **[owner]** Apply 3.4b — migrate the four existing rules to `reduce(count)` as a
      **separate** apply with its own dry-run, so a template flaw cannot take out working rules in
      the operation that adds new ones. Immediately check for **newly-firing** rules: that signal is
      the change's payoff. Arm 4 is not expected to fire on migration —
      `sum(increase(health_etl_rows_total[48h]))` is currently 0, so its `on()` guard holds

## 4. Deploy verification — each item is a distinct failure mode; do not collapse them

- [ ] 4.1 **[owner]** Test-fire an **existing** henk rule → arrives on `henk-events` and **not** in
      Discord. The `continue: true` regression check. Method: a temporary `vector(1)` rule in folder
      `henk` labelled `route=henk-events`, deleted after (precedent: `henk-prov-smoke`). Do **not**
      use `POST /api/alertmanager/grafana/config/api/v1/receivers/test` — it tests the contact point
      directly and **bypasses the notification policy**, so it cannot discharge this check or 4.2
- [ ] 4.2 **[owner]** Test-fire an **unlabelled** temporary `vector(1)` rule → still reaches Discord.
      Proves the policy edit did not break delivery for everything outside folder `henk`
- [ ] 4.3 **[owner]** Stop a node exporter for >2m — **pi2 or pi5 only**. Never
      `node-exporter-vps`: it carries the `health_etl_*` and `homelab_backup_*` textfile metrics, so
      stopping it drives `HenkHealthEtl`/`HenkBackupFreshness` to noData → `noDataState: OK` →
      "healthy", injecting this change's headline failure mode into its own verification.
      Confirm `HenkInstanceDown` reaches **both** `henk-events` and Discord, that Henk triages it
      with a full arc, and that restarting the exporter resolves it
- [ ] 4.3b **[owner]** Two-target case: two exporters down concurrently → **two distinct identities**,
      collapsed by debounce into **one** conversation. Verifies D9's scoping end-to-end; 4.3 alone
      leaves the multi-target path entirely unexercised
- [ ] 4.3c **[owner]** Host-down case: record **actual arrival timestamps** of the Gatus tier-1 alert
      and of `HenkInstanceDown`, and the resulting conversation count. Expected two conversations
      (~90–150s gap against a 120s debounce). **If confirmed, raise `cap_per_24h` 3 → 5** per design
      — applied by editing rp5's locally-modified `config.yaml` **in place**, never via
      `git checkout`
- [ ] 4.4 **[owner]** Confirm `HenkSwapPressure`'s live expression still reads as the pressure retune
      after every apply, not fullness
- [ ] 4.5 **[owner]** Re-run `--apply` → plan fully `unchanged`; policy tree holds exactly the
      declared routes (the old duplicate-prepend bug, now under test); exactly six rules in folder
      `henk` (2.7d)
- [ ] 4.6 **[owner]** Provoke `HenkContainerRestarting` with **`docker restart` ×2 inside 15m** on a
      low-stakes container — **not** `compose up -d --build`, which mints a new container ID and a
      new cadvisor series, so `changes()` restarts at 0 and the rule cannot fire. Confirm the event
      reaches `henk-events`, Discord receives nothing, Henk triages. Note the derived identity
- [ ] 4.7 Record whether 4.6's identity warrants a per-pattern cooldown override, and at what value

## 5. Henk-side follow-through

- [ ] 5.1 `[henk]` **TDD**: extend `_derive_grafana` to honour an `identity_scope: <labelname>` label
      — append that label's value when present, behave exactly as today when absent. Tests from
      `tests/fixtures/ntfy_events/henk-events-live.jsonl` asserting: two simultaneously-down targets
      yield **two distinct keys**; a fire and its resolve for one target share a key; an event with
      no `identity_scope` derives an unchanged key; and the identity is a stable parsed one, never
      the normalized-title fallback (a fallback identity makes every fire look new, cooldown never
      suppresses, and nothing errors — the trap `henk-events` deploy-verify (b) was written to catch)
- [ ] 5.2 `[henk]` If 4.3c or 4.7 say so: `cap_per_24h` and/or a cooldown override in `config.yaml`,
      with tests from the existing per-pattern override coverage. rp5's `config.yaml` is locally
      modified and must stay that way — edit in place, never `git checkout`. Note the existing
      `pattern: "swap"` is a case-insensitive regex over the key, and scoped keys are now longer
- [ ] 5.3 `[henk]` Append a real payload line per new rule to
      `tests/fixtures/ntfy_events/henk-events-live.jsonl` and extend that directory's README table

## 6. Documentation and close-out

- [ ] 6.1 `[docs-site]` Fix `services/monitoring.md:602` — it calls `grafana-henk-events.sh` "the
      idempotent provisioning script", false when written and only true once section 2 lands.
      Reword to describe the plan / apply / drift-refusal behaviour
- [ ] 6.2 `[docs-site]` Expand the henk rule table 4 → 6; document the policy tree including the
      dual-delivery route and which severities dual-deliver; note `grafana-henk-swap-retune.sh` is
      retired into the applier
- [ ] 6.3 `[docs-site]` Add the **redundancy map**: 20 of 23 natives have Grafana twins (7 DNS +
      `HighMemory` → Discord; 4 `HealthEtl*` + 7 backup + `DiskSpaceLow` → henk-events); 3 natives
      orphan (`InstanceDown`, `ContainerRestarting`, `HighCPU`); **2 Grafana rules orphan the other
      way** (`HenkSwapPressure`, `AuditShipStale`). The sets are not a subset relationship in either
      direction, so consolidating onto Prometheus would require *porting*, not just deleting — the
      factual basis the deferred D7 decision needs, and which this change had to derive from scratch
- [ ] 6.4 `[docs-site]` Record the template defect and its fix: Grafana rules fire on the expression's
      **value**, native Prometheus on **series returned**, so transcription is not mechanical;
      `reduce(count)` restores native semantics. Name `HenkHealthEtl` arm 4 as the live instance this
      change found and fixed. Record that `HighCPU` stays unrouted by decision (D8)
- [ ] 6.5 `[docs-site]` Correct the "High memory usage" rule's location (it is not in the DNS
      Performance group) and note that it carries no labels. Note `AuditShipStale`'s existence and
      that its purpose is unidentified — flagged, not answered
- [ ] 6.6 `[docs-site]` Note that Henk has no Gatus endpoint, so a permanently dead Henk is
      unobserved; `HenkContainerRestarting` covers a crash-loop only because ntfy retains the event
      for replay. Flags it for change D or a future Gatus endpoint
- [ ] 6.7 `[henk]` Correct `openspec/changes/archive/2026-08-02-henk-events/design.md:9` — it says 22
      Prometheus rules; the count is 23, which its own `tasks.md:53` already had right
- [ ] 6.8 `[henk]` `/opsx:sync` + `/opsx:archive`
