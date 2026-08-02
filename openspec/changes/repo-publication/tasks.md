# Tasks: repo-publication

## 1. Decouple from henk-events

- [x] 1.1 Remove task 6.4 from `henk-events/tasks.md`, pointing at this change — DONE 2026-08-02
- [x] 1.2 Drop the 6.4 half of `event-pipeline-durability` task 0.1's gate, so the archive
  ordering depends only on `henk-events` being archived (which 6.2 unblocked) — DONE 2026-08-02

## 2. Automated hygiene gate

- [x] 2.1 `.githooks/pre-commit` — two layers (secret scanner over staged content +
  repo-specific pattern checks), added-lines-only, hard-fail when the scanner is missing,
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
  non-allowlisted-domain matches, all of them the 6.4 task text quoting its own search
  pattern (self-reference, removed by task 1.1). Five phone-number matches, all the two
  documented placeholders, confined to `CLAUDE.md`, `config.yaml` and four test files.
  **No real hits — the gate is met.**
- [ ] 3.3 Re-run 3.1 and 3.2 immediately before the flip if further commits land after
  2026-08-02. The pre-commit hook makes this a formality rather than a real risk, but the
  flip is irreversible and the scan is cheap.

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
