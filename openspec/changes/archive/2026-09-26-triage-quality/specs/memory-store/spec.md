## RENAMED Requirements

- FROM: `### Requirement: Recall is injected at the first owner turn of each session`
- TO: `### Requirement: Recall is injected at the first turn of each session`

## MODIFIED Requirements

### Requirement: Recall is injected at the first turn of each session
The first turn of any session that has not yet received the recall block SHALL be prefixed
with it. That applies whether the turn is an **owner turn** or an **event turn**, and it
includes an owner turn continuing a session in which the block was not yet received.

The recall block is all stored memories, from both the `pinned` and the `agent`
namespaces, rendered as markdown grouped by type, newest-first within each group. It sits
inside a clearly delimited data block framed as remembered facts, not instructions. The
rendered block SHALL be bounded (default 8,000 characters). When the bound is hit, the
oldest facts are omitted from the render (across groups) and the block states the count of
omitted memories. Nothing is deleted from the store. Each memory's content SHALL be rendered through the same block-marker neutralisation as
untrusted-block text (incident-triage spec), so that no stored fact can open or close
the recall block or any other block. The block SHALL carry a short content hash of the
rendered block as injected, computed over the neutralised render. An empty store SHALL inject no block.

In an event turn the recall block SHALL precede the untrusted-data block and SHALL NOT be
placed inside it. The owner's facts are not sensor data. Recall in an event turn, and in
any turn of a tainted session, is deliberate. Reads are safe because Henk's outputs are
structurally owner-only. Writes there are denied (approval-gate spec), so a recalled
memory cannot be rewritten by the event that read it. Session continuity rules (`/new`,
idle expiry) are unchanged. Durable recall, not a longer idle window, is the continuity
mechanism.

#### Scenario: New owner session sees memories
- **WHEN** memories exist and a new owner session starts with an owner turn
- **THEN** the first turn's content contains the delimited recall block with the stored memories and its content hash

#### Scenario: Event turns get memory
- **WHEN** an event-triage turn is processed while memories of both types exist
- **THEN** the content passed to the agent session contains the recall block with both types, before and outside the untrusted-data block, and the triage's audit record carries the block's hash

#### Scenario: Owner follow-up in an event-started session is not re-sent recall
- **WHEN** an event turn received the recall block and the owner then sends a follow-up message in that session
- **THEN** the follow-up's content carries no recall block

#### Scenario: Owner follow-up gets recall when the event turn could not read it
- **WHEN** the store could not be read during an event turn and the owner then sends a follow-up in that session while the store is readable
- **THEN** the follow-up's content carries the recall block

#### Scenario: Empty store injects nothing
- **WHEN** no memories exist and a new owner session starts
- **THEN** the turn content contains no recall block

#### Scenario: Recall in an event turn cannot be written back
- **WHEN** an event turn carrying the recall block has a payload instructing Henk to store or change a memory
- **THEN** the memory store is unchanged

#### Scenario: A memory cannot close the recall block
- **WHEN** a stored memory contains the recall block's end marker and the recall block is rendered
- **THEN** the rendered block contains exactly one end marker, the renderer's own, the memory's copy appears in neutralised form, and the stored memory is unchanged

#### Scenario: Render bound holds without data loss
- **WHEN** the stored memories render beyond the block bound
- **THEN** the injected block is within the bound, states how many older memories were omitted, and the store still contains every memory

## ADDED Requirements

### Requirement: Recall renders the memory store and nothing else
The recall block SHALL be rendered from the memory store alone. Retained handoff documents
and their digests (triage-handoff spec), tool results, and any other model output SHALL
NOT be rendered into the recall block. No path SHALL write retained handoff content into
the memory store.

#### Scenario: Handoff history does not enter recall
- **WHEN** handoffs are retained and a new owner session starts while memories exist
- **THEN** the recall block contains the stored memories and no retained handoff text
