> **Live-verified scenarios.** Most scenarios below are verified by the test suite or by
> inspection. These are additionally verified against the deployed instance, and the
> verifying task is named beside each. A scenario not listed here has no live check and
> must not be described as one.
>
> - *ntfy credential is topic-scoped*, *Henk cannot write the sessions topic*, *Publisher
>   cannot read its own topic*, *Publisher cannot reach any other topic*, *Anonymous access
>   is denied* — task **8.2** (`ntfy-provision probe` against the vps, `--expect wo` for the
>   publisher and `--expect ro` for Henk, anonymous confirmed denied).
> - *Secret inventory is minimal*, *The publisher credential is not in the stack*, *ACL
>   unchanged*, *No new listening socket* — task **9.1** (byte-identical `tag:henk` grants
>   before and after, container listening sockets compared with `sessions.enabled` true, and
>   the enumerated secret set re-checked with the publisher token absent).

## MODIFIED Requirements

### Requirement: Scoped secrets only
All secrets SHALL live in a mode-600 `.env` file (or Docker secrets) on rp5 and be limited to: the Anthropic credential, the Tailscale auth key, and per-service scoped tokens (a single ntfy credential scoped per-topic to publish on the notify topic, subscribe on the events topic, publish on the handoffs topic, and subscribe on the sessions topic; the obsidian-todo-api read token; a Taiga MCP token if applicable). The stack SHALL NOT contain SSH keys, broad or admin API tokens, or any work/Anamata credentials, and the agent SHALL NOT be able to read secrets other than through its process environment. The workstation session publisher's ntfy credential SHALL NOT be present in the stack in any form.

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

## ADDED Requirements

### Requirement: Session awareness adds no network surface
Adding the sessions topic and the `sessions_read` tool SHALL introduce no new published
port, no new listening socket, no inbound ACL grant, and no new Tailscale ACL egress
grant. The tool SHALL ride the existing `tag:henk` egress grant to vps:2586 already
required by event intake and the handoff publisher. The zero-inbound posture of `tag:henk`
SHALL remain intact. The workstation publisher SHALL reach ntfy only outbound, and nothing
SHALL dial into the workstation for this feed.

#### Scenario: ACL unchanged
- **WHEN** the Tailscale ACL policy is compared before and after this change
- **THEN** `tag:henk`'s grants are identical

#### Scenario: Still nothing can dial in
- **WHEN** any tailnet device attempts to open a connection to the `tag:henk` node after this change
- **THEN** the ACL denies it

#### Scenario: The tool reaches only the already-granted ntfy port
- **WHEN** every request the `sessions_read` tool can issue is enumerated
- **THEN** each resolves to the configured ntfy base URL on vps:2586, and none targets any other host or port

#### Scenario: No new listening socket
- **WHEN** the running container's listening sockets are inspected with `sessions.enabled` true
- **THEN** they are unchanged from before this change

### Requirement: The workstation publisher holds a write-only credential for one topic
The session publisher SHALL authenticate to ntfy as a dedicated user whose only grant is
write-only on the sessions topic. Its token SHALL be held on the workstation at mode 600
inside a mode-700 directory under the `~/.config/<consumer>/<name>-token` convention, and
SHALL NOT be shared with any other workstation ntfy consumer. The publisher SHALL NOT hold
any Henk credential, and Henk SHALL NOT hold the publisher's.

#### Scenario: Publisher cannot read its own topic
- **WHEN** the publisher's credential is used to subscribe to or poll the sessions topic
- **THEN** ntfy denies the request

#### Scenario: Publisher cannot reach any other topic
- **WHEN** the publisher's credential is used to publish to any topic other than the sessions topic
- **THEN** ntfy denies the request

#### Scenario: Anonymous access is denied
- **WHEN** the sessions topic is published to or read without a credential
- **THEN** ntfy denies the request
