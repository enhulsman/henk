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
      - The policy API **omits** `continue` entirely on the child route (`has("continue")` is
        false) where the script wrote `false`; likewise no `group_by` → **the applier must
        normalise** (task 2.6)
      - **None** of the four rules carries a `severity` label → C3 must add it to all four (2b)
      - New rule discovered: `AuditShipStale` (Grafana-only, no native twin, watches
        `homelab_audit_last_flush_timestamp` on vps; unrelated to Henk's audit log). With
        `HenkSwapPressure` it means the two rule sets are **not** a subset relationship either way
      - Docs place "High memory usage" in the DNS Performance group; it is in another folder and
        carries no labels at all
- [x] 1.3 **[owner]** Back up the policy tree to a **dated** file on the vps (not the reboot-volatile
      `/tmp/grafana-policies.backup.json`, which is 2026-07-22 vintage). This is the rollback point
- [x] 1.4 `[claude-config]` Commit a credential-scrubbed copy of 1.1's dump as the applier's
      offline development fixture — no tokens, no admin user, no tailnet IPs (the contact-point URL
      contains one)

## 2. Applier rewrite — make it convergent before adding anything to it

- [x] 2.1 `[claude-config]` Restructure `grafana-henk-events.sh` around a **declared-state table**
      (uid → title, expr, `for`, labels, annotations, `noDataState`, condition pipeline), plus the
      contact point and policy tree, reproducing 1.1's live state **exactly** — including the
      already-deployed swap **pressure** retune and today's `reduce(last)` pipeline. At this point
      the declaration describes the current deployment with zero changes
- [x] 2.2 `[claude-config]` Implement the planner: GET live state, compare per object, print a
      `create`/`update`/`unchanged` plan with a full diff per change. `--dry-run` is the **default**;
      mutation requires an explicit `--apply`
- [x] 2.3 `[claude-config]` Implement the drift guard: abort naming the rule and printing the diff
      when a live rule's expr, condition pipeline, threshold or `for` differs from the declaration.
      Escape hatch `ACCEPT_DRIFT=<uid>[,<uid>]`. **Verify it works** by pointing a scratch
      declaration at the stale `>90%` fullness expression and confirming it refuses — this is the
      control that would have caught the swap revert
- [x] 2.4 `[claude-config]` Convert writes from POST to **PUT-by-uid** so re-runs converge instead
      of 409-ing under `set -euo pipefail`. Preserve `X-Disable-Provenance: true` on every
      provisioning write — without it, PUTs to provisioned rules can 403
- [x] 2.5 `[claude-config]` Rebuild the policy tree from the declaration matched on route identity,
      replacing the `jq` prepend that duplicated the henk route every run
- [x] 2.6 `[claude-config]` **Normalise before diffing**, per the fixture measured in 1.4. Two
      distinct classes, both of which would otherwise make every dry-run report drift on unchanged
      objects — and an operator who learns to bypass the drift guard has no drift guard:
      - **Absent-vs-false**: the child route **omits** `continue` entirely (`has("continue")` is
        false) where the old script wrote `false`. Normalise with `(.continue // false)`, which
        handles absent and null alike; do not test for `null` specifically
      - **Server-managed fields**: `id` (server-assigned) and `updated` (rewritten on every write)
        must be excluded from comparison
      - **Fields the API returns that the old script never sent**: each data node carries
        `queryType: ""`. Omitting it made all four rules read as `update`. Found by the offline
        test in 2.16, not by inspection — an earlier draft of this task asserted "the API injects
        no fields of its own", which was simply wrong. Comparing the full remainder structurally
        is deliberate: an unexpected live field shows up loudly rather than being normalised away
      - Shared rule defaults confirmed constant across all four: `orgID: 1`, `isPaused: false`,
        `keep_firing_for: "0s"`, `notification_settings: null`, `record: null`, `condition: "C"`,
        `execErrState: "Error"`, `maxDataPoints: 43200`, `intervalMs: 1000`, node A
        `relativeTimeRange {from:600,to:0}`, nodes B/C `{from:0,to:0}`
- [x] 2.7 `[claude-config]` Implement declaration invariants, failing the plan rather than warning:
      (a) every rule in folder `henk` carries `severity`; (b) every `severity=critical` rule is
      matched by a declared sibling route to a non-agent receiver; (c) every rule sets
      `instant: true` (a range query makes the reduce node meaningful again and shifts what the
      firing bar applies to); (e) the two new rules carry the `-1` bar and the four legacy rules
      match the requested stage; (d) no undeclared rule in folder `henk` — **reported
      and blocking, never pruned** (precedent: the old script's `henk-prov-smoke`)
- [x] 2.8 `[claude-config]` Post-apply state snapshot: live rules + policy tree to a committed file,
      credential-scrubbed
- [x] 2.9 `[claude-config]` Script header corrected (it documented its own non-idempotency, obsolete
      once 2.2–2.6 landed). **shellcheck clean** (0.11.0) across `grafana-henk-events.sh`,
      `test-offline.sh` and the retired `grafana-henk-swap-retune.sh`; the two suppressions are
      justified in place (`source=/dev/null` for the configurable env path, `SC2016` because
      `{{ $labels.x }}` is a Go template Grafana renders — shell expansion there would be the bug)
- [x] 2.16 `[claude-config]` **Offline test harness** — `mock_grafana.py` serves the 1.4 fixture
      over HTTP; `test-offline.sh` exercises the planner against it with no credentials, no vps and
      no network. Six cases, 26 assertions, all passing: baseline diffs to zero; target creates the
      two new rules without touching the reducer; the template migration trips the drift guard;
      `ACCEPT_DRIFT` releases it; an undeclared rule is reported and never pruned. This is what
      caught the `queryType` omission — without it, 2.10's "expect all unchanged" gate would have
      failed in front of the owner with four spurious updates
- [x] 2.10 **[owner]** DONE 2026-08-06 — all objects `unchanged`, 0 changes planned. `--dry-run` against live. **Expected: `unchanged` for every object.** Any
      `update` here means the declaration is wrong, not the deployment — fix the declaration

### 2b — declared corrections

- [x] 2.11 `[claude-config]` Add `severity` labels to all four existing rules (all `warning`).
      Note in the declaration why `HenkBackupFreshness` is `warning` despite combining a critical
      native (`ObsidianBackupVerifyFailed`) — a combined-OR rule cannot carry an honest per-arm
      severity; evidence for the deferred split
- [x] 2.12 `[claude-config]` Template the summary annotations on `{{ $labels.* }}` **for the two
      new rules only** (3.1/3.2), which is what the spec scenarios require ("an event identifying
      the unreachable target"). The four pre-existing summaries are deliberately left static: they
      keep root grouping and unscoped identity, so a per-instance summary would add little, and
      leaving them alone keeps the target diff to exactly one field (severity) per existing rule
- [x] 2.13 `[claude-config]` Retire `grafana-henk-swap-retune.sh` into the applier, leaving a pointer
      note in its place. **This is a repo action, not a state change** — the retune is already
      deployed and therefore belongs in 2.1's declaration
- [x] 2.14 **[owner]** DONE 2026-08-06 — merged into 3.5 (severity labels must land with the tree, since route 1 matches on severity). `--dry-run`: shows exactly these updates and nothing else, then `--apply`

### 2c — probe the firing-bar remedy before committing to it

- [x] 2.15 **[owner]** **DONE 2026-08-06 — probe run, remedy CHANGED.** Executed via Grafana's
      alerting eval endpoint (`POST /api/v1/eval`, Grafana 12.3.1) rather than provisioned scratch
      rules: it returns the computed value at every pipeline node, creates nothing, notifies nobody
      and needs no cleanup. Three findings:
      - **The defect is confirmed on real data.** The live `HenkHealthEtl` arm-4 expression returns
        11 frames, all `C=0` — a matching condition that cannot fire. Not an inference any more
      - **`gt -1` is REJECTED: the reduce node is a no-op on instant queries.** All six
        reducers returned `B=7` for `vector(7)` where `count` alone should return 1; the same
        `count` on a *range* query correctly returned `B=601`. `settings.mode` changed nothing.
        All six rules use `instant: true`, so the reducer migration would have been a no-op that
        looked like a fix — clean provision, clean diff, zero behaviour change
      - **`threshold gt -1` is ADOPTED and verified**: value-0 case fires (`C=[1]`), no-series case
        stays silent (zero rows → NoData → OK), multi-series preserved (`C=[1,1,1]`), ordinary
        conditions unaffected, and the real arm-4 expression now fires on real data
      - **Consequence that inverts an earlier decision:** `HenkInstanceDown` must use the filter
        form `up == 0`, never `up == bool 0`. Under `gt -1` the bool form returns a series for every
        target (1 down / 0 up) and all of them clear the bar — every target would alert permanently
- [x] 2.18 `[claude-config]` **Fourth representational difference, found on the first live apply:**
      Grafana **sorts a route's `object_matchers` alphabetically by label** on write — declared
      `[severity, route]` reads back as `[route, severity]`. Matchers are a conjunction, so order
      carries no meaning, but the positional comparison reported drift on every run. Fixed by
      sorting both sides before diffing, with regression test T6. Also: dry-runs no longer write a
      rollback backup (they mutate nothing, so those were pure litter in `/root`).
      Running tally of API quirks the declaration must absorb: absent `continue`, `group_by: null`
      meaning inherit, returned `queryType`, sorted `object_matchers`, server-managed
      `id`/`updated`. **`design.md` D4 was corrected to match** — it had described the first as
      `continue: null`, contradicting this task and inviting a `null`-specific test that misses it
- [x] 2.19 `[claude-config]` T5 now asserts the applier **refuses `--apply`** while an undeclared
      rule exists, not merely that it reports one. The refusal is the actual guarantee; the report
      was the only thing under test
- [x] 2.17 `[claude-config]` Applier switched from a reducer knob to a **threshold knob**
      (`LEGACY_THRESHOLD=0|-1`); new rules always `-1`. Invariant (e) added: the two new rules must
      carry `-1`, the four legacy rules must match the requested stage. Offline suite updated and
      re-run — 26 assertions across 6 cases, all passing. shellcheck clean

## 3. New rules, dual delivery, template migration

- [x] 3.1 `[claude-config]` Declare `HenkInstanceDown`: uid `henk-instancedown`, expr **`up == 0`**
      (the **filter** form, never `up == bool 0` — under the `gt -1` bar the bool form returns a
      series for every target, 1 for down and 0 for up, and all of them clear the bar, so every
      scrape target would alert permanently. Verified 2026-08-06), `for: 2m`, `noDataState: OK`, `gt -1`, `instant: true`, labels
      `route=henk-events`, `severity=critical`, `identity_scope=instance`; summary templated on
      `{{ $labels.instance }}`
- [x] 3.2 `[claude-config]` Declare `HenkContainerRestarting`: uid `henk-container`, expr
      `changes(container_start_time_seconds{name!=""}[15m]) > 1`, `for: 5m`, `noDataState: OK`,
      `gt -1`, `instant: true`, labels `route=henk-events`, `severity=warning`,
      `identity_scope=name`; summary templated on `{{ $labels.name }}`
- [x] 3.3 `[claude-config]` Declare the policy tree: route 1 `[route=henk-events]` → `henk-events`,
      **`continue: true`**, root grouping retained; route 2 `[severity=critical AND
      route=henk-events]` → `Discord-Grafana`, `continue: false`, **no** group_by override; plus a
      child route carrying instance grouping for the two new rules only. Route 2's second matcher is
      load-bearing — 1.2 measured that the four DNS criticals carry `severity=critical` with no
      route label, so severity-alone matching would pull them out of the parent
- [x] 3.4 `[claude-config]` Implement apply ordering: **contact point → policy tree → rules**, with
      an `ERR` trap restoring the policy backup. Tree-first is safe at every instant; rules-first
      would leave a critical rule live under the old `continue: false` tree with no non-agent path
- [x] 3.5 **[owner]** DONE 2026-08-06 — 7 changes applied exactly as the offline harness predicted; the four existing rules diffed on the severity label alone. `STAGE=target LEGACY_THRESHOLD=0` — new rules + tree, legacy bar untouched.
      Dry-run (expect `create` ×2, policy `update`, `update` ×4 for severity labels only), review,
      then `--apply`. Commit the snapshot
- [x] 3.6 **[owner]** DONE 2026-08-06 — drift guard refused the bare run and named all four rules; `ACCEPT_DRIFT` released it; threshold-only diffs applied. Post-apply all six rules `state=inactive health=ok` — nothing woke up, as predicted (arm 4's `on()` guard is currently false). `STAGE=target` — move the four existing rules to the `gt -1` bar, as a
      **separate** apply with its own dry-run, so a template flaw cannot take out working rules in
      the operation that adds new ones. The drift guard will refuse until the four uids are named in
      `ACCEPT_DRIFT` — correct, since a threshold change is the exact shape of the swap-retune
      revert and should require saying so out loud. Immediately check for **newly-firing** rules: that signal is
      the change's payoff. Arm 4 is not expected to fire on migration —
      `sum(increase(health_etl_rows_total[48h]))` is currently 0, so its `on()` guard holds

## 4. Deploy verification — each item is a distinct failure mode; do not collapse them

- [x] 4.1 **[owner]** **PASSED 2026-08-06.** Temporary `vector(1)` rule `VerifyHenkRoute`
      (`route=henk-events, severity=warning`) arrived on ntfy as
      `[FIRING:1] VerifyHenkRoute henk (henk-events warning)` and **did not** appear in Discord —
      Discord's digest read `1 firing · 0 resolved` naming only `VerifyDiscordRoute`. This is the
      `continue: true` regression check: D3 reasoned that a child route consumes the alert so the
      parent stays unreachable, and that is now measured rather than argued.
      Original text: Test-fire an **existing** henk rule → arrives on `henk-events` and **not** in
      Discord. The `continue: true` regression check. Method: a temporary `vector(1)` rule in folder
      `henk` labelled `route=henk-events`, deleted after (precedent: `henk-prov-smoke`). Do **not**
      use `POST /api/alertmanager/grafana/config/api/v1/receivers/test` — it tests the contact point
      directly and **bypasses the notification policy**, so it cannot discharge this check or 4.2
- [x] 4.2 **[owner]** **PASSED 2026-08-06.** Unlabelled `VerifyDiscordRoute` reached Discord via
      the parent route, proving the policy edit did not break delivery for everything outside
      folder `henk` (the 7 DNS criticals, `HighMemory`, `AuditShipStale`).
      Original text: Test-fire an **unlabelled** temporary `vector(1)` rule → still reaches Discord.
      Proves the policy edit did not break delivery for everything outside folder `henk`
- [x] 4.3 **PASSED 2026-08-06.** pi2's `node_exporter` stopped 18:25:41Z → started 19:12:53Z (47m).
      Measured arc, timestamps from Prometheus, ntfy's `Server stats` counter and rp5 container logs:
      - **18:26:30Z** Prometheus `up == 0` for `node-exporter-pi2`; native `InstanceDown` firing
      - **~18:28:4xZ** Grafana `HenkInstanceDown` fires → ntfy `messages_published` 657→658.
        Owner confirmed arrival on **both** ntfy **and** Discord — **dual delivery verified**, which
        is the headline claim of D3 and the whole point of the sibling route
      - **18:32:17Z** Henk agent spawns, **exactly 120s** after intake resumed → 18:32:23 context
        gathering (Gatus statuses, memory, disk, load1) → 18:32:38 handoff POST → **18:32:44 Signal
        `201 Created`**. Full triage arc
      - **19:12:53Z** exporter restarted → **19:13:0x–19:14:04Z** resolve published to ntfy
        (counter 661→662). Alert resolved and delivered
      - Isolation was cleaner than expected: pi2's node exporter is a **Gatus tier-3** endpoint with
        no `alerts:` block, so `HenkInstanceDown` fired *alone* — no Gatus alert confounded it
      **Two findings this test produced that were not being looked for:**
      - **Henk survived container recreation mid-debounce.** The rebuild (18:30:15) killed Henk
        inside the 120s debounce window for an already-received event. Because the checkpoint cursor
        advances only *after* a triage record is durable, the event **replayed** from ntfy on restart
        and was triaged 2m17s later. The durability guarantee held under an unplanned test
      - **Henk never reports a resolve inside cooldown, by design.** `evaluate()` applies cooldown on
        `ident.key` with **no exemption for `EventState.RESOLVED`**, and `identity.py` deliberately
        gives fire and resolve one shared key. The resolve landed ~41m after the fire triage — well
        inside the 6h cooldown — so it was suppressed with `reason="cooldown"`: audit record, no
        conversation. Correct per D6, but the consequence is that **the owner learns of a resolve
        only from the raw ntfy/Discord notification, never from Henk**, since a resolve inside 6h is
        the common case. Recorded as a UX gap for a follow-up change, not fixed here
      **Audit-log evidence (read 2026-08-06 from `/data/audit/henk-audit.jsonl`), which upgrades two
      of the above from inference to artifact:**
      - **D9 is verified in production.** The triage record's key is
        `grafana:HenkInstanceDown/RP2-TS-IP:9100` — the `instance` label value appended to the
        alertname (`RP2-TS-IP` stands in for pi2's tailnet address, which the real key contains
        verbatim; the placeholder is a publication rule, not the deployed value). Pre-change this
        rule would have keyed as bare `grafana:HenkInstanceDown`. This is the first live proof that
        `identity_scope` scopes a real Grafana alert end-to-end.
        **Operational consequence worth noting:** because `instance` for node exporters is a
        tailnet `IP:port`, scoped identity keys for those targets embed an address. That is fine for
        cooldown state, but it means audit records and `cooldown_overrides` patterns are written
        against addresses rather than hostnames — so a tailnet re-address silently creates new
        identities and re-arms cooldowns for targets that were already known
      - **The resolve suppression is recorded**: `record_type: suppression`, `identity_key`
        identical to the triage's, `reason: cooldown`, at 19:15:56Z — 43.2 min after the 18:32:44Z
        triage. Fire and resolve provably share one key, and the resolve provably produced no
        conversation
      - Henk's diagnosis was correct and appropriately hedged: "Pi2 (node-exporter-pi2) is
        unreachable — likely powered off, lost Tailscale/network connectivity, or node_exporter
        crashed on that host (confidence: moderate)". `triage_arc_complete: True`,
        `announceable: True`, handoff published
      - **Cadence-cap accounting measured**: exactly 2 announceable conversations in the trailing
        24h (`grafana:VerifyHenkRoute` 17:53:51Z, `grafana:HenkInstanceDown/…` 18:32:44Z) against
        `cap_per_24h: 3`. One slot remains until 2026-08-07T17:53:51Z. Noted because it constrains
        the *scheduling* of 4.3b and 4.6, not their validity: the audit log shows cap-suppressed
        turns still triage, diagnose and publish a handoff with `announceable: False`, so a capped
        test is still fully verifiable — only the Signal message is withheld
      Original text: Stop a node exporter for >2m — **pi2 ONLY**. (Corrected 2026-08-06 after
      measuring which exporter carries which textfile metric: `health_etl_*` is on
      **node-exporter-vps** only, `homelab_backup_last_success_timestamp` is on **both vps and
      pi5**, and every `obsidian_backup_*` series is on **pi5 only**. So stopping pi5 blinds all
      three obsidian arms of `HenkBackupFreshness` — milder than stopping vps, but still the
      change's own failure mode injected into its verification. pi2 carries only disk/swap series,
      whose other two hosts survive and which are nowhere near threshold.) Never
      `node-exporter-vps`: it carries the `health_etl_*` and `homelab_backup_*` textfile metrics, so
      stopping it drives `HenkHealthEtl`/`HenkBackupFreshness` to noData → `noDataState: OK` →
      "healthy", injecting this change's headline failure mode into its own verification.
      Confirm `HenkInstanceDown` reaches **both** `henk-events` and Discord, that Henk triages it
      with a full arc, and that restarting the exporter resolves it
- [x] 4.3b **PASSED 2026-08-06.** Targets chosen to make the test cost nothing: `pushgateway`
      (holds **only** its own `go_*` runtime metrics — no rule reads it, zero collateral) and
      `cadvisor-vps` (pi5's cadvisor keeps serving `container_start_time_seconds` for 15 containers,
      measured during the test, so `HenkContainerRestarting` never lost its input). pi2 was
      unusable here: its identity was in 6h cooldown from 4.3, so it would have been suppressed and
      proved nothing.
      Both stopped ~21:48:55Z, restarted 21:5x. Measured:
      - **21:51:05Z** ntfy publish #1; **21:52:0xZ** publish #2 (counter 662 → 664). **Two**
        notifications, one per instance — route 2's `group_by: [alertname, instance, name]` splits
        them, which is the entire reason route 2 exists. The second lagged the first by ~1 min
      - **Discord got exactly ONE grouped message** listing both alerts (`2 firing · 0 resolved`),
        because route 1 keeps root grouping on `[grafana_folder, alertname]` and both alerts share
        both values. The asymmetry is by design and is now measured: Discord one, ntfy two
      - **21:52:57Z** agent spawns — 120s after the *first* event, confirming the debounce deadline
        is fixed at first arrival and not extended by later ones, so event #2 landed inside the
        window → 21:53:20 handoff → **21:53:27 Signal `201`**
      - **The audit record is the proof:** one `record_type: session`, `announceable: True`,
        **`items: 2`** → `grafana:HenkInstanceDown/cadvisor:8080` and
        `grafana:HenkInstanceDown/pushgateway:9091`. **Two distinct scoped identities, one
        conversation** — D9 end-to-end. On restart, **two separate** suppression records, one per
        identity, confirming cooldown is per-identity rather than per-rule
      - **Bonus production proof of D2**, straight out of the Discord payload's node values:
        `A=0, B=0, C=1`. The expression's value is **0**, so the original `gt 0` bar would have
        computed `C=0` and stayed silent; `gt -1` yields `C=1` and fires. The firing-bar defect and
        its fix are both visible in one real notification — no probe required
      - The templated summary rendered correctly (`scrape target cadvisor:8080 (cadvisor-vps) is
        unreachable`), and the Silence link confirms all three labels deployed:
        `identity_scope=instance`, `route=henk-events`, `severity=critical`
      - **Cap now full**: this was announceable slot 3 of 3. Next slot frees 2026-08-07T17:53:51Z
- [x] 4.3c **PASSED 2026-08-07 — the split is CONFIRMED; two conversations per outage.** Run
      unattended (trap- and on-host-guard-protected) on pi2, the only host where this is safe: it is
      *backup* DNS, so pi5 and vps kept resolving. Stopped **AdGuardHome + node_exporter together**
      at `T0 = 00:35:47Z` to reproduce the shape of a host outage, restored 00:42:49Z (`active
      active` verified). Measured:
      | Time (UTC) | Event | Offset |
      |---|---|---|
      | 00:35:47 | `T0` both services stopped | — |
      | 00:35:53 | Gatus `Pi2 DNS` failure 1 | T+6s |
      | **00:36:23** | Gatus failure 2 → **tier-1 alert fires** (threshold 2) | **T+36s** |
      | **00:38:24** | **Henk spawn 1** → first arrival = spawn − 120s debounce = **00:36:24** | the Gatus event |
      | **00:40:57** | **Henk spawn 2** → first arrival = **00:38:57** | `HenkInstanceDown`, **T+190s** |
      | 00:42:49 | restore issued → `active active`, `DONE` | — |
      - **Arrival gap = 153s against the 120s debounce → they do NOT batch. Two conversations.**
        Design predicted a 90–150s gap and "expect two conversations per host outage, not one": the
        conclusion is exactly right, the gap 3s outside its upper bound. `HenkInstanceDown` at T+190s
        sits inside the predicted T+150–210s window; the Gatus alert came *earlier* than the modelled
        ~T+60s (T+36s) because the first check happened to land 6s after T0
      - **Conversation count read from `claude_agent_sdk` spawn lines** (one per conversation) rather
        than from the audit log, since `docker exec` needs an interactive password. Two spawns, two
        `POST /henk-handoffs`, and **no `signal-cli` lines at all** — the cadence cap was full, so
        both ran `announceable: false`. That incidentally re-confirms a capped triage still triages
        and still publishes a handoff; only the Signal message is withheld
      - Native `InstanceDown` was `firing` for `node-exporter-pi2` at capture time, corroborating
        that the Grafana twin had genuine cause
      Original text: Host-down case: record **actual arrival timestamps** of the Gatus tier-1 alert
      and of `HenkInstanceDown`, and the resulting conversation count. Expected two conversations
      (~90–150s gap against a 120s debounce). **If confirmed, raise `cap_per_24h` 3 → 5** per design
      — applied by editing rp5's locally-modified `config.yaml` **in place**, never via
      `git checkout`
- [x] 4.4 **PASSED 2026-08-06 — discharged from the committed artifact, no owner session needed.**
      `grafana-state-snapshot.json` (captured `20260806T174619Z`, i.e. after **both** applies, with
      `stage: target`, `legacy_threshold: -1`) records `HenkSwapPressure`'s live expr as the
      **pressure retune** — `((1 - node_memory_SwapFree_bytes / node_memory_SwapTotal_bytes) * 100
      > 95) or (rate(node_vmstat_pswpin[5m]) + rate(node_vmstat_pswpout[5m]) > 50)`, `for: 15m`,
      `gt -1`, `instant: true`, `noDataState: OK` — **not** the stale `>90%` fullness form. The same
      snapshot confirms `HenkInstanceDown` carries the **filter** form `up == 0`, never
      `up == bool 0`. Task 2.8's post-apply snapshot is what makes this checkable without
      credentials; that is the point of having it.
      Original text: Confirm `HenkSwapPressure`'s live expression still reads as the pressure retune
      after every apply, not fullness
- [x] 4.5 **[owner]** DONE 2026-08-06 — re-run after each apply printed `0 change(s) planned` across all eight objects, and no undeclared rule was reported in folder `henk`. This is the property the old script could never have: it prepended a duplicate route on every run. Re-run `--apply` → plan fully `unchanged`; policy tree holds exactly the
      declared routes (the old duplicate-prepend bug, now under test); exactly six rules in folder
      `henk` (2.7d)
- [x] 4.6 **RUN 2026-08-06 — FAILED, and the failure is in the RULE, not the test method.
      `HenkContainerRestarting` is structurally incapable of firing on this fleet by any mechanism.**
      Executed exactly as prescribed: `docker restart wordle-web` ×2, 75s apart (22:26:15Z and
      22:27:30Z), then waited out `for: 5m` + the 120s debounce. Result: **no alert, no ntfy publish,
      no Henk triage** — `changes()` never left 0.
      **Root cause, measured three independent ways:**
      - `container_start_time_seconds{name="wordle-web"}` is a **perfectly flat line** at
        `2026-05-29 10:20:09Z` across a 25-minute range query spanning both restarts (26 samples,
        **1 distinct value**), while `docker ps` reported `Up 9 minutes`. The container restarted;
        the metric did not move
      - **Cross-check that pins the semantics:** `henk-henk-1`, *recreated* today at 18:30:15Z,
        reports `container_start_time_seconds = 2026-08-06 18:30:15Z` — exactly its recreation.
        `wordle-web`, *restarted* twice tonight, still reports its 2-month-old value. So the metric
        is **container CREATION time**, not last-start time
      - Therefore both paths yield `changes() == 0`: `docker restart` keeps the container ID so the
        value is constant, and a recreate mints a new ID → a **new series**, whose `changes()` over
        any window is 0 because it has no earlier samples to differ from. The 15-day measurement
        (8 recreations of `henk-henk-1`, counter never reaching 1) was the second path; this test
        closes the first
      **This falsifies the design's own mitigation.** design.md states the rule "covers in-place
      restart loops with a stable container ID — which still includes the case it was chosen for, a
      crash-looping Henk under its restart policy". A crash loop under Docker's restart policy
      restarts the **same container ID**, so creation time is unchanged and `changes()` stays 0. The
      rule cannot fire for a crash loop either — the one case it existed for.
      **This is the same defect class as `HenkHealthEtl` arm 4**: provisions cleanly, reports
      `health=ok`, cannot fire. The change created to eliminate that class shipped a new instance of
      it. `gt -1` did not save it, because the bar is irrelevant when the input never changes.
      **No metric can fix it in place**: this Prometheus exposes **zero** metrics matching
      `*restart*`, and cadvisor's only nearby series are `container_start_time_seconds`,
      `container_last_seen`, `container_health_state`, `container_oom_events_total`,
      `container_tasks_state` — none a restart counter. A real fix needs a new source, e.g. Docker's
      `RestartCount` (`docker inspect -f '{{.RestartCount}}'`) published via the node-exporter
      textfile collector, which *is* a monotonic counter that `increase()` can read. That is a new
      change, not a patch to this one — it needs a metric pipeline, not a threshold edit
- [x] 4.7 **ANSWERED 2026-08-06: no cooldown override, keep the 6h default.** Measured over the
      full 15-day Prometheus retention with
      `count(changes(container_start_time_seconds{name!=""}[15m]) > 1)`: **zero samples** in which
      any container satisfied the rule's condition. A per-pattern override exists for *chronic*
      identities — the precedent is `pattern: "swap"` at 24h, added because swap pressure is
      persistent by nature. An identity that has not fired once in 15 days is the opposite of
      chronic, so an override would be tuning against no data.
      This is answerable without 4.6 because the override question is about firing *frequency*, not
      about the identity string. The one finding that would reopen it: if 4.6 shows
      `identity_scope: name` failing to separate containers (several collapsing onto one key), a
      crash-looping container could then suppress an unrelated one — but that is a scoping bug to
      fix, not a cooldown to widen. 4.3b already demonstrated per-target separation working on the
      sibling rule, so this is unlikely.
      Consequence for **5.2**: 4.7 contributes no config change. 5.2 now hinges on 4.3c alone

## 5. Henk-side follow-through

- [x] 5.1 `[henk]` **DONE — TDD, 11 new tests, full suite 254 → 265 green.** `_derive_grafana`
      honours an `identity_scope: <labelname>` label, appending that label's value to the key;
      absent label → exactly today's key. Covered: two simultaneously-down targets yield two
      distinct keys; fire and resolve for one target share a key; a resolve for one target does
      not satisfy another's incident; the scope label may name any label (`name` for containers);
      a rule with no `identity_scope` is bit-for-bit unchanged; a scope naming an absent or empty
      label degrades to the alertname key rather than inventing one or failing intake; the
      identity is never the normalized-title fallback; derivation is deterministic.
      Two non-obvious guards, each with its own test:
      - **The label lookup is anchored to the `- <label> = ` line form.** `alertname` ends with
        `name`, so an unanchored search for the label `name` matches inside
        `- alertname = HenkContainerRestarting` and keys every container on the rule name —
        reintroducing the exact collapse this feature removes, via the fix itself
      - **The appended value is length-bounded (120 chars).** Event payloads are untrusted data
        (design D4) and the key is persisted in cooldown state, so a hostile label must not grow
        it without limit
      Test payloads are synthesized against the real captured Grafana format and use hostnames
      rather than tailnet addresses; genuine captures land in 5.3 once 4.3/4.6 produce them
- [x] 5.2 **DONE 2026-08-07 — `cap_per_24h` raised 3 → 5, the pre-committed resolution, on 4.3c's
      evidence.** 4.7 contributed nothing (no cooldown override warranted), so this is the cap alone.
      - **Test written first**, encoding *why* rather than just the number:
        `test_two_host_outages_in_24h_fit_under_the_shipped_cap` replays 4.3c's measured shape — two
        outages, each a Gatus alert plus a `HenkInstanceDown` 153s later — and asserts all four halves
        stay announceable, that a 5th unrelated incident still fits, and that the **6th is still
        gated** so the cap remains a cap. At the old 3, a second outage would have gated its
        `HenkInstanceDown` half, i.e. the more diagnostic one
      - `config.yaml` (repo) updated with the reasoning inline, and `tests/test_config.py`'s
        contract assertion moved 3 → 5. That test failing was correct — it guards the shipped value,
        and the value changed by decision on measurement, not by weakening the test
      - **rp5's deployed `config.yaml` edited in place** (never `git checkout`): timestamped backup
        at `/home/pi/henk-config-deployed.yaml.bak.pre-cap`, then a single anchored `sed`. Verified
        by `diff` that **exactly one line** differs (`77c77`, `3` → `5`) and the owner Signal UUID /
        number / `todo_note_allowlist` are untouched
      - Suite 267 → **268 green**
      - **REMAINING: the container must be restarted for it to take effect** — `config.yaml` is a
        read-only bind mount read at startup, and `docker compose` needs an interactive password on
        rp5. Until then the file says 5 and the running process still holds 3
      - Note for future tuning: the existing `pattern: "swap"` override is a case-insensitive regex
        over the identity key, and scoped keys are now longer (`grafana:HenkInstanceDown/<instance>`),
        so a pattern intended to match a rule name can now also match an instance value
      Original text: If 4.3c or 4.7 say so: `cap_per_24h` and/or a cooldown override in `config.yaml`,
      with tests from the existing per-pattern override coverage. rp5's `config.yaml` is locally
      modified and must stay that way — edit in place, never `git checkout`. Note the existing
      `pattern: "swap"` is a case-insensitive regex over the key, and scoped keys are now longer
- [x] 5.3 `[henk]` **HALF DONE 2026-08-06 — `HenkInstanceDown` landed; `HenkContainerRestarting`
      blocked on 4.6.** Appended fixture lines 4 and 5 (real `[FIRING:1]` and `[RESOLVED]` captures
      for `HenkInstanceDown`, pulled from ntfy retention) and extended the README table.
      - Used the **`cadvisor:8080`** capture, not the pi2 one, deliberately: pi2's `instance` label
        is a tailnet address, and redacting it would have broken the README's verbatim promise.
        `cadvisor:8080` is a Docker-internal name, so the line commits publication-safe as-is
      - **Closed a gap the README had flagged as uncaptured**: a real Grafana `[RESOLVED]` payload
        now exists, so the synthesized variant is no longer the only coverage
      - **Two tests added** (`test_real_scoped_grafana_payload_keys_on_instance`,
        `test_real_scoped_grafana_resolve_shares_the_fire_key`) asserting the real payload derives
        `grafana:HenkInstanceDown/cadvisor:8080` and that its resolve shares that key. The D9 tests
        from 5.1 are synthesized; these run the same code over verbatim production bytes, so they
        fail if Grafana's ntfy rendering drifts. Suite 265 → 267
      - **Two payload-format findings recorded in the README:** the title absorbs grouped label
        *values* (line 4's title contains the bare word `instance`, which is the `identity_scope`
        value — so a title-based lookup would pass here by accident, which is why derivation reads
        the `- <label> = ` body lines), and a recovery arrives as **NoData**
        (`grafana_state_reason = NoData`) rather than a value-based resolve, because `up == 0`
        returns no series once the target is back
      - **Closed as complete-as-possible 2026-08-06.** The `HenkContainerRestarting` payload is
        **not capturable**: 4.6 proved the rule cannot fire, so no such event exists to capture.
        Recorded in the README as unobtainable with an explicit instruction **not** to synthesize
        one — a fixture for an impossible event would encode the defect as expected behaviour and
        give false confidence to exactly the tests meant to catch it

## 6. Documentation and close-out

- [x] 6.1 **DONE 2026-08-06.** `services/monitoring.md` no longer calls it "the idempotent
      provisioning script"; it now describes declared state, plan-before-write (`--dry-run` default,
      `--apply` explicit), drift refusal with `ACCEPT_DRIFT`, undeclared-rule blocking without
      pruning, PUT-by-uid + `X-Disable-Provenance`, apply ordering with the `ERR` rollback trap, and
      the offline harness. The four API representational quirks are recorded there too, with the
      instruction to re-probe after a Grafana upgrade
- [x] 6.2 **DONE 2026-08-06.** New `##### Expanded to six rules` subsection: 6-row table carrying
      `severity` and `identity_scope` per rule, the three-route policy tree as a table with the
      consume-not-continue explanation, an explicit statement of *which* severities dual-deliver
      (`severity=critical` in folder `henk` — today `HenkInstanceDown` alone), why route 1's second
      matcher is load-bearing for the seven DNS criticals, and why route 2 is kept off the
      catch-all. The swap-retune blockquote now ends with the tombstone note explaining that two
      scripts claiming authority over one rule is how the retune got reverted
- [x] 6.3 **DONE 2026-08-06** — `#### Prometheus ↔ Grafana redundancy map`. Recorded as **22 of 23**
      natives covered, not 20: the task text was written pre-deploy, and this change *is* what added
      the `InstanceDown` and `ContainerRestarting` twins, leaving `HighCPU` as the only uncovered
      native. Both reverse orphans (`HenkSwapPressure`, `AuditShipStale`) are named, with the
      not-a-subset-either-way consequence for D7 called out in a blockquote
- [x] 6.4 **DONE 2026-08-06** — `#### The firing-bar defect`. Series-returned vs value semantics,
      `HenkHealthEtl` arm 4 named as the live rule that was `health=ok` and unable to fire from
      2026-07-22 to 2026-08-06, the `gt -1` superset argument, plus both probe-only traps: that
      `reduce(count)` is a **no-op on instant queries** (the fix that would have changed nothing) and
      that `up == bool 0` under a `-1` bar alerts on every target permanently. `HighCPU`'s
      deliberate non-routing recorded with its 67.8%-vs-85% evidence
- [x] 6.5 **DONE 2026-08-06.** Rule table restructured with an explicit `Evaluation group` column;
      `High memory usage` moved out of DNS Performance and marked **no labels at all**, with the
      consequence spelled out (no label-based route can ever match it; it reaches Discord only via
      the root route). `AuditShipStale` added to the table and flagged as purpose-unidentified and
      unrelated to Henk's audit log, with a warning not to assume it is safe to delete
- [x] 6.6 **DONE 2026-08-06** — `#### Known blind spot: Henk cannot report its own death`, including
      the ntfy-retention/replay nuance and this session's live measurement of it, the absence of a
      Gatus endpoint for Henk, and the `changes()`-resets-on-rebuild finding with the
      `docker restart` ×2 instruction. **Beyond the listed scope, two `applications.md` corrections
      this change's verification forced** (both were traps hit for real today): intake logs *nothing*
      at INFO on arrival so a cooldown-suppressed event is indistinguishable from a lost one, and no
      log line appears for a full 120s debounce — the page previously implied `docker logs` was a
      sufficient "did Henk get it" check. The resolve-inside-cooldown UX gap is recorded there too
- [x] 6.7 **DONE 2026-08-06.** `archive/2026-08-02-henk-events/design.md:9` corrected 22 → 23 with
      the breakdown (7 DNS + 12 infrastructure + 4 health-pipeline). **The same error was also in
      that change's `proposal.md:7`**, which the task did not name; fixed as well, since leaving one
      of two identical wrong counts is worse than fixing neither
- [ ] 6.8 `[henk]` `/opsx:sync` + `/opsx:archive`
