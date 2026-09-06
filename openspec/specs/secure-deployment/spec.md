# secure-deployment Specification

## Purpose
The deployed shape of the inherited security posture: containerized on rp5 with scoped tokens
only, loopback/tailnet binds, an enumerated secret set, least-privilege ACLs, and an enumerated
durable-state surface (audit volume) — so what runs matches what the specs promise, and any new
surface is a deliberate spec change.
## Requirements
### Requirement: Containerized deployment on rp5
Henk SHALL run as a Docker Compose stack on rp5 under `/home/pi/Coding/henk/`, consisting of the agent container, the signal-cli-rest-api container, and a Tailscale sidecar container. The agent container SHALL run as a non-root user, with no Docker socket mount and no host filesystem mounts other than its own state/config volumes **and one enumerated read-only bind mount of the documentation corpus directory**. Every container SHALL have a memory limit.

The corpus bind mount is the **only** permitted host filesystem mount beyond the stack's own volumes. It SHALL be mounted read-only, SHALL be a directory owned and written exclusively by the host-side updater, and SHALL contain nothing but the documentation clone and its freshness stamp. Any further host mount remains a deliberate spec change.

The mount SHALL be declared so that **the container fails to start if the host path does not exist**, rather than allowing the container runtime to create the source directory automatically. Automatic creation would yield a present, readable, empty directory — which makes every documentation lookup return nothing while the deployment appears healthy, and moves a deploy-time typo into a silent runtime condition.

#### Scenario: Stack deploys as containers only
- **WHEN** the stack is deployed
- **THEN** all Henk components run as containers with `restart: unless-stopped`, and no Henk process runs directly on the host

#### Scenario: Agent container is unprivileged
- **WHEN** the agent container is inspected
- **THEN** it runs as a non-root user, has no Docker socket, and mounts nothing from the host outside its own volumes and the read-only corpus mount

#### Scenario: The corpus mount is read-only
- **WHEN** the agent container attempts to write anywhere inside the corpus mount
- **THEN** the write fails, because the mount is read-only

#### Scenario: An absent host path fails the deploy, loudly
- **WHEN** the stack is brought up while the configured corpus host path does not exist
- **THEN** the container fails to start with an error identifying the missing path, and the runtime does not create an empty directory in its place

#### Scenario: No other host mount appears
- **WHEN** the agent container's mounts are enumerated
- **THEN** they are its own state/config volumes plus exactly one read-only corpus mount, and nothing else from the host

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
Durable memory, capture-inbox, and reminder state SHALL live in one SQLite store on the existing backed-up audit volume; adding any of them SHALL NOT add a new volume, published port, listening socket, ACL/egress grant, or secret. The stored content is owner-personal free text and rides the volume's existing backup path. The reminder capability SHALL introduce no inbound surface and no outbound network dependency of its own: its added dependency is the timezone database, which is data read from the image rather than a service call. **The reminder scheduler SHALL introduce no inbound surface either: it is an in-process task with no listener, no port, and no external trigger — its only external effect is an outbound message to the configured owner over the existing channel adapter.**

#### Scenario: No new infrastructure surface
- **WHEN** the deployed stack's volumes, ports, and ACL grants are audited before and after this change
- **THEN** they are identical except for new files on the existing audit volume

#### Scenario: Reminders add no listener
- **WHEN** the running container's listening sockets are inspected with reminders enabled
- **THEN** they are unchanged from before this change

#### Scenario: The timezone database resolves inside the image
- **WHEN** a zone name is resolved in the built image rather than on a development host
- **THEN** it resolves successfully, so no reminder resolution depends on the base image happening to carry a zone database

#### Scenario: The scheduler cannot be triggered from outside
- **WHEN** the scheduler's activation paths are inspected with reminders enabled
- **THEN** ticks originate only from the in-process clock — no endpoint, socket, signal handler, or message can force one

### Requirement: The docs corpus is host-delivered and adds no secret to the container
The documentation corpus SHALL be maintained by the host: a dedicated clone in a
root-owned service directory on rp5, refreshed by a host timer, with the freshness stamp
written by the host beside it. Authentication for the pull SHALL be a **repo-scoped
read-only deploy key held by the host and never present inside any Henk container** — the
stack's enumerated secret set SHALL be unchanged by this capability. The clone SHALL be a
dedicated service directory rather than a personal working checkout, so that only the
updater writes to it.

The stamp writer SHALL be version-controlled in this repository and deployed from it, so
that its success-versus-failure behaviour is covered by tests rather than verified once by
hand. A writer that records a pull time on every *attempt* rather than every *success*
inverts the freshness signal, making a dead updater read as permanently fresh.

#### Scenario: The container holds no docs credential
- **WHEN** the deployed stack's environment and volumes are audited
- **THEN** no deploy key, git credential, or docs-site token is present in any container, and the enumerated secret set is unchanged from before this capability

#### Scenario: Henk makes no request for docs
- **WHEN** the agent container's outbound connections are observed while corpus tools are used
- **THEN** no connection is made to a git host or to the docs site, because the corpus is read from the mount

#### Scenario: The corpus lives in a service directory, not a personal checkout
- **WHEN** the corpus directory on rp5 is inspected
- **THEN** it is a root-owned service directory written only by the updater, and is not the owner's personal working checkout

#### Scenario: A failed pull does not advance the freshness signal
- **WHEN** a scheduled pull fails
- **THEN** the recorded last-pull time is unchanged, so the reported corpus age continues to grow

### Requirement: Read depth adds no network surface
Adding named queries and the documentation corpus SHALL introduce no new published port,
no new listening socket, no inbound ACL grant, and no new Tailscale ACL egress grant. The
named queries SHALL ride the existing `tag:henk` egress grants to rp5:8080 and vps:9090
already required by `homelab_health`, and the corpus SHALL require no grant at all. The
zero-inbound posture of `tag:henk` SHALL remain intact.

#### Scenario: ACL unchanged
- **WHEN** the Tailscale ACL policy is compared before and after this change
- **THEN** `tag:henk`'s grants are identical

#### Scenario: Still nothing can dial in
- **WHEN** any tailnet device attempts to open a connection to the `tag:henk` node after this change
- **THEN** the ACL denies it

#### Scenario: Queries reach only already-granted ports
- **WHEN** every registry template's target is enumerated
- **THEN** each resolves to rp5:8080 or vps:9090, both already within `tag:henk`'s existing grants, and none targets any other host or port

#### Scenario: No new listening socket
- **WHEN** the running container's listening sockets are inspected with both read-depth tools enabled
- **THEN** they are unchanged from before this change

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
