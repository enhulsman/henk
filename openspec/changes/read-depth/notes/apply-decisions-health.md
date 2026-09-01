# Apply decisions — §10, the `homelab_health` amendment

Separate from `apply-decisions.md` (which records §3/§4) because §10 was applied in its own
worktree alongside §6/§7. Everything here concerns `henk/tools/homelab_health.py` and the two
small shared-primitive edits it required.

---

## Rollback of this half is a CODE REVERT, not a config flip (task 10.5)

This is the one fact the migration plan singles out, and it is deliberately true rather than
incidental:

- The corpus half rolls back by setting a config key to false, and its bind mount can be
  removed independently.
- **This half has no key to flip.** The tool's three threshold constructor parameters
  (`memory_threshold_pct`, `disk_threshold_pct`, `load_threshold`) are **removed**, not
  defaulted differently, and no configuration path ever reached them —
  `henk/tools/__init__.py` constructed the tool with `client`, `gatus_url`,
  `prometheus_url`, `timeout` only, so the constructor defaults *were* the deployed values.
- Reverting therefore means reverting the commit (`henk/tools/homelab_health.py` plus the
  `Threshold.crossed_by` / `Threshold.describe` additions in `query_registry.py` and the
  two-line `_threshold_line` simplification in `query_renderers.py`) and redeploying.

Keeping an override parameter "just for rollback" was considered and **rejected**: a
per-instance override is exactly the mechanism by which the deployed 90 drifted away from the
live 75 in the first place, and a bar that can be overridden per instance re-opens the
"cannot disagree" hole the spec closes. `test_no_threshold_can_be_supplied_at_construction`
asserts the absence.

## What the owner will see change on Signal

The tool is the owner's daily driver, so the delta is stated in the terms he reads:

| before | after | why |
|---|---|---|
| `rp5:9100: mem 52%, disk 45%, load 1.10` | `rp5: mem 52% used, disk 55% free, load 1.10` | node named by enum, disk polarity is now the rule's own |
| memory judged against **90 % used** | judged against **75 % used** (`High memory usage`) | 90 was an unsourced constant; 75 is the bar that actually delivers |
| disk judged against **90 % used** | judged against **15 % free on `/`** (`HenkDiskPressure`) | the rule is `avail/size*100 < 15`, not "85 % used", and not across all filesystems |
| load judged against **8.0** | reported with **no verdict**, plus an explicit "no bar" line | no rule defines a load bar in **either** alerting system (all 18 Grafana + all 23 native rules checked, record 1.5) |
| no provenance | a closing `Bars, pinned from the live rules: …` line naming each rule | the numbers are now traceable from the Signal message itself |

**Against the fleet's live readings (probe 1.5), no verdict actually flips today:**

- memory 7-day fleet maximum **58.11 %** — well under both the retired 90 and the new 75. So
  the memory change is a correctness fix, not new alert noise.
- disk now: rp5 54.71 % free, vps 30.24 % free, rp2 41.03 % free — all far above the 15 %
  floor. Under the old reading these were 45/70/59 % used against 90, also clear. Same verdict,
  correct arithmetic.
- load now: 1.10 / 1.14 / 0.70 — under the retired 8.0 anyway. The change here is that a future
  load spike will no longer be called DEGRADED against a bar nothing alerts on.
- `swap_used` is **not** reported by this tool at all (it never was), so the vps's chronic
  77 %-average / **89.7 %** 7-day-max fullness does not reach `homelab_health` and cannot be
  mistaken for an incident here. It is `node_resource_trend`'s to report, already labelled
  not-the-trigger.

Net: the owner should see the same healthy/DEGRADED verdicts he saw yesterday, with different
wording, an honest disk polarity, and provenance attached. The first real difference will come
the day memory sits between 75 and 90 — which today's data says is not imminent.

## How "the two tools cannot disagree" is made structural, not asserted

Three couplings, each replacing a hand-written copy:

1. **The bar itself.** `homelab_health.THRESHOLDS` holds the *same objects* as
   `QUERY_REGISTRY["node_resource_trend"].thresholds`, selected by resource key.
   `test_both_tools_read_the_same_threshold_object_not_a_copy` asserts **identity** (`is`),
   not equality — two equal copies can be edited apart, which is the whole history here.
2. **The predicate.** `Threshold.crossed_by(reading)` was added to `query_registry.py` and is
   now the single place `>` / `<` is written around a bar. The renderer's `_threshold_line`
   calls it; `homelab_health._crossings` calls it. Before this, the renderer had the only
   copy and a second one in the health tool would have had to re-derive that disk is a
   *floor*.
3. **The measurement.** `homelab_health.QUERIES` is not a second set of PromQL strings: each is
   `node_resource_trend`'s own expression with `job="<job>"` replaced by
   `job=~"node-exporter.*"`. A drifting expression is how "disk 90 % used" came to be compared
   against a 15 %-free rule, so the expression is derived, and a test asserts the derivation
   rather than the resulting string.

The regex-over-jobs selector is address-free and is the same form D5 already uses to derive
`dns_performance`'s node mapping, so it introduces no new commit-time exposure.

## Decisions made alone (no one available to ask)

1. **Load keeps its figure and loses its verdict.** The spec pins "no bar" for cpu/load/
   temperature; it does not say whether `homelab_health` should keep reporting load at all.
   Dropping it would remove information the owner has had for months; keeping a bar would
   guarantee the disagreement the spec forbids (the query renders load with "No bar: no alert
   rule … defines a threshold" while the health tool would call 8.1 DEGRADED). So: report the
   number, print an explicit no-bar line, never raise DEGRADED on it. Chosen because it is the
   reading under which the two tools cannot disagree — the tie-break the task set.
   The no-bar line is **computed** from `resource not in THRESHOLDS`, so if a load rule is ever
   added to the registry the health tool picks it up with no further edit.
2. **`is_trigger=False` bars would be reported but never raise DEGRADED.** No such bar is in
   `homelab_health`'s three resources today (`swap_used` is the only one and this tool does not
   report swap), so this is defensive. It encodes D5's rule: a bar documented as not what the
   rule fires on must not be rendered as an approaching incident.
3. **DEGRADED lines now keep the figures.** Previously a crossing replaced the node's numbers
   with the problem string; now the line reads `rp5: mem 95% used, disk 55% free, load 1.10 —
   DEGRADED: memory 95.00 percent used crossed the 75.00 bar (High memory usage)`. Losing the
   other two figures at exactly the moment the owner is triaging was a real defect, and 10.4
   licenses the output-shape change.
4. **Endpoints are named by Gatus's own `key`.** Probe 1.1 pins `key` as a field of every bulk
   status element, and `endpoint_history` takes that key as its argument — so naming endpoints
   this way lets the owner carry a name straight from one tool into the other, and two
   endpoints sharing a `name` across groups stay distinct. Falls back to
   `compose_gatus_key(group, name)`, then to the bare name.
5. **The "source unreachable" error string is scrubbed.** Found while reading, not specified:
   httpx's `HTTPStatusError` quotes the request URL, and the deployed `gatus.base_url` /
   `prometheus.base_url` **are tailnet addresses** — so `Gatus: source unreachable (Client
   error '500 …' for url 'http://<addr>:8080/…')` was a live address path out of this tool
   whenever a backend returned a non-2xx. `_error_text` runs `scrub_addresses` over it. This is
   the projection rule applied to backend free text, exactly as the renderers already do for
   Prometheus's `lastError`, and it is a **runtime-hygiene fix, not a commit-time one** — no
   address was ever in the repo.
6. **A sample with no `job` label is named `unidentified target`, never by `instance`.** The
   old code's fallback chain ended at the `instance` label; the new one must not, or a scrape
   config missing a job name would reintroduce the leak silently.
7. **Multiplicity is not handled here.** Record 1.3 measures exactly one series per job across
   all seven jobs, and a second series on one job would overwrite silently in this tool's dict
   (as it did before, keyed by instance). `node_resource_trend` handles the case explicitly;
   `homelab_health` does not, and that asymmetry is accepted rather than papered over — adding
   a multiplicity branch to a fleet-wide summary is out of §10's scope. Recorded so it is not
   later mistaken for an oversight.
8. **`swap` and `temperature` stay out of `homelab_health`.** They were never in it; adding
   them would widen a daily-driver summary that the spec only asked to make consistent.
   `node_resource_trend` is where they live.

## Mutations run (all three killed; each reverted)

| # | mutation | outcome |
|---|---|---|
| 1 | Replace the registry-sourced memory bar with a local `Threshold(90.0, …, "hardcoded")` | **killed** — 4 tests, including the identity test and the 80 %-reading discriminator |
| 2 | Name nodes by `sample["metric"]["instance"]` again | **killed** — 7 tests, including both no-address tests and the no-`job`-label fallback |
| 3 | Make `Threshold.crossed_by` always compare `>`, dropping the `below` branch | **killed** — 4 tests, both sides of the disk polarity **and** the trend renderer's own disk rendering via the cross-tool test |

Mutation 3 is the informative one: `tests/test_query_renderers.py` passed unchanged under it.
§4's renderer tests cover the memory (`above`) bar but never render a *disk* comparison, so the
`below` branch had no behavioural test before §10. The cross-tool test added here
(`test_a_reading_either_side_of_the_bar_agrees_across_both_tools[disk]`) drives the shipping
renderer directly and closes that gap from this side, without editing §4's test file.

## Test counts

- Before (branch point `512767f`): **1828 passed, 12 deselected**
- After: **1843 passed, 12 deselected** — +15, all in
  `tests/test_tools_homelab_health.py` (7 tests before, 22 collected after).
- `openspec validate --all` → 16/16, unchanged.

## Files touched

- `henk/tools/homelab_health.py` — rewritten per above.
- `henk/tools/query_registry.py` — `Threshold.crossed_by` and `Threshold.describe` added.
  No threshold value, expression, domain or entry changed.
- `henk/tools/query_renderers.py` — `_threshold_line` now calls `crossed_by`; behaviour
  identical (the previous inline expression computed the same predicate).
- `tests/test_tools_homelab_health.py` — amended; every existing scenario kept and updated
  rather than deleted, and the one test that encoded a threshold literal (`95 %` "beyond the
  90 % threshold") now reads the bar from the registry.

`tasks.md` deliberately **not** edited — 10.1-10.5 are ticked by the coordinating session, to
keep the file conflict-free while §6/§7 land in parallel.

## Publication-safety check on this file

No tailnet address, no ACL node name, no credential, no real hostname beyond the
already-public `rp5`/`vps`/`rp2` enum values. The only addresses named are the
`10.0.0.x` placeholders used in the test fixtures.
