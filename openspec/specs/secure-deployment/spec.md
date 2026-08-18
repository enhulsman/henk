# secure-deployment Specification

## Purpose
The deployed shape of the inherited security posture: containerized on rp5 with scoped tokens
only, loopback/tailnet binds, an enumerated secret set, least-privilege ACLs, and an enumerated
durable-state surface (audit volume) — so what runs matches what the specs promise, and any new
surface is a deliberate spec change.
## Requirements
### Requirement: Containerized deployment on rp5
Henk SHALL run as a Docker Compose stack on rp5 under `/home/pi/Coding/henk/`, consisting of the agent container, the signal-cli-rest-api container, and a Tailscale sidecar container. The agent container SHALL run as a non-root user, with no Docker socket mount and no host filesystem mounts other than its own state/config volumes. Every container SHALL have a memory limit.

#### Scenario: Stack deploys as containers only
- **WHEN** the stack is deployed
- **THEN** all Henk components run as containers with `restart: unless-stopped`, and no Henk process runs directly on the host

#### Scenario: Agent container is unprivileged
- **WHEN** the agent container is inspected
- **THEN** it runs as a non-root user, has no Docker socket, and mounts nothing from the host outside its own volumes

### Requirement: Own tailnet identity with least-privilege tag
Henk SHALL join the tailnet as its own node via the Tailscale sidecar, tagged `tag:henk`, using a pre-authorized auth key scoped to that tag. The ACL (via a PR to the GitOps repo) SHALL grant `tag:henk` egress only to `tag:server` on the exact ports its tools require (8080, 8000 on rp5; 9090, 8089, 2586 on vps), SHALL grant no inbound access to `tag:henk`, and SHALL grant `tag:henk` no SSH rules.

#### Scenario: Tool egress uses Henk's identity
- **WHEN** a Henk tool queries a homelab service
- **THEN** the traffic originates from the `tag:henk` node, not from rp5's host identity

#### Scenario: Out-of-scope port blocked
- **WHEN** a process in the Henk stack attempts to reach a tailnet service on any port outside the granted list (e.g., vps:5432)
- **THEN** the ACL denies the connection

#### Scenario: Nothing can dial in
- **WHEN** any tailnet device attempts to open a connection to the `tag:henk` node
- **THEN** the ACL denies it

### Requirement: Signal bridge is never exposed
The signal-cli-rest-api container SHALL attach only to the compose-internal network: no published ports, no tailnet attachment, no Cloudflare tunnel entry. Only the agent container SHALL be able to reach its API.

#### Scenario: Bridge unreachable from outside the stack
- **WHEN** the bridge's API port is probed from the rp5 host, the LAN, or the tailnet
- **THEN** the connection fails; only the agent container, via the internal network, can connect

### Requirement: Scoped secrets only
All secrets SHALL live in a mode-600 `.env` file (or Docker secrets) on rp5 and be limited to: the Anthropic credential, the Tailscale auth key, and per-service scoped tokens (a single ntfy credential scoped per-topic to publish on the notify topic, subscribe on the events topic, and publish on the handoffs topic; the obsidian-todo-api read token; a Taiga MCP token if applicable). The stack SHALL NOT contain SSH keys, broad or admin API tokens, or any work/Anamata credentials, and the agent SHALL NOT be able to read secrets other than through its process environment.

#### Scenario: Secret inventory is minimal
- **WHEN** the deployed stack's environment and volumes are audited
- **THEN** only the enumerated scoped secrets are present, and no `~/.ssh`, admin token, or work credential exists anywhere in the stack

#### Scenario: ntfy credential is topic-scoped
- **WHEN** Henk's ntfy credential is used against any topic outside its per-topic grants (or for an ungranted operation on a granted topic)
- **THEN** ntfy denies the request

### Requirement: Signal account state persists
Signal registration data SHALL persist in a named volume so container recreation does not require re-registering the number, and that volume SHALL be included in rp5's existing backup routine.

#### Scenario: Stack recreation keeps identity
- **WHEN** the stack is torn down and recreated (`docker compose down && up`)
- **THEN** Henk's Signal identity works without re-registration

### Requirement: Event intake adds no network exposure
This change SHALL introduce no new published ports, no listening sockets, no inbound ACL grants, and no new Tailscale ACL egress grants — event intake and handoff publishing ride the existing vps:2586 grant. The zero-inbound posture of `tag:henk` SHALL remain intact.

#### Scenario: ACL unchanged
- **WHEN** the Tailscale ACL policy is compared before and after this change
- **THEN** `tag:henk`'s grants are identical

#### Scenario: Still nothing can dial in
- **WHEN** any tailnet device attempts to open a connection to the `tag:henk` node after this change
- **THEN** the ACL denies it

### Requirement: Audit volume persists and is backed up
The audit log SHALL live on a named Docker volume that survives container recreation and is included in rp5's backup volume allowlist.

#### Scenario: Recreation keeps the audit trail
- **WHEN** the stack is torn down and recreated
- **THEN** previously written audit records are still present

### Requirement: Graceful shutdown within the container stop grace period
The process SHALL handle SIGTERM (as sent by `docker stop`) by unwinding its shutdown path — cancelling the receive loop and coordinator, and flushing the open session's audit record — within the container's stop grace period, so state is flushed cleanly rather than lost to a SIGKILL escalation. SIGINT SHALL behave identically for interactive shutdown.

#### Scenario: docker stop flushes cleanly
- **WHEN** the container receives SIGTERM while a session is open
- **THEN** the open session's audit record is flushed and the process exits within the stop grace period without escalating to SIGKILL

#### Scenario: Intake offset persisted at shutdown
- **WHEN** the process is stopped gracefully
- **THEN** the last-seen event id checkpoint on the audit volume reflects the most recently processed event, so the next start resumes correctly

### Requirement: Pipeline checkpoints share the backed-up audit volume
Durable pipeline state (the intake offset checkpoint and any cadence-rehydration source) SHALL live on the existing backed-up audit volume; this change SHALL NOT add a new volume, published port, listening socket, or ACL/egress grant.

#### Scenario: No new infrastructure surface
- **WHEN** the deployed stack's volumes, ports, and ACL grants are audited before and after this change
- **THEN** they are identical except for new files on the existing audit volume

### Requirement: Memory and inbox stores share the backed-up audit volume
Durable memory and capture-inbox state SHALL live in a SQLite store on the existing backed-up audit volume; this change SHALL NOT add a new volume, published port, listening socket, ACL/egress grant, or secret. The stored content is owner-personal free text and rides the volume's existing backup path.

#### Scenario: No new infrastructure surface
- **WHEN** the deployed stack's volumes, ports, and ACL grants are audited before and after this change
- **THEN** they are identical except for new files on the existing audit volume
