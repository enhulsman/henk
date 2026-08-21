## Why

Henk can say *that* something is wrong but not *how* wrong, *since when*, or *what the
box is supposed to look like*. `homelab_health` returns one fixed summary — endpoint
up/down plus current memory, disk, and load — so when a `henk-events` alert lands, the
follow-up questions the owner actually asks ("is it still climbing?", "how long has that
target been down?", "which container?", "what's the documented recovery step?") have no
tool behind them. The North Star's promise that Henk "investigates with real depth" is
the one clause the current toolset cannot honour.

This is roadmap item 3 of 5, and it is now the cheapest of the remaining items: the
query half needs no new network grant (`tag:henk` already reaches rp5:8080 and vps:9090
for `homelab_health`), and the corpus half's delivery and auth were settled by the owner
on 2026-08-21 — a host-side git clone, pulled on a timer, bind-mounted read-only.

## What Changes

- **A named-query registry** — one `homelab_query` tool (class: read-only) whose
  `query_name` argument is a **closed enum** selecting a fixed, owner-reviewed
  PromQL or Gatus template. Six names in v1, each measuring a condition no other
  entry measures:

  | `query_name` | Bound parameters | Answers |
  |---|---|---|
  | `node_resource_trend` | `node`, `resource`, `window` | is it climbing, flat, or recovering |
  | `scrape_targets` | — | which exporters are down, since when, and why |
  | `endpoint_history` | `endpoint`, `window` | one Gatus endpoint's failures, uptime, latency, failing condition |
  | `freshness_check` | — | backup / dump / ETL pipeline ages from their raw-mtime metrics |
  | `container_state` | `node` | per-container last-seen, health, OOM events, creation time |
  | `dns_performance` | `node`, `window` | AdGuard resolution latency against its measured baseline |

  **The admission criterion, applied uniformly:** a query earns a slot if it *measures a
  condition* the owner asks about that no other query in the set measures. Delivery
  destination is irrelevant — whether an alert reaches Henk, Discord, or nobody says
  nothing about whether the underlying question is worth answering.

- **No free-text query path, ever.** The tool SHALL NOT accept a PromQL string, a metric
  name, a label selector, or a Gatus endpoint key as free text. Every parameter is
  enum-bound and validated in-process before a request is built, so an argument the model
  invents cannot widen what is queried. This is the action axis of the North Star
  permission model applied one level down.

- **Depth comes from measuring the rule's input, not from reading another system's
  conclusion.** Alerts already reach Henk by push, with firing-versus-resolved state, so
  there is nothing to poll for. Every rule on the `henk-events` feed — Grafana-side and
  Gatus-side — has a query above that reads the same input the rule reads.

- **A homelab-docs corpus tool** — `homelab_docs` (class: read-only) over the 17-file
  documentation corpus, mounted read-only from a host-side clone. Section-level
  retrieval, because two files exceed 50 KB and whole-file reads are unusable as tool
  results.

- **A default-deny path allowlist over the corpus**, authoritative in Henk's own process
  and applied **at index build** so a denied file contributes no search candidate either.
  The corpus mixes personal and work/Anamata content, which the existing `homelab-tools`
  requirement already covers; this change complies with it rather than claiming an
  exemption. Initial membership is **all 17 files** (owner decision, 2026-08-22): none of
  the content is a credential, and the security-model files are among the most useful for
  incident follow-up.

- **Every corpus result carries a freshness stamp** (commit + timestamp, written by the
  host updater). A stamp that is missing, unparseable, or older than a configured bound
  makes the result say so. An auto-pull that silently stops — rotated key, removed timer,
  local modification — is otherwise indistinguishable from fresh docs.

- **Tool result text stays out of the audit log.** This is already a global, default-deny
  property of the audit path rather than something each tool implements; this change
  asserts that neither new tool opts into result capture. Bound parameters are
  deliberately *not* logged either: a corpus search query is model-authored free text that
  can quote owner-personal content, which the audit log's existing free-text rule excludes.

- Not in scope, recorded so their absence does not later read as an oversight: **no
  mutations** (roadmap item 5); **no Grafana API access** (needs a vps:3000 grant and a
  credential); and **no `alerts_firing` query** — it reads rule state rather than
  measuring anything, and every condition it could report is either covered by a direct
  query above or already delivered to the owner on Discord.

## Capabilities

### New Capabilities
- `homelab-docs`: the documentation-corpus tool — a read-only, host-delivered corpus with
  section-level retrieval, a default-deny path allowlist applied at index build, an
  explicit freshness contract (stale is reported, never hidden), and honest runtime
  failure when the corpus becomes unavailable.

### Modified Capabilities
- `homelab-tools`: gains the named-query registry — the closed `query_name` enum,
  enum-bound parameters with per-query domains, no free-text query path, per-query
  boundary declarations, and job-label selection with address-free projection. Also
  **amends the existing `homelab_health` requirement**, because this change would
  otherwise create a second, conflicting threshold source.
- `secure-deployment`: gains the corpus mount as an enumerated surface — one read-only
  bind mount with `create_host_path: false`, no new published port, no listening socket,
  no ACL/egress grant, and no new secret inside the container.
- `sensor-routing`: marks its container-restart scenario **known-unsatisfied**. The
  homelab's own measurements establish that the rule behind it is structurally incapable
  of firing; the North Star requires such a contradiction to be amended deliberately
  rather than absorbed silently into a tool's disclaimer.

## Impact

- **Code**: two new tools under `henk/tools/`; the query-template registry as reviewable
  data rather than scattered strings; tool registration in the agent's toolset; and
  changes to `henk/tools/homelab_health.py` for the amended requirement.
- **User-visible behaviour change**: `homelab_health` is the owner's daily driver. Its
  thresholds move to the pinned measured record and its output stops rendering raw
  `instance` labels. This is a deliberate behaviour change, and its rollback is a **code
  revert**, not a config flip — the two halves of this change are not symmetrically
  rollback-able.
- **Config**: new keys for the corpus mount path, the path allowlist, the stamp staleness
  bound, the read byte budget, the search result count, and `query_range_max_points`. Each
  must default safely through `Config.from_dict`, since rp5's `config.yaml` is deliberately
  skip-worktree'd and will not carry new keys. Backend timeouts **reuse** the existing
  `endpoints.gatus` / `endpoints.prometheus` values rather than adding a key. No key is
  needed for DNS node identification — that mapping is derived at runtime from job labels.
- **Deployment (rp5, manual provisioning)**: a repo-scoped read-only deploy key, a fresh
  clone at a root-owned service directory, a daily pull timer, and the stamp writer.
  Recorded in `~/.claude-config/tooling-backlog.md` at apply time as an automation
  candidate; the stamp writer itself is committed to this repo and deployed from it, so
  its success/failure branch is unit-tested rather than hand-checked once.
- **Publication safety**: the corpus is **never** vendored into this repo — 12 of 17 files
  carry tailnet addresses and `.githooks/pre-commit` blocks that pattern in added lines. It
  arrives at runtime. Additionally, **this change's `notes/*.md` are publication-gated
  artifacts**: probe records capture metric names, job names, and value *shapes*, never raw
  Prometheus or Gatus response bodies, which carry `instance` labels and scrape URLs.
- **Docs**: README tools table; `docs-update` for the rp5 clone, timer, and mount; and a
  correction pushing apply-time DNS measurements back into `services/monitoring.md`, whose
  recorded baselines are six months stale.
- **Known collateral**: new config keys shift the line numbers cited in
  `openspec/changes/owner-acknowledgement/proposal.md`'s findings. Expected; re-grep later.
