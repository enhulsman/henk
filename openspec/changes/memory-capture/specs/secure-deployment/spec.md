# secure-deployment Specification (delta)

## ADDED Requirements

### Requirement: Memory and inbox stores share the backed-up audit volume
Durable memory and capture-inbox state SHALL live in a SQLite store on the existing backed-up audit volume; this change SHALL NOT add a new volume, published port, listening socket, ACL/egress grant, or secret. The stored content is owner-personal free text and rides the volume's existing backup path.

#### Scenario: No new infrastructure surface
- **WHEN** the deployed stack's volumes, ports, and ACL grants are audited before and after this change
- **THEN** they are identical except for new files on the existing audit volume
