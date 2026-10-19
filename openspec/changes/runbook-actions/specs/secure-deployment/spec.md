## MODIFIED Requirements

### Requirement: Scoped secrets only
All secrets SHALL live in a mode-600 `.env` file (or Docker secrets) on rp5 and be limited to: the Anthropic credential, the Tailscale auth key, and per-service scoped tokens (a single ntfy credential scoped per-topic to publish on the notify topic, subscribe on the events topic, publish on the handoffs topic, subscribe on the sessions topic, publish on the runbook request topic, and subscribe on the runbook result topic; the obsidian-todo-api read token; a Taiga MCP token if applicable). The stack SHALL NOT contain SSH keys, broad or admin API tokens, or any work/Anamata credentials, and the agent SHALL NOT be able to read secrets other than through its process environment. The workstation session publisher's ntfy credential SHALL NOT be present in the stack in any form, and neither SHALL the runbook executor's ntfy credential or the workstation's hardware address.

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
- **THEN** the enumerated secret set is unchanged from before the change, and the publisher's write-only token appears nowhere in the stack

#### Scenario: Henk cannot read requests or forge results
- **WHEN** Henk's ntfy credential is used to subscribe to the runbook request topic or to publish to the runbook result topic
- **THEN** ntfy denies the request

#### Scenario: The executor credential is not in the stack
- **WHEN** the deployed stack's environment, volumes, and image are audited after this change
- **THEN** the executor's token and the workstation's hardware address appear nowhere in the stack

## ADDED Requirements

### Requirement: Runbook actions add no network surface and no host mount
Adding runbook actions SHALL introduce no new published port, no new listening socket in the stack or on rp5, no inbound ACL grant, no new Tailscale ACL egress grant, and no new host mount, socket mount, or capability for the agent container. Requests and results SHALL ride the existing `tag:henk` egress grant to vps:2586. The rp5 executor SHALL reach ntfy only outbound, from rp5's host identity, and SHALL accept no connection. The executor SHALL run as a dedicated unprivileged system user whose only privilege is a single sudoers entry for `/usr/local/sbin/agent-job run henk-actions *`, from root-owned files installed outside any user-writable checkout. The zero-inbound posture of `tag:henk` SHALL remain intact.

#### Scenario: ACL unchanged
- **WHEN** the Tailscale ACL policy is compared before and after this change
- **THEN** `tag:henk`'s grants are identical

#### Scenario: Container mounts unchanged
- **WHEN** the agent container's mounts are enumerated after this change
- **THEN** they are its own state/config volumes plus exactly one read-only corpus mount, and no socket

#### Scenario: The executor listens on nothing
- **WHEN** rp5's listening sockets are compared with the executor running and stopped
- **THEN** they are identical

#### Scenario: The executor cannot run outside its project
- **WHEN** the executor's user attempts `sudo /usr/local/sbin/agent-job run henk redeploy` or any command other than `agent-job run henk-actions <job>`
- **THEN** sudo refuses it
