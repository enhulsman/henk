## MODIFIED Requirements

### Requirement: Scoped secrets only
All secrets SHALL live in a mode-600 `.env` file (or Docker secrets) on rp5 and be limited to: the Anthropic credential, the Tailscale auth key, and per-service scoped tokens (a single ntfy credential scoped per-topic to publish on the notify topic, subscribe on the events topic, publish on the handoffs topic, and subscribe on the sessions topic; the obsidian-todo-api read token; a Taiga MCP token if applicable; the `henk` Google service-account key, shared with exactly one calendar and used only with the `calendar.events` scope). The stack SHALL NOT contain SSH keys, broad or admin API tokens, any OAuth credential for the owner's own Google account, or any work/Anamata credentials, and the agent SHALL NOT be able to read secrets other than through its process environment. The workstation session publisher's ntfy credential SHALL NOT be present in the stack in any form.

#### Scenario: Secret inventory is minimal
- **WHEN** the deployed stack's environment and volumes are audited
- **THEN** only the enumerated scoped secrets are present, and no `~/.ssh`, admin token, or work credential exists anywhere in the stack

#### Scenario: ntfy credential is topic-scoped
- **WHEN** Henk's ntfy credential is used against any topic outside its per-topic grants (or for an ungranted operation on a granted topic)
- **THEN** ntfy denies the request

#### Scenario: Henk cannot write the sessions topic
- **WHEN** Henk's ntfy credential is used to publish to the sessions topic
- **THEN** ntfy denies the request, because the grant is read-only

#### Scenario: The publisher credential is not in the stack
- **WHEN** the deployed stack's environment, volumes, and image are audited after this change
- **THEN** the publisher's write-only token appears nowhere in the stack

#### Scenario: The calendar key is the only new secret
- **WHEN** the deployed stack's environment is audited with the calendar enabled
- **THEN** the only secret added by the calendar is `GOOGLE_CALENDAR_SA_KEY_B64` in `.env`, and the agent process's own environment no longer contains it once the runtime is built

## ADDED Requirements

### Requirement: Calendar access is the one named outbound exception
Calendar access SHALL be the single, named exception to the publisher-push principle (NORTH-STAR principle 3): Henk's process SHALL reach exactly two additional hosts, `oauth2.googleapis.com` and `www.googleapis.com`, over HTTPS, and only while `calendar.enabled` is true. It SHALL introduce no published port, no listening socket, no inbound ACL grant, no new volume or mount, and no Tailscale ACL change; the window snapshot SHALL be a file on the existing audit volume. Every other data source SHALL continue to arrive by publisher push.

Confinement to one calendar SHALL rest on code, not on Google: a service account can reach its own primary calendar and any calendar shared with it, so the client SHALL build request URLs only from the configured calendar id, and no tool or configuration path SHALL supply another. The insert body SHALL never carry `attendees`, which is what keeps Henk without an outbound messaging path through the calendar. Domain-wide delegation SHALL NOT be enabled for the service account; the deploy runbook states this, and the owner confirms it at deploy.

#### Scenario: Requests go to two hosts only
- **WHEN** every request the calendar client can issue is enumerated
- **THEN** each targets `https://oauth2.googleapis.com/token` or a path under `https://www.googleapis.com/calendar/v3/calendars/<configured id>/events`

#### Scenario: Still nothing can dial in
- **WHEN** the running container's listening sockets and the Tailscale ACL are compared before and after enabling the calendar
- **THEN** both are unchanged

#### Scenario: Insert body never names a recipient
- **WHEN** any insert the client can send is inspected
- **THEN** its body's keys are exactly `summary`, `start` and `end`, and its query carries `sendUpdates=none`

#### Scenario: Snapshot stays on the audit volume
- **WHEN** the calendar is enabled and has refreshed
- **THEN** `calendar-windows.json` exists in the directory holding the audit log, and the compose file declares no new volume or mount
