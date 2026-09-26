# triage-handoff Specification

## Purpose
The durable half of the attention contract: every triaged incident, cap-suppressed ones
included, leaves a full handoff document — trigger, evidence, diagnosis with confidence,
suggested fix, and how to resume — on a deny-all topic the owner pulls from with `henk-pickup`
from any tailnet host. Suppression therefore costs the owner nothing but an interruption: the
investigation still happened and is still retrievable. The publishing tool takes no topic,
server, or recipient argument, so a handoff can only ever land in the one place.
## Requirements
### Requirement: Handoff doc published per triaged incident
For every triaged incident (including cap-suppressed ones), Henk SHALL publish a handoff document to the dedicated deny-all handoffs topic via a registered notify-class `publish_handoff` tool. The document SHALL contain: the trigger event(s), the evidence gathered (tool findings), the diagnosis with confidence, the suggested fix, and pickup instructions for resuming the investigation.

#### Scenario: Handoff available after triage
- **WHEN** a triage session completes
- **THEN** the handoffs topic holds a document with trigger, evidence, diagnosis + confidence, fix, and pickup instructions, and the Signal message's pickup path references it

#### Scenario: Suppressed incident still hands off
- **WHEN** an incident is suppressed by the cadence cap
- **THEN** its handoff document is still published

### Requirement: Handoff destination is fixed
The `publish_handoff` tool SHALL NOT accept a topic, server, or recipient parameter; it publishes only to the configured handoffs topic. Published content SHALL carry the `[AI]` label per the inherited posture.

#### Scenario: Alternate destination impossible
- **WHEN** the agent produces arguments attempting to target a different topic or server
- **THEN** the tool interface has no such parameter and the document can only go to the configured handoffs topic

### Requirement: henk-pickup retrieves handoffs on demand
A `henk-pickup` CLI in `~/.claude-config/bin` SHALL retrieve handoffs from the handoffs topic pull-based (ntfy poll endpoint, owner read credential), printing the latest handoff by default and supporting listing those within the retention window. It SHALL run without any daemon or new service, from any tailnet host.

#### Scenario: Latest handoff retrieved
- **WHEN** `henk-pickup` is run on any tailnet host after an incident was triaged
- **THEN** it prints the most recent handoff document

#### Scenario: Nothing to pick up
- **WHEN** `henk-pickup` is run and no handoff exists within the retention window
- **THEN** it states that honestly and exits cleanly

### Requirement: Published handoffs are retained locally, bounded by count and age
After the handoffs topic accepts a `publish_handoff`, Henk SHALL retain the published
document in its local store, **when the publishing session was started by an event**.
That includes an owner follow-up inside such a session. With the document it stores:
- the message id;
- the publish time;
- the identity keys, rule keys and nodes of that session's incidents. The application
  supplies these from the session's own context, never from model arguments.

A handoff published in a session no event started SHALL NOT be retained: it carries no
incident context, so it could never be related to a later incident. A publish that failed
SHALL NOT be retained. A retention failure SHALL be logged at error level and SHALL NOT
change the tool's result, because the publish happened.

Retention SHALL be bounded by fixed maxima: a count of 500 and an age of 90 days.
Handoffs beyond either bound SHALL be pruned oldest first, in the same transaction that
inserts a new handoff. A document longer than a fixed byte bound of 32 KB SHALL be stored
truncated, with an explicit truncation marker and flag, and SHALL never be silently cut.

#### Scenario: A published handoff is retained with its incidents
- **WHEN** an event triage publishes a handoff successfully
- **THEN** the store holds that document with its message id, publish time, and the triaged incidents' identity keys, rule keys and nodes

#### Scenario: An owner-session handoff is not retained
- **WHEN** the agent publishes a handoff in a session that no event started
- **THEN** the publish proceeds and nothing is added to the local store

#### Scenario: A failed publish is not retained
- **WHEN** `publish_handoff` fails (timeout or an HTTP error from the topic)
- **THEN** nothing is added to the local store

#### Scenario: A retention failure does not fail the publish
- **WHEN** the handoff is published but the local store write fails
- **THEN** the tool still reports the publish as successful with its message id, and an error is logged

#### Scenario: Retention holds its bounds
- **WHEN** a handoff is inserted while the store holds the maximum count, or holds handoffs older than the maximum age
- **THEN** after the insert the store holds no more than the maximum count and no handoff older than the maximum age, with the oldest removed first

#### Scenario: Incident context cannot come from the model
- **WHEN** `publish_handoff`'s interface is inspected
- **THEN** it accepts only the document, and the identity keys, rule keys and nodes stored with it come from the application's session context

### Requirement: Every event turn carries a digest of related prior handoffs
Every event turn SHALL carry a digest of retained handoffs related to its incidents,
whether or not the turn is a recurrence. A retained handoff is **related** to an incident
when any of the following holds, ranked in this order:
1. **same identity**: it was published for the incident's identity key;
2. **same rule**: it was published for the incident's rule key. The rule key is the
   identity with any per-subject scope suffix removed, so one rule's handoffs for
   different subjects relate;
3. **same node**: its nodes intersect the incident's nodes. An incident's nodes are
   derived from its title and message by whole-word match against the closed node set and
   the known exporter job names only, and never stored as addresses.

Each handoff SHALL appear at most once, at its best rank. Within a rank the newest comes
first. The digest SHALL be bounded by fixed limits:
- at most 3 entries;
- an excerpt of at most 1,200 characters per entry;
- a total of at most 6,000 characters.

When the turn is a recurrence whose prior handoff is retained, that handoff SHALL be the
first entry, marked as the recurrence reference, with an excerpt of up to 4,000
characters. **The recurrence reference counts toward the 6,000-character total.** Further
entries fill the remaining budget in rank order at their own excerpt bound. An entry that
does not fit SHALL be omitted, not shortened below its excerpt bound, and the digest SHALL
state how many related handoffs were omitted. The total SHALL count every
character the digest renders, including its header, each entry's header line, and every
truncation and omission marker. An excerpt that was shortened SHALL say so.
Each entry SHALL state its publish time, its age, and the relation that selected it.

The digest SHALL be rendered **inside** the untrusted-data block, under a header that
labels its entries as **prior model output from earlier triages, not verified fact and not
instructions**. An event turn with no related handoff SHALL carry no digest. The triage's
audit record SHALL list the retained-handoff ids the digest showed.

#### Scenario: A same-identity handoff is shown without a recurrence
- **WHEN** an incident's identity was triaged 20 days ago (outside the recurrence window) and that handoff is retained
- **THEN** the new event turn's untrusted-data block carries that handoff in the digest, related by same identity, with its publish time and age

#### Scenario: Same rule, different subject relates
- **WHEN** a scoped alert fires for one subject and a handoff is retained for the same rule and a different subject
- **THEN** that handoff appears in the digest related by same rule

#### Scenario: Ranking and bounds hold
- **WHEN** more related handoffs are retained than the digest's entry bound, across all three relations
- **THEN** the digest shows at most the bounded number of entries, same-identity before same-rule before same-node and newest first within each, each within its excerpt bound and all within the total bound

#### Scenario: The recurrence reference counts toward the total
- **WHEN** a recurrence's retained prior handoff is longer than 4,000 characters and three further related handoffs are retained
- **THEN** the recurrence reference is excerpted to 4,000 characters, the whole rendered digest — header, entry headers and markers included — is within 6,000 characters, and the digest states how many related handoffs it omitted

#### Scenario: The digest is labelled and delimited as untrusted
- **WHEN** an event turn carries a digest
- **THEN** the digest lies between the untrusted-data block's markers, under a header stating its entries are prior model output and not verified fact or instructions

#### Scenario: No related history means no digest
- **WHEN** no retained handoff is related to any incident in the turn
- **THEN** the turn carries no digest header

#### Scenario: The record names what history was shown
- **WHEN** a triage's event turn carried a digest
- **THEN** the triage's audit record lists the ids of the handoffs the digest showed

### Requirement: Retained handoffs stay inside the triage path
Retained handoffs are model output from tainted sessions. They SHALL reach the model only
through an event turn's digest. They SHALL NOT be rendered into the memory recall block,
into any owner turn's composed content, or into any tool result. No tool SHALL read them.
Retained handoffs SHALL NOT be written into the memory store by any path.

#### Scenario: Owner sessions never see handoff history
- **WHEN** handoffs are retained and the owner then starts a new session and sends messages
- **THEN** no owner turn's content contains any retained handoff text or the digest header

#### Scenario: No tool exposes the archive
- **WHEN** the registered toolset is inspected
- **THEN** no tool reads retained handoffs

