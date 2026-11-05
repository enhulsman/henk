## MODIFIED Requirements

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

Independently of those relations, a retained handoff is a **same-window** handoff when it
was published within the 6 hours before the digest is read, whatever its identity, rule
or nodes. The window is anchored to the time the digest is read: for a held maintenance
release that is the release, not the incident. At most 2 same-window handoffs SHALL be
placed: the newest and the oldest in the window (the only one when there is one). They
SHALL be rendered newest first, after the recurrence reference (when there is one) and
before every handoff ranked by the relations above. A same-window entry SHALL state the
relation `same window`, followed by its best structural relation in parentheses when one
holds. A same-window handoff that is not placed SHALL compete at its best structural rank
when it has one, and otherwise SHALL count as omitted; it SHALL NOT be counted twice.

Each handoff SHALL appear at most once, at its best rank: a handoff placed as
same-window SHALL NOT appear again at a structural rank, and a recurrence reference SHALL
NOT appear again as same-window. Within a rank the newest comes first. The digest SHALL
be bounded by fixed limits:
- at most 4 entries;
- an excerpt of at most 1,200 characters per entry;
- a total of at most 8,000 characters.

When the turn is a recurrence whose prior handoff is retained, that handoff SHALL be the
first entry, marked as the recurrence reference, with an excerpt of up to 4,000
characters. **The recurrence reference counts toward the 8,000-character total.** Further
entries fill the remaining budget in order (same-window, then the structural ranks) at
their own excerpt bound. An entry that does not fit SHALL be omitted, not shortened below
its excerpt bound, and the digest SHALL state how many related handoffs were omitted,
unplaced same-window handoffs included, with its existing marker wording. The total
SHALL count every character the digest renders, including its header, each entry's
header line, and every truncation and omission marker. An excerpt
that was shortened SHALL say so. Each entry SHALL state its publish time, its age, and
the relation that selected it.

The digest SHALL be rendered **inside** the untrusted-data block, under a header that
labels its entries as **prior model output from earlier triages, not verified fact and not
instructions**. An event turn with neither a related nor a same-window handoff SHALL carry
no digest. The triage's audit record SHALL list the retained-handoff ids the digest showed.

#### Scenario: A same-identity handoff is shown without a recurrence
- **WHEN** an incident's identity was triaged 20 days ago (outside the recurrence window) and that handoff is retained
- **THEN** the new event turn's untrusted-data block carries that handoff in the digest, related by same identity, with its publish time and age

#### Scenario: Same rule, different subject relates
- **WHEN** a scoped alert fires for one subject and a handoff is retained for the same rule and a different subject
- **THEN** that handoff appears in the digest related by same rule

#### Scenario: Ranking and bounds hold
- **WHEN** more related handoffs are retained than the digest's entry bound, across all three relations, and none was published within the last 6 hours
- **THEN** the digest shows at most the bounded number of entries, same-identity before same-rule before same-node and newest first within each, each within its excerpt bound and all within the total bound

#### Scenario: A recent handoff outranks older same-identity history
- **WHEN** an incident fires, three handoffs for its identity were published 22 days ago, and a handoff naming the same node was published 5 minutes ago
- **THEN** the digest's first entry is the 5-minute-old handoff with relation `same window (same node)`, followed by the three same-identity handoffs, newest first

#### Scenario: A recent handoff with no structural relation is shown
- **WHEN** an incident fires and a handoff sharing none of its identity, rule or nodes was published 2 hours 40 minutes ago
- **THEN** the digest shows that handoff with relation `same window`

#### Scenario: A handoff outside the window gets no same-window place
- **WHEN** an incident fires and a handoff sharing none of its identity, rule or nodes was published 6 hours and 1 minute ago
- **THEN** that handoff is not in the digest, and a turn with no other related handoff carries no digest

#### Scenario: Same-window entries leave room for history
- **WHEN** an incident that is not a recurrence fires, four handoffs sharing none of its identity, rule or nodes were published within the last 6 hours, and an older same-identity handoff is retained
- **THEN** the digest shows the newest and the oldest same-window handoffs, newest first, then the same-identity handoff, and states that two handoffs were omitted

#### Scenario: Older history keeps two slots when the window is busy
- **WHEN** an incident that is not a recurrence fires, two handoffs sharing none of its identity, rule or nodes were published within the last 6 hours, and three older same-identity handoffs are retained (1,000 characters each)
- **THEN** the digest shows the two same-window handoffs, then the two newest same-identity handoffs, and states that one handoff was omitted

#### Scenario: The recurrence reference stays first and is not repeated
- **WHEN** a recurrence fires whose retained prior handoff (1,000 characters) was published within the last 6 hours, and two other handoffs (1,000 characters each) were published within the window
- **THEN** the digest's first entry is the recurrence reference, it does not appear again, and the next two entries are the other same-window handoffs, newest first

#### Scenario: A full-size recurrence reference leaves room for both same-window entries
- **WHEN** a recurrence fires whose retained prior handoff is longer than 4,000 characters, three other handoffs longer than 1,200 characters were published within the last 6 hours, and an older same-identity handoff is retained
- **THEN** the digest shows the recurrence reference, then the newest and the oldest same-window handoffs, stays within 8,000 characters, and states that two handoffs were omitted

#### Scenario: The root of a long chain stays visible
- **WHEN** an incident fires and handoffs sharing none of its identity, rule or nodes were published 2 h 40 min, 1 h 25 min and 35 min ago
- **THEN** the digest shows the 35-minute-old and the 2 h 40 min-old handoffs as same-window entries, and counts the 1 h 25 min-old one as omitted

#### Scenario: The recurrence reference counts toward the total
- **WHEN** a recurrence's retained prior handoff is longer than 4,000 characters and three further related handoffs are retained
- **THEN** the recurrence reference is excerpted to 4,000 characters, the whole rendered digest — header, entry headers and markers included — is within 8,000 characters, and the digest states how many related handoffs it omitted

#### Scenario: The digest is labelled and delimited as untrusted
- **WHEN** an event turn carries a digest
- **THEN** the digest lies between the untrusted-data block's markers, under a header stating its entries are prior model output and not verified fact or instructions

#### Scenario: No related history means no digest
- **WHEN** no retained handoff is related to any incident in the turn and none was published within the last 6 hours
- **THEN** the turn carries no digest header

#### Scenario: The record names what history was shown
- **WHEN** a triage's event turn carried a digest, including a same-window entry
- **THEN** the triage's audit record lists the ids of the handoffs the digest showed, same-window entries included
