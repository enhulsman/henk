# Synthetic homelab-docs corpus fixture

**Every byte of this tree is invented.** Nothing here was captured from, scrubbed
out of, or transcribed from the real `homelab-docs-site` repository. The real
corpus is deliberately never vendored into this repo (`homelab-docs` spec, "The
corpus is never committed to this repository"): 12 of its 17 files carry tailnet
addresses and `.githooks/pre-commit` blocks that pattern in added lines. What this
fixture reproduces is the *shape* the indexer has to survive, not the content.

Addresses here are documentation placeholders in the `10.0.0.0/24` example range.
Sentinel strings (`SYNTHETIC-…-SENTINEL`) mark the text that must **not** escape a
given boundary, so a leak fails an assertion instead of passing unnoticed.

## Layout

`clone/` stands in for the read-only bind mount of the host-side clone. The tool's
documentation root is `clone/src/content/docs`; everything above it is repository
furniture that must never be indexed.

| Path | Why it is here |
|---|---|
| `clone/homelab-docs-stamp.json` | the freshness stamp, at the **clone root**, outside the docs tree (design D9) |
| `clone/README.md` | repository README — `.md`, but outside the docs subpath (`SYNTHETIC-REPO-README-SENTINEL`) |
| `clone/package.json`, `clone/astro.config.mjs` | build configuration that must never be indexed |
| `clone/node_modules/astro/readme.md` | a dependency directory holding a markdown file |
| `clone/outside/private-notes.md` | symlink target, **outside** the docs subpath (`SYNTHETIC-OUTSIDE-SENTINEL`) |
| `clone/src/content/docs/outside-notes.md` | **symlink** to the above — the indexer must not follow it |
| `clone/src/content/docs/index.mdx` | `.mdx`, frontmatter with `SYNTHETIC-FRONTMATTER-SENTINEL` |
| `clone/src/content/docs/devices/rp5.md` | preamble before the first heading, `##`/`###` nesting, and a **fenced code block whose lines start with `#`** — the heading-splitter trap |
| `clone/src/content/docs/devices/rp2.md` | two sections sharing the heading path `Notes`, so id disambiguation is exercised |
| `clone/src/content/docs/services/monitoring.md` | ordinary prose plus the literal string `(a+)+$`, for the "query is not a regex" test |
| `clone/src/content/docs/services/large-runbook.md` | **~53 KB**, forty-odd sections, one of them deliberately past the 8000-byte read budget |
| `clone/src/content/docs/security/access.md` | the file tests exclude from the allowlist (`SYNTHETIC-EXCLUDED-SENTINEL`) |

## What is *not* committed, and why

A literal `.git/` directory cannot be committed inside another git repository, so
`tests.corpus_fixture.build_corpus` materialises one (plus its `objects/` and a
markdown file inside it) in the copied tree at test time. Same helper, same
guarantee: the walker meets version-control metadata containing a `.md` file.

`build_corpus` copies this tree into a `tmp_path` (preserving the symlink) so tests
can mutate the corpus — rewrite the stamp, delete a file, chmod a directory — without
touching the committed fixture.
