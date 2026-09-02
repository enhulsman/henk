# Scrutiny record — `session-awareness` proposal

**Reviewed:** 2026-09-02, `/scrutinize` loop on the five change artifacts, 8 rounds to a
clean APPROVED. Round 1 reviewed the original artifacts; rounds 2–8 reviewed a fix plan that
was then applied to the artifacts in one pass and re-verified (two-phase token sweep,
`openspec validate --all`, the publication hook).

| Round | Verdict | Blocking findings | What moved |
|---|---|---|---|
| 1 | NEEDS REWORK | 2 critical, 12 major | `deny_roots` load rule self-cancelling; Henk had no in-process gate (homelab-docs precedent) |
| 2 | approved with concerns | 4 major | composition defects between the new Henk gate and the rendering |
| 3 | approved with concerns | 3 major | rendering model reorganised into layers |
| 4 | approved with concerns | 2 major | breaker tripped on the rendering section → holistic rewrite |
| 5 | NEEDS REWORK (cold read, fresh reviewer) | 2 critical, 8 major | sweep specified backwards; a marker not a substring of its own sentence |
| 6 | NEEDS REWORK | 1 critical, 3 major | same-turn taint cannot be delivered by the turn-entry snapshot (`henk/agent/core.py:542-548`) |
| 7 | approved with concerns | 5 major (mechanical) | titles deferred out of v1 — removed the critical by scope |
| 8 | **APPROVED** | 0 | four editorial notes folded in |

**Decisions the review forced, all recorded in `design.md`:**
- Henk enforces its own default-deny label allowlist in-process (`personal_data.session_project_allowlist`); the publisher is where filtering *decides*, not the only place it happens.
- Both `cwd` and `foreground_cwd` are gated; deny wins at any depth; the load-time refusal is an explicit unsafe-root set, not an ancestor rule; the publisher config is a closed schema (a typo in `deny_roots` must fail loudly).
- Rendering is a gate then headline/body/notes, with every sentence pinned beside a literal marker and a self-match + uniqueness test.
- **Session titles and branches are deferred** to a `session-titles` follow-up (design.md "Deferred: session titles"). Owner decision 2026-09-02: ship without titles. Not to be re-raised at apply.

**Lessons carried to memory:** verify the plan's own verification apparatus (markers, sweep tokens, named mechanisms) empirically before resubmitting; a consolidation must be diffed against the *original* artifacts, not the previous plan.
