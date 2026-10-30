# Scrutiny record: `approval-reactions` proposal

**Reviewed:** 2026-10-30, two rounds by one `project-scrutinizer`, read against the base
`approval-gate` and `channel-adapter` specs, the gate, channel, app, tool and audit code,
and the calendar archive branch. `openspec validate approval-reactions --strict` passed
after each round.

| Round | Verdict | Blocking findings | What moved |
|---|---|---|---|
| 1 | NEEDS REWORK | 0 critical, 5 major, 13 minor | Undeclared argument keys could forge prompt lines (an existing gap, now closed: D8). The 4xx fallback could not fire against the real bridge client (`SignalBridgeRefused`, not retried, tested on the real class). A trailing backslash could escape its monospace span (`⧵` stand-in). The live escaping check ran with styling off (now on, prompt denied). The pending slot's lifetime around the edit was unstated (released first). Minors: first answer wins atomically, at-least-once wording, edit mode follows the prompt's, size bound on the sent form, default off until the live check, D7 corrected, four base requirements MODIFIED so the synced spec is not keyword-only, references normalised to int, edit notifications observed. |
| 2 | APPROVED WITH CONCERNS | 0 critical, 1 major, 1 minor | Gate-caused cancels (undeclared key, oversized prompt) got their own model-facing reasons and receipt details instead of the unrelated-message reason. The footer pick ("Any other message cancels it.") written into proposal, design and spec, pending owner decision 3. Both applied. |

**Accepted, stated in D4 and the proposal:** a reaction that arrives before the prompt
reference is stored, or on a prompt sent without one, is ignored; the owner reacts again
or types.

**Sequencing:** the calendar requirement this change modifies exists only on
`chore/archive-maintenance-calendar`; task 0.1 rebases after that merges.
