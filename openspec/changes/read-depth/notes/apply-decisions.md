# Apply decisions — `read-depth` §2 (config surface) and §3 (query registry)

**Applied:** 2026-09-02. Task groups §2 (2.1–2.6) and §3 (3.1–3.9).
Baseline before this work: **1503 passed, 12 deselected**. After §2: **1544**.
After §3: **1662 passed, 12 deselected** (+41 config tests, +118 registry and
dispatch tests).

Standing rule for this change, honoured here: every number and every name that
reaches the registry comes from `notes/backend-probe.md`, and nothing is
transcribed from `design.md`, `tasks.md`, the homelab docs, or the existing
`henk/` code. Where the probe record contradicts the design or the task text, the
probe wins and the contradiction is recorded below.

---

## §2 — Config surface

### What was added

| key | default | why this default |
|---|---|---|
| `homelab_query.enabled` | `true` | the query half rides the existing `tag:henk` egress; there is no host provisioning to stage, so there is nothing for an off state to protect |
| `homelab_query.query_range_max_points` | `60` | task 2.3; 60 points spreads 24h over ~24-minute buckets |
| `homelab_docs.enabled` | `false` | the corpus needs a clone, a timer and an allowlist on rp5 first (migration steps 2–6); enabled sooner it can only answer "corpus unavailable" |
| `homelab_docs.path` | `""` | no plausible-looking default: Docker auto-creates a missing bind source, so a typo would search successfully and find nothing |
| `homelab_docs.stamp_max_age_seconds` | `93600` (26 h) | design D9 — the fleet's own `BackupStale > 26h` convention, not a new number |
| `homelab_docs.read_byte_budget` | `8000` | ~2k tokens, the same per-injection bound `store.recall_render_limit` already uses |
| `homelab_docs.search_result_count` | `5` | several candidates (D8's mitigation for keyword search) and bounded, since each carries a snippet |
| `personal_data.docs_path_allowlist` | `()` | default-deny; an unset allowlist surfaces nothing |

No timeout key was added: the backends' timeouts stay `endpoints.gatus` and
`endpoints.prometheus` (task 2.3), and a test asserts no new key's name contains
`timeout`.

Defaults are read through the loader, not only declared on the dataclass —
`_bounded_settings` reads `sec.get(name, getattr(cls, name))`, the same
table-driven shape `_DELIVERY_SETTINGS` uses. rp5's `config.yaml` is
skip-worktree'd and will never carry a new key, so the loader's fallback *is* the
deployed value; every §2 test therefore goes through `Config.from_dict` rather
than reading dataclass attributes.

### Validation (2.5)

Positivity is enforced for `stamp_max_age_seconds`, `read_byte_budget`,
`search_result_count` **and** `query_range_max_points`, unconditionally (not only
when the capability is enabled), with `ConfigError` naming `<section>.<key>`.
`homelab_docs.enabled` with no `path` is a `ConfigError` naming both keys (design
D11 layer 1). Host state is deliberately not checked at load.

---

## §3 — The query registry

`henk/tools/query_registry.py` holds the six entries as one reviewable table:
backend, templates, parameter domains, renderer, thresholds, baselines, caveats,
and the measured availability holes. `henk/tools/homelab_query.py` holds the
dispatch and the schema; `henk/tools/query_renderers.py` holds six named stubs
§4 fills. **The tool is not registered** — that is §7 — so `henk/tools/__init__.py`
is untouched and the deployed toolset is unchanged by this commit.

### What the probe record changed, versus `design.md` and `tasks.md`

| the written record said | the registry does | why |
|---|---|---|
| six scrape targets (D3, tasks 1.3/4.4) | no count anywhere; `scrape_targets` enumerates whatever bare `up` returns | live it is **seven** — the six named jobs plus `pushgateway`, which `InstanceDown` alerts on. A test asserting six would fail against production |
| `endpoint_history` window `APPLY-RESOLVED:gatus-window` | `{1h, 24h, 7d, 30d}` | the deployed Gatus states its own vocabulary in its 400 body; it rejects `15m` and `6h` |
| memory bar `APPLY-RESOLVED:memory-bar` | **75** % used | the live Grafana rule reads 75; the 90 in `homelab_health` and in the Prometheus-native rule is a different, non-delivering bar |
| `freshness_check` selects four families | the **eight** exact `__name__` values | `health_etl_*` carries a `_seconds` suffix, so a `*_timestamp` glob silently drops it |
| `dns_performance` "per-upstream response time" | `adguard_avg_processing_time_seconds` | the metric all fourteen live DNS rules evaluate (§1's own decision, followed here) |

### Publication safety

No template contains an address, an `instance` selector, a `server` selector, or
a scrape URL — asserted per template, including `dns_performance`'s. The
result-side rule is a shared primitive rather than six renderers each remembering
it: `project_labels` drops the seven address-bearing labels **by name** and any
value that is address-shaped **by value**, and `describe_target` names a target
by its friendly enum value plus its job. Fixtures use `10.0.0.x` placeholders.

### The three outcomes

`plan_query` raises `QueryRefused` (out of domain), returns a `NOT_DERIVABLE`
plan carrying the specific condition, or returns an `ANSWERED` plan. The two
measured availability holes are declared from the record: `temperature` on `vps`
short-circuits as not-derivable (there is no series to read), while
`container_health_state` on `rp5` is an **aspect** — the query still runs and the
result carries a caveat, because an omitted health column reads as "no container
is unhealthy".

---

## Decisions made alone

1. **`docs_path_allowlist` lives in `personal_data`, not in `homelab_docs`.**
   Task 2.3 lists it among the corpus keys, which reads as a `homelab_docs` key.
   The in-repo precedent points the other way: `todo_read`'s allowlist is
   `personal_data.todo_note_allowlist`, not a key on a todo section, because the
   Tier-W data axis is reviewed as one surface. D13 says explicitly that the
   corpus complies with the *existing* personal-data scoping requirement rather
   than claiming an exemption, so it belongs beside the other two. A test pins
   `PersonalDataConfig`'s field set to exactly the three.

2. **`query_range_max_points` is validated too, and floored at 2.** Task 2.5 names
   only the staleness bound, the byte budget, and the result count. A zero point
   count makes the range step a division by zero on the first trend query, and a
   count of 1 collapses first/last/min/max onto one sample so "direction of
   travel" — which the spec requires — is unanswerable. Strengthening, not
   widening.

3. **The `enabled`-with-no-`path` refusal is implemented in §2, not deferred to
   §7.** Design D11 states it as a `ConfigError` in `from_dict`; task 7.1 tests
   that it kills startup. Implementing the check where the design puts it lets
   §7 assert the behaviour rather than build it.

4. **The two new sections are also written into the repo's `config.yaml`.** The
   sample file documents every other section's keys and rationale, and
   `test_config.py` loads it. Values are identical to the defaults, so the file
   is documentation rather than a second source of truth (a test asserts the two
   agree).

5. **§3 ships a minimal execution seam, not just a planner.** Task 3.6 requires
   refusals to be asserted **on the transport**. With no code path that ever
   issues a request, that assertion is vacuous — deleting the validation would
   leave it green. So `HomelabQueryTool._fetch` issues the planned requests now,
   and `test_an_accepted_query_reaches_the_backend_and_awaits_its_renderer` pins
   the other side of the boundary. Rendering and the honest-failure polish stay
   in §4; an accepted query currently returns an explicit "renderer not
   implemented" failure rather than anything shaped like data.

6. **Thresholds are stored in the rule's own form, and DNS bars in
   milliseconds.** The DNS rules are written in seconds (`> 0.06`) and the
   record's headroom table in milliseconds (`60 ms`). Storing milliseconds lets
   the test compare the registry against **both** of the record's tables and
   assert they agree with each other — so a typo in either one of them fails.

7. **`node_resource_trend(vps, temperature)` short-circuits without querying.**
   There is no series to read, so issuing the query would return an empty result
   that reads exactly like "measured, nothing there". The declaration is sourced
   from the probe record and cited in the message. Residual risk, recorded
   deliberately: if a thermal sensor ever appears on the vps, the message goes
   stale until the record is re-probed. §4 must additionally map an empty
   backend response to the same outcome, so runtime absence is covered too.

8. **`temperature` reads `node_thermal_zone_temp{type="cpu-thermal"}`.** The
   record measures two temperature metrics that disagree per-series on rp5, and
   `node_hwmon_temp_celsius` spans six series including an NVMe sensor — a bare
   "temperature" over it would report a disk sensor as the CPU. One metric,
   named.

9. **`container_state` declares a named-container template it does not yet use.**
   The spec requires that a template naming a container explicitly use a form
   returning a value when the series is absent. v1 takes no container parameter,
   so the requirement would otherwise be vacuous; the entry declares the
   `or vector()` form (the fleet's own idiom, from `MollySocketLiveness`) for §4
   to fill, with its `<container>` slot fillable only from a name discovered in
   the query's own result set — never from model free text.

---

## Mutations

Every group's tests were written first and run red before implementation. Where a
test passed on first run, the implementation was mutated to confirm the test
binds, then reverted. A green suite that survives a deliberate defect is not
evidence.

| # | group | mutation | outcome |
|---|---|---|---|
| M1 | §2 | `HomelabQueryConfig.enabled` default flipped `True` → `False` | **caught** — 4 failures, incl. `test_the_corpus_ships_disabled_and_the_queries_ship_enabled` |
| M2 | §2 | `_bounded_settings` stops reading the config key (`sec.get(name, …)` → `getattr(cls, name)`) — the "builder never reads the key" trap task 2.1 names | **caught** — 19 failures |
| M3 | §2 | positivity check disabled (`if value <= 0` → `if False`) | **caught** — 9 failures across all four bounds |
| M4 | §2 | `stamp_max_age_seconds` default 26 h → 24 h | **caught** — 3 failures |
| M5 | §2 | corpus `enabled`-without-`path` refusal disabled | **caught** — 2 failures |
| M6 | §3 | `container_state`'s node domain widened to the node-exporter set (i.e. `rp2` admitted) | **caught** — 4 failures, incl. the spec-literal comparison |
| M7 | §3 | the domain-membership check removed — the guard that keeps a bad argument off the wire | **caught** — 10 failures, every one of them an *AssertionError raised by the transport itself* |
| M8 | §3 | memory bar 75 → 90 (`homelab_health`'s constant) | **caught** — the pinned-record comparison and the named test |
| M9 | §3 | an `instance` selector with a placeholder address planted in one template | **caught** — publication-safety and dispatch |
| M10 | §3 | `domain_for` returns `tuple(domain)` instead of the enforced object | **SURVIVED** — `tuple(t)` returns `t` itself for a tuple, so identity held. Not a real defect, but the test was weaker than intended until re-run as M10b |
| M10b | §3 | `domain_for` returns a genuinely new tuple (`tuple(list(domain) + [])`) | **caught** — the same-object test |
| M11 | §3 | the not-derivable branch collapsed into an ordinary answered plan | **caught** — 3 failures, incl. the three-outcome test |
| M12 | §3 | a seventh `alerts_firing` entry added to the enum | **caught** — 3 failures |
| M13 | §3 | projection drops by label **name** only, not by value shape | **caught** — the unforeseen-label test |
| M14 | §3 | freshness selector replaced by a `*_timestamp` glob | **caught** — the eight-metric test |
| M15 | §3 | `scrape_targets` filtered to `up == 0` | **caught** — a healthy fleet would be indistinguishable from a broken query |
| M16 | §3 | `swap_used` marked as the rule's trigger | **caught** |
| M17 | §3 | the **record's** memory bar edited 75 → 80 | **caught** — proves the test reads `notes/backend-probe.md` rather than a literal |
| M18 | §3 | the **record's** rp2 DNS warning bar edited 200 → 250 ms | **caught** — and the record's two tables are cross-checked against each other |
| M19 | §3 | the **spec's** `container_state` domain widened to include `rp2` | **caught** — proves the domain test reads the binding delta |

M10 is the one worth keeping in view: a mutation that *looks* like a defect and
is not can leave a test looking stronger than it is. The identity property does
hold — it just needed a mutation that actually breaks it.
