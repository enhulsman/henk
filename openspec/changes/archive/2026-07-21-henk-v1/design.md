# Design: henk-v1

## Context

Henk is project A1 from the homelab AI brief: a personal agent on the Claude Agent SDK, reached over Signal, wired to existing read-only homelab surfaces. The security posture is inherited and non-negotiable (brief §2 constraint 6, CLAUDE.md): owner-only DMs, read-only default, approval-gated mutations, scoped tokens, own least-privilege Tailscale ACL tag, containerized, never public.

Relevant verified infrastructure facts (homelab docs, 2026-07-20):

- **Pi5 (rp5):** 8 GB RAM, ~12 containers + system services; hosts Gatus (:8080), Taiga MCP (:8000, systemd FastMCP), primary DNS. App-tier compose projects live under `/home/pi/Coding/<app>/`.
- **VPS:** CX22 (2 vCPU / 4 GB), ~18 containers, swap persistently ~81% — no spike headroom; the planned RAM upgrade is earmarked for the Immich wave. Hosts ntfy (:2586, dual-bound 127.0.0.1 + Tailscale), Prometheus (:9090), obsidian-todo-api (:8089, systemd), Taiga app stack.
- **ACLs:** GitOps repo `enhulsman/tailscale-acl-gitops` (`policy.hujson`, PR-validated, drift-checked). Five-tag model; `tag:server` inter-server traffic is a fixed port list. Any new port/tag = a PR.
- **Critical finding:** the `homelab-health` / `homelab-dns-check` CLIs work by **Tailscale SSH from a `tag:admin` device** (workstation) into each node. A least-privilege Henk container must not hold SSH rights to servers, so these CLIs **cannot be reused as-is**. The same data is available over read-only HTTP: Gatus API (rp5:8080) and Prometheus HTTP API (vps:9090).

## Goals / Non-Goals

**Goals:**

- One weekend-sized deployable unit: Signal bridge + agent loop + 3 read tools + notify + tested approval-gate scaffold.
- Channel layer swappable behind an adapter interface (Telegram later, no agent-logic changes).
- Every element of the inherited security posture structurally enforced, not policy-enforced (own tailnet node, scoped tokens, closed toolset).

**Non-Goals:**

- No mutating tools in production (the gate ships tested but unused).
- No DNS deep-check tool in v1 (`homelab-dns-check` semantics need on-device SSH; revisit if a read-only HTTP surface for it appears).
- No memory/persistence beyond in-process session state; no scheduled/proactive behavior (Henk only speaks when spoken to, plus explicit notify tool calls within a turn).
- No group chats, no multi-user support, no voice/attachments.
- No new public exposure of anything (no Cloudflare tunnel entries).

## Decisions

### D1 — Host: Raspberry Pi 5 (`rp5`)

The VPS is disqualified today: swap at ~81% with a 15-day peak of 82% means no headroom for signal-cli-rest-api's JVM (~300–500 MB) plus the agent process, and the planned upgrade is reserved for Immich. The Pi5 has 8 GB with real headroom and already hosts the app tier (finance-bot, taiga-mcp). The "primary DNS blast radius" argument that pushed Immich off the Pi5 doesn't apply: Henk is CPU-light, storage-flat, and memory-bounded.

*Alternative considered:* VPS after the RAM upgrade — rejected as a dependency on an unscheduled upgrade; revisitable later since the stack is one compose file.

### D2 — Own tailnet node via Tailscale sidecar, new `tag:henk`

The "own least-privilege ACL tag" requirement is only structurally real if Henk's traffic is attributable to its own tailnet identity. A tailscale container joins the compose stack; the agent container shares its network namespace (`network_mode: service:tailscale`). All tool egress to homelab services goes over the tailnet as `tag:henk`.

New ACL grant (PR to `policy.hujson`): `tag:henk` → `tag:server` on exactly: `tcp:8080` (Gatus, rp5), `tcp:8000` (Taiga MCP, rp5), `tcp:9090` (Prometheus, vps), `tcp:8089` (obsidian-todo-api, vps), `tcp:2586` (ntfy, vps). No inbound grants to `tag:henk` from anywhere. `tag:henk` gets no SSH rules. Anthropic API traffic egresses via ordinary container internet access, not the tailnet.

*Alternative considered:* running on the host network and inheriting rp5's `tag:server`+`tag:router` identity — rejected: massively over-privileged and unauditable.

*Verify during implementation:* obsidian-todo-api :8089 must actually listen on the VPS Tailscale interface (the VPS convention is 127.0.0.1-only with dual-bind exceptions); dual-bind it in the same change if not. ntfy and Prometheus are already documented dual-bound. Note: Tailnet Lock is active (rp5 + VPS are signing nodes) — the new Henk node must be signed after joining (`tailscale lock sign <node-key>` from rp5).

### D3 — Signal identity: dedicated number, not a linked device

A linked device would hand Henk the owner's entire Signal account — every conversation with every contact becomes agent-readable input. That maximizes the untrusted-input leg of the lethal trifecta and violates least privilege in spirit. A dedicated number means Henk sees only what is sent to Henk; the owner allowlist then reduces that to one sender. Cost: acquiring one number (prepaid SIM / VoIP) at registration time.

*Alternative considered:* linked device — simpler onboarding, rejected on the privilege argument above.

### D4 — v1 tool list (final)

| Tool | Class | Backend | Notes |
|---|---|---|---|
| `homelab_health` | read-only | Gatus API `rp5:8080` + Prometheus HTTP API `vps:9090` | Reimplements the intent of the `homelab-health` CLI over HTTP (the CLI's Tailscale-SSH approach is admin-privileged and unavailable to Henk by design). Summarizes endpoint up/down + node memory/disk/load. |
| `taiga_read` | read-only | Taiga MCP `rp5:8000` (MCP client) | Client-side allowlist of read tools only (`get_*`, `list_*`); write tools are never registered even though the server exposes them. Fallback if taiga-mcp turns out stdio-only (verify in task 1.5): call the Taiga REST API read endpoints directly — same read-only posture. |
| `todo_read` | read-only | obsidian-todo-api `vps:8089`, GET endpoints with scoped token | |
| `notify` | notify-only | ntfy `vps:2586`, scoped publish token, fixed topic | Every message prefixed `[AI]` (brief constraint 5). Notify-only class: no approval needed, but it can only push to the owner's own ntfy topic. |

Three read tools + notify matches the "2–3 read tools" weekend budget. `homelab-dns-check` is explicitly deferred (needs on-device SSH; see Non-Goals).

### D5 — Runtime: Python + Claude Agent SDK

Python with the `claude-agent-sdk` package (Anthropic's Claude Agent SDK for Python, the renamed successor of `claude-code-sdk`) and async I/O. Rationale: the adjacent homelab agent surface is already Python (taiga-mcp is FastMCP), MCP client support is first-class, and the codebase is small enough that the language choice is low-stakes. Model default: Sonnet (interactive judgment-light traffic; decision rule 3/4 of the brief), configurable via env. SDK auth via a dedicated credential in the container env. Verified 2026-07-20: the reported "separate Agent SDK credit pool" was cancelled by Anthropic on 2026-06-15 — SDK/headless usage draws from the normal subscription limits. Auth path: `claude setup-token` OAuth (personal use of own plan), with a low-budget API key as fallback (task 1.4).

The SDK ships built-in host-touching tools (Bash, file read/write, web); these MUST be disabled via its allowed/disallowed-tools options so the session exposes exactly the four Henk tools (spec: agent-core). The agent-core tests assert this configuration; task 1.4 pins the package version and confirms the mechanism.

### D6 — Signal bridge wiring

`bbernhard/signal-cli-rest-api` container in `json-rpc` mode (persistent daemon, websocket receive → low latency, no polling). It attaches only to the compose-internal network: no published ports, unreachable from tailnet and LAN. The agent connects to it by service name over the internal network. Signal account data persists in a named volume, included in the Pi5's existing backup routine.

### D7 — Layout, config, secrets

- Repo: this repo (`~/Coding/henk`), deployed to `/home/pi/Coding/henk/` on rp5 per the app-tier convention; `docker compose` stack of three services (`signal-cli-rest-api`, `tailscale`, `henk`).
- Config: single `config.yaml` (owner identity, model, timeouts, tool endpoints) + `.env` mode-600 for secrets (Anthropic credential, Taiga MCP token if any, todo-api token, ntfy token, tailscale auth key). Tailscale auth key is a pre-authorized, tagged, ephemeral-off key scoped to `tag:henk`.
- Container runs as non-root; no docker socket, no host mounts beyond its own state volume.
- Observability: structured logs to stdout (docker logs); optional Gatus entry for the bridge is deferred until there's an HTTP health endpoint worth probing (Tier 3, dashboard-only, no new inbound ACL needed if probed from rp5... which Gatus is on — but inbound to `tag:henk` is denied, so defer entirely).

## Risks / Trade-offs

- [Prometheus/todo-api not Tailscale-bound on VPS] → verified early in implementation; dual-bind (127.0.0.1 + Tailscale IP) via the established VPS convention if needed. Small, reversible change.
- [Henk's SDK usage shares the subscription's normal limits with interactive Claude Code work] → v1 is interactive-only and light; watch `/usage` during the first week; if it crowds the allowance, drop model to Haiku via config or move Henk to a spend-capped API key.
- [signal-cli-rest-api JVM memory on Pi5] → ~300–500 MB against multi-GB headroom; set a compose `mem_limit` so a leak can't squeeze DNS. Pi5 remains primary DNS — the limit is the blast-radius control.
- [Prompt injection via tool outputs (Gatus/Taiga/todo content is semi-trusted)] → the trifecta is cut structurally: even a fully hijacked agent can only read homelab state and message the owner/its own ntfy topic; no mutations exist, no other recipients are reachable. Residual risk accepted for v1.
- [Dedicated number acquisition friction] → owner-provided prepaid/VoIP number; registration is a one-time manual step in the deploy runbook.
- [Taiga MCP exposes write tools] → client-side allowlist (D4); a compromised Taiga MCP server is out of Henk's threat model (it's an existing trusted service).

## Migration Plan

Greenfield — no migration. Deploy order: ACL PR (`tag:henk` + grants) → verify VPS binds → compose up on rp5 → sign the Henk node with rp5's Tailnet Lock key → register Signal number → smoke-test allowlist (second phone) → `/docs-update` for the homelab docs (new service page entry, ports, ACL). Rollback: `docker compose down`; revert ACL PR.

## Open Questions

- Which number source for Henk's Signal identity (prepaid SIM vs VoIP provider)? Owner decision at registration time; does not block implementation.
- Exact Anthropic credential type for headless Agent SDK use under the work subscription — narrowed 2026-07-20: no separate credit pool exists (cancelled 2026-06-15); try subscription OAuth via `claude setup-token`, fall back to a spend-capped API key (task 1.4; affects one env var, not the design).
