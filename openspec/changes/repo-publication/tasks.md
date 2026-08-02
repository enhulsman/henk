# Tasks: repo-publication

## 1. Decouple from henk-events

- [x] 1.1 Remove task 6.4 from `henk-events/tasks.md`, pointing at this change — DONE 2026-08-02
- [x] 1.2 Drop the 6.4 half of `event-pipeline-durability` task 0.1's gate, so the archive
  ordering depends only on `henk-events` being archived (which 6.2 unblocked) — DONE 2026-08-02

## 2. Automated hygiene gate

- [x] 2.1 `.githooks/pre-commit` — three layers: secret scanner over staged content,
  repo-specific pattern checks, and an optional untracked `local-checks.sh` for
  operator-specific rules that must not be published. Added-lines-only, hard-fail when the
  scanner is missing, absent local checks reported rather than silently skipped,
  `--no-verify` bypass documented in the hook header. DONE 2026-08-02
- [x] 2.2 Enable via `git config core.hooksPath .githooks` and document it in `CLAUDE.md`
  so a fresh clone turns it on. DONE 2026-08-02
- [x] 2.3 Verify each detector actually fires — an untested gate is worse than none. DONE
  2026-08-02, 8 cases: clean content passes; tailnet address, non-allowlisted domain,
  token-shaped string and non-placeholder phone number each block; the sanctioned
  placeholder and a public address outside the tailnet range each correctly pass. The
  token case was caught by the scanner layer rather than the pattern layer, confirming
  both layers are live. The hook also caught a literal address in **its own comment** on
  first run — fixed by rewording rather than allowlisting, which is the behaviour the
  hook's own guidance recommends.

## 3. Pre-flip audit

- [x] 3.1 Full-history secret scan — DONE 2026-08-02: 37 commits, ~864 KB scanned, **no
  leaks found**. Working-tree scan surfaces exactly one hit, the untracked and gitignored
  `.env`; confirmed not tracked, and history is clean.
- [x] 3.2 Pattern audit of commits since the scrub (`e4ae1b8..HEAD`, 18 commits) — DONE
  2026-08-02. **Zero tailnet addresses, zero token-shaped strings.** Three
  domain-pattern matches, all of them the 6.4 task text quoting its own search
  pattern (self-reference, removed by task 1.1). Five phone-number matches, all the two
  documented placeholders, confined to `CLAUDE.md`, `config.yaml` and four test files.
  **No real hits — the gate is met.**
- [x] 3.3 Re-ran 3.1/3.2 before the flip — and the re-run **found a real problem the first
  pass missed**, which is the whole reason this task exists. The literal identity string was
  still present at HEAD in two files, and more importantly the *narrative* around it
  appeared in **23
  commits plus 2 commit messages**. Redacting a value but publishing prose explaining that a
  value is being hidden is worse than either alone: it flags the association as interesting
  and invites exactly the search it was meant to prevent. A pattern audit that greps for
  *values* cannot catch this — future audits must also read for **framing**.

  Remediation (2026-08-02): all identity-revealing prose genericized at HEAD; the
  operator-specific rule moved out of the published repo into an untracked
  `.githooks/local-checks.sh` (master copy in the private claude-config repo) loaded as an
  optional third hook layer; history rewritten with `git-filter-repo` to purge both the
  literal and the framing, including commit messages. Post-rewrite verification: 0 matches
  across all trees and all diffs, 41 commits preserved, gitleaks clean, 233 tests green,
  `openspec validate --all` 11/11.

## 4. The flip (owner-executed)

- [ ] 4.1 **Owner runs:** `gh repo edit enhulsman/henk --visibility public`. Note the
  `github.com-work` SSH alias handles pushes for `enhulsman/*` via the gitconfig
  `insteadOf` mapping, but the active `gh` account is the **work** account — verify
  `gh auth status` targets the right identity first, or the command will fail or act on
  the wrong account.
- [ ] 4.2 Portfolio project card on `hulsman.dev/projects`. Frame it on what is actually
  demonstrated rather than on daily operational value: prompt-injection resistance under a
  real hostile payload, exactly-once replay across restarts, cadence state surviving
  redeploys, and a wedge found by probing an undocumented external contract. The
  first-week watch recorded **zero real events in nine days** (henk-events 5.4), so a
  claim of routine incident triage would not be honest.
- [ ] 4.3 After the flip, confirm the repository is reachable while logged out, and that
  no GitHub Actions secrets, deploy keys, or environment settings became visible.
