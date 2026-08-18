# memory-store Specification

## Purpose
Henk's durable, capped store of short owner facts — continuity by rebuild: what the owner tells
Henk survives restarts and idle expiry and is recalled into every owner conversation, with
explicit, receipted write paths and structural protection from untrusted input.
## Requirements
### Requirement: Durable, capped, type-namespaced memory store
The system SHALL persist owner memories as one row per short natural-language fact in a SQLite store whose deployment surface is governed by the secure-deployment spec ("Memory and inbox stores share the backed-up audit volume"), so memories survive process restarts and container recreation. Each memory SHALL carry a type namespace: `pinned` (owner-authored, cap 50) or `agent` (agent-authored, cap 20). A fact longer than a configured limit (default 500 characters) SHALL be rejected with an explicit error naming the limit — never silently truncated. When an insert would exceed its type's cap, the oldest memories of that type SHALL be evicted first (FIFO) until the cap holds, and the write's confirmation (command reply or tool result) SHALL name the evicted memory's content; types SHALL be capped independently, and eviction in one type SHALL never remove memories of another type.

#### Scenario: Memory survives restart
- **WHEN** a memory is stored and the process is then restarted
- **THEN** the memory is present in recall after the restart

#### Scenario: FIFO eviction at the type cap names the evicted
- **WHEN** a memory is added to a type already at its cap
- **THEN** the oldest memory of that type is evicted, the new memory is stored, the type's count equals its cap, and the confirmation names the evicted memory's content

#### Scenario: Types are capped independently
- **WHEN** the `agent` type is at its cap and a `pinned` memory is added
- **THEN** no `agent` memory is evicted

#### Scenario: Over-limit fact rejected honestly
- **WHEN** a fact exceeding the length limit is submitted via `/remember` or `store_memory`
- **THEN** nothing is stored and the reply or tool result states the limit; no truncated variant is stored

### Requirement: Owner memory commands
The system SHALL provide owner commands handled without an agent turn: `/remember <text>` SHALL store `<text>` as a `pinned` memory and reply with a confirmation; `/forget <text>` SHALL delete all memories whose trimmed content contains `<text>` as a case-insensitive substring, and reply echoing the content of the removed memories — up to 10 echoed in full, further removals reported as a count — so a mistaken bulk forget is recoverable by re-adding (or reply that none matched); `/memories` SHALL reply with the stored memories listed with their ids and types. A `/remember` with empty or whitespace-only text SHALL store nothing and reply that the text was empty. Mutating command receipts are governed by the audit-log spec ("Mutation receipts are durable at decision time").

#### Scenario: Remember stores and confirms
- **WHEN** the owner sends `/remember the workstation dual-boots via GRUB`
- **THEN** a pinned memory with that content exists and the owner receives a confirmation reply

#### Scenario: Forget removes matching memories and echoes them
- **WHEN** the owner sends `/forget backup` and two stored memories contain "backup"
- **THEN** both memories are deleted and the reply echoes both removed contents

#### Scenario: Forget with no match is honest
- **WHEN** the owner sends `/forget quantum` and no stored memory matches
- **THEN** no memory is deleted and the reply states nothing matched

#### Scenario: Memories are listable
- **WHEN** the owner sends `/memories`
- **THEN** the reply lists every stored memory with its id and type

#### Scenario: Empty remember is rejected
- **WHEN** the owner sends `/remember` with no text
- **THEN** nothing is stored and the reply says the text was empty

### Requirement: store_memory agent tool
The system SHALL provide a `store_memory` tool (class: mutating, authorization tier: standing, turn scope: owner-only) that stores its text argument as an `agent`-type memory. The tool SHALL reject empty or whitespace-only content with an explicit error result. The stored memory SHALL be durable before the tool result reports success. Turn-scope and taint enforcement are governed by the approval-gate spec.

#### Scenario: Agent stores a memory
- **WHEN** the agent invokes `store_memory` with non-empty text in an untainted owner session
- **THEN** an `agent`-type memory with that content exists and the tool result confirms it was stored

#### Scenario: Empty content fails safe
- **WHEN** the agent invokes `store_memory` with whitespace-only text
- **THEN** nothing is stored and the tool returns an explicit error result

### Requirement: Untrusted event input cannot reach memory
No content authored during an event turn or any turn of a tainted session SHALL enter the memory store: `store_memory` is owner-turn-only and the gate denies it in those contexts (approval-gate spec), and no other write path exists for model-authored content. The owner-command write paths are exempt — they are owner-authored input that never passes through the model.

#### Scenario: Event payload cannot plant a memory
- **WHEN** an event turn's payload instructs Henk to store a memory and the turn is processed
- **THEN** the memory store is unchanged

#### Scenario: Tainted-session content never reaches later sessions
- **WHEN** an event-started session's turns attempt memory writes and a new owner session later starts
- **THEN** the new session's recall block contains no content authored during the tainted session

### Requirement: Recall is injected at the first owner turn of each session
The first **owner turn** of any session that has not yet received it — including an owner turn continuing a session that an event turn started — SHALL be prefixed with a recall block: all stored memories rendered as markdown grouped by type, newest-first within each group, inside a clearly delimited data block framed as remembered facts (not instructions). The rendered block SHALL be bounded (default 8,000 characters); when the bound is hit, the oldest facts are omitted from the render (across groups) and the block states the count of omitted memories — nothing is deleted from the store. The block SHALL carry a short content hash of the rendered block as injected. An empty store SHALL inject no block. Event turns SHALL never carry the recall block — memory is never mixed into the untrusted-sensor-data path. (Recall in a tainted session is deliberate: reads are safe because Henk's outputs are structurally owner-only; writes there are denied — see the approval-gate spec.) Session continuity rules (`/new`, idle expiry) are unchanged; durable recall, not a longer idle window, is the continuity mechanism.

#### Scenario: New owner session sees memories
- **WHEN** memories exist and a new owner session starts with an owner turn
- **THEN** the first turn's content contains the delimited recall block with the stored memories and its content hash

#### Scenario: Owner follow-up in an event-started session gets recall
- **WHEN** an event turn starts a session and the owner then sends a follow-up message
- **THEN** that owner turn's content carries the recall block (and the event turn's did not)

#### Scenario: Empty store injects nothing
- **WHEN** no memories exist and a new owner session starts
- **THEN** the turn content contains no recall block

#### Scenario: Event turns get no memory
- **WHEN** an event-triage turn is processed while memories exist
- **THEN** the content passed to the agent session contains no recall block

#### Scenario: Render bound holds without data loss
- **WHEN** the stored memories render beyond the block bound
- **THEN** the injected block is within the bound, states how many older memories were omitted, and the store still contains every memory

### Requirement: Memory store failures are loud but honest
A store read failure during recall SHALL be logged at error level and SHALL NOT block the turn — the session proceeds without a recall block. A store write failure (`/remember`, `store_memory`) SHALL surface as an explicit error reply or tool error result and SHALL NOT be reported as success.

#### Scenario: Unreadable store does not block conversation
- **WHEN** the store cannot be read at session start
- **THEN** the turn proceeds with no recall block and an error is logged

#### Scenario: Failed write is never reported as success
- **WHEN** a `/remember` write fails
- **THEN** the reply states the failure and no success confirmation is sent
