# homelab-tools

## ADDED Requirements

### Requirement: homelab_health read-only tool
The system SHALL provide a `homelab_health` tool (class: read-only) that reports homelab status by querying the Gatus API (rp5:8080) and the Prometheus HTTP API (vps:9090) over the tailnet, returning a structured summary of endpoint up/down states and per-node memory, disk, and load. The tool SHALL NOT use SSH or any other host-level access.

#### Scenario: Healthy homelab summarized
- **WHEN** the agent invokes `homelab_health` and all Gatus endpoints are up
- **THEN** the tool returns a summary marking all endpoints healthy with current node resource figures

#### Scenario: Degraded service reported
- **WHEN** Gatus reports an endpoint down or Prometheus reports a node metric beyond its threshold
- **THEN** the tool's result names the affected endpoint/node and the failing measurement

#### Scenario: Backend unreachable
- **WHEN** Gatus or Prometheus cannot be reached
- **THEN** the tool returns an explicit "source unreachable" result for that backend (never fabricated data), and the other backend's data is still returned if available

### Requirement: taiga_read read-only tool
The system SHALL provide a `taiga_read` tool (class: read-only) backed by the existing Taiga MCP server (rp5:8000) as an MCP client that registers only read operations (`get_*`, `list_*`), or — if the Taiga MCP server does not serve an HTTP-based MCP transport — by the Taiga REST API's read endpoints directly, with the same read-only posture. Write-capable operations SHALL NOT be registered or callable, regardless of what the backend exposes.

#### Scenario: Board contents fetched
- **WHEN** the agent invokes `taiga_read` to list user stories for a project
- **THEN** the current stories with status are returned from the Taiga MCP server

#### Scenario: Write operation impossible
- **WHEN** the agent attempts to reach any Taiga write operation (create/update/assign)
- **THEN** no such tool exists in its registry and the attempt fails without any request reaching the Taiga MCP server

### Requirement: todo_read read-only tool
The system SHALL provide a `todo_read` tool (class: read-only) that fetches todos from obsidian-todo-api (vps:8089) using only GET endpoints and a scoped read token.

#### Scenario: Todos fetched
- **WHEN** the agent invokes `todo_read`
- **THEN** current todos are returned from obsidian-todo-api

#### Scenario: Only GET is used
- **WHEN** any `todo_read` invocation executes
- **THEN** every HTTP request it makes uses the GET method

### Requirement: notify tool with AI labeling
The system SHALL provide a `notify` tool (class: notify-only) that publishes to a single configured ntfy topic (vps:2586) with a scoped publish token. Every published message SHALL begin with the `[AI]` label. The tool SHALL NOT accept a topic, server, or recipient parameter.

#### Scenario: Notification sent and labeled
- **WHEN** the agent invokes `notify` with message text
- **THEN** ntfy receives the message on the fixed topic, prefixed with `[AI]`

#### Scenario: Alternate destination impossible
- **WHEN** the agent produces arguments attempting to target a different topic or server
- **THEN** the tool interface has no such parameter and the message can only go to the configured topic

### Requirement: Tool failures are honest
Every homelab tool SHALL return an explicit error result on backend failure (timeout, non-2xx, malformed response) containing what failed and why. Tools SHALL NOT return stale, cached-as-fresh, or fabricated data.

#### Scenario: Backend timeout surfaces as error
- **WHEN** a tool's backend does not respond within its timeout
- **THEN** the tool result states the backend and the timeout, and the agent's reply to the owner reflects that the data was unavailable
