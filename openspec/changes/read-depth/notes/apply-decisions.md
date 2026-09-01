# Apply decisions — `read-depth` §2 (config), §3 (registry), §4 (the six queries)

**Applied:** 2026-09-02. Task groups §2 (2.1–2.6), §3 (3.1–3.9) and §4 (4.1–4.11).
Baseline before this work: **1503 passed, 12 deselected**. After §2: **1544**.
After §3: **1662 passed, 12 deselected** (+41 config tests, +118 registry and
dispatch tests). After §4: **1731 passed, 12 deselected** (+69 renderer,
discovery and honest-failure tests; three §3 dispatch tests were rewritten rather
than added to — see §4's "Tests changed, not weakened").

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


---

## §4 — The six named queries and their renderers

`henk/tools/query_renderers.py` holds the six summaries; `henk/tools/
query_projection.py` is new and holds the projection rule; `henk/tools/
homelab_query.py` gained first-use Gatus discovery and event-to-argument
resolution. The tool is still **not registered** — that is §7 — so
`henk/tools/__init__.py` is untouched and the deployed toolset is unchanged.

### The projection rule moved to its own module

`query_registry` imports `query_renderers` to bind each entry's renderer, so a
renderer importing the registry back is a circular import that fails at load.
Rather than deferring imports inside six functions, the shared primitives —
node/job maps, `project_labels`, `describe_target`, `friendly_target`,
`scrub_addresses` — now live in `query_projection`, which neither imports. The
registry **re-exports** every one of them, so its public surface and every
existing test import are unchanged.

### `scrub_addresses`: the projection rule had a hole in free text

The spec requires `scrape_targets` to surface each down target's `lastError`,
and the live shape of that field is
`Get "http://<addr>:9100/metrics": dial tcp <addr>: connect: connection refused`.
A projection that filtered only *labels* would therefore publish an address in
the one field the owner most wants to read — and dropping the field instead
would lose the answer. So backend-authored free text (scrape errors, Gatus
condition strings, container names) is scrubbed: URLs, bare IPv4 with optional
port, and `*.ts.net` hosts are replaced with `<address redacted>`; text carrying
no address comes back byte-identical. This was not in the task text; it is the
same requirement applied to a surface the task text did not name.

### Runtime empties land on the third outcome (§3 decision 7's other half)

Every renderer maps an empty in-domain response to "could not be derived", with a
message that names the condition and says explicitly that it is neither a reading
of zero nor a rejection. So the two holes the registry short-circuits from the
probe record (`temperature` on the vps, and `container_state`'s health column on
rp5) and the holes only runtime can discover are the same outcome to the reader.

### `dns_performance`'s two underivable conditions are kept apart

"rp2's node-exporter series are absent" and "rp2 reports, but no AdGuard series
carries its host" are both not-derivable, and the owner acts differently on each
— the first sends them to the host, the second to the exporter's configuration.
Mutation M24 showed the two branches' messages were interchangeable; both are now
pinned by their own test.

### Windows, steps, and which roles are range queries

`QueryEntry` gained `range_roles`. `dns_performance` issues a **range** query for
its measurement and an **instant** query for the job-labelled series it derives
the node mapping from; without the distinction the mapping query would pay for a
window of samples to read one label set.

## §4 — Decisions made alone

10. **The `endpoint` argument accepts an event's identifying text, not only a
    key.** The spec requires the event-to-argument derivation to be *specified*
    rather than inferred. It is implemented as `resolve_endpoint_key`: strip a
    `Gatus:` prefix, split `{group}/{endpoint}`, compose
    `sanitize(lower(group)) + "_" + sanitize(lower(name))` — the measured 1:1
    character substitution — and then **test membership in the discovered set**.
    Every branch ends in that membership test, so this widens what the agent may
    *say* without widening what the tool may *reach*: an unresolvable name is
    refused by name and no invented key is ever requested.

11. **Discovery TTL is a constructor default (300 s), not a config key.** Task
    2.3 fixed the config surface and rp5's `config.yaml` is skip-worktree'd. The
    TTL only bounds how long a *deleted* endpoint stays queryable — a refresh on
    lookup miss already makes a *new* one queryable immediately — so it is not a
    value the owner needs to reach.

12. **Discovery failure fails closed even when a key set is already in memory.**
    The spec's requirement is about the *invocation*. Serving a remembered set
    would answer from a snapshot of a backend that is currently unreachable, and
    the whole reason the domain is discovered is that the owner edits it by hand.
    Mutation M28 confirmed this was untested until a test was added for it.

13. **`named_container_expression` is exercised by tests but not yet called by
    the tool.** v1 takes no container parameter, so the spec's "a named container
    absent from the backend still returns a reading" would otherwise be vacuous.
    The helper takes its container name from **the query's own result set**
    (`known=`), never from model free text, so it is a bound parameter rather
    than a hole in the no-free-text rule. Wiring a second round trip into
    `container_state` would be a design change, not a §4 one.

14. **Ages are derived from Prometheus's own evaluation timestamp**, not from the
    local clock. It is the correct instant for the sample, it needs no clock
    injection into the renderers, and it is what makes the frozen-writer test
    real: the raw timestamp stands still while the evaluation time advances.

15. **The memory threshold's note no longer cites `homelab_health`'s constant.**
    It said "Grafana 75, Prometheus-native 90, homelab_health's constant 90" — a
    claim §10 is about to invalidate. It now names only the native rule, which
    stays true after §10 lands. The `swap_used` note lost the phrase "not an
    approaching incident" for a phrasing that does not put the forbidden framing
    into the rendered text at all.

16. **The `freshness_check` widening stays a gap.** Record 1.4's six accepted
    gaps (the `*_errors_total` / `*_failed_total` / `*_duration_seconds` /
    `*_rows_total` rules) are all inside the metric families `freshness_check`
    already selects, so carrying each pipeline's error counter beside its
    timestamp would take rule coverage to 23 of 23 with no new query, parameter
    or backend call. **Not implemented here**: the spec delta binds
    `freshness_check` to raw timestamps and derived ages, and widening its
    projection is a spec question. Recorded as the follow-up.

## §4 — Tests changed, not weakened

Three §3 tests in `tests/test_query_dispatch.py` were written against a stubbed
seam that §4 replaced, and were rewritten rather than deleted:

| test | before | after |
|---|---|---|
| `..._fails_closed_when_discovery_has_not_run` | "discovery has not run" was reachable because nothing ever discovered | discovery is real, so the reachable state is a discovery **failure**; the test now 503s the discovery route and asserts no key reaches a backend route |
| `..._refuses_an_undiscovered_key_with_no_request` | asserted **no** request at all | a miss refreshes once — that is what makes a rename queryable — so it now asserts no request **for that key**, which is what the spec says |
| `..._reaches_the_backend_and_awaits_its_renderer` | pinned the "renderer not implemented" failure | asserts the accepted query renders a summary naming its target |

## §4 — Mutations

| # | mutation | outcome |
|---|---|---|
| M20 | `project_labels`' address-bearing **name** denylist disabled | **SURVIVED** — the value-shape filter caught every fixture, so the name filter was untested. A fixture whose `instance` is a MagicDNS-style host (not address-shaped) was added; re-run as M20b, **caught** |
| M21 | two series rendered as one figure (multiplicity branch removed) | **caught** |
| M22 | "since when" taken from `results` instead of `events` | **caught** — 2 failures |
| M23 | a duration invented for a target never up in the window | **caught** |
| M24 | `dns_performance`'s node-host derivation dropped | **SURVIVED twice** — the query still fell through to the *other* underivable branch, whose message was interchangeable with the first. The two diagnoses are now pinned separately; re-run as M24c, **caught** |
| M25 | `lastError` no longer scrubbed | **caught** — a placeholder address reached the rendered result |
| M26 | freshness age computed from a fixed now, so a frozen writer's age stops growing | **caught** |
| M27 | an empty runtime response rendered as a measurement rather than as not-derivable | **caught** |
| M28 | discovery failure serves the remembered key set instead of failing closed | **SURVIVED** — every discovery test started from an empty cache, so the fallback path was unreached. A stale-cache-plus-failed-refresh test was added; re-run as M28b, **caught** |

Three survivors out of nine, and all three were the same shape: a defence that
**another** defence happened to cover for every fixture in the suite. M20 (name
denylist behind value-shape), M24 (one underivable branch behind another) and M28
(fail-closed behind an empty cache) were each invisible until a fixture existed
that only that defence could catch. §3's M10 was the same lesson from the other
direction, and it is worth stating plainly: a mutation that survives is either a
missing test or a redundant mechanism, and telling those two apart is the work.

---

## §6 — Audit assertions

**Applied:** 2026-09-02. Tasks 6.1–6.4, in `tests/test_audit_read_depth.py`
(+12 tests; 1828 → 1840 in this checkout, with §5 already merged). No `henk/`
file was modified: §6 is an assertion about a mechanism that already exists.

`RESULT_CAPTURING_TOOLS` is untouched and still `frozenset({publish_handoff})`.
No redaction code was added, and its removal was **not** mutation-tested — that
would break `handoff_message_id` for the one tool that legitimately consumes a
result.

### The tests run the real path, not a hand-built record

Each test executes a real tool (`homelab_query` against a `MockTransport`,
`homelab_docs` against the committed synthetic corpus), feeds its **genuinely
rendered** output to the production `_StatsAccumulator` in real SDK block shapes,
drives that through `AgentCore`'s audit writer with a real `AuditLog`, and reads
the JSONL back off disk. A record assembled by hand would prove nothing about the
path production takes.

### §6 — Decisions made alone

17. **"No substring of the result body" is enforced against a *control record*,
    not against a hand-maintained vocabulary list.** The literal reading is
    unsatisfiable (every single character is a substring), and the first
    implementation — sweep tokens of ≥4 characters — produced two false
    positives immediately: `hash` from `memory_hash` and `read` from the
    `read-only` tool class. Exempting those by name would have been the start of
    a list that quietly grows until it exempts a real leak. Instead every session
    is written **twice**, once with the real bodies and once with empty ones, and
    a token counts as leaked only if it appears in the real record and *not* in
    the control. The control also supports a stronger assertion than any token
    sweep: the two records are compared **byte-for-byte** (timestamps
    normalised), so the property asserted is that the result body had no
    influence on the record at all.

18. **A deliberate leak-detector test carries the whole file's non-vacuity.**
    `test_the_harness_would_catch_a_leak` runs the same corpus section through
    `publish_handoff` — the one tool that *does* opt in — and asserts the text
    **does** reach `result_id`. Without it, every other assertion in the file
    would pass against a harness that wrote nothing.

19. **The failure path is asserted as well as the success path.** A backend error
    message is backend-authored free text and the surface most likely to quote an
    address (§4's `scrub_addresses` exists for exactly that reason on the
    rendering side). It is a result like any other and is covered by its own test.

20. **6.4's address assertion is paired with a body assertion.** Mutation M31
    showed that a test naming one value passes against a capture that truncates
    before that value. The section it came from is now asserted absent too, so
    6.4 binds on its own rather than only in company.

### §6 — Mutations

| # | mutation | outcome |
|---|---|---|
| M29 | `homelab_query` added to `RESULT_CAPTURING_TOOLS` | **caught** — 8 failures |
| M30 | `homelab_docs` added to `RESULT_CAPTURING_TOOLS` | **caught** — 5 failures |
| M31 | every non-opted-in tool captures a 40-character "preview" of its result | **caught** — 7 failures; but 6.4's address test and the `6h` parameter case both **survived**, because both values sit past character 40 |
| M31b | the same mutation, after 6.4 gained a body-level assertion | **caught** — 8 failures, 6.4 among them |

M31 is the §6 entry worth carrying forward, and it is §4's M20/M24/M28 lesson in
a new costume: an assertion naming **one value** is only as strong as that
value's position in the payload. A partial capture is a plausible defect — "just
log a preview" — and it defeated the narrowest test in the file while eight
others caught it.
