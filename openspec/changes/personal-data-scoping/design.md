# Design: personal-data-scoping

## Context

Henk's charter (`CLAUDE.md`, inherited from `homelab-ai-revised.md` §2 constraint 6)
is unambiguous: **no work/Anamata data, ever (Tier W).** The v1 toolset was built
read-only precisely so a compromised or confused agent could at worst read, not act —
but "read-only" is not "reads only the right things." `todo_read` read the *whole*
Obsidian vault, and the vault is a mixed personal/work store. On 2026-07-23 that
surfaced Anamata todos into a triage handoff (published to the deny-all
`henk-handoffs` topic). The work content also reached the audit record — not as a raw
tool-output dump (the audit stores only the model-authored `diagnosis` free-text plus
tool-call metadata: name, class, result id — never raw tool output; see `logger.py` /
`session.py`), but *via* that `diagnosis` free-text and the handoff derived from it,
into which the model had copied work-note content. Source-scoping the tool is therefore
a complete fix: with work notes never surfaced to the model, they cannot enter the
diagnosis or the handoff in the first place. It was deregistered (`559363d`) as
containment.

Two facts about the obsidian-todo-api (host `vps`, port 8089, GET-only, scoped read
token) shape this design:

- **Response is note-grouped, not flat.** The real shape is
  `{"todos": {"<note path>": [ {..., "source_note": "<note path>"} ]}, "total_count", "note_count"}`.
  The v1 `_summarize` assumed `data["todos"]` was a list; it is a dict, so the parser
  fell through to `return str(data)` and dumped everything unparsed.
- **A `source_note` query filter exists** — substring match, single value, fail-open
  (an unmatched or malformed value returns *everything*, not nothing).

## Goals / Non-Goals

**Goals:**

- Re-enable `todo_read` such that it can surface **only** owner-designated personal
  notes, with the boundary enforced authoritatively inside Henk's process.
- Make the default-deny property structural: unconfigured or misconfigured → empty,
  never leaky.
- Fix the summariser so no code path can emit unparsed backend output.
- Establish the allowlist pattern generically so `taiga_read` (project-id keyed) can
  reuse it without a new spec.

**Non-Goals:**

- Registering `taiga_read` (fast-follow; needs a personal-scoped Taiga account first).
- Any write/mutation to Obsidian or Taiga (both remain read-only; `todo_write` is
  dead per the direction memo — the API is read-only by design).
- Server-side re-scoping of the obsidian-todo-api or the vault itself. The vault stays
  mixed; the boundary is Henk-side.
- Changing the note-grouped response contract or asking the API for a stricter filter.

## Decisions

### D1 — The allowlist is the boundary; the API filter is defense-in-depth only

The authoritative filter runs **in Henk's process**: after fetching, the tool keeps a
todo only if its `source_note` matches the allowlist, and drops every other item
before formatting. The API's `source_note` query parameter is sent as an
*optimization* (less work-note text ever crosses into Henk's process — data
minimization, not just wire bytes) but is explicitly **not trusted**: it is
substring-based, accepts a single value, and **fails open** (unmatched → returns all).
Trusting it would re-introduce exactly the leak.

**When the param is sent.** The `source_note` query param is sent **iff the effective
allowlist (after D3 normalization) has exactly one entry.** With one prefix the backend
returns a superset (substring, fail-open) that the tool-side prefix filter reduces to
exactly the allowlisted set — no silent drop, and the data-minimization benefit is
preserved in the common single-area case. With **0 or ≥2 entries** the param is
**omitted** and the tool fetches all, then filters tool-side: a single-valued substring
filter cannot express a multi-prefix allowlist, and sending one of several prefixes
would make the backend omit the *other* in-scope prefixes' todos — a silent incomplete
answer the tool-side re-filter cannot recover (it can only re-filter what it received).

**What value is sent (len==1 case).** The substring sent to the API is the single
effective entry in its **pre-trailing-slash form** — the largest substring that actually
occurs in real `source_note` values. `Personal/` is sent as `Personal/`; a file-path
entry `Personal/inbox.md` is sent as `Personal/inbox.md`, **never** as
`Personal/inbox.md/`. The trailing-slash normalization introduced in D3 is used **only**
for the tool-side folder-boundary match, never for the wire value — a trailing-slash
form appears in no real `source_note`, so sending it would match nothing → fail-open →
whole vault over the wire (re-filtered safe, but defeats data-minimization).

Order of operations: (optionally) send the query param per the len==1 rule above, then
**unconditionally** re-filter the response tool-side against the full allowlist.
Rejected alternative: rely on the API filter alone — fails open, wrong capability class
for a Tier-W boundary; also rejected: send a "dominant" prefix from a multi-entry list —
undefined and silently drops the non-dominant in-scope prefixes.

### D2 — Default-deny: empty allowlist surfaces nothing

The allowlist config defaults to empty, and empty means **surface zero todos** (with
an honest "no allowlisted todos" result), never "surface all." This inverts the v1
failure mode: a forgotten or fat-fingered config can only make `todo_read` unhelpful,
never leaky. The tool never has a "no filter → pass through" path. This is the single
most important property in the change and is asserted by its own test.

### D3 — Matching semantics: case-sensitive folder-boundary prefix match on `source_note`

Each configured entry is treated as a **folder-boundary path prefix** matched against
the todo's `source_note` (the vault-relative note path). Obsidian organizes by folder,
so prefixes (`Personal/`) are the natural unit and let the owner allow a whole area
without enumerating files.

**Normalization pipeline (strict).** For each configured entry, in order: (a) strip
surrounding whitespace; (b) strip a leading `/`; (c) **discard the entry if it is now
empty or whitespace-only**, logging a WARNING that names the dropped entry. This closes
the fail-open hole where `"/"` or `""` normalizes to `""` and `startswith("")` matches
everything. **Invariant:** if no non-empty entry survives normalization, the effective
allowlist is empty and the tool surfaces **nothing** — this collapses to the D2
default-deny path, so `["", "/"]` behaves identically to `[]`. Matching is
case-sensitive (Obsidian paths are case-sensitive on the Linux host).

**Folder-boundary match.** Normalize every surviving entry to end in exactly one `/`
(append `/` if absent). A `source_note` (itself normalized: leading `/` stripped) is in
scope iff it **equals the entry sans trailing slash** OR **starts with the entry
including its trailing `/`**. Net effect: `Personal/` matches `Personal/x.md` and
`Personal/sub/y.md`, but NOT `Personal-work/z.md`, `PersonalNotes/w.md`, or a root
`Personal.md`. This closes the sibling-folder over-match a raw `startswith` allows
(`Personal` matching `Personal-work/`) as well as the mid-path sneak-through.

**`..` path-segment rejection (Tier-W insurance).** Before the allowlist test, an item
whose resolved note path contains a `..` **path segment** SHALL be dropped as an
unexpected/unsafe path (split the path on `/`; reject if any segment equals `..`). This
is a segment check, **not** a naive substring match — `Personal/notes..archive.md` is
*not* dropped, but `Personal/../Work/x.md` is dropped despite the `Personal/` prefix.
Not reachable in the real threat model (`source_note` is a backend-generated canonical
vault-relative path, not attacker-controlled per-request input), so it is cheap
insurance, not a load-bearing control.

Matching is on the item's own `source_note` field first, falling back to the group key
when an item omits it (the group key *is* the note path in the observed shape, so the
two agree — the fallback just hardens against a missing field). The exact prefixes are
the owner's to supply — the owner has supplied `Personal/` (proposal Open Questions,
now resolved); the code still ships with none in the repo default so a forgotten config
fails closed.

### D4 — Rewrite `_summarize` to walk the note-grouped dict

The new summariser:

1. Reads `data["todos"]`; if it is a **dict**, iterate `(...note_path, items...)`
   pairs; if a list (defensive, for older/other shapes), treat each item's
   `source_note` as its key.
2. For each item, resolve its note path (`item["source_note"]` or the group key). If the
   path has no usable key at all (no `source_note` and no group key), **drop it** — never
   surface an item whose scope cannot be established. Drop any item whose resolved path
   contains a `..` path segment (D3). Then apply the D3 folder-boundary allowlist test
   and drop non-matches.
3. Format only the survivors (count + `- [ ]/[x] text` lines), grouped or flat.
4. **Never** `return str(data)` and never emit an item whose note path was not
   allow-matched. An unrecognized top-level shape returns an explicit
   "unexpected response shape" error, not a dump.

The count reported to the agent is the **allowlisted** count, not `total_count`
(reporting the vault-wide total would itself leak the existence/volume of work notes).

### D5 — Config placement

Add a personal-data scoping config field carrying the note-path allowlist (a list of
prefix strings), non-secret, in `config.yaml`. Suggested shape: a `todo.note_allowlist`
list under the existing `todo` endpoint section, or a dedicated `personal_data`
section (`personal_data.todo_note_allowlist`, and later
`personal_data.taiga_project_allowlist`). The dedicated section is preferred because it
groups the Tier-W boundary knobs in one visible place and pre-shapes the `taiga_read`
fast-follow. Default: empty tuple → default-deny (D2). The value is threaded into
`TodoReadTool` construction in `build_production_registry`.

### D6 — Re-registration is gated on a non-empty, owner-supplied allowlist

`build_production_registry` re-registers `todo_read` (wired to the allowlist) and the
base `system_prompt` re-adds it to the enumerated toolset. Because of D2, registering
with an empty allowlist is *safe* (returns nothing) but *useless*. The owner has now
supplied the prefix (`Personal/`), so task 4.1 is no longer blocked on owner input; the
repo default stays empty (fail closed). When `todo_read` is registered with an **empty
effective allowlist**, `build_production_registry` SHALL emit a startup **WARNING**
("`todo_read` registered but always empty — no allowlist configured") so a
registered-but-useless deployment is visible, not silent. The deferral docstring in
`build_production_registry` is updated: `todo_read` moves from "deferred" to
"registered, note-path scoped"; `taiga_read` stays documented as deferred with the
project-id allowlist pattern noted.

### D7 — `taiga_read`: same pattern, deferred re-registration

The general default-deny requirement (spec delta) is written to cover any mixed-store
personal-data tool. For `taiga_read` the key is the **project id** (a closed allowlist
of personal project ids; anything else dropped), with the same default-deny and
authoritative-tool-side properties. Re-registration is out of scope here because it
also needs a server-side prerequisite (a Taiga read account scoped to personal
projects) that does not exist yet. When that lands, re-enabling is: populate
`personal_data.taiga_project_allowlist`, add a project-id filter mirroring D1/D2,
register. No new spec needed.

## Risks / Trade-offs

- **[Owner forgets to populate the allowlist]** → tool returns nothing, agent says so;
  no leak. Acceptable failure mode by design (D2).
- **[Vault reorganization moves personal notes out of an allowed prefix]** → those
  todos silently stop appearing. Detection: the owner notices missing todos. Mitigation:
  prefixes are broad folders, not file lists, so this is rare; documented as a config
  the owner owns.
- **[A work note is filed under a personal prefix in the vault]** → it would be
  surfaced. This is a vault-hygiene boundary Henk cannot see past; the allowlist is
  only as good as the owner's folder discipline. Noted, not solvable tool-side.
- **[API filter fails open and returns work notes anyway]** → harmless, because the
  tool-side re-filter (D1) drops them before formatting. This is exactly why the API
  filter is not the boundary.
- **[Response shape drifts from the captured fixture]** → the summariser returns an
  explicit "unexpected shape" error (D4), never a dump; a test pins the fixture and
  fails loudly on drift.

## Migration Plan

1. **Hand-author a synthetic fixture** (note-grouped, with `source_note`) into
   `tests/fixtures/` reproducing the observed shape with entirely invented paths *and*
   text — never a captured-then-scrubbed live payload (the item text is real work line
   items; capture-then-scrub is error-prone for exactly the content being removed).
   Include a `SYNTHETIC-WORK-SENTINEL` under a work-shaped path. Provenance risk → zero.
2. TDD: write the spec-scenario tests (task 2.x) — they fail against current code.
3. Implement D1–D5 until green.
4. Owner supplies the allowlist prefixes; populate `config.yaml` (repo example) and the
   skip-worktree'd `config.yaml` on rp5.
5. Re-register in `build_production_registry`, re-add to `system_prompt` enumeration.
6. Deploy-verify on rp5: a `todo_read` call returns only allowlisted notes; a work-note
   path is confirmed absent from the result, and — since the audit stores the
   model-authored `diagnosis` plus tool-call metadata, not raw output — eyeball the
   `diagnosis`/handoff-derived fields of the flushed audit record to confirm no work
   content was carried through.
7. Rollback: revert the registry re-registration (one line) → `todo_read` is
   deregistered again, exactly the current contained state.

## Resolved Decisions

- Exact allowlist prefixes — **resolved: `Personal/`** (owner-supplied; generic,
  publication-safe folder name). Task 4.1 is no longer owner-blocked. Repo default stays
  empty (fail closed).
- `taiga_read` in-scope vs fast-follow — **resolved: fast-follow, deferred.** Its
  project-id filter and personal-scoped Taiga read account are not built here; it MUST
  NOT be registered until the filter exists (spec note).
- Whether to report a redacted "N todos hidden by scope" count to the agent — **resolved:
  no.** Even a count leaks the existence/volume of work notes; the agent sees only the
  allowlisted world (reflected in D4: the reported count is the allowlisted count).
