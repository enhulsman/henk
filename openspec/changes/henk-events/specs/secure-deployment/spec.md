# secure-deployment (delta)

## MODIFIED Requirements

### Requirement: Scoped secrets only
All secrets SHALL live in a mode-600 `.env` file (or Docker secrets) on rp5 and be limited to: the Anthropic credential, the Tailscale auth key, and per-service scoped tokens (a single ntfy credential scoped per-topic to publish on the notify topic, subscribe on the events topic, and publish on the handoffs topic; the obsidian-todo-api read token; a Taiga MCP token if applicable). The stack SHALL NOT contain SSH keys, broad or admin API tokens, or any work/Anamata credentials, and the agent SHALL NOT be able to read secrets other than through its process environment.

#### Scenario: Secret inventory is minimal
- **WHEN** the deployed stack's environment and volumes are audited
- **THEN** only the enumerated scoped secrets are present, and no `~/.ssh`, admin token, or work credential exists anywhere in the stack

#### Scenario: ntfy credential is topic-scoped
- **WHEN** Henk's ntfy credential is used against any topic outside its per-topic grants (or for an ungranted operation on a granted topic)
- **THEN** ntfy denies the request

## ADDED Requirements

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
