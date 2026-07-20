# Tasks: henk-v1

## 1. Prerequisites (infra, outside this repo)

- [x] 1.1 ACL PR #8 (`enhulsman/tailscale-acl-gitops`) MERGED: `tag:henk` egress to `tag:server` on tcp 8080/9090/8089/2586 (8000/taiga dropped — taiga_read deferred), no inbound, no SSH; guardrail tests assert exactly those ports + deny 8000/22/53/5432.
- [x] 1.2 obsidian-todo-api (:8089) was 127.0.0.1-only. Dual-bound via an `API_HOST=0.0.0.0` systemd drop-in + a UFW rule scoping 8089 to the tailnet (v4+v6). It's a systemd service, so UFW governs it: default-deny blocks the public NIC, loopback stays served for the Cloudflare tunnel. Verified `LISTEN 0.0.0.0:8089`.
- [ ] 1.3 Mint scoped tokens: ntfy publish token (one topic), obsidian-todo-api read token, Taiga MCP token (if the server requires one); generate a pre-authorized `tag:henk` Tailscale auth key
  - [x] ntfy: write-only user `henk`, single topic `homie-henk`, token minted → `.env` NTFY_TOKEN; `config.yaml` topic set. (Topic-secrecy is moot: instance is auth-default-access deny-all.)
  - [x] obsidian-todo-api: has NO caller auth → no token needed; access gated by the tag:henk ACL + read-only GET.
  - [~] Taiga MCP token: N/A for v1 (taiga_read deferred to v1.1).
  - [x] Tailscale `tag:henk` pre-authorized auth key: generated → `.env` `TS_AUTHKEY`.
- [x] 1.4 Anthropic credential set: `claude setup-token` OAuth → `CLAUDE_CODE_OAUTH_TOKEN` in rp5 `.env` (regenerated clean, no whitespace). `claude-agent-sdk` pinned `==0.2.123`. Built-ins disabled structurally via the default-deny `can_use_tool` decision (see 3.4), not just allowed-tools config.
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
  - [x] real `claude_agent_sdk` binding implemented (`SdkSessionFactory.create` + in-process MCP server + `can_use_tool`), lazy-imported so it stays off the test path. Marked VERIFY-AT-DEPLOY for the exact 0.2.123 client/option field names.
  - [ ] deploy smoke test that a built-in (e.g. Bash) is genuinely uncallable in a real SDK session — deploy-gated (needs the SDK + creds); see task 1.4/5.3.
- [x] 3.5 Composition layer (`henk/app.py`: `Dispatcher` + `App`) + production wiring (`henk/runtime.py`) + entrypoint (`henk/__main__.py`), with integration tests (`test_app.py`, `test_runtime.py`) proving stranger-drop, owner-accept, and pending-approval fail-closed-then-requeue through the real path. [added post-scrutiny: the controls were previously unwired islands]

## 4. Tools (tests first per tool, from specs/homelab-tools)

- [x] 4.1 `homelab_health`: tests (healthy/degraded/backend-unreachable against recorded Gatus+Prometheus fixtures), then implementation
- [~] 4.2 `taiga_read`: tool + tests implemented (REST read endpoints, read-allowlist only, write tools absent) BUT **deferred from the v1 production registry**. The rp5 Taiga instance holds mixed personal/work data; wiring it safely needs a dedicated Taiga account scoped to personal projects (server-side) + a client-side project-id allowlist. Split to a v1.1 change ("scoped Taiga account for agents"). ACL PR amended to drop the unused `rp5:8000` grant.
- [x] 4.3 `todo_read`: tests (GET-only, token used, honest failures), then implementation
- [x] 4.4 `notify`: tests (`[AI]` prefix mandatory, no topic/server parameter), then ntfy implementation

## 5. Containerization and deploy

- [x] 5.1 Dockerfile (non-root uid 10001) + `docker-compose.yml`: `henk` (network_mode: service:tailscale), `tailscale` sidecar (tag:henk), `signal-cli-rest-api` (json-rpc mode, named volume, **no published ports** — the private-bridge guarantee, since it needs outbound internet to reach Signal so an isolated network was wrong); mem_limits on all three (signal bridge = PLACEHOLDER pending the 1.5 RSS probe); `.env.example` + `.dockerignore` added; `.env` mode-600 documented in the runbook. `docker compose config` validates.
- [ ] 5.2 Deploy to rp5 `/home/pi/Coding/henk/`; sign the new Henk tailnet node with rp5's Tailnet Lock signing key (`tailscale lock sign <node-key>`); register the dedicated Signal number (owner provides number); verify Signal state survives `compose down && up`; verify rp5's backup routine covers the Signal state volume (inspect backup config, confirm the volume path appears in a test backup run)
- [ ] 5.3 Smoke tests on rp5: owner DM round-trip; stranger DM gets silence (needs any non-owner Signal account — family phone or throwaway signal-cli registration; if unavailable on deploy day, defer explicitly — allowlist behavior stays covered by 2.2's automated tests — and log-inspect a real stranger drop within the first week); each tool answers a real question; bridge port unreachable from host/LAN/tailnet; out-of-scope tailnet port (e.g., vps:5432) blocked
  - [ ] DEPLOY-VERIFY (M3/M4, from scrutiny): confirm the identity field Signal reports for the owner matches `owner.id` — else the allowlist silently drops every owner message (see `signal.py` DEPLOY-VERIFY); do NOT loosen the match. Capture a real group envelope and confirm `is_group` detection against it.
  - [ ] DEPLOY-VERIFY (C1): deploy smoke test that a built-in tool (e.g. Bash) is genuinely uncallable in a real SDK session.

## 6. Wrap-up

- [x] 6.1 README: architecture sketch (mermaid), config reference, tool table, local-dev, deploy + Signal registration runbook, deploy-verify checklist, rollback, cost note
- [ ] 6.2 `/docs-update`: homelab docs entry (service page, ports, ACL tag), present diff for review
- [ ] 6.3 Watch Agent SDK credit pool usage for the first days; drop model to Haiku via config if tight
