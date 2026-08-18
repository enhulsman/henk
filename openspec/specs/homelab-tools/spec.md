# homelab-tools Specification

## Purpose
Henk's hands: read-only views of the homelab (Gatus and Prometheus over the tailnet, never SSH),
the owner's personal todos, and notify-class sends to fixed topics that take no destination
argument. Two rules make the set trustworthy rather than merely useful. Failures are honest —
a tool that could not reach its backend says so and never returns stale, cached-as-fresh, or
invented data. And any tool backed by a store that mixes personal with work/Anamata content
enforces a default-deny allowlist inside Henk's own process, so an unset allowlist surfaces
nothing and a backend-side filter is never the boundary.
## Requirements
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
The system SHALL provide a `todo_read` tool (class: read-only) that fetches todos from obsidian-todo-api (vps:8089) using only GET endpoints and a scoped read token. The tool SHALL apply a default-deny note-path allowlist (see "Default-deny personal-data scoping") so that only todos whose source note matches an allowlisted path/prefix are surfaced; all other todos SHALL be dropped before any result leaves the tool. The tool SHALL correctly parse the obsidian-todo-api's note-grouped response (`{"todos": {"<note path>": [items]}, "total_count", "note_count"}`, each item carrying a `source_note`) and SHALL NOT under any condition emit unparsed backend output (e.g. a raw `str(data)` dump) or a todo whose source note was not allow-matched. The count the tool reports SHALL be the allowlisted count, never the vault-wide total.

#### Scenario: Todos fetched
- **WHEN** the agent invokes `todo_read` and at least one todo's source note matches the allowlist
- **THEN** only the allowlisted todos are returned from obsidian-todo-api, formatted from the parsed note-grouped response

#### Scenario: Only GET is used
- **WHEN** any `todo_read` invocation executes
- **THEN** every HTTP request it makes uses the GET method

#### Scenario: Work/non-allowlisted notes are dropped
- **WHEN** the obsidian-todo-api response contains todos from a note path that is not on the allowlist (e.g. a work/Anamata note)
- **THEN** those todos are absent from the tool result, and neither their text nor their note path appears anywhere in the output

#### Scenario: Note-grouped response is parsed, never dumped
- **WHEN** the backend returns the note-grouped dict shape (`todos` is a dict of note-path → item list)
- **THEN** the tool walks the groups and formats individual allowlisted todos, and no code path returns the raw stringified response

#### Scenario: Unexpected response shape fails safe
- **WHEN** the backend returns a shape the tool does not recognize
- **THEN** the tool returns an explicit "unexpected response shape" error and surfaces no todo content

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

### Requirement: Default-deny personal-data scoping
Any Henk tool backed by a store that mixes personal and work/Anamata content (the obsidian-todo-api vault; the Taiga instance) SHALL enforce a default-deny allowlist that is authoritative **inside Henk's own process** — the tool SHALL surface only records whose scoping key matches the configured allowlist and SHALL drop everything else, regardless of what the backend returns. For `todo_read` the scoping key is the todo's source note path (matched by folder-boundary path prefix); for `taiga_read` (when re-registered) it is the project id. An empty or unset allowlist SHALL cause the tool to surface **nothing** (fail closed); there SHALL be no configuration or code path in which an absent allowlist surfaces all records. An allowlist entry that is empty after normalization (leading `/` and surrounding whitespace stripped) SHALL be discarded and SHALL NOT broaden scope; a list containing only such entries SHALL behave as an empty allowlist and surface nothing. Where the backend offers a server-side filter (e.g. the obsidian-todo-api `source_note` query parameter), it MAY be used as defense-in-depth to reduce data transferred, but it SHALL NOT be relied on as the security boundary — the in-process allowlist SHALL re-filter every returned record.

> Note: `taiga_read`'s project-id scoping is **unimplemented pending the fast-follow** — this requirement establishes the pattern, but no project-id filter exists yet. `taiga_read` MUST NOT be registered in the production toolset until that filter is implemented.

#### Scenario: Empty allowlist surfaces nothing
- **WHEN** the allowlist for a personal-data tool is empty or unset and the tool is invoked
- **THEN** the tool returns an empty/"no allowlisted items" result and surfaces no record from the backend

#### Scenario: In-process allowlist is authoritative over the backend filter
- **WHEN** the backend's own filter fails open and returns records outside the allowlist (e.g. the fail-open `source_note` substring filter)
- **THEN** the tool's in-process allowlist drops those out-of-scope records before any result is produced

#### Scenario: Only allowlisted scope keys pass
- **WHEN** the backend returns records spanning both allowlisted and non-allowlisted scope keys (note paths / project ids)
- **THEN** only records whose scope key matches an allowlist entry are surfaced, and non-matching records are absent from the result

