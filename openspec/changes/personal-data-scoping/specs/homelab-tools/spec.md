# homelab-tools (delta)

## MODIFIED Requirements

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

## ADDED Requirements

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
