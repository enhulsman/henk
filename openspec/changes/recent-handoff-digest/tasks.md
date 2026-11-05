## 1. Tests first (from the delta scenarios)

- [ ] 1.1 `tests/test_handoff_digest.py`: one test per new `triage-handoff` scenario:
      recent outranks older same-identity history (label `same window (same node)`);
      recent with no structural relation shown as `same window`; 6 h 1 min is outside
      the window and yields no digest when nothing else relates; four unrelated recent
      handoffs plus one old same-identity give the newest and oldest same-window
      entries, then the same-identity one, and an omission count of 2; a recent
      recurrence reference stays first and is not repeated; a full-size reference leaves
      room for one same-window entry, within 6,000 characters; the root of a long chain
      stays visible; an unplaced same-window handoff with a structural relation competes
      at that rank and is not also counted as omitted.
- [ ] 1.2 Boundary tests on the window: published exactly 6 h before `now` is inside;
      a `published_at` later than `now` (clock skew) is not treated as same-window and
      renders an age of 0m rather than a negative one.
- [ ] 1.3 Existing tests whose fixtures fall inside 6 h now run the same-window path
      (new labels, the framing step): `tests/test_handoff_digest.py` at about lines 458,
      494, 607, 660, 733, 752 and 763, and `tests/test_digest_wiring.py` at about 454,
      477 and 561. For each, decide on purpose: move the fixture outside 6 h when the
      test is about structural ranking or recurrence (752 would otherwise pass without
      testing ranking), or keep it and add a same-window assertion. Nothing they assert
      today may be weakened.
- [ ] 1.4 Regression test for 2026-11-05, placeholder-safe (all three of the night's
      handoffs were confirmed in the rp5 archive on 2026-11-05 by message id, so this
      tests a real path): rebuild the archive shape of that night (three 22-day-old same-identity backup handoffs, one older VPS DNS
      handoff, a 03:54 outage handoff naming rp5 and rp2, a 03:59 backup handoff). Assert
      the 03:59 turn's digest shows the 03:54 handoff first, and the 06:36 VPS DNS turn's
      digest shows the 03:59 and 03:54 handoffs before any older one.
- [ ] 1.5 `tests/test_digest_wiring.py` (or the composer tests): the framing carries the
      same-window step only when the digest shows a same-window entry; a turn without
      one composes byte-identical to today's output.
- [ ] 1.6 Audit: a triage whose digest showed a same-window entry lists its id in
      `prior_handoff_ids`.

## 2. Digest selection and labelling

- [ ] 2.1 Add `RECENT_WINDOW_SECONDS = 6 * 3600` and `RECENT_MAX_ENTRIES = 2` beside the
      existing bounds in `henk/agent/digest.py`, with the reasoning from design D1.
- [ ] 2.2 Add `Relation.SAME_WINDOW` and carry the optional structural relation for the
      label (design D2); render `same window` or `same window (<relation>)` in `_entry`.
- [ ] 2.3 In `_read`, pick same-window candidates from `by_id.values()` (window on the
      same `now`, reference excluded; place the newest and the oldest, rendered newest
      first), exclude the placed ones from `rank_related`, and pass them to
      `render_digest` between the reference and the ranked rest.
- [ ] 2.4 Add `extra_omitted` to `render_digest` (design D3a): unplaced same-window
      handoffs with no structural relation are counted there, inside the marker reserve;
      ones with a structural relation compete at their rank and are not counted twice.
- [ ] 2.5 Add `has_same_window` to `Digest`, set from what was actually rendered (an
      entry omitted for size does not count).

## 3. Framing

- [ ] 3.1 Add the same-window step to `_framing` in `henk/agent/triage.py` (wording in
      design D3), after the memory step, passed from `compose_event_turn_content` via
      the digest.

## 4. Verify and ship

- [ ] 4.1 Full suite and lint as CI runs them; `openspec validate recent-handoff-digest --strict`.
- [ ] 4.2 Deploy to rp5 per the README runbook (owner OK).
- [ ] 4.3 Live check after the next incident with more than one triage: its audit record's
      `prior_handoff_ids` includes the earlier handoff from the same window.
- [ ] 4.4 Archive the change on the feature branch.
