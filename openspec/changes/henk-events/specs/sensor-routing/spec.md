# sensor-routing

## ADDED Requirements

### Requirement: Gatus alerts publish to the events topic
Gatus SHALL be configured to publish endpoint failure and resolution alerts to the dedicated events topic on the vps ntfy instance, identifying the affected endpoint.

#### Scenario: Endpoint failure produces an event
- **WHEN** a Gatus-monitored endpoint transitions to failing
- **THEN** an event naming that endpoint appears on the events topic

### Requirement: Curated Prometheus subset routes via a Grafana contact point
A Grafana ntfy-compatible contact point and notification policy SHALL route exactly the owner-approved Prometheus alert subset — `HealthEtl*`, backup freshness, disk usage above 85%, and swap pressure — to the events topic. Alert rules outside the curated subset SHALL NOT publish to the events topic. No Alertmanager SHALL be deployed for this change.

#### Scenario: Curated alert fires
- **WHEN** a `HealthEtl*` alert enters firing state in Prometheus/Grafana
- **THEN** an event identifying the alert appears on the events topic

#### Scenario: Non-curated alert fires
- **WHEN** an alert rule outside the curated subset fires
- **THEN** no event is published to the events topic

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
