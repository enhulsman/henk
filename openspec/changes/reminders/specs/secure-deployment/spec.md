# secure-deployment Specification (delta)

## MODIFIED Requirements

### Requirement: Memory and inbox stores share the backed-up audit volume
Durable memory, capture-inbox, and reminder state SHALL live in one SQLite store on the existing backed-up audit volume; adding any of them SHALL NOT add a new volume, published port, listening socket, ACL/egress grant, or secret. The stored content is owner-personal free text and rides the volume's existing backup path. The reminder scheduler SHALL introduce no inbound surface: it is an in-process task with no listener, no port, and no external trigger — its only external effect is an outbound message to the configured owner over the existing channel adapter.

#### Scenario: No new infrastructure surface
- **WHEN** the deployed stack's volumes, ports, and ACL grants are audited before and after this change
- **THEN** they are identical except for new files on the existing audit volume

#### Scenario: Scheduler adds no listener
- **WHEN** the running container's listening sockets are inspected with reminders enabled
- **THEN** they are unchanged from before this change
