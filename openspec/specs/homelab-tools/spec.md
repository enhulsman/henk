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
questions by dispatching on a `query_name` argument whose domain is a **closed enum** of
owner-reviewed registry entries. Each entry SHALL bind `query_name` to a fixed PromQL
expression or Gatus API route recorded in the registry. The v1 enum SHALL be exactly:
`node_resource_trend`, `scrape_targets`, `endpoint_history`, `freshness_check`,
`container_state`, `dns_performance`. A `query_name` outside the enum SHALL be refused as
a tool error with no request issued to any backend.

#### Scenario: A named query dispatches its registered template
- **WHEN** the agent invokes `homelab_query` with `query_name` set to an enumerated name
- **THEN** the request issued to the backend is the expression or route recorded in that registry entry, and the result is rendered from that backend's response

#### Scenario: An unregistered query name is refused before any request
- **WHEN** the agent invokes `homelab_query` with a `query_name` that is not in the enum
- **THEN** the tool returns an error naming the rejected value, and no HTTP request is made to Gatus or Prometheus

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
SHALL NOT be shared across queries where the underlying fleet differs. The v1 domains SHALL
be exactly:

- `node_resource_trend`: `node` ∈ {`rp5`, `vps`, `rp2`}; `resource` ∈ {`cpu`, `memory`,
  `disk`, `load`, `swap_used`, `swap_io`, `temperature`}; `window` ∈ {`15m`, `1h`, `6h`,
  `24h`}.
- `container_state`: `node` ∈ {`rp5`, `vps`} — **rp2 runs no cadvisor**.
- `dns_performance`: `node` ∈ {`rp5`, `vps`, `rp2`}; `window` ∈ {`15m`, `1h`, `6h`, `24h`}.
  The parameter SHALL be named `node`, matching `node_resource_trend`.
- `endpoint_history`: `endpoint` — discovered (see "The endpoint-history domain is
  discovered"); `window` ∈ {`1h`, `24h`, `7d`, `30d`} — Gatus's accepted uptime durations, pinned
  2026-09-02 from the live server's own rejection message (`notes/backend-probe.md` §1.1).
- `scrape_targets`, `freshness_check`: no parameters. `scrape_targets`' lookback window
  SHALL be `24h`.

A parameter value outside the invoked entry's domain SHALL cause an explicit tool error;
the tool SHALL NOT narrow, substitute, or best-effort the query, and SHALL NOT return an
empty result in place of a rejection.

#### Scenario: An out-of-domain node is rejected, not answered emptily
- **WHEN** the agent invokes `container_state` with `node` set to `rp2`
- **THEN** the tool returns an error stating that the value is outside this query's domain, and does not return an empty container list

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
return a summary — first value, last value, minimum, maximum, direction of travel, and any
threshold crossing — rather than the sample series. The step SHALL be derived from the
requested window and a configured maximum point count so that every supported window costs
a bounded amount to render. The tool SHALL NOT return the raw sample series under any
window.

#### Scenario: A trend is summarized, not dumped
- **WHEN** the agent invokes `node_resource_trend` over the longest supported window
- **THEN** the result contains the summary figures and direction, and does not contain the individual samples

#### Scenario: Step scales with the window
- **WHEN** either range query is invoked with each supported window
- **THEN** the number of points requested from Prometheus does not exceed the configured maximum for any window

### Requirement: Threshold comparisons are per-resource and traceable to a measurement
`node_resource_trend` SHALL NOT apply a single generic "crossed the corresponding alert
rule's threshold" comparison, because the fleet's rules do not share a shape. Each resource
SHALL be compared as follows:

- `disk`: scoped to `mountpoint="/"` and reported in the rule's own form, **percent free
  below its bar**, not percent used.
- `swap_io`: the **primary** swap signal, compared against the rule's sustained
  pages-per-second bar.
- `swap_used`: reported, and explicitly labelled as **not the rule's trigger** and not
  alarming on this fleet. Swap fullness and swap pressure are anti-correlated here, so a
  fullness figure approaching a fullness bar SHALL NOT be presented as an approaching
  incident.
- `memory`: compared against the `High memory usage` Grafana rule's live bar, **> 75 %
  used** (pinned 2026-09-02 from the provisioning API, `notes/backend-probe.md` §1.5), and noted as delivering to
  Discord rather than to this agent.
- `cpu`, `load`, `temperature`: reported with **no threshold comparison**, because no rule
  defines one. For `temperature` the result SHALL note that no alert exists in either
  alerting system.

Every numeric threshold in the registry SHALL be traceable to the pinned record of live
rule expressions; the registry SHALL NOT carry a threshold transcribed from documentation
prose.

#### Scenario: Swap fullness is not presented as an approaching incident
- **WHEN** `node_resource_trend` reports `swap_used` at a level below the rule's fullness bar but within the fleet's documented chronic range
- **THEN** the result presents it as normal and states that pressure, not fullness, is what the rule triggers on

#### Scenario: Disk is scoped and expressed in the rule's own form
- **WHEN** `node_resource_trend` reports `disk`
- **THEN** the measurement covers the root filesystem only and is expressed as percent free against the rule's bar

#### Scenario: Resources with no rule carry no bar
- **WHEN** `node_resource_trend` reports `cpu`, `load`, or `temperature`
- **THEN** the result contains the figure and direction and no threshold comparison

#### Scenario: Registry thresholds match the pinned record
- **WHEN** the registry's threshold values are compared against the recorded live rule expressions
- **THEN** every value matches, and no threshold exists in the registry that is absent from the record

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
The system SHALL NOT provide a query that reports alert-rule firing state in v1, and the
closed enum SHALL contain only the six measuring queries.

This is recorded as a deliberate absence rather than an omission. Reading rule state was
excluded by the admission criterion — it measures nothing, and every condition it could
report is either covered by a direct measuring query above or already delivered to the
owner by another route. It would also be actively misleading: the rules that reach this
agent are evaluated outside Prometheus and are invisible to `ALERTS`, so an empty result
could be read as an incident clearing.

#### Scenario: No rule-state query is registered
- **WHEN** the v1 registry is inspected
- **THEN** no entry reports alert-rule firing state, and the closed enum contains only the six measuring queries

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
`container_state` SHALL label `container_start_time_seconds` as the container's **creation**
time and SHALL state that in-place restart loops are not observable from the available
metrics. It SHALL additionally state its **omission semantics**: the container-metrics
exporter drops a container's series entirely when that container stops, so a container
absent from the result may be stopped or removed rather than absent from the host. For any
container the query names explicitly, the template SHALL use a form that returns a value
when the series is absent, so absence yields a reading rather than no series.

#### Scenario: Creation time is not presented as a restart signal
- **WHEN** `container_state` returns a container's creation timestamp
- **THEN** the value is labelled as creation time and the result states that in-place restart loops are not observable

#### Scenario: A stopped container's absence is declared, not silent
- **WHEN** a container has stopped and its series are therefore absent from the backend
- **THEN** the result states that an absent container may be stopped or removed, so its absence is not read as evidence that nothing is wrong

#### Scenario: A named container absent from the backend still returns a reading
- **WHEN** the query names a container explicitly and that container's series is absent
- **THEN** the template returns a value indicating absence rather than an empty series

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
