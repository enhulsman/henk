# sensor-routing Specification

## Purpose
What the homelab is allowed to tell Henk about, and how. Exactly one curated alert subset
reaches a deny-all events topic — Gatus endpoint transitions plus owner-approved Grafana-managed
rules — under a payload contract that lets triage name an incident without querying the sensor
first and lets intake derive a stable alert identity. Two constraints protect the owner from the
agent: routed critical alerts keep their pre-existing non-agent delivery path, because Henk
applies cooldowns and a daily cap and is a single process that can be down, so it may never be
the sole path for something critical; and routing config is convergent, so re-applying it
changes nothing and preserves existing alert semantics.
## Requirements
### Requirement: Gatus alerts publish to the events topic
Gatus SHALL be configured to publish endpoint failure and resolution alerts to the dedicated events topic on the vps ntfy instance, identifying the affected endpoint.

#### Scenario: Endpoint failure produces an event
- **WHEN** a Gatus-monitored endpoint transitions to failing
- **THEN** an event naming that endpoint appears on the events topic

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

### Requirement: Events follow a minimal payload contract
Every event published by a sensor SHALL follow the payload contract: the ntfy message **title** carries the source system (Gatus or Grafana/Prometheus), the alert or endpoint name, and firing-versus-resolved state (achieved via a notification template on the Grafana side and alert description/placeholders on the Gatus side); the body is free-form detail. This lets triage name the incident without querying the sensor first, and lets the intake derive a stable alert identity (see event-intake).

#### Scenario: Event content is triageable
- **WHEN** any sensor publishes an event
- **THEN** the event's title contains the source, the alert or endpoint name, and its state

### Requirement: Events topic is deny-all with least-privilege grants
The events topic SHALL live on the vps ntfy instance under its default-deny access policy, with write access granted only to sensor identities and read access granted only to Henk's ntfy user and the owner's admin account. Anonymous access SHALL be denied.

#### Scenario: Anonymous publish rejected
- **WHEN** an unauthenticated client attempts to publish to the events topic
- **THEN** ntfy rejects the request

#### Scenario: Henk can subscribe
- **WHEN** Henk's ntfy user opens a subscription to the events topic
- **THEN** the subscription is accepted and events stream to it

### Requirement: Critical routed alerts retain a delivery path Henk cannot suppress
Any alert routed to the events topic at critical severity SHALL also be delivered to the pre-existing non-agent contact point. Henk applies a per-alert cooldown and a daily cap on unprompted messages, and is itself a single process that can be down; it SHALL therefore never be the sole delivery path for a critical alert. Non-critical routed alerts MAY be delivered to the events topic alone.

Because a notification-policy child route consumes the alert — the parent receiver is used only when no child matches — dual delivery SHALL be achieved by a second sibling route matching the same alert, not by relying on fallthrough to the parent. That sibling route SHALL be selected by the alert's own severity rather than by a routing-specific marker, so the obligation extends automatically to alerts introduced later. It SHALL additionally be constrained to the curated subset, so that alerts outside that subset continue to reach the non-agent contact point by their existing path and do not acquire this route's grouping or timing.

#### Scenario: A critical routed alert fires
- **WHEN** an alert in the curated subset at critical severity enters firing state
- **THEN** it is delivered both to the events topic and to the non-agent contact point

#### Scenario: A warning-severity routed alert fires
- **WHEN** an alert in the curated subset at warning severity enters firing state
- **THEN** it is delivered to the events topic only, and the non-agent contact point receives nothing

#### Scenario: A critical alert outside the curated subset fires
- **WHEN** an alert at critical severity that is not in the curated subset enters firing state
- **THEN** it reaches the non-agent contact point exactly as before this change, with unchanged grouping and timing, and no events-topic delivery occurs

#### Scenario: A critical alert is declared without a dual-delivery path
- **WHEN** the declared configuration contains a curated rule at critical severity that no declared route delivers to a non-agent receiver
- **THEN** the configuration is rejected before any change is applied

### Requirement: Routing configuration is convergent and semantics-preserving
The provisioning of contact points, alert rules, and notification policy SHALL be convergent: applying the declared configuration to a system already in that state SHALL succeed and leave the state unchanged. Re-application SHALL NOT duplicate notification-policy routes and SHALL NOT alter the expression, threshold, firing condition, or pending duration of any existing rule except where the declared configuration deliberately changes it. Comparison SHALL normalise representational differences between what is written and what the configuration interface reads back, so that an unchanged system never reports as diverged.

Where deployed state diverges from the declared configuration in a way the declaration does not account for, the applier SHALL refuse to apply and report the divergence rather than overwrite it. Objects present in the managed scope but absent from the declaration SHALL be reported and SHALL block application, but SHALL NOT be deleted.

Application SHALL be ordered so that no intermediate state violates a requirement that the final state satisfies. Where full atomicity is unavailable, the notification policy SHALL be written before the rules that depend on it, and a failure part-way SHALL restore the notification policy to its pre-application state.

After a successful apply, the resulting rule and policy state SHALL be recorded to a durable, credential-free artifact, so that deployed routing state is readable without inferring it from the provisioning source.

#### Scenario: Applying to an already-provisioned system
- **WHEN** the declared configuration is applied to a system already in the declared state
- **THEN** the apply succeeds, the notification policy contains no duplicated routes, and no rule's expression, firing condition, threshold, or pending duration changes

#### Scenario: Written and read-back representations differ
- **WHEN** the configuration interface reads back a declared value in a different but equivalent representation
- **THEN** the comparison reports no divergence and the object is left unchanged

#### Scenario: A deployed rule was retuned outside the declared configuration
- **WHEN** a deployed alert rule's expression differs from the declared configuration and the declaration does not record that difference as intended
- **THEN** the applier refuses to apply, names the diverged rule, and leaves the deployed rule untouched

#### Scenario: An undeclared rule exists in the managed scope
- **WHEN** the managed scope contains an alert rule that the declaration does not describe
- **THEN** the applier reports it and refuses to apply, and does not delete it

#### Scenario: Application fails part-way
- **WHEN** application fails after the notification policy has been written but before all rules have converged
- **THEN** the notification policy is restored to its pre-application state, and no rule is left at critical severity in the curated subset without a dual-delivery route

#### Scenario: Reading deployed routing state
- **WHEN** the deployed routing state is inspected after a successful apply
- **THEN** the recorded state artifact reflects the live rule set and policy tree and contains no credentials
