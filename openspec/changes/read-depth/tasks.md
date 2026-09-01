> **TDD is not optional here.** Every group writes its tests from the spec scenarios
> *before* its implementation, and each spec scenario maps to at least one test. Where a
> group's tests pass on first run, mutate the implementation to confirm the tests actually
> bind — a green suite that survives a deliberate defect is not evidence.
>
> **Two standing rules for this change specifically.**
> 1. **Every numeric and every name in the spec deltas carries an `APPLY-RESOLVED:<slug>`
>    marker until §1 pins it from a live probe.** Three transcribe-from-prose defects were
>    caught during review, two of them inside fixes written to prevent exactly that. Task
>    9.7 gates archive on zero remaining markers.
> 2. **`notes/*.md` in this change are publication-gated artifacts.** Record metric names,
>    job names, label *names*, and value *shapes* — never raw Prometheus or Gatus response
>    bodies, which carry `instance` labels and scrape URLs, and never ACL node names.

## 1. Pin the backend facts the registry depends on

Probing first prevents shipping a query that returns empty and looks healthy.

- [ ] 1.1 Probe the deployed Gatus for its per-endpoint status route **and its uptime-window vocabulary**; resolve `APPLY-RESOLVED:gatus-window`. Record the working route, or that only the bulk `/api/v1/endpoints/statuses` route exists and the in-process filter fallback is required
- [ ] 1.2 Enumerate `/api/v1/label/__name__/values` for the `homelab_backup_*`, `homelab_dump_*`, `health_etl_*`, and `obsidian_backup_verify_*` families; record the exact metric names `freshness_check` will select
- [ ] 1.3 Confirm the `job` label values for **all six** jobs (three node-exporter, two cadvisor, one adguard-exporter) against the live Prometheus, and confirm which are expected to yield exactly one series
- [ ] 1.4 **Enumerate all 23 native Prometheus rules** and map each to the query that measures its input, or record it as an accepted gap. Coverage asserted from an un-enumerated rule set is not evidence — this is the only artifact that would catch a rule family nobody has considered
- [ ] 1.5 Pin every threshold: enumerate each Henk-folder Grafana rule's **live expression and threshold** from the provisioning artifact, resolving `APPLY-RESOLVED:memory-bar`. Record alongside 1.4's mapping
- [ ] 1.6 Pin `dns_performance`: the exact metric name, the `server` label's value set, the derived node↔series mapping, and **measured** per-device baselines resolving `APPLY-RESOLVED:dns-baselines`. Confirm the 1:1 derivation from `up{job=~"node-exporter.*"}` still holds. The docs' recorded baselines are six months stale and MUST NOT be transcribed
- [ ] 1.7 Write 1.1–1.6's findings into `notes/backend-probe.md` — **shapes and names only**, per the standing rule above. A later reviewer must be able to check each registry value against this record

## 2. Config surface

- [x] 2.1 Write config tests first: every new key resolves to its safe default when absent from the config mapping, exercised through `Config.from_dict` with an empty section. The real trap is a `from_dict` builder that never reads the key at all — assert against that, not against dataclass attributes
- [x] 2.2 Add a test asserting the corpus tool defaults to disabled and the query tool defaults to enabled, so a later edit cannot silently flip either
- [x] 2.3 Add config for: query enable flag, `query_range_max_points` (default 60), corpus enable flag, corpus directory path, **corpus path allowlist**, stamp staleness bound (default 26h), read byte budget, search result count. State a default for every one. **Reuse** the existing `endpoints.gatus` / `endpoints.prometheus` timeouts — do not add a timeout key
- [x] 2.4 Follow the in-repo pattern for defaults rather than duplicating literals across the dataclass and the builder
- [x] 2.5 Validate the staleness bound, byte budget, and result count are positive; reject non-positive values at load time with a named error
- [x] 2.6 Assert **no config key holds an address** — `dns_performance`'s mapping is derived, not configured

## 3. The query registry as reviewable data

- [x] 3.1 Write registry-shape tests first: every entry declares backend, template, parameter domains, and renderer; the `query_name` set is exactly the six names and contains **no** rule-state query
- [x] 3.2 Write the publication-safety test: **no registry template contains a tailnet address, an `instance` selector, or a `server` selector** — every template, `dns_performance` included. Add a second assertion that no *rendered result* contains an `instance`, `scrapeUrl`, or `server` value
- [x] 3.3 Write a test comparing every declared parameter domain against the spec's literals, so the code and the binding spec cannot drift
- [x] 3.4 Write per-query domain tests: `container_state` **rejects `rp2` with an error, not an empty result**; `node_resource_trend` accepts it; every out-of-domain `resource`/`window` is refused
- [x] 3.5 Write the three-outcome test: in-domain-and-available, out-of-domain (refused), and **in-domain-but-not-derivable** each produce a distinct, distinguishable result
- [x] 3.6 Write tests asserting an unregistered `query_name` and every out-of-domain value are refused with **no HTTP request issued** — assert on the transport, not the return value
- [x] 3.7 Write the boundary test: validation holds when a value reaches the tool's execution path **bypassing any schema check**, since the schema layer's enforcement is unverified in this codebase. Adopt the same-object enum-and-dispatch idiom so advertised and enforced domains cannot diverge
- [x] 3.8 Write a test that every registry threshold matches `notes/backend-probe.md`, and that no threshold exists in the registry absent from that record
- [x] 3.9 Implement the registry, domain validator, and dispatch to satisfy 3.1–3.8

## 4. `homelab_query` — the six named queries

- [ ] 4.1 Write projection tests first. **Fixtures must use placeholder addresses only** (`10.0.0.1`-style, per the existing in-repo convention) — this is the task whose fixtures contain address-shaped data by construction. Assert `instance`, `scrapeUrl`, and `server` values are absent from rendered output and the enum name appears instead
- [ ] 4.2 Write the series-multiplicity test: a job returning two series is *reported as such*, never rendered as one figure
- [ ] 4.3 Write `node_resource_trend` tests: seven resources, four windows, summary-only output at every window, point count within `query_range_max_points`, and the **per-resource** comparison rules — disk scoped to `/` as percent-free; `swap_io` primary; `swap_used` labelled not-the-trigger and **not alarming within the fleet's chronic range**; cpu/load/temperature carrying no bar; temperature noting no alert exists in either system
- [ ] 4.4 Write `scrape_targets` tests: **bare `up` enumerating all six targets with values** so a healthy fleet is distinguishable from a broken query; `lastError` surfaced for down targets; a target never up within the window reported as "down for longer than the window" with **no invented duration**
- [ ] 4.5 Write `endpoint_history` tests: discovered keys accepted; undiscovered refused; **first-use discovery with no network call during construction**; a rename becoming queryable via refresh-on-miss **without a restart**; discovery failure failing closed per-call; and an unsafe key percent-encoded or rejected
- [ ] 4.6 Write the event→argument tests: an arriving Gatus event resolves to a queryable endpoint argument without the agent constructing a key by guesswork; an unresolvable event name is reported rather than queried as an invented key
- [ ] 4.7 Write `freshness_check` tests: raw timestamp beside the derived age; a frozen writer surfacing as a **growing** age across invocations
- [ ] 4.8 Write `container_state` tests: creation-time labelling; the restart-loop-invisibility statement; **the omission statement** (an absent container may be stopped or removed); and a named container's absence returning a reading rather than no series
- [ ] 4.9 Write `dns_performance` tests: the derived node↔series mapping; no address in any result; an underivable node reported rather than returned empty; comparison against the **measured** baselines
- [ ] 4.10 Write backend-failure tests inheriting the existing honest-failure requirement: timeout and non-2xx produce an explicit error naming backend and cause, never fabricated data or partial-presented-as-whole
- [ ] 4.11 Implement the six queries and their renderers against 4.1–4.10

## 5. `homelab_docs` — corpus retrieval

- [ ] 5.1 Build a corpus fixture mirroring the real shape — nested directories, frontmatter, a >50 KB document, deep heading hierarchies, repository/build files that must not be indexed, and **a symlink pointing outside the docs subpath**. Placeholder addresses only
- [ ] 5.2 Write sectioniser tests: heading-boundary splitting, heading-path construction, frontmatter excluded, a >50 KB document yielding sections rather than itself
- [ ] 5.3 Write section-id tests: `read` accepts only indexed ids; unknown and traversal-shaped values both refused; **assert no filesystem join ever occurs on the supplied value**
- [ ] 5.4 Write id-stability tests: a rebuild after a corpus change **preserves ids for unchanged heading paths**, so a search-then-read across an update cannot silently return different content
- [ ] 5.5 Write index-scope tests: only documentation files by extension under the docs subpath are indexed; repository metadata and build config are not; **the symlink yields no index entry**
- [ ] 5.6 Write allowlist tests: **filtering happens at index build** — a non-allowlisted file contributes **no search candidate and no snippet**, not merely an unreadable id; empty/unset allowlist surfaces nothing; entries empty after normalization discarded; entries resolved relative to the **docs root**; composition is glob-then-allowlist
- [ ] 5.7 Write search tests: ranking over section text plus heading-path boost, result count honoured, and a regex-metacharacter query matched literally with no pattern compilation
- [ ] 5.8 Write truncation tests: an oversized section truncated **and saying so, naming the section**; a section within budget returned whole with no notice
- [ ] 5.9 Write freshness tests: fresh stamp reported; past the bound serves content with an explicit staleness marker; missing and unparseable stamps both yield "freshness unknown"; **an old commit with a recent pull is NOT marked stale** (the bound applies to last-pull only)
- [ ] 5.10 Write the silent-pull-failure test: advancing time past the bound without touching the corpus makes results self-identify as stale
- [ ] 5.11 Write index-invalidation tests: a changed last-pull value rebuilds the index and serves new content; an unchanged stamp reuses it
- [ ] 5.12 Write availability tests: config error (enabled, no path) fails startup; **missing/empty/unreadable/unstamped corpus registers the tool and returns an explicit per-call error naming path and condition**; runtime loss behaves identically to startup absence; an empty corpus is distinguishable from a search with no match; a mid-operation unreadable file errors rather than returning partial content
- [ ] 5.13 Write the two-gates test: corpus-unavailable and allowlist-empty produce **distinct** diagnostics
- [ ] 5.14 Implement the sectioniser, index, allowlist, keyword ranker, stamp reader, and both actions against 5.2–5.13

## 6. Audit assertions (not implementation — the mechanism already exists)

Result capture is already global and default-deny; only the handoff tool opts in. These
tasks **assert** that property, they do not build it. Do not add redaction code, and do not
mutation-test its removal — removing it would break every other tool.

- [ ] 6.1 Write a test asserting **neither** new tool is in the result-capturing set
- [ ] 6.2 Write a test asserting no audit record for a `homelab_query` session contains any substring of the rendered result body, and that bound parameter values are absent
- [ ] 6.3 Write a test asserting no audit record contains corpus section text or search snippets, and that the search query string itself is absent
- [ ] 6.4 Write a targeted test using a fixture section containing a **placeholder** address, asserting that address appears in no audit field

## 7. Registration and startup

- [ ] 7.1 Write startup tests per 5.12's split: config error kills startup; host state does not
- [ ] 7.2 Write a toolset test asserting both tools register with read-only class, and that **no network call occurs during runtime construction** (discovery is first-use)
- [ ] 7.3 Implement registration and the startup config check
- [ ] 7.4 Run the full suite. Note the 1503-passed baseline is **invalidated** by the `homelab_health` amendment in §10 — record the new baseline and account for the delta rather than expecting 1503

## 8. Host provisioning on rp5 (owner-gated)

- [ ] 8.1 Mint a repo-scoped read-only deploy key for the docs repo on its own GitHub account; confirm passphrase-free and host-held
- [ ] 8.2 Clone to the root-owned service directory `/opt/homelab-docs/`, deliberately **not** the existing personal checkout
- [ ] 8.3 Commit `deploy/homelab-docs-stamp.sh` to **this repo** with unit tests covering the success and failure branches — a writer that stamps on every *attempt* rather than every *success* inverts the freshness signal. Deploy from the committed script
- [ ] 8.4 Install the daily pull timer; confirm the stamp is written **after** content updates and that a **failed** pull leaves the last-pull value untouched so the age grows
- [ ] 8.5 Read all 17 corpus files against the Tier W wall; record the finding in `notes/`. Owner decision 2026-08-22: initial allowlist membership is **all 17 files** — record the reasoning, not just the outcome
- [ ] 8.6 Add the read-only bind mount to the compose file **with automatic host-path creation disabled**; verify a write from inside the container fails
- [ ] 8.7 Set the corpus path allowlist in rp5's hand-maintained `config.yaml`
- [ ] 8.8 Flip the corpus enable key in rp5's `config.yaml` — the hard stop, as with reminders
- [ ] 8.9 Append the 8.1/8.2/8.4 procedure to `~/.claude-config/tooling-backlog.md` as an automation candidate, with the verbatim commands as its spec. The stamp writer is excluded — it is version-controlled now

## 9. Verification and close-out

- [ ] 9.1 Verify the surface claims empirically: `tag:henk` grants before and after, container mounts enumerated, listening sockets enumerated with both tools enabled. **Record findings, not raw output** — ACL records name tailnet nodes
- [ ] 9.2 Audit the deployed container for any docs credential; confirm the enumerated secret set is unchanged
- [ ] 9.3 Verify the deploy-time guard: with the host path absent, the container **fails to start** rather than the runtime creating an empty directory
- [ ] 9.4 Exercise each of the six queries live; record **shapes and conclusions** in `notes/apply-enumerations.md`, flagging any empty result and why. No raw response bodies
- [ ] 9.5 Exercise the corpus tool live: a search, a read, and a deliberately stale stamp, confirming the marker reaches a real Signal reply
- [ ] 9.6 Confirm no corpus file or excerpt is tracked here and none is in the built image; let `.githooks/pre-commit` run on every commit. Run the hook over `notes/*.md` deliberately before committing them
- [ ] 9.7 **Archive gate**: replace every `APPLY-RESOLVED:` placeholder in the spec deltas with its pinned value from `notes/backend-probe.md`, then assert `grep -r 'APPLY-RESOLVED' openspec/changes/read-depth/specs/` returns nothing. Two markers exist today — `gatus-window` and `memory-bar`. Also update `design.md`'s D5 table and Open Questions with the same pinned values, so the design record does not keep asserting an unknown that has since been measured (`design.md` is excluded from the grep assertion only because `tasks.md` names the token literally)
- [ ] 9.8 Update the README tools table; write the `homelab-docs` capability Purpose; run `/docs-update` for the rp5 clone, timer, and mount, and **push the apply-time DNS measurements back into `services/monitoring.md`**, whose baselines are six months stale. Present the doc diff before committing
- [ ] 9.9 Re-grep `owner-acknowledgement/proposal.md`'s cited line numbers and correct drift from the new config keys
- [ ] 9.10 `openspec validate --all`, then `/opsx:archive`

## 10. `homelab_health` amendment (user-visible behaviour change)

Sequenced last among the code groups because it depends on §1's pinned record, but it ships
with the query half.

- [ ] 10.1 Write tests first: `homelab_health` and `node_resource_trend` evaluate the same node and resource against the **same** threshold value, so one cannot call a measurement healthy while the other reports a crossing
- [ ] 10.2 Write a test asserting no raw `instance` value appears in `homelab_health`'s output
- [ ] 10.3 Replace the hardcoded 90/90/8.0 constants with the pinned record's values; apply the projection rule to its renderer
- [ ] 10.4 Update the existing `homelab_health` tests that assert the old thresholds and output shape
- [ ] 10.5 Record in the apply notes that rollback of this half is a **code revert**, not a config flip
