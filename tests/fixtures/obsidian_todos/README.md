# Synthetic obsidian-todo-api fixtures (personal-data-scoping)

These fixtures are **hand-authored** — invented note paths *and* invented item text,
never a captured-then-scrubbed live payload. The item text of a real vault is real
work line items; capture-then-scrub is exactly the error-prone step this change
exists to eliminate, so provenance risk here is zero by construction. The shape is
all the parser needs.

## `note_grouped.json` — the observed response shape

The obsidian-todo-api (`vps:8089`, GET-only, scoped read token) returns a
**note-grouped** dict, NOT a flat list:

```
{"todos": {"<note path>": [ {..., "source_note": "<note path>"} ]},
 "total_count": <int>, "note_count": <int>}
```

Each item carries a `source_note` equal to its group key. The v1 `_summarize`
assumed `data["todos"]` was a list; because it is a dict, the parser fell through
to `return str(data)` and dumped the whole vault (work notes included).

Groups in the fixture:

| Group key | Purpose |
|---|---|
| `Personal/inbox.md` | personal todos (`buy cat food`, `renew passport`) — surfaced under a `Personal/` allowlist |
| `Personal/projects/garden.md` | personal todo one folder deeper — exercises sub-folder prefix match |
| `Homelab/backups.md` | second allowlistable area — exercises a multi-prefix (`["Personal/", "Homelab/"]`) allowlist |
| `Work/sprint-planning.md` | **work-shaped** group; its text carries `SYNTHETIC-WORK-SENTINEL` — must NEVER appear in output when `Work/` is not allowlisted (raw-dump regression guard) |

`total_count` (5) is deliberately larger than any single allowlisted subset so tests
can assert the tool reports the **allowlisted** count, never the vault-wide total.

Folder-boundary, `..`-segment, missing-key, and fail-open edge cases are built as
small inline dicts in `tests/test_tools_todo_read.py` (clearer read at the assertion
site than a sprawling shared fixture).

## `source_note` query-parameter behavior (recorded, not captured)

Confirmed against the live API (task 1.2). Only the *behavior* is recorded here — no
payloads were copied:

- **Substring match.** The value is matched as a substring of each todo's note path,
  not an exact/prefix match.
- **Single value.** The endpoint honors one `source_note` value; it cannot express a
  multi-prefix allowlist.
- **Fail-open.** An unmatched or malformed value returns **everything**, not nothing.

Consequences baked into the design (D1): the query param is defense-in-depth only —
sent **iff** the effective allowlist has exactly one entry (in its pre-trailing-slash
form, the largest substring that actually occurs in real note paths), and the
in-process allowlist **always** re-filters every returned item regardless. It is never
the security boundary.
