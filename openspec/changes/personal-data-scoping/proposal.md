# Proposal: personal-data-scoping

## Why

During the henk-events v1.2 deploy-verify (5.3, 2026-07-23), `todo_read` surfaced
**Work/Anamata Obsidian todos** into a triage handoff. That is a direct violation of
the charter's non-negotiable Tier-W posture (`CLAUDE.md`: "no work/Anamata credentials
or data, ever"). The work content also reached the audit record — not as a raw
tool-output dump (the audit stores only the model-authored `diagnosis` free-text plus
tool-call metadata, never raw tool output) but *via* that diagnosis and the handoff the
model wrote from it. Source-scoping the tool is therefore a complete fix: work notes the
model never sees cannot enter its diagnosis or handoff. The tool was deregistered on the
spot (commit `559363d`) as containment; this change is the spec'd, tested re-enable.

Two independent root causes produced the leak, and both must be fixed before
`todo_read` is trustworthy again:

1. **No note-path scoping.** The tool fetched every todo the obsidian-todo-api
   exposes — the vault mixes personal and work notes — with nothing filtering by
   source note. There was no boundary between Tier-1 personal content and Tier-W
   work content at all.
2. **A broken summariser that raw-dumped the vault.** The obsidian-todo-api does
   **not** return a flat list. It returns a **note-grouped** dict:
   `{"todos": {"<note path>": [items…]}, "total_count", "note_count"}`, and each item
   carries a `source_note` field. `TodoReadTool._summarize` expected `data["todos"]`
   to be a *list*; because it is a *dict*, the `isinstance(items, list)` branch was
   skipped and the function fell through to `return str(data)` — dumping the entire
   vault, work notes included, as one blob.

So even a note-path filter alone would not have been enough: the summariser never
looked at `source_note` and never walked the group keys, so it had no structured
data to filter on. Both the scoping boundary and the parser are in scope here.

The same class of risk applies to the still-deferred `taiga_read` tool: the Taiga
instance holds mixed personal/work projects, so it needs the same default-deny
allowlist pattern (keyed on project id) before it can be registered. This change
establishes that pattern generically and applies it concretely to `todo_read`;
`taiga_read`'s re-registration is a scoped fast-follow (see Open Questions).

## What Changes

- **Default-deny note-path allowlist (tool-side, authoritative).** `todo_read` gains
  a note-path allowlist sourced from config. Only todos whose `source_note` matches
  an allowlisted path/prefix are surfaced; **everything else is dropped**. The
  allowlist is the authoritative boundary — enforced in Henk's own process, not
  delegated to the backend. **An empty/unset allowlist surfaces nothing** (fail
  closed), so a misconfiguration or a forgotten config value can never leak work
  data — it can only make the tool unhelpfully empty.
- **API `source_note` filter as defense-in-depth.** The obsidian-todo-api supports a
  `source_note` query parameter (substring match, single value, fail-open). Henk sends
  it **only when the effective allowlist has exactly one entry** (sent in its
  pre-trailing-slash form so it actually matches real note paths), so less work-note
  text ever crosses into Henk's process; with 0 or ≥2 entries the param is omitted and
  Henk fetches all, then filters — a single-valued filter cannot express a multi-prefix
  allowlist without silently dropping the other in-scope prefixes. Because the API
  filter is substring-based, single-valued, and fail-open, it is **never** the security
  boundary; the tool-side allowlist re-filters every returned item regardless.
- **Rewrite `_summarize` to walk the note-grouped dict.** Parse
  `{"todos": {note_path: [items]}, ...}` correctly, key filtering on each item's
  `source_note` (falling back to the group key), and format only allowlisted todos.
  No code path may emit `str(data)` or otherwise dump unparsed backend output.
- **Config field for the allowlist.** A new config value carries the personal note
  paths/prefixes (default empty → fail closed). Non-secret, lives in `config.yaml`.
- **Re-register `todo_read`** in `build_production_registry` once it is filtered and
  correctly parsing, wired to the allowlist config. Its class and tests were kept
  after deregistration precisely for this.
- **`taiga_read` stays out of the registry** in this change (fast-follow). The
  general default-deny allowlist requirement below is written to cover it (project-id
  keyed) so re-enabling it later is config + a small filter, not a new spec.

## Capabilities

### Modified Capabilities

- `homelab-tools`: the `todo_read` requirement is strengthened — it must apply a
  default-deny note-path allowlist and parse the note-grouped response correctly. A
  new requirement establishes the default-deny personal-data scoping principle for
  any tool backed by a mixed personal/work store (obsidian todos by note path, Taiga
  by project id).

> Note: these deltas land against the `openspec/specs/homelab-tools` baseline synced
> from the archived `henk-v1` change.

## Impact

- **Repo code:** `henk/tools/todo_read.py` (allowlist filter + `source_note` query
  param + rewritten `_summarize`); `henk/config.py` (new allowlist field);
  `henk/tools/__init__.py` (`build_production_registry` re-registers `todo_read`,
  updates the deferral docstring); `config.yaml` / `config.yaml.example` gain the
  allowlist key; the base `system_prompt` tool enumeration re-adds `todo_read`.
- **Tests:** new scenarios for allowlist enforcement (default-deny, prefix match,
  work-note drop), note-grouped parsing, and the no-raw-dump guarantee — written
  first, from the spec scenarios below.
- **Infra / owner:** none required beyond providing the allowlist paths and updating
  the deployed `config.yaml` on rp5 (skip-worktree'd there — edit in place). The
  obsidian-todo-api and its scoped read token are unchanged; no new ports, no ACL
  change.
- **Tier W:** this change *tightens* the Tier-W boundary; it introduces no new data
  source. `taiga_read` remains deferred.
- **Docs:** no homelab-doc surface changes (no new services/ports/topics); the tool
  scoping is internal to Henk.

## Resolved Decisions

- **Personal Obsidian note prefix to allowlist — resolved: `Personal/`.** The owner has
  supplied `Personal/` (a generic, publication-safe folder name), matched as a
  folder-boundary path prefix against each todo's `source_note` (design D3). Task 4.1 is
  no longer owner-blocked. The repo default stays empty (tool returns nothing) so a
  forgotten config still fails closed.
- **`taiga_read` scope — resolved: fast-follow, deferred.** `taiga_read` has never leaked
  (it is unregistered and there is no personal-scoped Taiga account provisioned yet), so
  it carries an *unmet infra prerequisite* (a dedicated read account scoped to personal
  projects, server-side) that `todo_read` does not; bundling it would block the urgent
  `todo_read` fix on that provisioning. The general allowlist requirement here covers it,
  so re-enabling is later config + a project-id filter, no new spec. It MUST NOT be
  registered until that project-id filter exists (spec note).
- **Format assumptions:** the note-grouped response shape and the `source_note`
  query-param behavior (substring, single value, fail-open) were observed this session;
  task 1.1 pins the shape with a **hand-authored synthetic fixture** (invented paths and
  text) and task 1.2 records the query-param behavior, so the parser is written against a
  known shape without any live payload entering git.
