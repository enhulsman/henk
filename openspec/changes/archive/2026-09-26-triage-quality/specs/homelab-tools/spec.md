## MODIFIED Requirements

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

## ADDED Requirements

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
