# Tasks: personal-data-scoping

## 1. Prerequisites

- [x] 1.1 **Hand-author a synthetic fixture** in `tests/fixtures/` reproducing the
      observed note-grouped shape (`{"todos": {"<note path>": [items]}, "total_count",
      "note_count"}`, each item with a `source_note`) using **entirely invented paths
      *and* text** — never a captured-then-scrubbed live payload (the higher risk is the
      item *text*, real work line items, which would land in git history). Include a
      personal group (e.g. `Personal/inbox.md` → "buy cat food") and a work-shaped group
      whose text contains a `SYNTHETIC-WORK-SENTINEL`. The shape is all the parser needs;
      provenance risk → zero.
- [x] 1.2 Confirm the `source_note` query-parameter behavior against the live API
      (substring match, single value, fail-open) and record **only that behavior** (no
      payloads copied) in the fixture README so the defense-in-depth layer is coded
      against observed behavior, not assumption.

## 2. Tests first (from spec scenarios; fake ntfy/HTTP backends)

> Write these before any implementation change; they MUST fail against the current
> deregistered/broken code first.

- [x] 2.1 Note-grouped parsing (spec: "Note-grouped response is parsed, never dumped"):
      feed the fixture; assert the tool walks the `todos` dict, formats individual
      todos from the groups, and that no result ever equals or contains `str(data)`.
      Include a raw-dump regression assertion (a sentinel work-note string present in
      the fixture must never appear in output).
- [x] 2.2 Default-deny (spec: "Empty allowlist surfaces nothing"): with an empty
      allowlist, a fixture full of todos yields an empty/"no allowlisted todos" result
      and zero todo text in the output.
- [x] 2.3 Prefix allowlist match (spec: "Todos fetched", "Only allowlisted scope keys
      pass"): with allowlist `["Personal/", "Homelab/"]`, only todos whose `source_note`
      starts with an allowlisted prefix are surfaced; the reported count is the
      allowlisted count, not `total_count`.
- [x] 2.4 Work-note drop (spec: "Work/non-allowlisted notes are dropped"): a work-note
      todo (e.g. under `Work/`) is absent from the result — neither its text nor its
      note path appears anywhere in the output. Also assert: (a) an item with **no
      `source_note` and no usable group key** is dropped, never surfaced; (b) an item
      whose resolved note path contains a `..` **path segment** (e.g. `Personal/../Work/x.md`)
      is dropped despite the `Personal/` prefix, while `Personal/notes..archive.md` is
      NOT dropped (segment check, not substring).
- [x] 2.5 API filter is not the boundary + send-gate (spec: "In-process allowlist is
      authoritative over the backend filter"): simulate the fail-open backend returning
      out-of-scope todos despite a `source_note` query param; assert the in-process
      filter still drops them. Assert the query param is sent **iff
      `len(effective_allowlist) == 1`**: with one entry it is sent; with **≥2 entries**
      it is omitted AND all in-scope prefixes' todos are surfaced (guards against the
      single-valued filter silently dropping the non-sent prefixes). Assert a single
      **file-path** entry (`Personal/inbox.md`) is sent in its pre-trailing-slash form so
      it produces a real server-side match (superset returned), NOT a fail-open
      whole-vault fetch, and the tool-side re-filter still yields exactly that file's
      todos.
- [x] 2.6 Unexpected shape (spec: "Unexpected response shape fails safe"): a
      malformed/unknown response yields an explicit error result and surfaces no todo
      content (no dump).
- [x] 2.7 Preserve existing invariants: GET-only, bearer token, honest timeout/non-2xx
      errors, `tool_class is READ_ONLY` (retain the kept v1 tests). Reflect the
      constructor-signature change: `TodoReadTool` now takes an allowlist param, so the
      `_make_tool` helper in the kept tests must pass an allowlist and the fixtures must
      carry `source_note`; move `test_todos_fetched` to the note-grouped shape with a
      **non-empty** allowlist (else it fails under default-deny).
- [x] 2.8 Empty-entry normalization (spec: "Default-deny personal-data scoping" —
      empty-after-normalization entries): allowlist `["", "/"]` surfaces nothing
      (collapses to default-deny); `["/", "Personal/"]` surfaces only `Personal/` todos;
      assert a WARNING naming each dropped empty/whitespace entry is emitted.
- [x] 2.9 Folder-boundary match (spec: "Only allowlisted scope keys pass"): allowlist
      `["Personal/"]` surfaces `Personal/x.md` and `Personal/sub/y.md`, but drops
      `Personal-work/z.md`, `PersonalNotes/w.md`, and a root `Personal.md` (sibling
      folders and same-prefix names must not over-match).

## 3. Implementation (make 2.x pass)

- [x] 3.1 `henk/config.py`: add the note-path allowlist config field (design D5 —
      preferred: a `personal_data` section with `todo_note_allowlist`, pre-shaping the
      `taiga_project_allowlist` fast-follow), default empty tuple → fail closed. Parse
      it in `from_dict`.
- [x] 3.2 `henk/tools/todo_read.py`: rewrite `_summarize` to walk the note-grouped dict
      (design D4); add the default-deny folder-boundary allowlist filter keyed on each
      item's `source_note` with group-key fallback, including the strict normalization
      pipeline (strip whitespace → strip leading `/` → discard now-empty entries with a
      WARNING) and the `..`-path-segment drop (D3); drop items with no usable key; report
      the allowlisted count; never emit `str(data)` or a non-matched item; send the
      `source_note` query param as defense-in-depth **iff the effective allowlist has
      exactly one entry**, in its pre-trailing-slash wire form (D1). Constructor takes
      the allowlist.
- [x] 3.3 `henk/tools/__init__.py`: re-register `todo_read` in
      `build_production_registry` wired to the allowlist config; emit a startup WARNING
      when `todo_read` is registered with an **empty effective allowlist**
      ("registered but always empty"); update the deferral docstring (`todo_read` →
      registered/note-path-scoped; `taiga_read` → still deferred, project-id allowlist
      pattern noted).
- [x] 3.4 `henk/config.py` `AgentConfig.system_prompt`: re-add `todo_read` to the
      enumerated toolset (and fix the "these three"/"these four" miscount already
      present in the base prompt).
- [x] 3.5 `config.yaml` (repo) + example: add the `personal_data.todo_note_allowlist`
      key. Show `Personal/` as the owner-supplied example value in the commented
      guidance (generic, publication-safe), and keep a note that empty = fail closed. The
      repo default stays empty; the deployed rp5 config carries the real value.
- [x] 3.6 `tests/test_approval_gate.py`: update the production-registry name-set
      assertion in `test_production_registry_has_no_mutating_tools` to include
      `"todo_read"`, AND add `assert "taiga_read" not in registry.names()` so the
      `taiga_read` deferral is regression-guarded.

## 4. Owner input + wiring

- [x] 4.1 **[RESOLVED — owner supplied `Personal/`]** Populate the deployed
      skip-worktree'd `config.yaml` on rp5 with `personal_data.todo_note_allowlist:
      ["Personal/"]`; the repo `config.yaml` example shows `Personal/` in commented
      guidance but keeps the default empty (fail closed). No longer blocked on owner
      input.
- [x] 4.2 **[RESOLVED — fast-follow, deferred]** `taiga_read` stays out of this change.
      Re-enabling later is a scoped fast-follow: add the project-id allowlist filter
      mirroring 3.2, its tests mirroring 2.x, and register it — but only after a
      personal-scoped Taiga read account exists (server-side prerequisite). It MUST NOT
      be registered until that project-id filter exists.

## 5. Deploy and verify on rp5

> Owner-run constraint (per henk-events 5.x): rp5 deploy is owner-run; prepare exact
> commands.

- [x] 5.1 Deploy the re-registered tool (`compose up -d` with the updated image and the
      populated `config.yaml`).
- [x] 5.2 Deploy-verify: invoke `todo_read` via a Signal DM; confirm the reply contains
      only allowlisted-note (`Personal/`) todos. Assert **both** directions: every known
      personal todo under the allowlisted area is **present** (catches M1's silent-drop
      failure mode live, not just in tests), and a known work-note todo is **absent**
      from both the reply and the freshly flushed audit record (eyeball the
      diagnosis/handoff-derived fields, since the audit stores those, not raw output).
- [x] 5.3 Confirm the empty-allowlist fail-closed behavior once in prod (temporarily
      empty config or a probe) so the default-deny property is verified live, not only
      in tests.

## 6. Wrap-up

- [x] 6.1 README: note `todo_read` is re-enabled behind a default-deny note-path
      allowlist; document the `personal_data.todo_note_allowlist` config key.
- [x] 6.2 Update memory `henk-long-run-direction` (todo_read re-enabled + scoped;
      carry the `taiga_read` project-id-allowlist fast-follow forward).
- [x] 6.3 `/opsx:sync` + `/opsx:archive` this change once 5.x verified.
