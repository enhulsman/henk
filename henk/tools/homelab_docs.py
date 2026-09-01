"""homelab_docs — read-only, section-level retrieval over the mounted docs corpus.

The corpus is a host-side clone of the documentation site, bind-mounted read-only
into the container. This module never fetches it, never writes to it, and never
opens a network connection: keeping the mount current is the host's job, and this
tool's job is to be honest about how current it is.

Four properties carry the weight, and each has a silent failure mode behind it:

- **The allowlist filters at index build, not at read time** (design D13). ``search``
  ranks over the index and returns snippets, so a file filtered only at read time
  would still leak its text through a search result. A file outside
  ``personal_data.docs_path_allowlist`` therefore has no sections at all, and an
  empty allowlist surfaces nothing (fail closed), exactly as ``todo_read`` does.
- **``read`` takes an index key, never a path.** Traversal is closed by
  construction: the supplied value is looked up in a dict and, on a miss, nothing
  is opened. No code path joins it to a filesystem path.
- **Section ids are derived from the heading path, never from ordinal position.**
  An ordinal id silently remaps across a pull, so a search-then-read straddling an
  update would return *different* content under the *same* id — which refusing
  unknown ids does not catch, because the id is reused rather than absent.
- **The query is matched as literal tokens.** This module imports no pattern
  engine at all, so a model-authored query cannot become a catastrophic backtrack.

Availability follows design D11: a *config* error (enabled with no path) is a
startup refusal and lives in ``henk.config``; *host* state is not. A missing,
unreadable or empty corpus registers the tool anyway and fails per call, naming the
path and the condition — an absent tool would leave the model answering
documentation questions from its priors with no marker that the docs were
unreachable.

The freshness stamp contract (design D9), which
``deploy/homelab-docs-stamp.sh`` writes and this module only ever reads
=====================================================================

Location
    ``<mount>/homelab-docs-stamp.json`` — at the **clone root**, outside
    ``src/content/docs``, so it is neither indexable content nor inside the git
    work tree's documentation subpath.

Format
    A JSON **object**::

        {
          "commit": "<full or short commit id, string>",
          "committed_at": "<ISO-8601 timestamp of that commit>",
          "pulled_at": "<ISO-8601 timestamp of the last SUCCESSFUL pull>"
        }

    ``pulled_at`` is the only required field. Timestamps are ISO-8601; a trailing
    ``Z`` and a numeric offset are both accepted, and a value with no offset is
    read as **UTC** (the writer emits UTC, and reading a naive value as local time
    would make the reported age drift with the container's zone). A non-string or
    unparseable ``pulled_at`` is treated exactly like a missing stamp.

Write ordering
    The stamp is written **after** the corpus content is updated, so a reader never
    sees a fresh stamp against superseded content. This module keys its index cache
    on the raw ``pulled_at`` value, so an unstamped edit is deliberately invisible
    until the stamp moves.

Success versus attempt
    ``pulled_at`` records the last **successful** pull and MUST NOT be advanced on a
    failed one. A writer that stamps every attempt inverts the whole mechanism: a
    dead updater would then read as permanently fresh, which is the exact condition
    this stamp exists to expose.

Only ``pulled_at`` is bounded. ``committed_at`` is reported and never bounded — a
repository nobody has pushed to for weeks is healthy; an updater that has stopped
pulling is not.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from henk.tools.base import Tool, ToolClass, ToolResult

logger = logging.getLogger("henk.tools.homelab_docs")

#: Where documentation lives inside the clone. The walk starts here and never
#: above it, so `.git`, `node_modules`, `package.json` and `astro.config.mjs` are
#: out of reach rather than merely filtered out.
DOCS_SUBPATH = "src/content/docs"

#: The only file types that are documentation. Everything else under the subpath
#: (images, data files, component sources) is not indexed.
DOC_EXTENSIONS = (".md", ".mdx")

#: The freshness stamp, at the clone root — see the module docstring.
STAMP_FILENAME = "homelab-docs-stamp.json"

#: Section ids are opaque and self-identifying, so a value that is obviously a
#: path is obviously not one of these.
SECTION_ID_PREFIX = "sec-"
_SECTION_ID_HEX = 16

#: Characters stripped from a query token *only* when the token as given matches
#: nothing, so that "runbook." can still find "runbook" while `(a+)+$` is matched
#: exactly as supplied. Deliberately excludes brackets and `$`.
_TOKEN_TRIM = ".,;:!?\"'`"

_SNIPPET_CHARS = 200
_HEADING_BOOST = 5
_DISTINCT_TOKEN_BONUS = 10

_PREAMBLE_LABEL = "(document preamble)"


# --- the sectioniser -----------------------------------------------------


@dataclass(frozen=True)
class RawSection:
    """One heading-bounded slice of a document, before it gets an id."""

    heading_path: tuple[str, ...]
    level: int
    text: str


def strip_frontmatter(text: str) -> str:
    """Drop a leading `---` fenced frontmatter block.

    Frontmatter is metadata, not prose: returning it as section text would put
    sidebar ordering and slugs into search snippets and read results. An
    *unterminated* opening fence is not frontmatter and is left alone.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    for position in range(1, len(lines)):
        if lines[position].strip() in ("---", "..."):
            return "\n".join(lines[position + 1 :])
    return text


def _heading(line: str) -> tuple[int, str] | None:
    """`(level, title)` for an ATX heading line, else ``None``.

    Strict on purpose: the hashes must start the line and be followed by a space.
    An indented `#` is code, not a heading.
    """
    if not line.startswith("#"):
        return None
    level = 0
    while level < len(line) and line[level] == "#":
        level += 1
    if level > 6 or level >= len(line) or line[level] not in (" ", "\t"):
        return None
    title = line[level:].strip().rstrip("#").strip()
    if not title:
        return None
    return level, title


def split_sections(text: str) -> list[RawSection]:
    """Split a document at its heading boundaries, recording the heading ancestry.

    Fenced code blocks are tracked, because a shell comment inside a fence starts
    with `#` and a naive splitter would invent a heading from it — the corpus has
    exactly that shape.
    """
    body = strip_frontmatter(text)
    sections: list[RawSection] = []
    stack: list[tuple[int, str]] = []
    current_path: tuple[str, ...] = ()
    current_level = 0
    current_lines: list[str] = []
    in_fence = False
    fence_marker = ""

    for line in body.split("\n"):
        stripped = line.strip()
        if in_fence:
            if fence_marker and stripped.startswith(fence_marker):
                in_fence = False
            current_lines.append(line)
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = True
            fence_marker = stripped[:3]
            current_lines.append(line)
            continue
        found = _heading(line)
        if found is None:
            current_lines.append(line)
            continue
        level, title = found
        sections.append(
            RawSection(current_path, current_level, "\n".join(current_lines).strip("\n"))
        )
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        current_path = tuple(entry for _, entry in stack)
        current_level = level
        current_lines = []

    sections.append(
        RawSection(current_path, current_level, "\n".join(current_lines).strip("\n"))
    )
    # An empty lead-in before the first heading is not a section; an empty section
    # under a real heading is (the heading itself is the content).
    return [s for s in sections if s.heading_path or s.text.strip()]


def section_id(rel_path: str, heading_path: Sequence[str], repeat: int = 0) -> str:
    """A stable id for one section, derived from where it is in the *documents*.

    Content-derived rather than ordinal, so a section keeps its id across a pull
    that inserts or deletes sections above it. ``repeat`` disambiguates the case
    the heading path cannot: two sections in one file sharing a heading path.
    """
    parts = [rel_path, *heading_path]
    if repeat:
        parts.append(f"#{repeat}")
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"{SECTION_ID_PREFIX}{digest[:_SECTION_ID_HEX]}"


# --- the index -----------------------------------------------------------


@dataclass(frozen=True)
class DocSection:
    """One indexed section: where it came from, and its text for ranking."""

    section_id: str
    rel_path: str
    absolute_path: Path
    heading_path: tuple[str, ...]
    repeat: int
    text: str

    @property
    def display_heading(self) -> str:
        return " > ".join(self.heading_path) if self.heading_path else _PREAMBLE_LABEL


@dataclass(frozen=True)
class DocsIndex:
    """The whole allowlisted corpus, in memory.

    ``files_seen`` counts files that survived the glob but *before* the allowlist,
    which is what makes "the corpus is empty" distinguishable from "the allowlist
    excludes everything" — two default-deny conditions that must not read alike.
    """

    sections: tuple[DocSection, ...] = ()
    files_seen: int = 0
    files_indexed: int = 0
    by_id: dict[str, DocSection] = field(default_factory=dict)


@dataclass(frozen=True)
class _Prefix:
    """A normalized allowlist entry, in its folder-boundary forms."""

    wire: str
    with_slash: str
    sans_slash: str


def normalize_allowlist(entries: Sequence[str]) -> tuple[_Prefix, ...]:
    """Strip whitespace, drop a leading `/`, discard what is then empty.

    A discarded entry must never broaden scope — an allowlist of `["", " "]` is an
    empty allowlist, which surfaces nothing.
    """
    prefixes: list[_Prefix] = []
    for raw in entries or ():
        stripped = str(raw).strip()
        while stripped.startswith("/"):
            stripped = stripped[1:]
        stripped = stripped.strip()
        if not stripped:
            logger.warning(
                "homelab_docs: dropped empty/whitespace allowlist entry %r "
                "(does not broaden scope)",
                raw,
            )
            continue
        with_slash = stripped if stripped.endswith("/") else stripped + "/"
        prefixes.append(
            _Prefix(wire=stripped, with_slash=with_slash, sans_slash=with_slash[:-1])
        )
    return tuple(prefixes)


def _allowed(rel_path: str, prefixes: Sequence[_Prefix]) -> bool:
    """Folder-boundary prefix match, so `devices` never matches `devices-archive`."""
    return any(
        rel_path == prefix.sans_slash or rel_path.startswith(prefix.with_slash)
        for prefix in prefixes
    )


def _documentation_files(docs_root: Path) -> list[Path]:
    """Every documentation file under the docs subpath, following no symlink.

    ``os.walk`` with ``followlinks=False`` will not descend into a symlinked
    directory, and symlinked *files* are skipped explicitly — git can store a
    symlink, and a followed one would put content from outside the corpus into the
    index.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(docs_root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and not (here / name).is_symlink()
        )
        for name in sorted(filenames):
            candidate = here / name
            if candidate.is_symlink():
                continue
            if candidate.suffix not in DOC_EXTENSIONS:
                continue
            found.append(candidate)
    return found


def build_index(clone_path: str | Path, allowlist: Sequence[str]) -> DocsIndex:
    """Walk the docs subpath and build the allowlisted section index.

    Composition is **glob first, then allowlist**: an allowlist entry can never
    re-admit a file the extension/subpath filter already excluded, so
    `../README.md` in the allowlist is inert rather than an escape hatch.
    """
    docs_root = Path(clone_path) / DOCS_SUBPATH
    if not docs_root.is_dir():
        return DocsIndex()

    prefixes = normalize_allowlist(allowlist)
    sections: list[DocSection] = []
    files_seen = 0
    files_indexed = 0

    for absolute in _documentation_files(docs_root):
        files_seen += 1
        rel_path = absolute.relative_to(docs_root).as_posix()
        if not prefixes or not _allowed(rel_path, prefixes):
            continue
        try:
            text = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning(
                "homelab_docs: skipping unreadable documentation file %s (%s)",
                rel_path,
                exc.strerror or exc,
            )
            continue
        files_indexed += 1
        repeats: dict[tuple[str, ...], int] = {}
        for raw in split_sections(text):
            repeat = repeats.get(raw.heading_path, 0)
            repeats[raw.heading_path] = repeat + 1
            sections.append(
                DocSection(
                    section_id=section_id(rel_path, raw.heading_path, repeat),
                    rel_path=rel_path,
                    absolute_path=absolute,
                    heading_path=raw.heading_path,
                    repeat=repeat,
                    text=raw.text,
                )
            )

    return DocsIndex(
        sections=tuple(sections),
        files_seen=files_seen,
        files_indexed=files_indexed,
        by_id={s.section_id: s for s in sections},
    )


# --- the freshness stamp -------------------------------------------------


@dataclass(frozen=True)
class Stamp:
    """What the host recorded about the corpus, and what could not be read."""

    commit: str | None = None
    committed_at: datetime | None = None
    committed_at_raw: str | None = None
    pulled_at: datetime | None = None
    raw_pulled_at: str | None = None
    problem: str | None = None


def _parse_timestamp(value: Any) -> datetime | None:
    """ISO-8601 → aware datetime. A value with no offset is read as UTC."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_stamp(clone_path: str | Path) -> Stamp:
    """Read the freshness stamp. Every failure is a *reported* condition."""
    path = Path(clone_path) / STAMP_FILENAME
    if not path.exists():
        return Stamp(problem="is missing")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Stamp(problem=f"could not be read ({exc.strerror or exc})")
    try:
        payload = json.loads(raw)
    except ValueError:
        return Stamp(problem="is not valid JSON")
    if not isinstance(payload, dict):
        return Stamp(problem="is not a JSON object")

    raw_pulled = payload.get("pulled_at")
    pulled_at = _parse_timestamp(raw_pulled)
    if pulled_at is None:
        return Stamp(problem="has no readable pulled_at value")

    committed_raw = payload.get("committed_at")
    commit = payload.get("commit")
    return Stamp(
        commit=commit if isinstance(commit, str) and commit.strip() else None,
        committed_at=_parse_timestamp(committed_raw),
        committed_at_raw=committed_raw if isinstance(committed_raw, str) else None,
        pulled_at=pulled_at,
        raw_pulled_at=str(raw_pulled),
    )


def format_age(seconds: float) -> str:
    """Human age. Hours stay hours for ten days, because the bound is in hours."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 10 * 86400:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"
    return f"{int(seconds // 86400)}d {int((seconds % 86400) // 3600)}h"


# --- the tool ------------------------------------------------------------


class HomelabDocsTool(Tool):
    name = "homelab_docs"
    description = (
        "Search and read the homelab documentation corpus, mounted read-only from "
        "a host-side clone. Read-only, makes no network request. `search` returns "
        "ranked candidate sections with their heading path, file and section id; "
        "`read` returns one section's text, addressed by a section id from a prior "
        "search — never by a file path. Every result states how old the corpus is."
    )
    tool_class = ToolClass.READ_ONLY
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "read"],
                "description": (
                    "`search` for ranked candidate sections; `read` for one "
                    "section's text."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "search only: words to look for. Matched as literal tokens, "
                    "not as a pattern."
                ),
            },
            "section_id": {
                "type": "string",
                "description": (
                    "read only: a section id returned by a prior search. This is an "
                    "index key, not a path — a file path or path fragment is not a "
                    "value this parameter can hold."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        path: str,
        allowlist: Sequence[str] = (),
        stamp_max_age_seconds: float = 26 * 3600.0,
        read_byte_budget: int = 8000,
        search_result_count: int = 5,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = (path or "").strip()
        self._allowlist = normalize_allowlist(allowlist)
        self._stamp_max_age = float(stamp_max_age_seconds)
        self._read_byte_budget = int(read_byte_budget)
        self._search_result_count = int(search_result_count)
        self._clock = clock
        self._index: DocsIndex | None = None
        self._index_key: str | None = None
        self._index_builds = 0

    @property
    def effective_allowlist(self) -> tuple[str, ...]:
        """Surviving allowlist entries. Empty → the tool surfaces nothing."""
        return tuple(prefix.wire for prefix in self._allowlist)

    @property
    def index_build_count(self) -> int:
        """How many times the index has been built (the stamp decides; test seam)."""
        return self._index_builds

    # -- dispatch ---------------------------------------------------------

    async def _run(self, **arguments: Any) -> ToolResult:  # type: ignore[override]
        action = arguments.get("action")
        if action not in ("search", "read"):
            return ToolResult.failure(
                f"homelab_docs: unknown action {action!r}; the supported actions are "
                "'search' and 'read'"
            )
        query = arguments.get("query")
        supplied_id = arguments.get("section_id")
        if action == "search" and not (isinstance(query, str) and query.strip()):
            return ToolResult.failure("homelab_docs: the search action needs a query")
        if action == "read" and not (isinstance(supplied_id, str) and supplied_id.strip()):
            return ToolResult.failure(
                "homelab_docs: the read action needs a section_id from a prior search"
            )

        unavailable = self._availability_error()
        if unavailable is not None:
            return ToolResult.failure(unavailable)

        stamp = read_stamp(self._path)
        freshness = self._freshness_line(stamp)

        if not self._allowlist:
            return ToolResult.success(
                freshness
                + "\n\nNo documentation is in scope: the corpus path allowlist "
                "(personal_data.docs_path_allowlist) has no entries, so nothing is "
                "indexed and no file content can be surfaced."
            )

        index = self._index_for(stamp)
        if index.files_seen == 0:
            return ToolResult.failure(
                f"homelab_docs: the corpus at {self._path} contains no indexable "
                f"documentation — no {' or '.join(DOC_EXTENSIONS)} file exists under "
                f"{DOCS_SUBPATH}. This is an empty corpus, not a search that found "
                "nothing."
            )
        if index.files_indexed == 0:
            return ToolResult.success(
                freshness
                + "\n\nNo documentation is in scope: the corpus path allowlist "
                "(personal_data.docs_path_allowlist) matches none of the "
                f"{index.files_seen} documentation file(s) under {DOCS_SUBPATH}. Its "
                f"entries are {', '.join(self.effective_allowlist)}, read relative to "
                "the documentation root."
            )

        if action == "search":
            return self._search(freshness, index, str(query))
        return self._read(freshness, index, str(supplied_id))

    # -- availability -----------------------------------------------------

    def _availability_error(self) -> str | None:
        """Host state, checked on every call so runtime loss reads like absence."""
        if not self._path:
            return (
                "homelab_docs: homelab_docs.path is not configured, so there is no "
                "corpus to read"
            )
        root = Path(self._path)
        if not root.exists():
            return (
                f"homelab_docs: the documentation corpus directory {self._path} is "
                "missing — nothing is mounted there"
            )
        if not root.is_dir():
            return (
                f"homelab_docs: the documentation corpus path {self._path} is not a "
                "directory"
            )
        if not os.access(root, os.R_OK | os.X_OK):
            return (
                f"homelab_docs: the documentation corpus directory {self._path} is "
                "not readable by this process"
            )
        docs_root = root / DOCS_SUBPATH
        if not docs_root.is_dir():
            return (
                f"homelab_docs: the documentation subpath {DOCS_SUBPATH} is missing "
                f"under {self._path} — the mount does not look like the docs clone"
            )
        if not os.access(docs_root, os.R_OK | os.X_OK):
            return (
                f"homelab_docs: the documentation subpath {DOCS_SUBPATH} under "
                f"{self._path} is not readable by this process"
            )
        return None

    # -- freshness --------------------------------------------------------

    def _freshness_line(self, stamp: Stamp) -> str:
        stamp_location = f"{self._path}/{STAMP_FILENAME}"
        if stamp.pulled_at is None:
            problem = stamp.problem or "has no readable pulled_at value"
            return (
                f"Corpus freshness: UNKNOWN — the freshness stamp at {stamp_location} "
                f"{problem}. The content below is served anyway and must not be "
                "treated as current."
            )
        age = self._clock() - stamp.pulled_at.timestamp()
        commit_note = self._commit_note(stamp)
        if age > self._stamp_max_age:
            return (
                f"Corpus freshness: STALE — last pulled {format_age(age)} ago, past "
                f"the {format_age(self._stamp_max_age)} bound; the host updater may "
                f"have stopped. {commit_note} The content below is served anyway and "
                "may be out of date."
            )
        return f"Corpus freshness: last pulled {format_age(age)} ago. {commit_note}"

    def _commit_note(self, stamp: Stamp) -> str:
        """The commit half of the line. Reported, and deliberately never bounded."""
        commit = stamp.commit or "unrecorded"
        if stamp.committed_at is not None:
            committed_age = self._clock() - stamp.committed_at.timestamp()
            return (
                f"Commit {commit}, committed {stamp.committed_at_raw} "
                f"({format_age(committed_age)} ago)."
            )
        if stamp.committed_at_raw:
            return (
                f"Commit {commit}, committed {stamp.committed_at_raw} "
                "(commit date not parseable)."
            )
        return f"Commit {commit}, commit date not recorded."

    # -- index ------------------------------------------------------------

    def _index_for(self, stamp: Stamp) -> DocsIndex:
        """Rebuild iff the stamp's last-pull value moved.

        With no readable stamp there is no change signal at all, so the index is
        rebuilt every call rather than trusted — the corpus is small, and serving a
        superseded index is the failure that matters.
        """
        key = stamp.raw_pulled_at
        if key is None or self._index is None or self._index_key != key:
            self._index = build_index(self._path, self.effective_allowlist)
            self._index_key = key
            self._index_builds += 1
        return self._index

    # -- search -----------------------------------------------------------

    @staticmethod
    def _tokens(query: str) -> list[str]:
        """Whitespace-separated literal tokens. No pattern language, ever."""
        return [token for token in query.lower().split() if token]

    def score_sections(
        self, query: str, sections: Sequence[DocSection]
    ) -> list[tuple[DocSection, int]]:
        """Rank sections by literal token counts, boosting the heading path."""
        tokens = self._tokens(query)
        scored: list[tuple[DocSection, int]] = []
        for entry in sections:
            body = entry.text.lower()
            heading = " > ".join(entry.heading_path).lower()
            score = 0
            matched = 0
            for token in tokens:
                trimmed = token.strip(_TOKEN_TRIM)
                body_hits = body.count(token)
                if body_hits == 0 and trimmed and trimmed != token:
                    body_hits = body.count(trimmed)
                heading_hits = heading.count(token)
                if heading_hits == 0 and trimmed and trimmed != token:
                    heading_hits = heading.count(trimmed)
                if body_hits or heading_hits:
                    matched += 1
                score += body_hits + _HEADING_BOOST * heading_hits
            if score:
                scored.append((entry, score + _DISTINCT_TOKEN_BONUS * matched))
        scored.sort(key=lambda pair: (-pair[1], pair[0].rel_path, pair[0].heading_path))
        return scored

    def _search(self, freshness: str, index: DocsIndex, query: str) -> ToolResult:
        scored = self.score_sections(query, index.sections)
        scope = (
            f"{len(index.sections)} allowlisted section(s) in "
            f"{index.files_indexed} file(s)"
        )
        if not scored:
            return ToolResult.success(
                f"{freshness}\n\nNo section matched {query!r}. The corpus holds {scope}."
            )
        hits = scored[: self._search_result_count]
        lines = [
            freshness,
            "",
            f"{len(hits)} of {len(scored)} matching section(s) for {query!r} "
            f"(searched {scope}):",
        ]
        for position, (entry, score) in enumerate(hits, start=1):
            lines.append(
                f"{position}. {entry.rel_path} — {entry.display_heading}  "
                f"[{entry.section_id}] (score {score})"
            )
            lines.append(f"   {self._snippet(entry, query)}")
        lines.append("")
        lines.append("Use the read action with a section id above for the full text.")
        return ToolResult.success("\n".join(lines))

    def _snippet(self, entry: DocSection, query: str) -> str:
        """A window around the first literal token hit — never a whole section."""
        flat = " ".join(entry.text.split())
        lowered = flat.lower()
        position = -1
        for token in self._tokens(query):
            position = lowered.find(token)
            if position == -1:
                trimmed = token.strip(_TOKEN_TRIM)
                if trimmed and trimmed != token:
                    position = lowered.find(trimmed)
            if position != -1:
                break
        if position == -1:
            position = 0
        start = max(0, position - _SNIPPET_CHARS // 3)
        window = flat[start : start + _SNIPPET_CHARS]
        prefix = "…" if start > 0 else ""
        suffix = "…" if start + _SNIPPET_CHARS < len(flat) else ""
        return f"{prefix}{window}{suffix}"

    # -- read -------------------------------------------------------------

    def _read(self, freshness: str, index: DocsIndex, supplied_id: str) -> ToolResult:
        """Look the id up in the index. On a miss, nothing is opened at all.

        The supplied value is a dict key and never becomes part of a path, which is
        what closes traversal by construction rather than by sanitising.
        """
        entry = index.by_id.get(supplied_id)
        if entry is None:
            return ToolResult.failure(
                "homelab_docs: unknown section id. It is not in the index, so nothing "
                "was read. Section ids come from a homelab_docs search; a file path or "
                "path fragment is not one."
            )
        try:
            text = entry.absolute_path.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult.failure(
                f"homelab_docs: the documentation file {entry.rel_path} could not be "
                f"read ({exc.strerror or exc}); no partial or substituted content is "
                "returned"
            )

        found = None
        repeats: dict[tuple[str, ...], int] = {}
        for raw in split_sections(text):
            repeat = repeats.get(raw.heading_path, 0)
            repeats[raw.heading_path] = repeat + 1
            if raw.heading_path == entry.heading_path and repeat == entry.repeat:
                found = raw
                break
        if found is None:
            return ToolResult.failure(
                f"homelab_docs: the section {entry.display_heading!r} is no longer "
                f"present in {entry.rel_path}; the corpus changed without a new "
                "freshness stamp"
            )

        header = f"{entry.rel_path} — {entry.display_heading}  [{entry.section_id}]"
        body, notice = self._apply_budget(entry, found.text)
        parts = [freshness, "", header, "", body]
        if notice:
            parts.extend(["", notice])
        return ToolResult.success("\n".join(parts))

    def _apply_budget(self, entry: DocSection, text: str) -> tuple[str, str]:
        """Cap the section at the byte budget, and say so when it bites."""
        encoded = text.encode("utf-8")
        if len(encoded) <= self._read_byte_budget:
            return text, ""
        clipped = encoded[: self._read_byte_budget].decode("utf-8", errors="ignore")
        notice = (
            f"[truncated: section {entry.display_heading!r} in {entry.rel_path} is "
            f"{len(encoded)} bytes; the first {self._read_byte_budget} bytes are "
            "shown. Nothing was silently dropped — the rest of this section is not "
            "in this result.]"
        )
        return clipped, notice
