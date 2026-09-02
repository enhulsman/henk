# Apply decisions — `read-depth` §5 (`homelab_docs`, corpus retrieval)

**Applied:** 2026-09-02. Task group §5 (5.1–5.14), in an isolated worktree off
`0ffc6ad` (the §3 registry commit), while §4 ran concurrently in the main
checkout. Baseline in this worktree before the work: **1662 passed, 12
deselected**. After: **1759 passed, 12 deselected** (+97).

Separate from `notes/apply-decisions.md` on purpose — that file belongs to the
§2/§3/§4 line of work and the merge has to be conflict-free.

Not done here, deliberately: `henk/tools/__init__.py` is untouched, so the tool is
**not registered** (that is §7) and the deployed toolset is unchanged by this
commit. `homelab_query.py`, `query_registry.py`, `query_renderers.py` and
`homelab_health.py` were not opened. The audit assertions are §6; nothing in this
module opts into result capture, and there is no code path that could.

---

## The stamp contract §8.3 must write

This is the whole interface between the host updater and the tool. It is also
written into the module docstring of `henk/tools/homelab_docs.py`, and
`tests/test_tools_homelab_docs_freshness.py` pins every clause of it.

**Location.** `<mount>/homelab-docs-stamp.json` — the **clone root**, i.e. the
directory named by `homelab_docs.path`, *outside* `src/content/docs`. Design D9
also asks for it to be listed in `.git/info/exclude` so it is neither untracked
cruft nor removable by a `git clean -fd` in the updater.

**Format.** A JSON **object** (not an array, not a bare value):

```json
{
  "commit": "<commit id, string>",
  "committed_at": "<ISO-8601 timestamp of that commit>",
  "pulled_at": "<ISO-8601 timestamp of the last SUCCESSFUL pull>"
}
```

- `pulled_at` is the **only required field**. Anything else missing degrades the
  reported detail, never the freshness verdict.
- Timestamps are ISO-8601. A trailing `Z`, a numeric offset (`+02:00`), and
  fractional seconds are all accepted. **A value with no offset is read as UTC**,
  not as container-local time — the reader must not drift with `TZ`.
- A `pulled_at` that is absent, non-string, empty, or unparseable is treated
  exactly like a missing stamp: "freshness unknown", content still served.
- `commit` and `committed_at` are reported verbatim; an unparseable
  `committed_at` degrades to "(commit date not parseable)" and **does not** make
  the pull time unknown.

**Ordering.** Write the stamp **after** the content is updated. The reader keys
its index cache on the raw `pulled_at` string, so an unstamped content edit is
deliberately invisible until the stamp moves — which is what makes "the host
writes the stamp last" a real guarantee rather than a hope.

**Success, not attempt.** `pulled_at` records the last **successful** pull and
must not advance on a failed one. A writer that stamps every attempt inverts the
mechanism: a dead updater then reads as permanently fresh forever, which is the
precise condition the stamp exists to expose. (`secure-deployment` names this
failure mode too.)

**Bound.** Only `pulled_at` is bounded, at `homelab_docs.stamp_max_age_seconds`
(93600 = 26h). `committed_at` is reported and never bounded.

---

## Decisions made alone

Nobody was available to ask; each of these takes the reading that conforms to the
spec deltas, and each is recorded so it can be overturned rather than
rediscovered.

### 1. An unstamped corpus is **served with a marker**, not refused

The two binding requirements collide literally:

- *Corpus failures are honest*: "When the corpus directory is missing, empty,
  unreadable, **or unstamped** … every invocation SHALL return an explicit error
  naming the configured path and the specific condition."
- *Every result carries a freshness stamp*: "or the stamp is missing, or it cannot
  be parsed, the result SHALL carry an explicit staleness or 'freshness unknown'
  marker. **The tool SHALL still serve content in those cases rather than
  refusing**."

Both cannot hold for a present, readable, allowlisted corpus whose stamp is gone.
Resolved in favour of **serving with a marker**, on three grounds: the freshness
requirement has a concrete scenario for it ("A missing or unparseable stamp is not
silently fresh") and the availability requirement has none; design D11's own
argument for register-and-explain cites the old design's contradiction of "the
requirement that a missing stamp be *served with a marker*" as one of its four
faults; and D11 names the first-deploy trap — the stamp only exists after the
first successful pull — as a reason not to go dark.

The availability requirement's word "unstamped" is therefore read as being about
**registration** (the tool exists and answers) rather than about refusing to serve
content that is present. To keep the "naming path and condition" half of that
clause honest, the marker itself names the stamp's full path and the specific
condition (`is missing` / `is not valid JSON` / `is not a JSON object` / `has no
readable pulled_at value` / `could not be read (<errno text>)`).

**Flagged as the one clause not satisfiable literally.**

### 2. `search` returns `ok=True` for the two "nothing to show" states; only
availability failures are `ok=False`

- corpus missing / not a directory / unreadable / no docs subpath / empty →
  `ToolResult.failure` (explicit error, path and condition named).
- allowlist empty, or allowlist matching no file → `ToolResult.success` carrying
  an explicit "no documentation is in scope" statement.

The split follows the in-repo precedent D11 points at: `todo_read` with an empty
allowlist returns `ToolResult.success("No allowlisted todos.")`. It also gives the
two default-deny gates a structural difference on top of a textual one, which is
what "distinct diagnostics" is for.

### 3. Gate order: availability → allowlist → empty-corpus → allowlist-excludes-all

Checked in that order on **every call**, which is also what makes runtime loss
behave identically to startup absence without a separate code path. With both
gates shut (corpus missing *and* allowlist empty) the corpus error wins, because
it is the one the owner has to fix first; a test pins that.

"Empty corpus" and "the allowlist excludes everything" are told apart by counting
files **before** the allowlist (`DocsIndex.files_seen`) as well as after
(`files_indexed`) — otherwise an allowlist that matches nothing would report an
empty corpus, which is exactly the confusion D13 forbids.

### 4. The documentation subpath is a constant with no fallback to the mount root

`DOCS_SUBPATH = "src/content/docs"`, from design D8. A mount that lacks it gets a
per-call error naming the subpath, rather than falling back to walking the clone
root — a fallback would index `README.md`, `package.json` and anything else the
repository happens to carry the moment the mount is pointed one level off.

### 5. Section ids: `sec-` + 16 hex of `sha256(rel_path ∥ heading path ∥ repeat)`

Content-derived from the heading path, per the spec. Two additions the spec leaves
open:

- **Repeats.** Two sections in one file can share a heading path (the fixture's
  `rp2.md` has two `## Notes`). The id therefore includes an occurrence counter
  *within the same file and heading path*. This is ordinal-ish, but only among
  otherwise-indistinguishable siblings — every section whose heading path is
  unique in its file keeps a purely content-derived id, which is what the
  ids-survive-a-rebuild requirement is about.
- **Prefix.** `sec-` makes an id self-identifying, so a supplied value that is
  obviously a path is obviously not one.

### 6. `read` re-reads and re-splits the file rather than serving the cached text

The index holds each section's text for ranking and snippets, but `read` opens the
file again. Two requirements force it: a file that becomes unreadable
mid-operation must **error** rather than return substituted content (serving the
cached copy would silently succeed), and a section that has vanished from a
still-readable file must error rather than return stale text. The cost is
re-parsing one file (≤53 KB) per read.

### 7. Preamble sections are indexed, labelled `(document preamble)`

Text before a document's first heading is real content (the corpus has it) and
belongs in the index. Its heading path is empty, so its id is derived from the
file path alone and it renders under an explicit label rather than an empty
string.

### 8. Search is literal, whitespace-tokenised, with a punctuation fallback

The query is lowercased and split on **whitespace only** — not on punctuation —
so `(a+)+$` is one token matched exactly as supplied, which is what "matched
literally" has to mean for a metacharacter query. A token that matches nothing is
retried once with `.,;:!?"'\`` trimmed from its ends (so `runbook.` still finds
`runbook`); brackets and `$` are deliberately **not** trimmed. Scoring is
`body_hits + 5·heading_hits`, plus `10` per distinct token matched. The module
imports no pattern engine at all, and a test asserts that from the source text as
well as by trapping `re.compile` during a search.

### 9. Index invalidation keys on the raw `pulled_at` **string**

Not on a parsed timestamp (a re-serialised equal value should not force a rebuild,
and an unparseable one has no timestamp to compare), and not on mtimes (the host
writes the whole tree). With **no** readable stamp there is no change signal at
all, so the index is rebuilt on every call rather than trusted — the corpus is 17
files, and serving a superseded index is the failure that matters. A test pins
both halves.

### 10. `index_build_count` is a public test seam

"An unchanged corpus is not re-indexed per call" is a statement about work done,
not about output, so it needs a counter to be testable at all. Documented as a
seam on the property itself.

### 11. Age formatting keeps hours as hours for ten days

`39h 0m`, not `1d 15h`. The bound is expressed in hours (26h) and the fleet's
convention is `BackupStale > 26h`, so an age the owner has to compare against that
bound should be in the same unit. Past ten days it switches to `Nd Nh`, which is
where the commit age usually lands.

### 12. Fenced code blocks suspend heading detection

The real corpus is full of shell snippets whose lines start with `#`. A splitter
that ignored fences would invent headings from comments, and the invented heading
would then change section ids for everything after it. The fixture carries exactly
that trap in `devices/rp5.md`.

### 13. The fixture is a committed static tree plus two materialised pieces

`tests/fixtures/homelab_docs_corpus/clone/` is committed, symlink included. A
literal `.git/` directory cannot be committed inside this repository, so
`tests/corpus_fixture.build_corpus` creates one (holding a `.md` file, so the
extension filter is not what keeps version-control metadata out) in the `tmp_path`
copy. Copying to `tmp_path` also lets tests mutate the corpus — rewrite the stamp,
delete a page, chmod a directory — without touching the fixture.

Publication safety: every byte is invented, addresses are `10.0.0.x` placeholders,
and the text that must not cross a boundary carries `SYNTHETIC-…-SENTINEL` so a
leak fails an assertion instead of passing unnoticed.

---

## Mutations

Every test passed on first run, so each behaviour that carries a security or
honesty claim was mutated once and the suite re-run. Two mutations initially
**survived**; both were real gaps in the tests, and the tests were strengthened
until the mutation failed.

| # | Mutation | Caught? | Failing tests |
|---|---|---|---|
| M1 | Allowlist filtered at **read** time instead of index build | yes | 8, incl. `test_a_non_allowlisted_file_contributes_no_candidate_and_no_snippet` |
| M2 | On an id miss, join the supplied value onto the docs root and read it | yes | 6, all five `test_a_traversal_shaped_value_is_refused_and_never_joined` cases plus `test_a_path_that_exists_is_still_not_a_section_id` |
| M3 | `import re` + `re.compile(query)` in the tokeniser | yes | 2 (`…never_compiled_as_a_pattern`, `…imports_no_regex_engine`) |
| M3b | Rank with `re.findall(token, …)` instead of literal counting | yes | 3, incl. `test_a_regex_metacharacter_query_is_matched_literally` |
| M4 | Bound staleness on `committed_at` instead of `pulled_at` | yes | 7, incl. `test_an_old_commit_with_a_recent_pull_is_not_stale` |
| M5 | On an unreadable file, serve the cached index text | yes | 2 (`…unreadable_mid_operation_errors`, `…errors_rather_than_substituting_content`) |
| M6 | Ordinal section ids instead of heading-derived | yes | 16, incl. `test_ids_survive_a_rebuild_that_shifts_ordinals` |
| M7 | **Follow symlinks** in the walk (`followlinks=True`, no `is_symlink` skip) | **NO — survived** | see below |
| M8 | Do not strip frontmatter | yes | 1 (`test_frontmatter_is_excluded_from_section_text`) |
| M10 | Empty allowlist fails **open** (surfaces everything) | yes | 6, incl. all five `…empty_or_blank_allowlist_surfaces_nothing` cases |
| M11 | Walk from the clone root instead of the docs subpath | **partly** — caught by 20+ tests, but the two tests *specifically about scope* passed vacuously | see below |
| M12 | Never invalidate the index once built | yes | 3, incl. `test_a_changed_pull_stamp_rebuilds_the_index` |
| M13 | Truncate silently (drop the notice) | yes | 2 |

### M7 — survived, and why that mattered

Following symlinks changed nothing, because the symlinked paths
(`outside-notes.md`, `linked/`) were **not in the test allowlist**. The two symlink
tests were passing on the allowlist, not on the walk — the defect they exist to
catch was invisible to them. Fixed by giving those two tests an allowlist that
explicitly admits the link paths (`FULL_ALLOWLIST + ("outside-notes.md",)` and
`+ ("linked/",)`), so the *only* thing that can keep the target out of the index is
the indexer refusing to follow it. Re-run under M7: both fail. Reverted; green.

### M11 — caught, but the scope tests were vacuous

Walking from the clone root broke twenty-odd tests, but for the wrong reason: the
widened rel-paths (`src/content/docs/devices/rp5.md`, `README.md`, …) matched no
allowlist entry, so the index came out **empty** and everything downstream failed.
`test_repository_and_build_files_are_not_indexed` and
`test_filtering_composes_glob_first_then_allowlist` — the two tests whose whole
subject is scope — therefore passed against an empty index. Both were rewritten to
use a *greedy* allowlist that names the repository furniture as well as the docs
(`README.md`, `package.json`, `node_modules/`, `.git/`, `outside/`, `src/`,
`src/content/docs/`), so a walk that started too high is admitted rather than
accidentally filtered. Re-run under M11: both fail. Reverted; green.

The general lesson, worth carrying: with two filters in series, a test that exercises
one of them must make the *other* permissive, or it proves nothing about the filter
it names.

---

## Files

| Path | What |
|---|---|
| `henk/tools/homelab_docs.py` | new — sectioniser, index, allowlist, ranker, stamp reader, both actions |
| `tests/test_tools_homelab_docs.py` | new — 5.2–5.8 plus tool shape (no network, no writes) |
| `tests/test_tools_homelab_docs_freshness.py` | new — 5.9–5.13 |
| `tests/corpus_fixture.py` | new — materialises the corpus into `tmp_path` |
| `tests/fixtures/homelab_docs_corpus/**` | new — the synthetic clone, with its own README |
