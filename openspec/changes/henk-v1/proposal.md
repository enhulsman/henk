# Proposal: henk-v1

## Why

The homelab already exposes useful read-only surfaces (health CLIs, Taiga, obsidian-todo-api, ntfy), but reaching them means opening a laptop. Henk — the flagship A1 project from the homelab AI brief (§5-A1) — puts a personal agent on the Claude Agent SDK behind the owner's daily messenger (Signal), so "is everything up?" or "what's on my board?" becomes a chat message. It also doubles as the personal testbed for agent patterns (tool scoping, approval flows, memory) that transfer to work.

v1 is deliberately weekend-sized: prove the channel → agent → read-only tools loop end-to-end with the inherited security posture intact, before growing tool-by-tool.

## What Changes

- New Signal channel bridge via a containerized `signal-cli-rest-api`, DM-allowlisted to the owner's number only; unknown senders are silently dropped.
- Channel layer built as a thin, swappable adapter interface so a Telegram (or other) adapter can be added later without touching agent logic.
- New agent loop on the Claude Agent SDK with a small, read-only v1 toolset (final list decided in design.md; candidates: `homelab-health`, `homelab-dns-check`, Taiga read, obsidian-todo read).
- Outbound notification tool via existing ntfy (notify-only, `[AI]`-labeled per constraint 5 of the brief).
- Approval-gate scaffold: the mutation-approval mechanism (propose → inline owner approval → execute) exists and is tested in v1, even though v1 ships zero mutating tools — so future write tools plug into a gate instead of growing one ad hoc.
- New containerized deployment with its own least-privilege Tailscale ACL tag, scoped tokens only, tailnet/loopback bind — host (Pi5 vs VPS) decided in design.md.

Security posture is inherited from the brief (§2 constraint 6) and CLAUDE.md, not re-derived: owner-only conversations, read-only by default, approval-gated mutations, scoped per-task credentials, no third-party skill marketplaces, own ACL tag.

## Capabilities

### New Capabilities

- `channel-adapter`: Signal bridge (signal-cli-rest-api), owner allowlisting, and the swappable channel-adapter contract between messenger and agent.
- `agent-core`: the Agent SDK loop — session/conversation handling, model selection, tool registration, and how channel messages become agent turns and replies.
- `homelab-tools`: the v1 read-only toolset (health/DNS, Taiga read, obsidian-todo read) plus the ntfy notify tool and its `[AI]` labeling rules.
- `approval-gate`: the mutation approval scaffold — how a tool declares itself mutating, how approval is requested inline over the channel, and what happens on approve/deny/timeout.
- `secure-deployment`: container, host placement, Tailscale ACL tag, network binding, and secrets/token scoping requirements.

### Modified Capabilities

None — this is the project's first change; no existing specs.

## Impact

- New repo code: channel adapter, agent loop, tool wrappers, approval gate, Docker/compose files.
- Homelab infrastructure: one new container stack on the chosen host; a Tailscale ACL PR for the new tag (per the "any new service port = an ACL PR" rule); a new scoped Taiga token and obsidian-todo-api token; a dedicated Signal identity (linked device or dedicated number — decided in design.md).
- External dependencies: `signal-cli-rest-api` image, Claude Agent SDK (draws from the Agent SDK credit pool — light chat use fits; no 24/7 polling loops beyond the Signal receive channel).
- Docs: homelab docs need a new service entry + ACL/port updates after implementation (`/docs-update`).
- No work/Anamata systems or credentials touched (Tier W untouched, per posture).
