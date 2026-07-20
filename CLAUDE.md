# Henk — "Homie Henk"

Personal homelab agent ("HOmelab agENt Konsult" if anyone asks; really it's just Henk,
the Dutch neighbor who knows where everything is). Reachable via a chat channel,
built on the **Claude Agent SDK**, wired to existing homelab tools.

## Canonical context (read before designing anything)

- **Project brief:** `~/Coding/homelab-ai-revised.md` — §5-A1 is this project's charter;
  §2 constraint 6 (lethal-trifecta rules) is the non-negotiable security posture.
  The spec must INHERIT that posture, not re-derive or relax it.
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
