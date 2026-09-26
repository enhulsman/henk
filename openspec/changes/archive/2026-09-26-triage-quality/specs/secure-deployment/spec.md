## MODIFIED Requirements

### Requirement: Memory and inbox stores share the backed-up audit volume
Durable memory, capture-inbox, reminder, and **retained-handoff** state SHALL live in one
SQLite store on the existing backed-up audit volume. Adding any of them SHALL NOT add a
new volume, published port, listening socket, ACL/egress grant, or secret. The stored
content is owner-personal free text and model-authored handoff text, and it rides the
volume's existing backup path. The reminder capability SHALL introduce no inbound surface
and no outbound network dependency of its own. Its added dependency is the timezone
database, which is data read from the image rather than a service call. **The reminder
scheduler SHALL introduce no inbound surface either: it is an in-process task with no
listener, no port, and no external trigger. Its only external effect is an outbound
message to the configured owner over the existing channel adapter.**

#### Scenario: No new infrastructure surface
- **WHEN** the deployed stack's volumes, ports, and ACL grants are audited before and after this change
- **THEN** they are identical except for new files on the existing audit volume

#### Scenario: Retained handoffs share the one store file
- **WHEN** handoffs are retained
- **THEN** they are stored in the same SQLite file as memories, inbox and reminders, and no new database file, volume or mount exists

#### Scenario: Reminders add no listener
- **WHEN** the running container's listening sockets are inspected with reminders enabled
- **THEN** they are unchanged from before this change

#### Scenario: The timezone database resolves inside the image
- **WHEN** a zone name is resolved in the built image rather than on a development host
- **THEN** it resolves successfully, so no reminder resolution depends on the base image happening to carry a zone database

#### Scenario: The scheduler cannot be triggered from outside
- **WHEN** the scheduler's activation paths are inspected with reminders enabled
- **THEN** ticks originate only from the in-process clock — no endpoint, socket, signal handler, or message can force one

## ADDED Requirements

### Requirement: Triage recordings and replay add no surface and stay on the audit volume
Triage recordings, reference cases and replay outputs SHALL be written only beneath the directory holding
the audit log, at paths derived from the audit log's path rather than configured. No
configuration key SHALL be able to place them elsewhere. They SHALL NOT be committed to
this repository. They ride the audit volume's existing backup, so their effective
retention is their live retention plus the backup's snapshot retention. The homelab
backup documentation SHALL record this.

The replay and grading entry point SHALL run as a **one-shot container of the henk
service** (`docker compose run --rm --no-deps`), invoked by the owner, using the service's
existing image, volumes and credential. It SHALL NOT run by `exec` inside the live
container, where its memory would count against the live process's limit. It SHALL use a
separate agent-CLI state directory from the live process.

The entry point SHALL refuse to run, naming the expected path, when the audit log is
absent. That is the symptom of the compose project resolving to a copy of the checkout
with empty volumes. It SHALL add no volume, published port, listening socket, ACL/egress
grant, or secret, and it SHALL NOT run as a long-lived service.

#### Scenario: Recording paths are derived from the audit path
- **WHEN** the recording and replay-output directories are resolved for a given configuration
- **THEN** both are subdirectories of the audit log's directory, and no configuration key changes that

#### Scenario: Replay cannot starve the live process
- **WHEN** the documented replay command is run while Henk is live
- **THEN** the replay runs in its own container with its own memory limit, and the live container's memory accounting is unaffected

#### Scenario: Replay keeps its own agent-CLI state
- **WHEN** the documented replay command runs while Henk is live
- **THEN** the replay's agent-CLI state directory differs from the live process's, and no file in the live process's agent-CLI state directory is created or modified by the replay

#### Scenario: A wrong compose project is refused
- **WHEN** the replay entry point starts and the audit log does not exist at the configured path
- **THEN** it exits without any model call, naming the path and stating that the compose project may have resolved to a copy with empty volumes

#### Scenario: Replay adds no network surface
- **WHEN** the host's listening sockets, published ports and the container secrets are inspected before and during a replay
- **THEN** they are unchanged

#### Scenario: Recordings are never in the repository
- **WHEN** the repository's tracked files are inspected
- **THEN** none is a triage recording, replay output, or grade file, and every test fixture recording uses placeholder hostnames and documentation-range addresses
