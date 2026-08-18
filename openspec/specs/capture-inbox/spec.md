# capture-inbox Specification

## Purpose
The durable landing place for passing thoughts — capture is one message or command away, nothing
is ever silently evicted, and the inbox drains oldest-first. Backed today by a Henk-local store
behind a seam shaped for the future personal-inbox service.
## Requirements
### Requirement: capture tool appends to a durable inbox
The system SHALL provide a `capture` tool (class: mutating, authorization tier: standing, turn scope: owner-only) that appends its text as a new inbox item with a unique id, creation timestamp, source, and status `open`. The item SHALL be durable before the tool result reports success, and the result SHALL confirm the capture with the item's id. Empty or whitespace-only text SHALL be rejected with an explicit error result and nothing stored. Turn-scope and taint enforcement are governed by the approval-gate spec.

#### Scenario: Thought captured and durable
- **WHEN** the agent invokes `capture` with non-empty text and the process is killed non-gracefully (SIGKILL) after the tool result
- **THEN** the item is present in the inbox with status `open` after restart

#### Scenario: Empty capture fails safe
- **WHEN** the agent invokes `capture` with whitespace-only text
- **THEN** nothing is stored and the tool returns an explicit error result

#### Scenario: Capture never prompts
- **WHEN** the agent invokes `capture` during an untainted owner conversation
- **THEN** the item is stored without any approval prompt being sent

### Requirement: /capture owner command
The system SHALL provide a `/capture <text>` owner command that appends an inbox item exactly as the `capture` tool does — same validation, durability, and confirmation-with-id — without an agent turn. Being owner-authored input that never passes through the model, it works in any conversation state, including during a triage follow-up. Its receipt is governed by the audit-log spec.

#### Scenario: Owner captures directly
- **WHEN** the owner sends `/capture buy bike lights`
- **THEN** the item is durably stored, the reply confirms it with the item's id, and no agent session or model tokens are used

### Requirement: Inbox storage sits behind a swappable backend seam
The `capture` and `inbox_read` tool contracts SHALL be independent of the storage backend: tools and commands SHALL access the inbox only through a storage interface (append, list, mark done), with the v1 backend a SQLite store whose deployment surface is governed by the secure-deployment spec ("Memory and inbox stores share the backed-up audit volume"). A replacement backend implementing the same interface (the planned personal-inbox service) SHALL be adoptable without changing tool contracts, agent logic, or this capability's owner-visible behavior.

#### Scenario: Backend swap is behavior-invariant
- **WHEN** the SQLite backend is replaced by a test double implementing the storage interface
- **THEN** `capture` and `inbox_read` pass the same behavioral tests unchanged

#### Scenario: Inbox persists across store reopen
- **WHEN** items exist and the store is closed and reopened on the same file
- **THEN** the items are still present

### Requirement: Inbox read-back drains oldest-first
The system SHALL provide an `inbox_read` tool (class: read-only) and an `/inbox` owner command, both returning the **oldest** N open items (default 20) with id, text, and creation time, plus an explicit count of any newer remainder — a capture inbox is a queue to drain, so the head is always visible and every item is reachable by draining. An `/inbox all` owner command SHALL list every open item (long replies follow the channel adapter's existing message-splitting rules). An `/inbox done <id>` owner command SHALL mark the item with that id as done and confirm; done items SHALL no longer appear in open-item listings but SHALL NOT be deleted. An unknown id SHALL produce an explicit error reply and change nothing. Owner commands run without an agent turn.

#### Scenario: Captured item is readable
- **WHEN** an item has been captured and the agent invokes `inbox_read`
- **THEN** the result lists the item with its id, text, and creation time

#### Scenario: Oldest items are always visible and actionable
- **WHEN** 25 items are open and the owner sends `/inbox`
- **THEN** the oldest 20 are listed with "and 5 newer", and the oldest item's id is shown and can be marked done

#### Scenario: Full listing on demand
- **WHEN** 25 items are open and the owner sends `/inbox all`
- **THEN** all 25 are listed

#### Scenario: Done removes from the open list
- **WHEN** the owner sends `/inbox done <id>` for an open item
- **THEN** the item no longer appears in `/inbox` or `inbox_read` output, and a confirmation is sent

#### Scenario: Unknown id fails honestly
- **WHEN** the owner sends `/inbox done 9999` and no item has that id
- **THEN** nothing changes and the reply states no such item exists

### Requirement: Inbox items are never silently evicted
The inbox SHALL have no cap and no eviction: a captured item SHALL remain until the owner marks it done. Marking done SHALL be the only state change; item text SHALL NOT be edited or deleted by any code path in this capability.

#### Scenario: No eviction under growth
- **WHEN** many items have been captured without any being marked done
- **THEN** every captured item is still present, open, and reachable via the oldest-first listing or `/inbox all`

### Requirement: Inbox store failures are loud but honest
A store write failure (`capture`, `/capture`) SHALL surface as an explicit error and the result SHALL NOT claim the capture succeeded. A store read failure (`inbox_read`, `/inbox`, `/inbox all`) SHALL surface as an explicit error and SHALL NOT be presented as an empty inbox.

#### Scenario: Failed capture is never reported as success
- **WHEN** a `capture` write fails
- **THEN** the tool returns an explicit error result and no success confirmation is produced

#### Scenario: Unreadable inbox is not "empty"
- **WHEN** the store cannot be read and the owner sends `/inbox`
- **THEN** the reply states the failure, not an empty inbox
