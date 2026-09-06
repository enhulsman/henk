# Henk — "Homie Henk"

Personal homelab agent ("HOmelab agENt Konsult" if anyone asks; really it's just Henk,
the Dutch neighbor who knows where everything is). Reachable via a chat channel,
built on the **Claude Agent SDK**, wired to existing homelab tools.

## Commit hygiene (repo is publication-bound — history scrubbed 2026-07-22)

This repo is hosted under the owner's public identity (`enhulsman`) and intended to
go public. Every commit must be publication-safe: NO secrets/tokens (`.env` only),
NO tailnet IPs (write `vps`/`rp5` hostnames or `VPS-TS-IP`-style placeholders in
as-built notes), NO real phone numbers or account UUIDs (placeholders like
`+31600000000` in tests/config). Reference `hulsman.dev` in examples. ntfy topic
names are fine (instance is auth deny-all). When in doubt, redact in the commit
and keep the real value in the deployed config on rp5.

**These rules are enforced by a pre-commit hook, not just by attention.** Turn it on
once per clone:

```bash
git config core.hooksPath .githooks
GOBIN=$HOME/.local/bin go install github.com/zricethezav/gitleaks/v8@latest  # or: sudo apt install gitleaks
```

`.githooks/pre-commit` runs gitleaks over the staged content plus pattern checks for
the four repo-specific shapes above. It inspects **added lines only**, so deleting an
offending value is never blocked, and it hard-fails when gitleaks is missing rather
than skipping the scan silently. If it flags something you believe is a false
positive, prefer **rewording over `--no-verify`** — the hook caught a literal address
inside its own comment on first run, and the right fix was to describe the pattern
instead of embedding it. Governance and the public-flip milestone live in
`openspec/changes/repo-publication/`.

## Canonical context (read before designing anything)

- **North Star:** `NORTH-STAR.md` (this repo, settled 2026-08-07) — what Henk is *for*:
  identity statement, attention contract, the two-axis permission model, architecture
  principles, roadmap. Read it before proposing any change; spec Purposes derive from it.
- **Project brief:** `~/Coding/homelab-ai-revised.md` — §5-A1 is this project's charter;
  §2 constraint 6 (lethal-trifecta rules) is the non-negotiable security posture
  (Tier W refined 2026-08-07: the hard wall is *client* data; owner's own work
  metadata is shareable by allowlist). The spec must INHERIT that posture, not
  re-derive or relax it.
- **Homelab facts:** `~/Documents/homelab-docs-site/src/content/docs/` (devices,
  services, ACLs, ports). Do not assume infrastructure — read it.

## Security posture (inherited, non-negotiable)

- DM-allowlisted to the owner only; unknown senders get nothing.
- Read-only toolset by default; every mutation (Taiga writes, todo changes, anything)
  requires inline human approval.
- Runs containerized with scoped tokens only — no `~/.ssh`, no broad API keys,
  no work/Anamata credentials or data, ever (Tier W).
- Own least-privilege Tailscale ACL tag; loopback/tailnet bind only; never public.
- No third-party skill marketplaces.

## Workflow

OpenSpec project: `/opsx:propose` → `/scrutinize` to APPROVED → TDD from spec
scenarios → implement in a fresh session → `/opsx:sync` + `/opsx:archive`.

## Channel (user preference, 2026-07-20)

**Signal-first** (FOSS/security stance; user plans to switch to Molly, a Signal client
fork — so Henk lands natively in their daily messenger). Implement via a containerized
signal-cli bridge (e.g. signal-cli-rest-api) as a linked device or dedicated number.
Spec the channel layer as a thin, swappable adapter so a Telegram adapter can be added
later without touching agent logic.

## Candidate first tools (spec decides final v1 set)

`homelab-health` / `homelab-dns-check` (read-only CLIs in `~/.claude-config/bin/`),
Taiga MCP (rp5:8000, read), obsidian-todo-api (read), ntfy (notify-only).
