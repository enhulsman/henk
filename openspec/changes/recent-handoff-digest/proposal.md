# Recent handoffs in the triage digest

## Summary for the owner

**What changes for you.** When several alerts fire in one bad night, Henk's later
handoffs will tie back to the earlier ones from the same night. You will see lines
like "likely the same cause as the 03:54 handoff (home site offline)" instead of a
fresh, unconnected diagnosis per alert. That is model behaviour, so it should happen,
not must. When, how often and to whom Henk messages is unchanged, as are approval
prompts. Nothing new is stored.

**Your decisions** (my pick first; the build follows the pick unless you say otherwise):

1. **How far back counts as "the same night"**: 6 hours (pick), 3 hours, or 12 hours.
   At 06:36 the 03:54 handoff was 2 h 42 min old, so 3 hours would have caught it, with
   18 minutes to spare. The link flapped until about 06:32, though, so a recovery or
   retry alert around 07:00 would already have been outside 3 hours. 6 hours covers a
   whole night's incident with margin. 12 hours starts linking unrelated day and night
   incidents.
2. **Room in the digest**: recent handoffs take at most 2 of the existing 3 slots
   (pick), or a 4th slot is added just for them. The 2 are the newest and the oldest
   in the window, so the start of a long chain stays visible. The pick keeps the size and token
   cost the same. A 4th slot keeps all older history but makes every triage prompt
   about 300 tokens longer.

## Why

On 2026-11-05 the home internet dropped at 03:27 CET and flapped until about 06:32.
Henk wrote three handoffs. The 03:59 backup handoff guessed at a failed backup step and
linked it to a 13 October incident. It missed the outage handoff from five minutes
earlier, and the backup had in fact failed because rsync could not reach the VPS. The
06:36 VPS DNS handoff did not see either of the night's earlier handoffs. The audit log
shows the cause: the digest showed handoffs 7, 6, 5 and 12, 8, 4, all from before that
night.

The digest only shows handoffs related by identity, rule or node, ranked in that order
regardless of age. So an old handoff for the same alert outranks a five-minute-old one
about the same machine. And a site outage that shows up on a different node (VPS DNS,
seen from rp5) is not "related" at all. Timing is the strongest signal of a shared
cause, and the digest ignores it.

## What Changes

- A retained handoff published within the recent window (6 h) before the triage is
  eligible for the digest whatever its identity, rule or node. Its relation is
  labelled **same window**.
- Up to 2 such handoffs, newest first, are placed right after the recurrence
  reference (if any) and before the identity, rule and node ranks. Each handoff still
  appears once. The existing bounds (3 entries, 1,200-character excerpts, 6,000
  characters in total) are unchanged.
- When the digest shows a same-window entry, the triage framing tells Henk that the
  entry was picked for its timing alone, and asks him to say whether it shares this
  incident's cause and to link it only when the evidence supports that.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `triage-handoff`: the related-handoff digest requirement gains the same-window
  relation and its slot rule.
- `incident-triage`: the method framing gains a step, present only when the digest
  shows a same-window entry.

## Impact

- Code: `henk/agent/digest.py` (selection and labelling) and `henk/agent/triage.py`
  (framing line). The archive, the audit record (`prior_handoff_ids`) and
  `publish_handoff` are unchanged.
- Tests: `tests/test_handoff_digest.py`, `tests/test_digest_wiring.py`.
- Verification: a test rebuilds last night's archive (placeholder ids) and asserts the
  03:59 and 06:36 turns now show the night's earlier handoffs. A replay cannot show
  this, because replay sends the recorded composed content, digest included, unchanged.
  After deploy, the next multi-alert incident's audit record (`prior_handoff_ids`) is
  the live check.
- No new dependency, no data migration, no config key (the window and slot count are
  module constants, like the existing bounds).

## Left out

- Alerts lost while the home link is down (Gatus on rp5 cannot reach ntfy on the VPS).
  That is a monitoring-path question, separate from Henk.
- A site-level node grouping, such as "home site = rp5 + rp2". The window covers last
  night's case without a new taxonomy.
- An owner-facing tool to browse handoff history (rejected earlier in triage-quality
  D9).
