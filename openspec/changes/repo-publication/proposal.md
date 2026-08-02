# Proposal: repo-publication

## Why

Two separate problems, one fix.

**1. A publication milestone is blocking a spec-lifecycle step.** The public flip lives as
task 6.4 inside `henk-events`. That change is otherwise complete — 5.1–5.4 and 6.1–6.3 are
all closed — so an open portfolio decision is the only thing keeping it from archiving. And
because `event-pipeline-durability` task 0.1 is gated on `henk-events` archiving first, the
flip transitively blocks *both* changes' specs from entering the baseline. Repo visibility
has no bearing on whether those capabilities are specified correctly; coupling them holds
the baseline hostage to a decision with no deadline.

**2. Publication safety is currently discipline, not enforcement.** History was
`git-filter-repo` scrubbed before the first push (2026-07-22), and the hygiene rules live in
`CLAUDE.md`. That was adequate while the repo was private, because a mistake could be
redacted before anyone saw it. Once public, that safety net is gone: a push is a publication
event, and content can be cloned, cached, or indexed within minutes of landing. Deleting it
afterwards does not unpublish it.

The 6.4 gate as originally written is also a one-shot manual grep, and it is noisy — run on
2026-08-02 it returned five matches, every one benign (three were the task text quoting its
own search pattern; two were the sanctioned test placeholders). A gate whose hits are
routinely benign trains its operator to wave hits through, which is precisely how the real
one gets missed.

## What Changes

- **Extract 6.4 out of `henk-events`** so the flip is independently trackable and neither
  archive waits on it. `henk-events` task 6.4 is removed and `event-pipeline-durability`
  task 0.1 loses the 6.4 half of its gate.
- **Add an automated publication-hygiene gate** — a version-controlled `.githooks/pre-commit`
  running two independent layers: `gitleaks` over the staged content (catches unknown-shaped
  secrets) and repo-specific pattern checks (catch the four shapes gitleaks has no rule for:
  tailnet addresses in the CGNAT range, the non-allowlisted domain, service tokens, and
  non-placeholder phone numbers). Checked into the repo rather than left in `.git/hooks/`, so
  the control survives a fresh clone.
- **Keep the pre-flip audit**, but as a documented one-time step whose benign-match classes
  are known in advance, so a future operator can tell signal from noise.
- **Perform the flip**, owner-executed, paired with the portfolio card.

Non-goals: no change to Henk's runtime, tools, egress, ACLs, or deployment. This change
touches repository governance only.

## Capabilities

### Added Capabilities

- `repo-publication`: publication safety of the source repository itself — automated
  pre-commit enforcement of the hygiene rules, the pre-flip audit gate, and the constraint
  that the irreversible visibility change is owner-executed.
