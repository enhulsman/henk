# secure-deployment Specification

## Purpose
TBD - created by archiving change henk-v1. Update Purpose after archive.
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
All secrets SHALL live in a mode-600 `.env` file (or Docker secrets) on rp5 and be limited to: the Anthropic credential, the Tailscale auth key, and per-service scoped tokens (ntfy publish token for one topic, obsidian-todo-api read token, Taiga MCP token if applicable). The stack SHALL NOT contain SSH keys, broad or admin API tokens, or any work/Anamata credentials, and the agent SHALL NOT be able to read secrets other than through its process environment.

#### Scenario: Secret inventory is minimal
- **WHEN** the deployed stack's environment and volumes are audited
- **THEN** only the enumerated scoped secrets are present, and no `~/.ssh`, admin token, or work credential exists anywhere in the stack

### Requirement: Signal account state persists
Signal registration data SHALL persist in a named volume so container recreation does not require re-registering the number, and that volume SHALL be included in rp5's existing backup routine.

#### Scenario: Stack recreation keeps identity
- **WHEN** the stack is torn down and recreated (`docker compose down && up`)
- **THEN** Henk's Signal identity works without re-registration

