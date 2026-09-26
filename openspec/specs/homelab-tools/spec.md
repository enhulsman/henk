# homelab-tools Specification

## Purpose
Henk's hands: read-only views of the homelab (Gatus and Prometheus over the tailnet, never SSH),
the owner's personal todos, and notify-class sends to fixed topics that take no destination
argument. Two rules make the set trustworthy rather than merely useful. Failures are honest —
a tool that could not reach its backend says so and never returns stale, cached-as-fresh, or
invented data. And any tool backed by a store that mixes personal with work/Anamata content
enforces a default-deny allowlist inside Henk's own process, so an unset allowlist surfaces
nothing and a backend-side filter is never the boundary.
## Requirements
### Requirement: homelab_health read-only tool
The system SHALL provide a `homelab_health` tool (class: read-only) that reports homelab status by querying the Gatus API (rp5:8080) and the Prometheus HTTP API (vps:9090) over the tailnet, returning a structured summary of endpoint up/down states and per-node memory, disk, and load. The tool SHALL NOT use SSH or any other host-level access.

Its threshold values SHALL derive from the **same pinned record of live rule expressions**
as the named-query registry, so that the two tools cannot disagree about whether a
measurement is healthy. Its output SHALL follow the same projection rule as the named-query
registry: nodes are named by their friendly enum value, and raw `instance` label values are
omitted.

#### Scenario: Healthy homelab summarized
- **WHEN** the agent invokes `homelab_health` and all Gatus endpoints are up
- **THEN** the tool returns a summary marking all endpoints healthy with current node resource figures

#### Scenario: Degraded service reported
- **WHEN** Gatus reports an endpoint down or Prometheus reports a node metric beyond its threshold
- **THEN** the tool's result names the affected endpoint/node and the failing measurement

#### Scenario: Backend unreachable
- **WHEN** Gatus or Prometheus cannot be reached
- **THEN** the tool returns an explicit "source unreachable" result for that backend (never fabricated data), and the other backend's data is still returned if available

#### Scenario: The two tools cannot disagree about a threshold
- **WHEN** `homelab_health` and `node_resource_trend` report on the same node and resource in the same period
- **THEN** both evaluate the measurement against the same threshold value, so one cannot call it healthy while the other reports a crossing

#### Scenario: No raw address in the summary
- **WHEN** `homelab_health` renders per-node figures
- **THEN** each node is named by its friendly enum value and no `instance` label value appears in the output

### Requirement: taiga_read read-only tool
The system SHALL provide a `taiga_read` tool (class: read-only) backed by the existing Taiga MCP server (rp5:8000) as an MCP client that registers only read operations (`get_*`, `list_*`), or — if the Taiga MCP server does not serve an HTTP-based MCP transport — by the Taiga REST API's read endpoints directly, with the same read-only posture. Write-capable operations SHALL NOT be registered or callable, regardless of what the backend exposes.

#### Scenario: Board contents fetched
- **WHEN** the agent invokes `taiga_read` to list user stories for a project
- **THEN** the current stories with status are returned from the Taiga MCP server

#### Scenario: Write operation impossible
- **WHEN** the agent attempts to reach any Taiga write operation (create/update/assign)
- **THEN** no such tool exists in its registry and the attempt fails without any request reaching the Taiga MCP server

### Requirement: todo_read read-only tool
The system SHALL provide a `todo_read` tool (class: read-only) that fetches todos from obsidian-todo-api (vps:8089) using only GET endpoints and a scoped read token. The tool SHALL apply a default-deny note-path allowlist (see "Default-deny personal-data scoping") so that only todos whose source note matches an allowlisted path/prefix are surfaced; all other todos SHALL be dropped before any result leaves the tool. The tool SHALL correctly parse the obsidian-todo-api's note-grouped response (`{"todos": {"<note path>": [items]}, "total_count", "note_count"}`, each item carrying a `source_note`) and SHALL NOT under any condition emit unparsed backend output (e.g. a raw `str(data)` dump) or a todo whose source note was not allow-matched. The count the tool reports SHALL be the allowlisted count, never the vault-wide total.

#### Scenario: Todos fetched
- **WHEN** the agent invokes `todo_read` and at least one todo's source note matches the allowlist
- **THEN** only the allowlisted todos are returned from obsidian-todo-api, formatted from the parsed note-grouped response

#### Scenario: Only GET is used
- **WHEN** any `todo_read` invocation executes
- **THEN** every HTTP request it makes uses the GET method

#### Scenario: Work/non-allowlisted notes are dropped
- **WHEN** the obsidian-todo-api response contains todos from a note path that is not on the allowlist (e.g. a work/Anamata note)
- **THEN** those todos are absent from the tool result, and neither their text nor their note path appears anywhere in the output

#### Scenario: Note-grouped response is parsed, never dumped
- **WHEN** the backend returns the note-grouped dict shape (`todos` is a dict of note-path → item list)
- **THEN** the tool walks the groups and formats individual allowlisted todos, and no code path returns the raw stringified response

#### Scenario: Unexpected response shape fails safe
- **WHEN** the backend returns a shape the tool does not recognize
- **THEN** the tool returns an explicit "unexpected response shape" error and surfaces no todo content

### Requirement: notify tool with AI labeling
The system SHALL provide a `notify` tool (class: notify-only) that publishes to a single configured ntfy topic (vps:2586) with a scoped publish token. Every published message SHALL begin with the `[AI]` label. The tool SHALL NOT accept a topic, server, or recipient parameter.

#### Scenario: Notification sent and labeled
- **WHEN** the agent invokes `notify` with message text
- **THEN** ntfy receives the message on the fixed topic, prefixed with `[AI]`

#### Scenario: Alternate destination impossible
- **WHEN** the agent produces arguments attempting to target a different topic or server
- **THEN** the tool interface has no such parameter and the message can only go to the configured topic

### Requirement: Tool failures are honest
Every homelab tool SHALL return an explicit error result on backend failure (timeout, non-2xx, malformed response) containing what failed and why. Tools SHALL NOT return stale, cached-as-fresh, or fabricated data.

#### Scenario: Backend timeout surfaces as error
- **WHEN** a tool's backend does not respond within its timeout
- **THEN** the tool result states the backend and the timeout, and the agent's reply to the owner reflects that the data was unavailable

### Requirement: Default-deny personal-data scoping
Any Henk tool backed by a store that mixes personal and work/Anamata content (the obsidian-todo-api vault; the Taiga instance) SHALL enforce a default-deny allowlist that is authoritative **inside Henk's own process** — the tool SHALL surface only records whose scoping key matches the configured allowlist and SHALL drop everything else, regardless of what the backend returns. For `todo_read` the scoping key is the todo's source note path (matched by folder-boundary path prefix); for `taiga_read` (when re-registered) it is the project id. An empty or unset allowlist SHALL cause the tool to surface **nothing** (fail closed); there SHALL be no configuration or code path in which an absent allowlist surfaces all records. An allowlist entry that is empty after normalization (leading `/` and surrounding whitespace stripped) SHALL be discarded and SHALL NOT broaden scope; a list containing only such entries SHALL behave as an empty allowlist and surface nothing. Where the backend offers a server-side filter (e.g. the obsidian-todo-api `source_note` query parameter), it MAY be used as defense-in-depth to reduce data transferred, but it SHALL NOT be relied on as the security boundary — the in-process allowlist SHALL re-filter every returned record.

> Note: `taiga_read`'s project-id scoping is **unimplemented pending the fast-follow** — this requirement establishes the pattern, but no project-id filter exists yet. `taiga_read` MUST NOT be registered in the production toolset until that filter is implemented.

#### Scenario: Empty allowlist surfaces nothing
- **WHEN** the allowlist for a personal-data tool is empty or unset and the tool is invoked
- **THEN** the tool returns an empty/"no allowlisted items" result and surfaces no record from the backend

#### Scenario: In-process allowlist is authoritative over the backend filter
- **WHEN** the backend's own filter fails open and returns records outside the allowlist (e.g. the fail-open `source_note` substring filter)
- **THEN** the tool's in-process allowlist drops those out-of-scope records before any result is produced

#### Scenario: Only allowlisted scope keys pass
- **WHEN** the backend returns records spanning both allowlisted and non-allowlisted scope keys (note paths / project ids)
- **THEN** only records whose scope key matches an allowlist entry are surfaced, and non-matching records are absent from the result

### Requirement: Named-query tool with a closed query-name enum
The system SHALL provide a `homelab_query` tool (class: read-only) that answers homelab
questions by dispatching on a `query_name` argument. The argument's domain is a **closed
enum** of owner-reviewed registry entries. Each entry SHALL bind `query_name` to fixed
PromQL expressions or Gatus API routes recorded in the registry. The enum SHALL be
exactly:
- `node_resource_trend`
- `scrape_targets`
- `endpoint_history`
- `freshness_check`
- `container_state`
- `dns_performance`
- `memory_movers`
- `host_service_state`

A `query_name` outside the enum SHALL be refused as a tool error, with no request issued
to any backend.

#### Scenario: A named query dispatches its registered template
- **WHEN** the agent invokes `homelab_query` with `query_name` set to an enumerated name
- **THEN** the request issued to the backend is the expression or route recorded in that registry entry, and the result is rendered from that backend's response

#### Scenario: An unregistered query name is refused before any request
- **WHEN** the agent invokes `homelab_query` with a `query_name` that is not in the enum
- **THEN** the tool returns an error naming the rejected value, and no HTTP request is made to Gatus or Prometheus

#### Scenario: The host-coverage queries are enumerated
- **WHEN** the registry's query names are inspected
- **THEN** `memory_movers` and `host_service_state` are present, and the advertised enum equals the dispatch table

### Requirement: No free-text query path
The `homelab_query` tool SHALL NOT accept a PromQL expression, a metric name, a label
name, a label value, a label selector, a Gatus endpoint key supplied by the model, a URL,
a URL path, or any other free-text string that reaches a backend request. Every argument
the tool accepts SHALL be validated against a bounded domain before a request is
constructed. There SHALL be no configuration option, debug flag, or code path that admits
a model-supplied query expression.

#### Scenario: Validation happens in the tool, not only in the schema
- **WHEN** an argument value outside its declared domain reaches the tool's own execution path, bypassing any schema-level check
- **THEN** the tool's in-process validation rejects it and no request is issued

#### Scenario: Injection through a bound parameter is not possible
- **WHEN** the agent supplies a parameter value containing PromQL, selector, or path syntax (for example a value carrying `}`, `|`, or `../`)
- **THEN** the value fails domain validation, the tool returns an error, and no request is issued

#### Scenario: The declared enum and the dispatch table cannot diverge
- **WHEN** the registry is inspected
- **THEN** the parameter domains advertised to the model and the domains enforced at execution derive from one shared definition, so a domain cannot be advertised that is not enforced

### Requirement: Query parameter domains
Each registry entry SHALL declare its own domain for each parameter it accepts. Domains
SHALL NOT be shared across queries where the underlying fleet differs: a query's node
domain is its own tuple, never the job map that happens to serve it. The domains SHALL be
exactly:

- `node_resource_trend`:
  - `node` ∈ {`rp5`, `vps`, `rp2`};
  - `resource` ∈ {`cpu`, `memory`, `disk`, `load`, `swap_used`, `swap_io`,
    `temperature`};
  - `window` ∈ {`15m`, `1h`, `6h`, `24h`}.
- `container_state`: `node` ∈ {`rp5`, `vps`, `rp2`}. **rp2 runs no cadvisor**, so
  `container_state` for `rp2` SHALL be a registered `Unavailable` entry for the whole
  query. It resolves to the not-derivable outcome, stating that rp2 has no container
  metrics. It is neither an out-of-domain refusal nor an empty container list.
- `memory_movers`: `node` ∈ {`rp5`, `vps`, `rp2`}; `window` ∈ {`15m`, `1h`, `6h`, `24h`}.
  rp2 SHALL be a registered `Unavailable` entry for the whole query, for the same reason.
- `host_service_state`: `node` ∈ {`rp5`, `vps`, `rp2`}; `window` ∈ {`15m`, `1h`, `6h`,
  `24h`}. `rp5` and `rp2` SHALL each be a registered `Unavailable` entry for the whole
  query. Their node-exporters run without the systemd collector. Measured 2026-09-23: the
  unit-state series exist only for the vps node-exporter job.
- `dns_performance`: `node` ∈ {`rp5`, `vps`, `rp2`}; `window` ∈ {`15m`, `1h`, `6h`,
  `24h`}. The parameter SHALL be named `node`, matching `node_resource_trend`.
- `endpoint_history`:
  - `endpoint` is discovered (see "The endpoint-history domain is discovered");
  - `window` ∈ {`1h`, `24h`, `7d`, `30d`}. These are Gatus's accepted uptime durations,
    pinned 2026-09-02 from the live server's own rejection message
    (`notes/backend-probe.md` §1.1).
- `scrape_targets`, `freshness_check`: no parameters. `scrape_targets`' lookback window
  SHALL be `24h`.

A parameter value outside the invoked entry's domain SHALL cause an explicit tool error.
The tool SHALL NOT narrow, substitute, or best-effort the query, and SHALL NOT return an
empty result in place of a rejection.

#### Scenario: An out-of-domain value is rejected, not answered emptily
- **WHEN** the agent invokes `node_resource_trend` with `node` set to a value outside {`rp5`, `vps`, `rp2`}
- **THEN** the tool returns an error stating that the value is outside this query's domain, and does not return an empty measurement

#### Scenario: rp2 container state is not available, and says so
- **WHEN** the agent invokes `container_state` or `memory_movers` with `node` set to `rp2`
- **THEN** no request is issued, the result is the not-derivable outcome stating that rp2 runs no cadvisor so no container metrics exist for it, and the result is neither an out-of-domain refusal nor an empty list

#### Scenario: Host service state is not available where the collector is missing
- **WHEN** the agent invokes `host_service_state` with `node` set to `rp5` or `rp2`
- **THEN** no request is issued, and the result is the not-derivable outcome stating that this node's node-exporter runs without the systemd collector

#### Scenario: The same value is valid for a query whose domain includes it
- **WHEN** the agent invokes `node_resource_trend` with `node` set to `rp2`
- **THEN** the query proceeds against rp2's node-exporter job

#### Scenario: Every enumerated domain is enforced
- **WHEN** the agent supplies a `resource` or `window` value outside the literal domains above
- **THEN** the tool returns an error and issues no request

#### Scenario: In-domain but not currently derivable is its own outcome
- **WHEN** a parameter value is inside its declared domain but the data needed to service it cannot be obtained (for example `dns_performance` for a node whose node-exporter is not reporting, so its mapping cannot be derived)
- **THEN** the result states that specific condition, distinguishably from both an out-of-domain rejection and an empty measurement

### Requirement: Templates select by job label and results carry no addresses
Registry templates SHALL select Prometheus series by the `job` label and SHALL NOT contain
a tailnet address, an `instance` label value, a `server` label value, or a scrape URL. Tool
results SHALL name a target by its `job` label together with any distinguishing
**non-address** label (`name`, `mountpoint`), and SHALL omit the `instance` label, the
`scrapeUrl` field, and the `server` label from their output. The `server` label on the
AdGuard metrics SHALL NOT be treated as a non-address discriminator: it is a URL containing
a tailnet address. Where a template's job is expected to yield one series and the backend
returns more than one, the tool SHALL report that fact rather than selecting one silently.

#### Scenario: The registry contains no addresses
- **WHEN** every registry template is inspected, including `dns_performance`
- **THEN** each selects on `job` label values, and none contains a tailnet address, an `instance` selector, or a `server` selector

#### Scenario: Addresses are dropped from every result
- **WHEN** a backend returns samples carrying `instance` labels, entries carrying `scrapeUrl`, or series carrying `server` labels
- **THEN** the tool's result names the target by job and enum value and contains none of those values

#### Scenario: Unexpected series multiplicity is reported
- **WHEN** a template expected to match one series matches more than one
- **THEN** the result states that the job returned multiple series, rather than presenting one of them as the target's figure

### Requirement: Range queries return bounded summaries
`node_resource_trend` and `dns_performance` SHALL issue Prometheus range queries and SHALL
return a summary rather than the sample series. The summary consists of:
- the first value;
- the last value;
- the minimum;
- the maximum;
- the direction of travel;
- any threshold crossing.

**Every value in the summary SHALL carry the time it refers to:**
- first and last carry the times of those samples;
- minimum and maximum carry the time the extreme was first reached, plus the time it was
  last reached when the same extreme recurs later in the window;
- the summary SHALL also state the time the window ends.

Times SHALL be rendered in UTC in one fixed format across every query. The step SHALL be
derived from the requested window and a configured maximum point count, so that every
supported window costs a bounded amount to render. The tool SHALL NOT return the raw
sample series under any window.

#### Scenario: A trend is summarized, not dumped
- **WHEN** the agent invokes `node_resource_trend` over the longest supported window
- **THEN** the result contains the summary figures and direction, and does not contain the individual samples

#### Scenario: Extremes carry their times
- **WHEN** `node_resource_trend` summarizes a series whose maximum occurs at one sample and whose minimum occurs at another
- **THEN** the result states the maximum with the UTC time of that sample and the minimum with the UTC time of its sample, alongside the first and last values with their times and the window's end time

#### Scenario: A repeated extreme reports first and last occurrence
- **WHEN** the maximum value is reached at an early sample and again at a later sample in the same window
- **THEN** the result states both the time it was first reached and the time it was last reached

#### Scenario: DNS summaries carry the same times
- **WHEN** `dns_performance` summarizes a series
- **THEN** its summary carries the same per-figure times and window end time, in the same format as `node_resource_trend`

#### Scenario: Step scales with the window
- **WHEN** either range query is invoked with each supported window
- **THEN** the number of points requested from Prometheus does not exceed the configured maximum for any window

### Requirement: Threshold comparisons are per-resource and traceable to a measurement
`node_resource_trend` SHALL NOT apply a single generic "crossed the corresponding alert
rule's threshold" comparison, because the fleet's rules do not share a shape. Each resource
SHALL be compared as follows:

- `disk`: scoped to `mountpoint="/"` and reported in the rule's own form, **percent free
  below its bar**, not percent used.
- `swap_io`: the **pressure branch** of `HenkSwapPressure`, compared against the rule's
  sustained pages-per-second bar.
- `swap_used`: the **fullness branch** of `HenkSwapPressure`.
  - The rule is the OR of the fullness branch and the pressure branch. Either branch
    alone can fire it, and neither SHALL be presented as the rule's only or main
    trigger.
  - A fullness figure **below** its bar SHALL be presented as below that branch's bar,
    and SHALL NOT be presented as an approaching incident on its own.
  - A fullness figure **above** its bar SHALL be presented as able to fire the rule on
    its own once sustained for the rule's `for` window. It SHALL NOT be described as "not
    the rule's trigger".
- `memory`: compared against the `High memory usage` Grafana rule's live bar, **> 75 %
  used** (pinned 2026-09-02 from the provisioning API, `notes/backend-probe.md` §1.5), and
  noted as delivering to Discord rather than to this agent.
- `cpu`, `load`, `temperature`: reported with **no threshold comparison**, because no rule
  defines one. For `temperature` the result SHALL note that no alert exists in either
  alerting system.

Both `swap_used` and `swap_io` results SHALL state that the rule fires on either branch,
that the alert's value does not identify which branch fired, and that both measurements
need checking.

Every compared threshold SHALL also state its rule's **`for` window**: the duration the
condition must hold before the rule fires, pinned from the same record as the bar. Where
the record pins no `for` for a rule, the result SHALL say that the `for` window is not in
the pinned record, and SHALL NOT state one.

Every numeric threshold and every `for` window in the registry SHALL be traceable to the
pinned record of live rule expressions. The registry SHALL NOT carry either value
transcribed from documentation prose.

#### Scenario: Swap fullness below its bar is not presented as an approaching incident
- **WHEN** `node_resource_trend` reports `swap_used` at a level below the rule's fullness bar but within the fleet's documented chronic range
- **THEN** the result states that the figure is below the fullness branch's bar and that this branch alone would not fire the rule, and does not describe either branch as the rule's only or main trigger

#### Scenario: Swap fullness above its bar is presented as a branch that can fire
- **WHEN** `node_resource_trend` reports `swap_used` whose maximum is above the rule's fullness bar
- **THEN** the result states that this branch alone can fire `HenkSwapPressure` once sustained for the rule's `for` window, and does not describe fullness as not the rule's trigger

#### Scenario: Swap results say both branches need checking
- **WHEN** `node_resource_trend` reports either `swap_used` or `swap_io`
- **THEN** the result states that `HenkSwapPressure` fires on either branch, that the alert's value does not identify which, and that both measurements should be checked

#### Scenario: A comparison states the rule's for window
- **WHEN** `node_resource_trend` reports `swap_io` against its bar, and `memory` against its bar
- **THEN** each result states the bar, its source rule, and that rule's `for` window as pinned in the record (15m for `HenkSwapPressure`, 5m for `High memory usage`)

#### Scenario: Disk is scoped and expressed in the rule's own form
- **WHEN** `node_resource_trend` reports `disk`
- **THEN** the measurement covers the root filesystem only and is expressed as percent free against the rule's bar

#### Scenario: Resources with no rule carry no bar
- **WHEN** `node_resource_trend` reports `cpu`, `load`, or `temperature`
- **THEN** the result contains the figure and direction and no threshold comparison

#### Scenario: Registry thresholds match the pinned record
- **WHEN** the registry's threshold values and `for` windows are compared against the recorded live rule expressions
- **THEN** every value matches, and no threshold or `for` window exists in the registry that is absent from the record

### Requirement: DNS node identification is derived, never configured or hardcoded
`dns_performance` SHALL determine which AdGuard series belongs to which node by **deriving**
the mapping at query time from job-labelled series, and SHALL NOT read that mapping from a
hardcoded table or from configuration. Both discriminating labels on the AdGuard metrics
(`instance` and `server`) are tailnet addresses, so either form of stored mapping would
place an address in this repository or in deployed configuration. Address strings used
during derivation SHALL exist only in memory and SHALL NOT appear in any result.

#### Scenario: The mapping is derived at query time
- **WHEN** `dns_performance` is invoked
- **THEN** the node-to-series mapping is derived from job-labelled series, and no stored mapping table or configuration key supplies it

#### Scenario: No address is stored anywhere for this query
- **WHEN** the repository and the deployed configuration are inspected for this query's needs
- **THEN** neither contains an AdGuard `server` value, an `instance` value, or any tailnet address

#### Scenario: An underivable node is reported, not returned empty
- **WHEN** `dns_performance` is invoked for a node whose node-exporter series are absent, so its mapping cannot be derived
- **THEN** the result states that the mapping for that node could not be derived and why, and does not return an empty measurement

### Requirement: No rule-state query in v1
The system SHALL NOT provide a query that reports alert-rule firing state. The closed
enum SHALL contain only measuring queries: queries that read a measured series or a
backend's own check history.

This is recorded as a deliberate absence rather than an omission. Reading rule state was
excluded by the admission criterion, for two reasons:
- **It measures nothing.** Every condition it could report is either covered by a direct
  measuring query or already delivered to the owner by another route.
- **It would actively mislead.** The rules that reach this agent are evaluated outside
  Prometheus and are invisible to `ALERTS`, so an empty result could be read as an
  incident clearing.

`host_service_state` measures systemd unit state as exported by node-exporter. It
reports no alert rule's state and is admitted as a measuring query.

#### Scenario: No rule-state query is registered
- **WHEN** the registry is inspected
- **THEN** no entry reads `ALERTS`, a rule-evaluation API, or any alert-rule firing state, and every entry reads a measured series or a check history

### Requirement: scrape_targets proves it ran and says why a target is down
`scrape_targets` SHALL evaluate **bare `up`** and enumerate every scrape target with its
value, rather than filtering to `up == 0`. A filtered query returns an empty result set
when the fleet is healthy, which is indistinguishable from a failed or misdirected query.
The tool SHALL additionally surface each down target's last scrape error from the
Prometheus targets API, because the documented cause is most often a network-policy grant
rather than a dead service. Where a target's down-duration cannot be established within the
query window, the tool SHALL report that it has been down for longer than the window and
SHALL NOT state a specific duration.

#### Scenario: A healthy fleet is distinguishable from a broken query
- **WHEN** every scrape target is up
- **THEN** the result enumerates all targets with their values, demonstrating the query executed

#### Scenario: A down target carries its scrape error
- **WHEN** a target reports down
- **THEN** the result names the target by job and includes the backend's last scrape error for it

#### Scenario: An unknown down-duration is bounded, not invented
- **WHEN** a target was not up at any point within the query window
- **THEN** the result says it has been down for longer than the window, and states no specific duration

### Requirement: container_state declares both what it cannot see and what it omits
`container_state` SHALL label `container_start_time_seconds` as the container's
**creation** time and SHALL state that it does not move when a container restarts. It
SHALL describe the restart signal it reports exactly as measured (see "container_state
reports per-container memory, swap and restarts").

It SHALL additionally state its **omission semantics**:
- the container-metrics exporter drops a container's series entirely when that container
  stops, so a container absent from the result may be stopped or removed rather than
  absent from the host;
- host systemd units are not containers and are not listed, and `memory_movers` covers
  them.

For any container the query names explicitly, the template SHALL use a form that returns
a value when the series is absent, so absence yields a reading rather than no series. For
a node in the query's domain that has no container metrics, a named-container follow-up
SHALL resolve to the same not-derivable outcome as the query itself, and SHALL NOT be an
out-of-domain refusal.

#### Scenario: Creation time is not presented as a restart signal
- **WHEN** `container_state` returns a container's creation timestamp
- **THEN** the value is labelled as creation time and the result states that it does not move when the container restarts

#### Scenario: The restart caveat describes the measured behaviour
- **WHEN** `container_state` returns a result for `rp5` or `vps`
- **THEN** its caveats state that restarts are counted as resets of the container's CPU counter, that a recreated container appears only as a new creation time, and that several restarts inside one scrape interval count as one — and no caveat states that in-place restart loops are not observable

#### Scenario: A stopped container's absence is declared, not silent
- **WHEN** a container has stopped and its series are therefore absent from the backend
- **THEN** the result states that an absent container may be stopped or removed, so its absence is not read as evidence that nothing is wrong

#### Scenario: Host units are pointed elsewhere, not silently missing
- **WHEN** `container_state` returns a result
- **THEN** it states that host systemd units are not listed and that `memory_movers` covers them

#### Scenario: A named container absent from the backend still returns a reading
- **WHEN** the query names a container explicitly and that container's series is absent
- **THEN** the template returns a value indicating absence rather than an empty series

#### Scenario: A named-container follow-up on rp2 is not derivable
- **WHEN** a named-container expression is requested for `rp2`
- **THEN** the outcome is not-derivable, stating that rp2 runs no cadvisor, and not an out-of-domain refusal

### Requirement: freshness_check reports raw timestamps
`freshness_check` SHALL report each pipeline's **raw timestamp** alongside the computed age,
and SHALL NOT report an age supplied precomputed by an exporter. A writer that dies freezes
a raw timestamp, so a derived age keeps growing and the failure surfaces; a precomputed age
freezes and the failure hides.

#### Scenario: Freshness reports the raw timestamp
- **WHEN** `freshness_check` returns a pipeline's status
- **THEN** the result carries the raw metric timestamp as well as the age derived from it

#### Scenario: A frozen writer surfaces as a growing age
- **WHEN** a pipeline's metric writer has stopped and its timestamp is therefore static
- **THEN** the reported age increases over successive invocations rather than remaining constant

### Requirement: endpoint_history's domain is discovered at first use and fails closed
`endpoint_history`'s `endpoint` parameter domain SHALL be the set of endpoint keys
discovered from the Gatus API, not a list hardcoded in this repository. Discovery SHALL
occur on **first use**, memoized with a refresh interval and a refresh on lookup miss, and
SHALL NOT be required during process construction. The discovered set SHALL be treated as
closed: an argument not present in it SHALL be refused. If discovery fails, the tool SHALL
fail closed for that invocation with an explicit error, and SHALL NOT fall back to treating
the argument as a free-text key or path. A discovered key used as a URL path segment SHALL
be percent-encoded, or rejected if it cannot be safely encoded — discovered keys derive
from owner-authored free text and are a traversal source in their own right.

#### Scenario: A discovered key is accepted
- **WHEN** the agent invokes `endpoint_history` with a key present in the discovered set
- **THEN** that endpoint's check history is returned

#### Scenario: An undiscovered key is refused
- **WHEN** the agent invokes `endpoint_history` with a key absent from the discovered set
- **THEN** the tool returns an error and issues no request for that key

#### Scenario: A renamed endpoint becomes queryable without a restart
- **WHEN** an endpoint is renamed in the backend's configuration while the process runs, and its new key is requested
- **THEN** the lookup miss triggers a refresh, the new key is discovered, and the query succeeds

#### Scenario: Discovery failure closes the tool rather than opening it
- **WHEN** endpoint discovery fails
- **THEN** the invocation is refused with an explicit error, and no argument is passed through to a backend route

#### Scenario: A discovered key cannot traverse
- **WHEN** a discovered key contains characters that are unsafe in a URL path segment
- **THEN** it is percent-encoded or rejected, and no request escapes the intended route

### Requirement: The event that fired maps to a queryable endpoint argument
The system SHALL define how an arriving Gatus event is turned into the `endpoint`
argument for `endpoint_history`, because the event's title format and the backend's
queryable key format differ. The derivation SHALL be specified rather than left to
inference, or the agent SHALL be required to resolve the event's name against the
discovered key set.

#### Scenario: An arriving event yields a queryable argument
- **WHEN** a Gatus event arrives and the agent follows up on it
- **THEN** the endpoint argument used is resolved from the event's identifying fields against the discovered key set, without the agent constructing a key by guesswork

#### Scenario: An unresolvable event name is reported
- **WHEN** an event's identifying fields match no discovered key
- **THEN** the tool states that the endpoint could not be resolved, and does not query an invented key

### Requirement: Query result content is excluded from audit records
The `homelab_query` tool SHALL NOT opt into audit result capture, so no rendered result text
or backend response body reaches an audit record, and bound parameter values SHALL likewise
not be written into audit records. Result capture is already a **global, default-deny
property** of the audit path — capture is opt-in per tool and only the handoff tool opts in —
so this requirement asserts that global mechanism covers the new tool rather than adding a
per-tool redaction step.

#### Scenario: The new query tool does not opt into result capture
- **WHEN** the set of tools whose results are captured into audit records is inspected
- **THEN** `homelab_query` is absent from it

#### Scenario: No result text reaches a record
- **WHEN** a `homelab_query` invocation returns a result naming nodes, endpoints, or containers
- **THEN** no audit record written for that session contains any substring of the rendered result body

### Requirement: container_state reports per-container memory, swap and restarts
`container_state` SHALL report, for each container it lists:
- its **working-set memory** (`container_memory_working_set_bytes`);
- its **swap** (`container_memory_swap`).

Both are selected by the cadvisor `job` label and a non-empty `name`, and rendered in MiB.
Neither SHALL carry a threshold comparison, because no rule defines one, and the result
SHALL say so. The result SHALL name the containers with the highest working set and the
highest swap. A container name in Docker's auto-generated two-word form SHALL be
annotated as probably an ephemeral `docker run` container. The annotation is hedged,
because a hand-chosen name can share the form.

`container_state` SHALL report a **restart count** per container over two fixed
lookbacks, 15 minutes and 24 hours. The count is derived from resets of the container's
CPU-usage counter and collapsed per container name, so that one restart counts once
however many per-CPU series the exporter exposes. The lookbacks are literals, not
parameters.

The restart aspect SHALL exist only for cadvisor jobs whose restart signal the pinned
probe record states was verified against a known restart. As of 2026-09-23 both
`cadvisor-pi5` and `cadvisor-vps` are verified. On each, a `docker restart` moved the count
to 1 within one scrape while the creation time did not move.

An aspect that a registered `Unavailable` entry marks as missing on a node SHALL be
rendered in that aspect's place, carrying the entry's reason. It SHALL NOT be rendered
only as a trailing caveat, as an empty column, or as zero.

#### Scenario: Memory and swap are reported per container
- **WHEN** `container_state` is invoked for `rp5` or `vps` and the exporter reports memory and swap series for its containers
- **THEN** each listed container carries its working-set memory and swap in MiB, and the result names the containers with the highest working set and the highest swap

#### Scenario: Container memory carries no bar
- **WHEN** `container_state` reports container memory or swap
- **THEN** no threshold comparison is attached, and the result states that no rule defines one

#### Scenario: An auto-generated name is annotated
- **WHEN** a listed container's name has Docker's auto-generated two-word form
- **THEN** its row carries the hedged annotation that it is probably an ephemeral `docker run` container

#### Scenario: A restart is counted
- **WHEN** a container on `rp5` or `vps` was restarted in place within the last 15 minutes and `container_state` is invoked
- **THEN** that container's 15-minute and 24-hour restart counts are each at least 1, while its creation time is unchanged

#### Scenario: Per-CPU series count one restart once
- **WHEN** the exporter reports a container's CPU counter as several per-CPU series and every one of them reset once
- **THEN** that container's restart count is 1, not the number of series

#### Scenario: The restart aspect is traceable to a measurement
- **WHEN** the registry's restart aspect is compared against the pinned probe record
- **THEN** the aspect exists for exactly the cadvisor jobs the record states are verified

#### Scenario: An unavailable aspect is rendered in its place
- **WHEN** `container_state` is invoked for a node whose registered `Unavailable` entry marks one aspect missing (the health state on `rp5`)
- **THEN** that aspect's column reads as unavailable with the entry's reason for every row, and is neither empty nor zero

### Requirement: memory_movers names the cgroups whose working set moved, containers and host units alike
`memory_movers` SHALL report the cgroups on a node whose working-set memory moved most
within the window. It covers named containers and host systemd service units under
`/system.slice/`, selected by the cadvisor `job` label, by a non-empty `name` for
containers, and by the `id` label pattern for host service units. The ranking is by
maximum minus minimum over the window, computed from fixed range-function templates. Each
range function SHALL be applied to a single vector selector, and the two populations SHALL
be joined with `or` outside the range functions, because Prometheus rejects a range over
an `or` expression.

For each of the top five it SHALL state:
- the peak value;
- the time of the peak;
- whether it is a host unit (named by its unit name) or a container (named by its name,
  with the auto-generated-name annotation where it applies).

It SHALL state the resolution of the peak time, which is one range step. The max, min and
range results SHALL be joined on the cgroup `id` label alone. A ranked cgroup with no
sample at the window's last point SHALL be flagged as not present at the window's end. It SHALL state
that working set includes active page cache, and that a cgroup that existed for only part
of the window moved from absent. It SHALL carry no threshold. No template SHALL be filled
from a backend result, and no raw sample series SHALL be returned. A response with no
series for an in-domain node SHALL be not-derivable, and SHALL NOT be an empty ranking.

#### Scenario: A host unit's page-cache burst is named with its peak time
- **WHEN** a host service unit's working set rose from about 100 MB to about 1 GB inside the window while no container moved as much
- **THEN** the result ranks that unit first, named as a host unit by its unit name, with its peak value and the UTC time of the peak

#### Scenario: Containers and host units rank together
- **WHEN** both containers and host units moved within the window
- **THEN** the top five are drawn from both populations by movement, each labelled with which population it belongs to

#### Scenario: Range functions wrap only vector selectors
- **WHEN** the `memory_movers` templates are inspected
- **THEN** every `max_over_time` and `min_over_time` is applied to a single vector selector, and the host-unit and container results are joined by `or` outside them

#### Scenario: Results join on the cgroup id
- **WHEN** the instant max and min results carry no `__name__` label and the range results carry `__name__` and container labels
- **THEN** each cgroup's max, min and peak time are joined by its `id`, and every ranked row carries all three

#### Scenario: A cgroup gone at the window's end is flagged
- **WHEN** a ranked host unit has no sample at the window's last range point
- **THEN** its row is flagged as not present at the window's end, while it keeps its rank and peak

#### Scenario: Peak-time resolution is stated
- **WHEN** `memory_movers` is invoked over a `6h` window
- **THEN** the result states that peak times are accurate to within one range step and gives the step

#### Scenario: No series is not an empty ranking
- **WHEN** the backend returns no working-set series for an in-domain node
- **THEN** the result is not-derivable and states that no cgroup series were returned

### Requirement: host_service_state reports failed and activating host units, and proves it ran
`host_service_state` SHALL report, for the vps, every systemd unit that node-exporter
reported in the `failed` or `activating` state at any sample in the window. For each unit
it SHALL give:
- the unit's name;
- the state;
- how many of the window's samples it was in that state, out of the expected count;
- whether it was still in that state at the window's end.

The result SHALL also state how many units the collector reported, from a separate
count, so that a healthy host is distinguishable from a collector that did not report. A
zero or absent count SHALL be not-derivable, and SHALL NOT be an empty list. A unit that
spent most of the window `activating` SHALL be presented as a probable crash loop, with
the sample fraction as the evidence. Unit names SHALL be passed through the address
scrubber.

#### Scenario: A persistently failed unit is reported with its fraction
- **WHEN** a unit was `failed` in every sample of a 24h window
- **THEN** the result names it, its state `failed`, the fraction in the form `288/288 samples`, and that it is still failed at the window's end

#### Scenario: A crash-looping unit is flagged
- **WHEN** a unit was `activating` in most samples of the window
- **THEN** the result names it as a probable crash loop, with its sample fraction

#### Scenario: A healthy host proves the query ran
- **WHEN** no unit was `failed` or `activating` in the window and the collector reported units
- **THEN** the result states that none were found and how many units the collector reported

#### Scenario: A silent collector is not health
- **WHEN** the unit-count template returns zero or no series
- **THEN** the result is not-derivable and states that the systemd collector reported no units

