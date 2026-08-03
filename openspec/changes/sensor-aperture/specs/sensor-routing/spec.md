## MODIFIED Requirements

### Requirement: Gatus alerts publish to the events topic
Gatus SHALL be configured to publish endpoint failure and resolution alerts to the dedicated
events topic on the vps ntfy instance, identifying the affected endpoint.

Every Gatus-monitored endpoint SHALL carry an explicit routing decision: either an alert
routing to the events topic with a stated failure threshold, or a recorded deliberate
decision not to route it and the reason. An endpoint SHALL NOT be left without alerting
merely because no decision was taken — silent non-coverage is the defect this requirement
exists to prevent, since an unrouted endpoint is indistinguishable from a healthy one.

Failure thresholds SHALL be set per endpoint according to whether an isolated single failed
check is meaningful for that endpoint, rather than applied uniformly. Where a threshold is
lowered such that a single failed check routes an event, the debounce and cooldown pipeline
is the mechanism that absorbs the resulting repeats, and the lowered threshold SHALL be
recorded as a deliberate sensitivity decision with its rationale.

#### Scenario: Endpoint failure produces an event
- **WHEN** a Gatus-monitored endpoint transitions to failing
- **THEN** an event naming that endpoint appears on the events topic

#### Scenario: Every endpoint has a routing decision
- **WHEN** the Gatus configuration is audited
- **THEN** every endpoint either routes alerts to the events topic with a stated threshold, or
  has a recorded reason for not routing

#### Scenario: Single-failure sensitivity is deliberate
- **WHEN** an endpoint is configured so that one failed check routes an event
- **THEN** that endpoint's rationale for single-failure sensitivity is recorded, and repeats
  are absorbed by debounce and cooldown rather than by the threshold

### Requirement: Curated Prometheus subset routes via a Grafana contact point
A Grafana ntfy-compatible contact point and notification policy SHALL route the owner-approved
Prometheus alert subset to the events topic. The subset SHALL remain an explicit closed
allowlist: alert rules outside it SHALL NOT publish to the events topic, and the policy SHALL
NOT be expressed as "route everything except", since deny-by-default is inherited from the
security posture and MUST survive any widening. No Alertmanager SHALL be deployed.

The curated subset SHALL be enumerated in deployment configuration rather than fixed by this
requirement, so that membership can change without a spec change, and every Prometheus alert
rule SHALL have a recorded route-or-not decision so that a rule is never excluded merely by
oversight.

Widening the subset SHALL be staged rather than applied wholesale, so that observed cadence
behaviour can be attributed to a known set of rule families rather than to an
indistinguishable aggregate.

#### Scenario: Curated alert fires
- **WHEN** an alert rule inside the curated subset enters firing state in Prometheus/Grafana
- **THEN** an event identifying the alert appears on the events topic

#### Scenario: Non-curated alert fires
- **WHEN** an alert rule outside the curated subset fires
- **THEN** no event is published to the events topic

#### Scenario: Policy remains an allowlist after widening
- **WHEN** the notification policy is audited after the curated subset is widened
- **THEN** it matches an enumerated set of rules and routes nothing by default

#### Scenario: Every alert rule has a routing decision
- **WHEN** the Prometheus rule set is audited against the curated subset
- **THEN** every rule is either in the subset or has a recorded reason for exclusion
