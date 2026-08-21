## Context

`homelab_health` answers one fixed question and answers it well: are the Gatus endpoints
up, and what are the three headline node figures right now. It deliberately holds no
parameters, which is what makes it trivially reviewable — and also what makes every
follow-up question unanswerable. When a `henk-events` alert arrives, Henk can restate it
and nothing more.

Three facts shape everything below.

**The query half is already inside the network boundary.** `tag:henk` has egress to
rp5:8080 (Gatus) and vps:9090 (Prometheus) because `homelab_health` uses both. Named
queries ride those existing grants: no ACL change, no new secret, no new port.

**The corpus half's delivery was settled by the owner on 2026-08-21 and is not
re-opened here.** A dedicated clone at a root-owned service directory on rp5
(`/opt/homelab-docs/`, matching `/opt/gatus`, `/opt/monitoring`, `/opt/dawarich`), pulled
on a host timer with a repo-scoped read-only deploy key held by the host, bind-mounted
read-only into the container. Henk makes no network call for docs and holds no key for
them. The alternatives are both worse: the docs repo sits under a different GitHub
account than Henk's, and the published docs site sits behind Cloudflare Access, which a
container cannot authenticate through. Vendoring the
corpus into this repo is not merely undesirable — 12 of 17 files carry tailnet addresses
and `.githooks/pre-commit` blocks that pattern in added lines, so the publication gate
would refuse the commit.

**Read depth is the first change where Tier W actually bites.** Henk reads no work data
today: `taiga_read` is deliberately unregistered pending its project-id filter, and
`todo_read`'s allowlist is scoped to personal note paths. The documentation corpus, with
its 25 Anamata references, is the first place work metadata would enter Henk at all —
which is why D13 exists.

Measured corpus shape, because it decides the retrieval design: **17 files, 306 KB**, with
`services/monitoring.md` at 55.6 KB and `services/applications.md` at 52.6 KB. A
whole-file read of either is ~14k tokens — unusable as a tool result.

## Goals / Non-Goals

**Goals:**

- Henk can answer *since when*, *how much*, and *which one* about anything the
  `henk-events` feed delivers, without a raw query language and without SSH.
- Authorization remains a property of a **named** action: the enumerated `query_name`,
  not "access to Prometheus".
- The documentation corpus is readable at section granularity, behind a default-deny
  allowlist, and a result whose freshness cannot be vouched for says so in the result.
- Zero new infrastructure surface: no published port, no listening socket, no ACL or
  egress grant, no secret added to the container.
- No tailnet address, phone number, or account identifier enters this repo — including
  inside the query registry, inside deployed config, and inside this change's own
  `notes/` records, which are the parts most tempted to hold one.

**Non-Goals:**

- **Any mutation.** Read depth is read-only end to end; the runbook-actions verb
  registry is roadmap item 5 and its approval machinery is already built and idle.
- **Grafana's API.** It would need a vps:3000 egress grant and a credential, and it buys
  a boolean Henk can already derive — see D4.
- **Reading rule state at all.** No `alerts_firing`; see D4's criterion.
- **Alertmanager, or consolidating the two alerting brains.** Out of scope; the
  redundancy map in the homelab docs is the factual basis if that is ever taken up.
- **Embeddings or vector search.** 306 KB of owner-authored prose does not need them,
  and they would add a model call or a dependency to a tool whose whole appeal is that
  it is a local file read.
- **Free-text PromQL, metric names, label selectors, or filesystem paths** as tool
  arguments, under any framing.
- **Per-name audit identity.** Deferred to roadmap 5 — see D10.

## Decisions

### D1 — One tool with a closed `query_name` enum, not one tool per question

`homelab_query(query_name, …)` where `query_name` selects a fixed template from a
registry. Rejected alternatives:

- **One tool per question.** Maximally reviewable — each tool is a constant string — but
  six tools now and a linearly growing registry later, all near-duplicates of each
  other's HTTP and error handling.
- **Free PromQL behind a validator.** Rejected outright: an allowlist expressed as a
  grammar or regex over a query language is a parser to be defeated, and the North Star
  is explicit that the unit of authorization is the named action.

The enum gives the reviewability of the first option — the registry is a table an owner
reads in one sitting — with one code path.

### D2 — Parameters are enum-bound, and their domains are per-query, not global

Every parameter is validated against the registry entry's own domain **in-process**
before a request is constructed (see D7 on why the JSON schema is not the boundary). A
value outside the domain is a tool error, never a narrowed or best-effort query.

Domains are per-query because the fleet is not uniform: `node_resource_trend` accepts
`{rp5, vps, rp2}`, but `container_state` accepts only `{rp5, vps}` — **rp2 runs no
cadvisor**. A single global `node` enum would accept `container_state(node=rp2)` and
return an empty result, which reads exactly like "no containers" rather than "not
measured here".

Three distinct outcomes must be distinguishable, not two: **in domain and available**,
**out of domain** (refused), and **in domain but not currently derivable** — the last
arising when `dns_performance`'s node mapping cannot be derived because that node's
node-exporter is not reporting. Each gets its own message.

### D3 — Templates select by job label; results are projected

This is the decision that keeps the repo publication-safe. Prometheus's `instance`
labels are `<tailnet-address>:9100` — committing the registry with instance selectors
would put three tailnet addresses in this repo and the pre-commit hook would reject it.
The scrape config names its jobs, and those names carry no addresses. **Six jobs**, not
five:

| enum value | node-exporter job | cadvisor job |
|---|---|---|
| `rp5` | `node-exporter-pi5` | `cadvisor-pi5` |
| `vps` | `node-exporter-vps` | `cadvisor-vps` |
| `rp2` | `node-exporter-pi2` | — |

plus **`adguard-exporter`**, a single job on rp5 that scrapes all three AdGuard instances
and backs `dns_performance`. It has no node of its own in the enum, which is why
`scrape_targets` cannot render node names from a parameter domain (see below).

The registry therefore stores job names, and rp5's `config.yaml` needs no address either.

**Corollary — results are projected, not echoed.** Prometheus returns `instance` on every
sample; `/api/v1/targets` returns `scrapeUrl`; the AdGuard metrics carry `server`. All
three are addresses. Results name the target by its **`job` label** plus any
distinguishing **non-address** label — `name`, `mountpoint` — and omit
`instance`/`scrapeUrl`/`server`. Note `server` is *not* a safe distinguishing label: it
is measured to be a URL containing a tailnet address (see D5's `dns_performance` entry).

Two factual corrections worth recording, because both are easy to assume wrong:
`cadvisor-vps`'s target is `cadvisor:8080` — Docker DNS on the compose network, not a
tailnet address and not port 8083. And `node-exporter-pi5` and `adguard-exporter` share
one address differing only by port, so instance-based identification is already ambiguous
today.

**Calibration on how much the runtime half buys.** `HenkInstanceDown` already carries
`identity_scope: instance`, so tailnet addresses already reach Henk's intake identity
keys. The commit-time invariant (no address in the repo) is load-bearing and absolute; the
runtime projection is hygiene and readability. Nobody should over-invest in the second.

### D4 — Depth comes from measuring the rule's input, not from reading rule state

This is the load-bearing decision, and it is easy to get backwards.

The tempting design is "Henk asks Prometheus what is firing." It does not work, and it is
also not what is wanted. It does not work because Prometheus evaluates 23 native rules and
delivers them nowhere (no Alertmanager), while **the rules that actually feed Henk are
Grafana-evaluated and do not appear in Prometheus's `ALERTS` at all**. The two sets are not
a subset relationship in either direction: 21 of 23 natives have a working Grafana twin,
but `HenkSwapPressure` — whose 2026-08-02 pressure retune was never ported back — has no
native twin whatsoever.

It is not what is wanted because Henk does not discover alerts by asking:

- **The fire is pushed to him**, on the `henk-events` topic, carrying name, labels, and
  state — the live event-intake path, and North Star principle 3.
- **The resolve is pushed to him too.** `sensor-routing`'s payload contract carries
  firing-versus-resolved state, and `event-intake` pairs a fire with its later resolve
  under one identity key. Polling rule state would be a worse copy of something Henk
  already receives.

So the depth this change buys comes from reading **the same input the rule reads** — a
measurement, not a boolean one indirection from ground truth. Every publisher on Henk's
feed is covered, including the larger one:

| Feed publisher / rule | What it evaluates | Query over the same input |
|---|---|---|
| `HenkDiskPressure` | `node_filesystem_avail_bytes` on `/` | `node_resource_trend(disk)` |
| `HenkSwapPressure` | `SwapFree`, `node_vmstat_pswp*` | `node_resource_trend(swap_used / swap_io)` |
| `HenkHealthEtl` | `health_etl_*` | `freshness_check` |
| `HenkBackupFreshness` | `homelab_backup_*`, `obsidian_*` | `freshness_check` |
| `HenkInstanceDown` | `up == 0` | `scrape_targets` |
| `HenkContainerRestarting` | `container_start_time_seconds` | `container_state` (see D6) |
| **Gatus — 9 fan-out endpoints** (5 tier-1, 4 tier-2) | per-endpoint reachability checks | `endpoint_history` |

Gatus is the *larger* publisher and belongs in this table explicitly; an earlier revision
tabled only the Grafana half.

**The admission criterion, stated once and applied uniformly: a query earns a slot if it
measures a condition the owner asks about that no other query in the set measures.**
Delivery destination is not a criterion — whether an alert reaches Henk, Discord, or
nobody says nothing about whether the question is worth answering.

**That criterion is what excludes `alerts_firing` and admits `dns_performance`**, and an
earlier revision had this exactly backwards: it excluded a DNS query because those rules
deliver to Discord, then admitted a rule-state query *because* it surfaces rules that
deliver nowhere — citing the same seven rules for opposite conclusions. Applying one
criterion resolves it. `alerts_firing` measures nothing, and its unique yield collapses on
inspection: `HighMemory`'s Grafana twin reaches Discord, and `HighCPU` is unrouted **by
deliberate decision** (15-day peak 67.8% against an 85% bar, with the docs warning against
"fixing" it). DNS, by contrast, is the fleet's largest live rule family, on a chronically
sore device, and answers a recurring owner question nothing else in the set touches.

*(Do not cite `AuditShipStale` in this argument. It is a Discord rule in a different
folder and is documented as unrelated to Henk's audit log despite its name.)*

**One capability is genuinely given up**: Henk cannot ask Grafana "do you still consider
that firing?" He can only ask the metrics "is the condition still true?" — which is the
better question and the one the owner actually asks. Closing the gap needs a vps:3000
grant plus a credential in the container.

### D5 — The six queries, their domains, and their comparisons

Parameter domains, stated here and **normatively in the spec** (the spec is the binding
record; this table is orientation):

- `node_resource_trend`: `node` ∈ {`rp5`, `vps`, `rp2`}; `resource` ∈ {`cpu`, `memory`,
  `disk`, `load`, `swap_used`, `swap_io`, `temperature`}; `window` ∈ {`15m`, `1h`, `6h`,
  `24h`}.
- `container_state`: `node` ∈ {`rp5`, `vps`}.
- `dns_performance`: `node` ∈ {`rp5`, `vps`, `rp2`}; `window` as above. The parameter is
  named `node`, matching `node_resource_trend` — two names for one domain inside a single
  closed registry is an inconsistency the model will get wrong.
- `endpoint_history`: `endpoint` discovered (D7); `window` `APPLY-RESOLVED:gatus-window`
  — Gatus's uptime vocabulary is fixed and undocumented in the homelab docs, so it
  cannot be assumed to share `node_resource_trend`'s windows.
- `scrape_targets`, `freshness_check`: no parameters. `scrape_targets`' lookback window is
  the literal `24h`.

**Range queries return a summary, never the series.** `node_resource_trend` and
`dns_performance` issue range queries but return first / last / min / max, the direction
of travel, and any threshold crossing. Step is `window / query_range_max_points` (config,
default 60), so every window costs a bounded amount to render. Prometheus retention is 15
days, so all four windows are inside it.

**Comparisons are per-resource, and every numeric is traceable to a measurement.** A
general "crossed the corresponding alert rule's threshold" rule is *wrong* here, because
the rules do not share a shape:

| resource | comparison | why |
|---|---|---|
| `disk` | scoped to `mountpoint="/"`, reported as the rule's own `< 15% free` | the rule is `avail/size*100 < 15` on `/` only — not "85% used", and not across all filesystems |
| `swap_io` | `> 50 pages/s` sustained — **the primary swap signal** | this is what `HenkSwapPressure` actually fires on |
| `swap_used` | reported, explicitly labelled **not the rule's trigger** and not alarming on this fleet | fullness and pressure are *anti-correlated* here: the vps sits chronically at 64–86% (86.3% peak) while a Pi at 6.2% fullness hit 128.7 pages/s. "84% — approaching the 95% bar" would be an alarm about a documented non-condition |
| `memory` | `APPLY-RESOLVED:memory-bar`; noted as delivering to Discord, not to Henk | the obvious 90% is `homelab_health`'s hardcoded constant, **not** a verified property of the Grafana rule |
| `cpu` | figure only, no bar | deliberately unrouted; a busy homelab CPU is rarely the incident |
| `load` | figure only, no bar | no rule exists |
| `temperature` | figure only, no bar; note rp2 has no active cooling and **no alert in either brain** | no rule exists — this is a real monitoring gap the query surfaces for free |

Thresholds are pinned from the live Grafana provisioning artifact at apply time and
recorded in `notes/backend-probe.md`, with a test asserting the registry matches that
record. Transcribing them from prose would create a second uncontrolled copy — which is
precisely the failure this design cites elsewhere (`HenkSwapPressure`'s retune was never
ported back). Two independent instances of that defect were caught *inside* this design
during review; see Risks.

**`dns_performance`'s node identification is derived, not configured.** The metric
`adguard_avg_processing_time_seconds` carries exactly `{__name__, instance, job, server}`.
`job` is identical across all three AdGuard instances, and **both** discriminators are
tailnet addresses — measured, not assumed. So a hardcoded `server` selector would breach
this design's own commit-time invariant, and a config-supplied mapping would put an
address in deployed config and drift silently. Instead: query `up{job=~"node-exporter.*"}`
— a selector naming only jobs, which the registry may legitimately hold — extract the host
from each `instance`, extract the host from each `server` URL, and match. **Verified live:
all three map 1:1 with no ambiguity and no unmatched value.** This reuses D7's
discovered-domain pattern, keeps every address out of both the repo and deployed config,
and is self-healing when an address changes. Because nothing is configured, there is no
configured value to go stale; the only residual state is a node whose node-exporter is
down, which yields D2's third outcome rather than an empty result.

### D6 — `container_state` reports what cadvisor can see, and disclaims the rest

The homelab docs establish by measurement that `container_start_time_seconds` is the
container's **creation** time, not its last start: it does not move across
`docker restart` or across a crash loop — which keeps a stable container ID — which is why
`HenkContainerRestarting` is deployed and structurally cannot fire. This Prometheus
exposes no metric matching `*restart*`, and the native twin's expression is byte-identical,
so both alerting brains are equally blind.

So `container_state` reports `container_last_seen` (age), `container_health_state`,
`container_oom_events_total`, and `container_start_time_seconds` **labelled as "created"**,
and states in the result that in-place restart loops are not observable.

**It must also declare its own omission semantics**, which is the sharper trap: cadvisor
**drops the series entirely** when a container stops (verified live 2026-08-08 — this is
how `MollySocketLiveness` works). So a list built on `container_last_seen` silently omits
exactly the container the owner is asking about. The result must say that a container
absent from the list may be stopped or removed, and named containers use the
`or vector()` guard so their absence returns a value rather than no series.

### D7 — Discovery is at first use, memoized, and in-process validation is the boundary

`endpoint_history`'s `endpoint` domain is the Gatus key set. A hardcoded list of 19 keys
would rot silently against a config the owner edits by hand, and the docs warn that
renaming a group or endpoint orphans its history.

**Discovery happens at first use, memoized with a TTL and refresh-on-miss** — not at
startup. Startup was the first design and it was wrong three ways: `build_runtime` is
synchronous and carries "nothing network-facing is opened here" in its own docstring, so
there is no seam; a startup-only cache cannot pick up a rename during uptime, making the
renamed endpoint unqueryable until a restart; and discovery failure would permanently
disable the query instead of producing a per-call honest error.

The security property is preserved: the domain is a **closed set** the model cannot add
to, an unknown key is refused, and discovery failure fails closed. The enumerated,
owner-reviewed unit is the *action*; the domain is owner-authored monitoring config read
from a service already inside the trust boundary.

**The in-process check is the normative boundary, not the JSON schema.** Model arguments
are splatted into the tool's `_run` with no intervening validation, and whether the SDK's
MCP layer enforces `input_schema` carries a standing "verify at deploy" note in this
codebase. A requirement that tests the schema would test a layer that may enforce nothing.
Follow `taiga_read`'s idiom, where the enum and the dispatch table are the same object, so
the schema cannot drift from what is implemented.

**Discovered keys become URL path segments**, and a Gatus key is built from owner free
text. Percent-encode, or reject any key that is not a safe path segment — the traversal
scenario must cover discovery as a source, not only the model.

### D8 — Retrieval is section-level, addressed by discovered section id

`homelab_docs` exposes two actions: `search(query)` → ranked heading paths with file,
section id, and snippet; `read(section_id)` → that one section verbatim.

`read` takes a **section id from the index**, never a filesystem path. `../../etc/passwd`
is not a value this parameter can hold, and an id absent from the index is refused — so
traversal is closed by construction rather than by sanitising a path. Rejected: whole-file
read (the two 50 KB+ files make it unusable) and a hand-curated index (maintenance burden
against a corpus that already has `index.mdx` and a heading hierarchy).

Search treats the query as **literal tokens, never compiled as a regex** — a
model-supplied pattern is a denial-of-service surface otherwise. Accepted limitation: a
question phrased unlike the docs ranks poorly; the mitigation is returning several
candidates, not a smarter matcher. Read results are capped at a configured byte budget and
say so when truncated.

**Mechanics that must be specified rather than left to the implementer**, because each has
a silent failure mode: index only `src/content/docs/**/*.{md,mdx}` so the indexer never
walks `.git`, `node_modules`, or `astro.config.mjs`; **do not follow symlinks** (D8's
by-construction argument covers the *parameter*, not the *index*, and git can store
symlinks); and section ids are **content-derived** from the heading path, not ordinal —
ordinal ids remap across a pull, so a search-then-read straddling an update silently
returns different content, and "unknown id is refused" does not catch a *reused* id.

### D9 — Freshness: a stamp, a 26-hour bound, marked and never hidden

The host updater writes a stamp beside the docs (`commit`, `committed_at`, `pulled_at`),
and every corpus result carries its age.

A pull mechanism and a freshness signal are different things: an auto-pull that silently
stops — rotated key, removed timer, a local modification blocking the merge — is
**indistinguishable from fresh docs**. Not hypothetical: the personal checkout found on
rp5 was 23 commits and ~7 weeks behind. Benign there, but it is exactly the state a dead
timer reproduces indefinitely. The homelab's own remedy is the same shape:
`DawarichDumpStale` exports a **raw mtime** and alerts on age, so a dead writer freezes the
timestamp instead of hiding.

The bound is **26 hours** against a daily timer, reusing the fleet's existing
`BackupStale > 26h` convention rather than inventing a number. It is checked against
`pulled_at` only; `committed_at` is reported and **deliberately not bounded** — a repo
nobody pushed to for seven weeks is fine, a dead timer is not. Stated so nobody later
"fixes" it by bounding the wrong field.

Past the bound the result is **marked, not refused**. Rejected: refusing to serve stale
docs. The owner reads every result, and a tool that goes dark the first time a timer
hiccups is worse than one that answers with a loud caveat. A missing or unparseable stamp
is treated the same way: served, marked "freshness unknown". This is not the "stale,
cached-as-fresh" that `homelab-tools` forbids — the point is that it is never presented as
fresh.

**A measured counterexample, recorded because it sharpens the claim rather than breaking
it.** "Docs slightly out of date are still largely correct" is true of prose and false of
figures: `services/monitoring.md`'s DNS baselines (Pi5 ~17ms, VPS ~41ms, Pi2 ~134ms,
measured 2026-02-08) read **2.3ms / 57ms / 2.1ms** live — an order of magnitude off on two
of three devices, with the ordering inverted, in the file this tool will consult most. The
decision survives; the argument now carries the counterexample. The distinction worth
holding: **durable prose guidance ages well; measured figures do not.** The apply-time
measurements are pushed back upstream rather than left wrong for the next reader, who is
now also Henk.

**Index invalidation follows the stamp**, which the host writes **last** so a reader never
sees a new stamp against old content. The read-during-pull window is an accepted
limitation. The stamp lives at the clone root, outside the docs tree, and is excluded via
`.git/info/exclude` so it is neither untracked cruft nor deletable by a `git clean -fd` in
the updater.

**The stamp writer is committed to this repo** (`deploy/homelab-docs-stamp.sh`) and
deployed from there. It contains a path and a git invocation — no secrets, nothing the
publication gate blocks — and it is this change's one new safety mechanism, so it gets unit
tests rather than a single hand-check at apply time. The failure mode that matters: a
writer that stamps `pulled_at` on every *attempt* rather than every *success* inverts the
mechanism, and a dead pull then reads as fresh forever.

### D10 — Result text stays out of the audit log, which is already true globally

Corpus sections quote tailnet addresses and query results carry infrastructure detail.
Neither belongs in an audit record verbatim.

**This is already a global, default-deny property**: result capture is opt-in per tool and
exactly one tool opts in (the handoff tool), a posture established when a 2026-08-18 deploy
found `homelab_health`'s tailnet addresses in records. So the requirement here is an
**assertion** that neither new tool opts in — stated as a global property, because
restating it per-tool would imply each tool carries its own redaction and thereby weaken
the invariant.

**Bound parameters are deliberately not logged either.** A corpus search query is
model-authored free text that can quote owner-personal content, which the audit log's
existing free-text exclusion already covers. Logging parameters would also be a structural
change to a record type, requiring a schema version increment and a committed schema
document — a cost with no benefit here.

**Per-name audit identity is deferred.** An earlier revision required the audit identity to
be `homelab_query:<query_name>`. There is no seam: read-only tools produce no
authorization receipt by classification, and the session record's tool-call entries carry
`{name, tool_class, result_id, executed}` with arguments discarded upstream. The registry is
structured so per-name tiering is available without restructuring, and the identity
question belongs to roadmap 5, where mutating verbs make it load-bearing. Recorded in
Risks so the deferral is not lost.

### D11 — Corpus availability: config errors kill startup, host state degrades one tool

Three layers, and the middle one is where the first design was wrong:

1. **Config error → startup refusal.** Enabled with no path configured is a `ConfigError`
   in `from_dict`, matching every existing refusal in this codebase — all of which are
   pure config-value checks, deterministic and caught in dev.
2. **Bad host state → register anyway, fail honestly per call.** Missing, empty, or
   unstamped corpus means the tool **is present** and every invocation returns an explicit
   error naming the path and the condition.
3. **Deploy-time loudness → `create_host_path: false`.** Without it, Docker
   **auto-creates** a missing bind source as an empty root-owned directory, so an existence
   check at startup is guaranteed to pass and every search returns nothing —
   indistinguishable from "no match". This flag is what makes a path typo fail at deploy,
   where a human is watching.

Layer 2 was originally "do not register", which was wrong four ways. An absent tool
produces **no honest failure at all**: the model does not know a docs tool was supposed to
exist, so it answers from its priors with no marker that documentation was unreachable —
strictly worse than the empty-corpus hole, which at least produced "the docs don't say". It
contradicted D9 and the requirement that a missing stamp be *served with a marker*. It
created a first-deploy trap, since the stamp exists only after the first successful pull.
And it split one broken-corpus condition into two owner-visible behaviours decided purely
by *when* it broke, since runtime mount loss must fail honestly regardless. The in-repo
precedent settles the shape: `todo_read` with an empty allowlist **registers** and returns
an explicit "no allowlisted items" result rather than vanishing. Register-and-explain — not
register-and-go-silent, and not absent.

Every new key's safe default lives in `Config.from_dict`, since rp5's `config.yaml` is
skip-worktree'd and will not carry new keys. Backend timeouts **reuse** the existing
`endpoints.gatus` / `endpoints.prometheus` values rather than adding a key.

### D12 — Both halves in one change

The halves are combined because they **share the config surface, the registration path,
and the publication gate** — splitting would duplicate all three across two proposals for
no review benefit.

*Not* because the corpus half is small. An earlier revision argued that, and it is no
longer true: the corpus half now carries a default-deny allowlist with index-time
filtering, a committed and unit-tested deploy script, a mount clause in a third spec delta,
symlink rules, content-derived section ids, a Tier W corpus read, and an owner sign-off. A
decision resting on a false premise is the thing that gets cited later.

### D13 — The data axis, run explicitly on the corpus

The action axis is not the only axis, and an unargued widening of a default-deny data
circle is the failure the permission model exists to prevent. So, explicitly:

**What is in the corpus.** Owner work metadata (work repo paths, the work GitHub account
name, work-owned VPS tunnels — 25 Anamata references); credential *locations* (`.env`
paths, key filenames, a note that three on-disk SSH keys share one passphrase); a
break-glass map naming the account that bypasses all ACL/SSH rules as the homelab's single
highest-value credential; and tailnet addresses in 12 of 17 files. **No credential values.**

**What contains it.** Henk's external outputs are structurally owner-only: Signal to one
allowlisted identity, deny-all ntfy topics, and no tool anywhere accepting a recipient,
topic, or identity parameter. The toolset is read-only end to end in this change. This is
the North Star theorem — how much an agent may see scales with how constrained its output
reach is — and the trifecta's comms leg is cut structurally, not by policy.

**Where Tier W's line actually falls.** The hard wall is *client* data, which lives only on
client-issued machines and is a physical boundary Henk cannot cross. Owner work metadata and
Anamata-internal content are owner-scoped and **shareable by explicit per-store allowlist**
(2026-08-07 refinement). Credentials of any kind remain off-limits — and credential
*locations* are not credentials. So the corpus is on the permitted side of the line, gated
by an allowlist rather than waved through.

**Compliance, not exemption.** `homelab-tools`'s existing requirement covers "**any** Henk
tool backed by a store that mixes personal and work/Anamata content", and the corpus
mixes. This change therefore adds a default-deny path allowlist rather than arguing that a
documentation tree is not a "store" — a reading narrow enough to exempt it is lawyerly.

**Why paths and not sections.** Section headings are upstream-authored and change with
every doc edit, so a section-level allowlist would default-deny every newly added heading
inside an already-allowed file — silently removing content with no signal on each upstream
edit. Path keys drift far less, and when a whole *file* appears, default-deny is the wanted
behaviour. Section-level exclusion remains available later; the path allowlist is the
boundary, because default-deny means allowlist, not denylist.

**The allowlist filters at index build, not at read time.** A post-fetch filter would leak:
search ranks over the index and returns snippets, so a denied file's text would reach the
owner through search even though `read` would refuse its id. The `todo_read` idiom this
borrows from has no ranker and so does not carry this distinction.

**Initial membership: all 17 files** (owner decision, 2026-08-22). None of the content is a
credential; the security-model files are among the *most* useful for the job, since the
docs' own answer to "why is this scrape target down" is a Tailscale grant; and excluding a
file so the boundary has something in it is theatre. A full-membership allowlist is still a
default-deny boundary — the mechanism exists for when the owner does want to cut something.
An earlier revision pre-excluded `devices/workstation.md` on the grounds it was least useful
for incident follow-up, which was a usefulness argument dressed as a data-axis one; the Tier
W refinement makes owner work metadata allowlist-eligible, and roadmap 4's session-awareness
publisher makes that file its reference document anyway.

**Two default-deny gates now sit in series** — corpus-unavailable and allowlist-empty — and
they must produce **distinct diagnostics**, or the owner sees one opaque failure when the
real condition is "allowlist unset".

**Composition and base.** Glob first, then allowlist. Allowlist entries are relative to the
**docs root**, so the owner writes `devices/workstation.md`, not
`src/content/docs/devices/workstation.md`.

## Risks / Trade-offs

**Grafana's rule state is unreachable (D4)** → accepted; the direct measurements are the
better question, and closing it costs a vps:3000 grant plus a credential.

**Transcribe-from-prose is this change's recurring defect** → three independent instances
were caught during review, two of them *inside* fixes written to prevent it: an unsourced
`memory` bar copied from `homelab_health`'s constant, DNS baselines six months stale, and
an unverified `.RestartCount` fix path. Mitigation is structural: every numeric in the
spec carries an `APPLY-RESOLVED:` marker until pinned from a live probe recorded in
`notes/backend-probe.md`, and archive is gated on zero remaining markers.

**Per-name audit identity is deferred (D10)** → recorded so roadmap 5 picks it up rather
than rediscovering it.

**The exact Gatus per-endpoint route is version-dependent** → probe at apply; the
guaranteed fallback is filtering the bulk `/api/v1/endpoints/statuses` response in process,
which `homelab_health` already does successfully.

**`freshness_check`'s exact textfile metric names are not in the docs** — only the families
→ fixed selector over those families, exact `__name__` values enumerated at apply from
`/api/v1/label/__name__/values`.

**"Down since" cannot always be answered** → `scrape_targets` bounds it honestly to the
24h window rather than fabricating a duration. It also evaluates **bare `up`** and
enumerates all targets with their value, because `up == 0` returns an empty set when
everything is healthy — indistinguishable from a broken query — and surfaces `lastError`
from `/api/v1/targets`, since the docs' own answer to "why is it down" is almost always a
Tailscale grant.

**A renamed Gatus endpoint changes its key and orphans history** → first-use discovery with
TTL picks up the new key without a restart; the orphaned history is a Gatus-side property
this tool cannot paper over. Gatus history is also excluded from backup and resets on config
change, and only 10 of 19 endpoints alert at all — so "no history" is routine, not a fault.

**Keyword search misses paraphrases (D8)** → several candidates per search; accepted.

**Token cost** → range queries summarised (D5), reads byte-capped (D8).

**Host provisioning is manual** — deploy key, clone, timer → recorded in
`~/.claude-config/tooling-backlog.md` at apply time; the stamp writer itself is committed
and tested (D9).

**`homelab_health` changes behaviour for the owner's daily driver** → deliberate, declared
in Impact, and its rollback is a code revert rather than a config flip, so the two halves
of this change are not symmetrically rollback-able.

**New config keys shift line numbers cited in `owner-acknowledgement`'s findings** →
expected; re-grep there later.

## Migration Plan

Ordered, because five of the fixes above constrain the sequence:

1. Ship the query half — no host dependency, defaults on, verifiable against live Gatus
   and Prometheus from the existing grants. Includes the `homelab_health` amendment.
2. Provision rp5: deploy key, clone to `/opt/homelab-docs/`, install the daily pull timer
   and the committed stamp writer.
3. **Let the first pull complete**, so a stamp exists before any container start.
4. Add the read-only bind mount to the compose file **with `create_host_path: false`**.
5. Set the corpus **path allowlist** (all 17 files) in rp5's hand-maintained `config.yaml`.
6. Flip the corpus enable key. Restart.
7. Verify: search, read, a deliberately stale stamp, and the mount failing loudly with the
   host path absent.

Rollback is **not symmetric**. The corpus half reverts by config flip, and the mount can be
removed independently. The query half's `homelab_health` amendment is a **code revert**.

## Open Questions

- The deployed Gatus version's per-endpoint status route and its uptime-window vocabulary —
  probe at apply (`APPLY-RESOLVED:gatus-window`; fallback known-good).
- The exact textfile metric `__name__` values for `freshness_check` — enumerate at apply.
- The `High memory usage` rule's live threshold (`APPLY-RESOLVED:memory-bar`).
- Whether `endpoint_history`'s domain should later become a declared list if the Gatus
  endpoint set stabilises. Discovery is right while the owner still edits that config by hand.
- Whether `homelab_health` should eventually retire into a seventh `overview` query — one
  threshold source, one projection rule, one code path. Recorded as a follow-up, deliberately
  not this change's scope.
