## MODIFIED Requirements

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

## ADDED Requirements

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
