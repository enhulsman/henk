## MODIFIED Requirements

### Requirement: Curated Prometheus subset routes via a Grafana contact point
A Grafana ntfy-compatible contact point and notification policy SHALL route exactly the owner-approved Prometheus alert subset — `HealthEtl*`, backup freshness, disk usage above 85%, swap pressure, instance availability, and container restarts — to the events topic. Alert rules outside the curated subset SHALL NOT publish to the events topic. No Alertmanager SHALL be deployed; Prometheus-native alert rules SHALL remain unrouted, and any condition that is to reach the events topic SHALL be expressed as a Grafana-managed rule.

Instance availability SHALL cover every Prometheus scrape target, not only those fronted by an alerting Gatus endpoint, because the exporter-down-while-host-up failure mode is otherwise unobserved and additionally causes metric-dependent rules to report healthy under `noDataState: OK`.

Because a Grafana-managed rule's firing condition is evaluated against its expression's returned **value** whereas a Prometheus-native rule fires on any returned **series**, transcribing a native expression into a Grafana rule SHALL NOT be treated as mechanical. Every curated rule SHALL fire whenever its expression returns a series, irrespective of that series' value, so that a condition expressed as an equality or as a comparison yielding zero is not silently unreportable.

**Container restarts are KNOWN-UNSATISFIED within this subset** *(recorded by `read-depth`,
2026-08-22)*. The `read-depth` change discovered the contradiction while designing a query
over the same metrics, and the North Star requires a requirement that contradicts observed
reality to be amended deliberately rather than absorbed silently into a tool's disclaimer.
No routing behaviour changes here; the gap is recorded so it stops reading as satisfied
coverage.

The container-metrics exporter's start-time series records a container's **creation** time,
not its last start: it does not advance across an in-place restart, and a crash-looping
container retains its container identity — so a change-detecting expression over that
series evaluates to zero in exactly the case the rule exists to catch. This behaviour is
established by measurement, not inference. The clause above requiring every curated rule to
fire on any returned series does **not** rescue it: the bar is irrelevant when the input
never moves. This Prometheus exposes no restart-counter metric, and the Prometheus-native
rule's expression is byte-identical, so both alerting systems are equally blind. Container
crash-looping is therefore **unmonitored**, including for Henk's own container.

Closing it requires a **new metric source**, not an edited expression. A monotonic restart
counter exported through the node-exporter textfile collector is a *candidate, unverified* —
a container runtime's restart count resets on daemon restart and does not advance for an
operator-initiated restart, so its suitability SHALL be established by measurement before
adoption. Recorded as a gap with a candidate direction, not as a specified fix.

#### Scenario: Curated alert fires
- **WHEN** a `HealthEtl*` alert enters firing state in Prometheus/Grafana
- **THEN** an event identifying the alert appears on the events topic

#### Scenario: Non-curated alert fires
- **WHEN** an alert rule outside the curated subset fires
- **THEN** no event is published to the events topic

#### Scenario: A curated condition holds but evaluates to zero
- **WHEN** a curated rule's expression returns a series whose value is zero
- **THEN** the rule enters firing state and an event appears on the events topic

#### Scenario: A curated condition does not hold
- **WHEN** a curated rule's expression returns no series
- **THEN** the rule does not fire and no event is published

#### Scenario: A scrape target stops responding while its host stays reachable
- **WHEN** a Prometheus scrape target reports `up == 0` for longer than the rule's pending duration while the host running it remains reachable
- **THEN** an event identifying the unreachable target by name appears on the events topic

#### Scenario: A container restarts repeatedly
> **KNOWN-UNSATISFIED** — retained as the statement of intent and as the record of a real
> monitoring gap. Do not treat this scenario as covered, and do not attempt to close it by
> editing a rule expression or its firing bar; the input never changes.
- **WHEN** a monitored container restarts more than once inside the rule's evaluation window, retaining its container identity, for longer than the rule's pending duration
- **THEN** an event identifying that container by name appears on the events topic

#### Scenario: The container-restart gap is not mistaken for coverage
- **WHEN** the curated subset's coverage is assessed
- **THEN** container restarts are counted as unmonitored rather than as covered by a deployed rule

#### Scenario: A Prometheus-native rule fires
- **WHEN** a Prometheus-native alert rule enters firing state
- **THEN** no event is published to the events topic, because Prometheus has no configured Alertmanager and delivery is Grafana's responsibility
