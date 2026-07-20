# Tasks: henk-v1

## 1. Prerequisites (infra, outside this repo)

- [ ] 1.1 Open ACL PR in `enhulsman/tailscale-acl-gitops`: define `tag:henk`, grant egress to `tag:server` on tcp 8080/8000 (rp5) and 9090/8089/2586 (vps), no inbound, no SSH; merge after CI passes
- [ ] 1.2 Verify obsidian-todo-api (:8089) listens on the VPS Tailscale interface (Prometheus :9090 and ntfy :2586 are already documented dual-bound); dual-bind it (127.0.0.1 + Tailscale IP) per the VPS convention if not
- [ ] 1.3 Mint scoped tokens: ntfy publish token (one topic), obsidian-todo-api read token, Taiga MCP token (if the server requires one); generate a pre-authorized `tag:henk` Tailscale auth key
- [ ] 1.4 Set up the Anthropic credential for headless Agent SDK use: try `claude setup-token` (subscription OAuth — the June 2026 "separate Agent SDK credit pool" was cancelled 2026-06-15; SDK usage draws from normal subscription limits, monitor via `/usage`); fallback if OAuth is refused for SDK use: a low-budget API key from the console. Pin the `claude-agent-sdk` package version and confirm its allowed-tools configuration disables all built-in tools
- [ ] 1.5 Feasibility probes: run signal-cli-rest-api in json-rpc mode on rp5 for ~10 min — confirm websocket receive works and record steady-state RSS to size `mem_limit`; confirm taiga-mcp (rp5:8000) serves an HTTP-based MCP transport (fallback if stdio-only: `taiga_read` uses Taiga REST read endpoints, per design D4)

## 2. Project scaffold and test harness

- [x] 2.1 Scaffold Python project (uv/pyproject): `henk/` package with `channel/`, `agent/`, `tools/`, `gate/` modules; pytest + async test setup; config loading (`config.yaml` + env secrets)
- [x] 2.2 Write channel-adapter contract tests from `specs/channel-adapter`: allowlist (owner passes, stranger silently dropped + logged, group ignored), adapter interface neutrality — against a fake transport
- [x] 2.3 Write approval-gate tests from `specs/approval-gate`: classification required at registration, read-only bypass, approve/deny/timeout paths, single-use argument-bound approvals — against a channel test double and a test-only mutating tool
- [x] 2.4 Write agent-core tests from `specs/agent-core`: closed toolset (no built-in bash/file/web tools), serial per-conversation processing, `/new` reset, idle expiry, error turn handling — with the Agent SDK mocked

## 3. Core implementation (make 2.x tests pass)

- [x] 3.1 Implement the channel-neutral adapter interface + owner allowlist + drop logging
- [x] 3.2 Implement the Signal adapter against signal-cli-rest-api (json-rpc/websocket receive, send, backoff on bridge errors)
- [x] 3.3 Implement the approval gate (tool classification registry, inline prompt with one-time reference, approve/deny/timeout, fail-closed)
- [x] 3.4 Implement agent-core: Agent SDK session per conversation, built-in tools disabled, tool registration via the gate, message queueing, reset/idle logic, honest error replies
  - [x] session lifecycle, per-conversation reuse, message queueing, `/new` reset, idle expiry, honest error replies — implemented and tested (SDK mocked)
  - [x] closed toolset + gate as ONE default-deny `can_use_tool` decision (`henk/agent/permission.py`): unregistered/built-in tools denied, mutating tools routed through the gate — keyed on the registry so *registering* a mutating tool forces gating. No `allowed_tools` auto-approve (it would bypass the callback — confirmed against SDK 0.2.x). Unit-tested in `test_permission.py`.
  - [ ] real `claude_agent_sdk` binding (`SdkSessionFactory.create`) + deploy smoke test that a built-in (e.g. Bash) is genuinely uncallable — deploy-gated (needs the SDK + creds); see task 1.4/5.3.
- [x] 3.5 Composition layer (`henk/app.py`: `Dispatcher` + `App`) wiring adapter → allowlist → gate-routing → core, with an integration test (`test_app.py`) proving stranger-drop, owner-accept, and pending-approval fail-closed-then-requeue through the real path. [added post-scrutiny: the controls were previously unwired islands]

## 4. Tools (tests first per tool, from specs/homelab-tools)

- [x] 4.1 `homelab_health`: tests (healthy/degraded/backend-unreachable against recorded Gatus+Prometheus fixtures), then implementation
- [x] 4.2 `taiga_read`: tests (read allowlist only, write tools absent), then implementation — REST read endpoints (design-D4 fallback; MCP transport is a deploy-time probe, task 1.5)
- [x] 4.3 `todo_read`: tests (GET-only, token used, honest failures), then implementation
- [x] 4.4 `notify`: tests (`[AI]` prefix mandatory, no topic/server parameter), then ntfy implementation

## 5. Containerization and deploy

- [ ] 5.1 Dockerfile (non-root user) + `docker-compose.yml`: `henk` (network_mode: service:tailscale), `tailscale` sidecar, `signal-cli-rest-api` (internal network only, named volume, json-rpc mode); mem_limits on all three; `.env` mode-600
- [ ] 5.2 Deploy to rp5 `/home/pi/Coding/henk/`; sign the new Henk tailnet node with rp5's Tailnet Lock signing key (`tailscale lock sign <node-key>`); register the dedicated Signal number (owner provides number); verify Signal state survives `compose down && up`; verify rp5's backup routine covers the Signal state volume (inspect backup config, confirm the volume path appears in a test backup run)
- [ ] 5.3 Smoke tests on rp5: owner DM round-trip; stranger DM gets silence (needs any non-owner Signal account — family phone or throwaway signal-cli registration; if unavailable on deploy day, defer explicitly — allowlist behavior stays covered by 2.2's automated tests — and log-inspect a real stranger drop within the first week); each tool answers a real question; bridge port unreachable from host/LAN/tailnet; out-of-scope tailnet port (e.g., vps:5432) blocked
  - [ ] DEPLOY-VERIFY (M3/M4, from scrutiny): confirm the identity field Signal reports for the owner matches `owner.id` — else the allowlist silently drops every owner message (see `signal.py` DEPLOY-VERIFY); do NOT loosen the match. Capture a real group envelope and confirm `is_group` detection against it.
  - [ ] DEPLOY-VERIFY (C1): deploy smoke test that a built-in tool (e.g. Bash) is genuinely uncallable in a real SDK session.

## 6. Wrap-up

- [ ] 6.1 README: architecture sketch, config reference, deploy + Signal registration runbook, rollback
- [ ] 6.2 `/docs-update`: homelab docs entry (service page, ports, ACL tag), present diff for review
- [ ] 6.3 Watch Agent SDK credit pool usage for the first days; drop model to Haiku via config if tight
