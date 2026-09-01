"""homelab_docs — sectioniser, section ids, index scope, allowlist, search, truncation.

Tasks 5.2-5.8 of the `read-depth` change. Freshness, index invalidation and
availability live in `test_tools_homelab_docs_freshness.py`.

Every test here starts from the synthetic corpus in
`tests/fixtures/homelab_docs_corpus/` (see its README): invented text, placeholder
addresses, and sentinel strings marking content that must not cross a boundary.
"""

from __future__ import annotations

import os
import pathlib
import re
import socket
from pathlib import Path

import pytest

from henk.tools.base import ToolClass
from henk.tools.homelab_docs import (
    DOCS_SUBPATH,
    DOC_EXTENSIONS,
    HomelabDocsTool,
    build_index,
    section_id,
    split_sections,
)
from tests.corpus_fixture import (
    ALL_DOCS,
    BUILD_CONFIG_SENTINEL,
    DEFAULT_ALLOWLIST,
    DEPENDENCY_SENTINEL,
    EXCLUDED_SENTINEL,
    FRESH_NOW,
    FRONTMATTER_SENTINEL,
    FULL_ALLOWLIST,
    GIT_METADATA_SENTINEL,
    OUTSIDE_SENTINEL,
    REPO_README_SENTINEL,
    build_corpus,
    docs_root,
)


def make_tool(clone: Path, **overrides) -> HomelabDocsTool:
    kwargs = dict(
        path=str(clone),
        allowlist=FULL_ALLOWLIST,
        stamp_max_age_seconds=93600.0,
        read_byte_budget=8000,
        search_result_count=5,
        clock=lambda: FRESH_NOW,
    )
    kwargs.update(overrides)
    return HomelabDocsTool(**kwargs)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    return build_corpus(tmp_path)


def index_of(clone: Path, allowlist=FULL_ALLOWLIST):
    return build_index(clone, allowlist)


def heading_paths(index) -> set[tuple[str, ...]]:
    return {entry.heading_path for entry in index.sections}


# --- 5.2 the sectioniser -------------------------------------------------


def test_the_sectioniser_splits_at_heading_boundaries():
    text = "# Top\n\nfirst body\n\n## Second\n\nsecond body\n"
    sections = split_sections(text)
    assert [s.heading_path for s in sections] == [("Top",), ("Top", "Second")]
    assert "first body" in sections[0].text
    assert "second body" not in sections[0].text
    assert "second body" in sections[1].text


def test_a_heading_path_records_the_full_ancestry():
    text = "## Hardware\n\na\n\n### Storage\n\nb\n\n### Memory\n\nc\n\n## Networking\n\nd\n"
    sections = split_sections(text)
    assert [s.heading_path for s in sections] == [
        ("Hardware",),
        ("Hardware", "Storage"),
        ("Hardware", "Memory"),
        ("Networking",),
    ]


def test_a_deeper_heading_after_a_shallower_one_restarts_the_path():
    text = "## A\n\na\n\n### A1\n\nb\n\n## B\n\nc\n\n### B1\n\nd\n"
    assert [s.heading_path for s in split_sections(text)] == [
        ("A",),
        ("A", "A1"),
        ("B",),
        ("B", "B1"),
    ]


def test_text_before_the_first_heading_becomes_a_preamble_section():
    text = "lead-in prose\n\n## First\n\nbody\n"
    sections = split_sections(text)
    assert sections[0].heading_path == ()
    assert "lead-in prose" in sections[0].text


def test_frontmatter_is_excluded_from_section_text(clone: Path):
    index = index_of(clone)
    for entry in index.sections:
        assert FRONTMATTER_SENTINEL not in entry.text
        assert "sidebar:" not in entry.text
    raw = (docs_root(clone) / "index.mdx").read_text(encoding="utf-8")
    assert FRONTMATTER_SENTINEL in raw  # the sentinel is really in the file


def test_frontmatter_only_counts_at_the_top_of_the_file():
    text = "## A\n\nbody\n\n---\n\nstill body\n"
    sections = split_sections(text)
    assert "still body" in sections[0].text


def test_a_hash_line_inside_a_fenced_code_block_is_not_a_heading(clone: Path):
    index = index_of(clone)
    dns = next(
        e
        for e in index.sections
        if e.rel_path == "devices/rp5.md" and e.heading_path == ("Networking", "DNS")
    )
    assert "deliberate trap" in dns.text
    for entry in index.sections:
        for part in entry.heading_path:
            assert "comment line begins" not in part
            assert "deliberate trap" not in part


def test_a_document_past_fifty_kilobytes_yields_sections_not_itself(clone: Path):
    big = docs_root(clone) / "services" / "large-runbook.md"
    whole = big.read_text(encoding="utf-8")
    assert len(whole.encode("utf-8")) > 50_000

    index = index_of(clone)
    from_big = [e for e in index.sections if e.rel_path == "services/large-runbook.md"]
    assert len(from_big) > 10
    assert all(len(e.text.encode("utf-8")) < 50_000 for e in from_big)
    assert not any(e.text.strip() == whole.strip() for e in index.sections)


async def test_a_search_inside_the_large_document_returns_sections(clone: Path):
    result = await make_tool(clone).run(action="search", query="settle time")
    assert result.ok
    assert "large-runbook.md" in result.content
    assert "Procedure" in result.content
    # No candidate carries the whole document.
    whole = (docs_root(clone) / "services" / "large-runbook.md").read_text(
        encoding="utf-8"
    )
    assert whole not in result.content


# --- 5.3 section ids -----------------------------------------------------


def test_section_ids_are_unique_across_the_corpus(clone: Path):
    index = index_of(clone)
    ids = [e.section_id for e in index.sections]
    assert len(ids) == len(set(ids))


def test_repeated_heading_paths_in_one_file_get_distinct_ids(clone: Path):
    index = index_of(clone)
    notes = [
        e
        for e in index.sections
        if e.rel_path == "devices/rp2.md" and e.heading_path == ("Notes",)
    ]
    assert len(notes) == 2
    assert notes[0].section_id != notes[1].section_id


def test_a_section_id_is_derived_from_the_heading_path_not_the_position():
    first = section_id("devices/rp5.md", ("Hardware", "Storage"), 0)
    again = section_id("devices/rp5.md", ("Hardware", "Storage"), 0)
    elsewhere = section_id("devices/rp2.md", ("Hardware", "Storage"), 0)
    assert first == again
    assert first != elsewhere
    assert first != section_id("devices/rp5.md", ("Hardware", "Memory"), 0)


async def test_read_accepts_an_id_that_search_returned(clone: Path):
    tool = make_tool(clone)
    index = index_of(clone)
    target = next(
        e for e in index.sections if e.heading_path == ("Networking", "DNS")
    )
    result = await tool.run(action="read", section_id=target.section_id)
    assert result.ok
    assert "invented resolver" in result.content


async def test_an_unknown_section_id_is_refused(clone: Path):
    result = await make_tool(clone).run(action="read", section_id="sec-deadbeefdeadbeef")
    assert not result.ok
    assert "unknown section" in (result.error or "").lower()


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "../../../outside/private-notes.md",
        "/etc/passwd",
        "devices/rp5.md",
        "src/content/docs/security/access.md",
    ],
)
async def test_a_traversal_shaped_value_is_refused_and_never_joined(
    clone: Path, hostile: str, monkeypatch: pytest.MonkeyPatch
):
    """The parameter is an index key, so no join happens — assert that, don't assume it."""
    seen: list[str] = []

    real_join = os.path.join
    real_truediv = pathlib.PurePath.__truediv__
    real_joinpath = pathlib.PurePath.joinpath
    real_open = pathlib.Path.open
    real_builtin_open = open

    def spy_join(a, *rest):
        seen.extend(str(x) for x in (a, *rest))
        return real_join(a, *rest)

    def spy_truediv(self, other):
        seen.append(str(other))
        return real_truediv(self, other)

    def spy_joinpath(self, *others):
        seen.extend(str(o) for o in others)
        return real_joinpath(self, *others)

    def spy_path_open(self, *a, **kw):
        seen.append(str(self))
        return real_open(self, *a, **kw)

    def spy_builtin_open(file, *a, **kw):
        seen.append(str(file))
        return real_builtin_open(file, *a, **kw)

    tool = make_tool(clone)
    await tool.run(action="search", query="warm")  # build the index before spying

    monkeypatch.setattr(os.path, "join", spy_join)
    monkeypatch.setattr(pathlib.PurePath, "__truediv__", spy_truediv)
    monkeypatch.setattr(pathlib.PurePath, "joinpath", spy_joinpath)
    monkeypatch.setattr(pathlib.Path, "open", spy_path_open)
    monkeypatch.setattr("builtins.open", spy_builtin_open)
    try:
        result = await tool.run(action="read", section_id=hostile)
    finally:
        monkeypatch.undo()

    assert not result.ok
    assert "unknown section" in (result.error or "").lower()
    for recorded in seen:
        assert hostile not in recorded
        assert "etc/passwd" not in recorded
    assert OUTSIDE_SENTINEL not in (result.content or "")


async def test_a_path_that_exists_is_still_not_a_section_id(clone: Path):
    """`devices/rp5.md` names a real file — and is still not an index key."""
    result = await make_tool(clone).run(action="read", section_id="devices/rp5.md")
    assert not result.ok
    assert "unknown section" in (result.error or "").lower()
    assert "SYNTHETIC-PREAMBLE-SENTINEL" not in (result.content or "")


# --- 5.4 id stability across a rebuild -----------------------------------


def test_ids_survive_a_rebuild_that_shifts_ordinals(clone: Path):
    before = {
        e.heading_path: e.section_id
        for e in index_of(clone).sections
        if e.rel_path == "devices/rp5.md"
    }
    page = docs_root(clone) / "devices" / "rp5.md"
    text = page.read_text(encoding="utf-8")
    # Delete an earlier section and insert a new one at the top: every ordinal
    # after the edit moves.
    text = text.replace(
        "## Hardware\n\nA made-up single-board computer with made-up specifications.\n",
        "## Provenance\n\nInvented, like everything else here.\n\n"
        "## Hardware\n\nA made-up single-board computer with made-up specifications.\n",
    )
    page.write_text(text, encoding="utf-8")

    after = {
        e.heading_path: e.section_id
        for e in index_of(clone).sections
        if e.rel_path == "devices/rp5.md"
    }
    for heading_path, ident in before.items():
        assert after[heading_path] == ident, heading_path
    assert ("Provenance",) in after


async def test_a_search_then_read_across_an_update_addresses_the_same_section(
    clone: Path,
):
    tool = make_tool(clone)
    index = index_of(clone)
    target = next(e for e in index.sections if e.heading_path == ("Recovery",))
    ident = target.section_id

    page = docs_root(clone) / "devices" / "rp5.md"
    page.write_text(
        "## Brand new first section\n\nInserted ahead of everything.\n\n"
        + page.read_text(encoding="utf-8").split("---\n", 2)[2],
        encoding="utf-8",
    )

    rebuilt = index_of(clone)
    same = next(e for e in rebuilt.sections if e.section_id == ident)
    assert same.heading_path == ("Recovery",)
    assert "off and on again" in same.text


# --- 5.5 index scope -----------------------------------------------------


def test_only_documentation_extensions_under_the_docs_subpath_are_indexed(clone: Path):
    index = index_of(clone)
    assert {e.rel_path for e in index.sections} == set(ALL_DOCS)
    for entry in index.sections:
        assert Path(entry.rel_path).suffix in DOC_EXTENSIONS
        assert entry.absolute_path.is_relative_to(docs_root(clone))


#: An allowlist that names the repository furniture as well as the docs, so a walk
#: that started too high would be admitted rather than filtered out by accident.
GREEDY_ALLOWLIST = FULL_ALLOWLIST + (
    "README.md",
    "package.json",
    "astro.config.mjs",
    "node_modules/",
    ".git/",
    "outside/",
    "src/",
    "src/content/",
    "src/content/docs/",
)


def test_repository_and_build_files_are_not_indexed(clone: Path):
    index = index_of(clone, GREEDY_ALLOWLIST)
    blob = "\n".join(e.text for e in index.sections)
    for sentinel in (
        REPO_README_SENTINEL,
        BUILD_CONFIG_SENTINEL,
        DEPENDENCY_SENTINEL,
        GIT_METADATA_SENTINEL,
    ):
        assert sentinel not in blob
    rels = {e.rel_path for e in index.sections}
    assert not any(r.startswith("..") for r in rels)
    assert "README.md" not in rels
    assert not any("node_modules" in r or ".git" in r for r in rels)


def test_the_docs_subpath_is_where_the_walk_starts(clone: Path):
    assert DOCS_SUBPATH == "src/content/docs"
    assert (clone / DOCS_SUBPATH).is_dir()


def test_a_symlink_out_of_the_docs_subpath_yields_no_index_entry(clone: Path):
    link = docs_root(clone) / "outside-notes.md"
    assert link.is_symlink()
    assert link.resolve().exists()  # the target really is reachable

    # The allowlist explicitly admits the link's path, so the *only* thing that can
    # keep it out of the index is the indexer refusing to follow it. Without this
    # the test would pass for the wrong reason.
    index = index_of(clone, FULL_ALLOWLIST + ("outside-notes.md",))
    assert "outside-notes.md" not in {e.rel_path for e in index.sections}
    assert OUTSIDE_SENTINEL not in "\n".join(e.text for e in index.sections)


async def test_a_symlinked_directory_is_not_walked(clone: Path, tmp_path: Path):
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (outside_dir / "leaked.md").write_text(
        "## Leaked\n\nSYNTHETIC-LINKED-DIR-SENTINEL\n", encoding="utf-8"
    )
    (docs_root(clone) / "linked").symlink_to(outside_dir, target_is_directory=True)

    # As above: the allowlist admits `linked/`, so only the walk can keep it out.
    allowlist = FULL_ALLOWLIST + ("linked/",)
    index = index_of(clone, allowlist)
    assert "SYNTHETIC-LINKED-DIR-SENTINEL" not in "\n".join(
        e.text for e in index.sections
    )
    assert not any(e.rel_path.startswith("linked/") for e in index.sections)

    result = await make_tool(clone, allowlist=allowlist).run(
        action="search", query="SYNTHETIC-LINKED-DIR-SENTINEL"
    )
    assert result.ok
    assert "no section matched" in result.content.lower()


# --- 5.6 the allowlist, applied at index build ---------------------------


def test_a_non_allowlisted_file_is_absent_from_the_index(clone: Path):
    index = index_of(clone, DEFAULT_ALLOWLIST)
    assert "security/access.md" not in {e.rel_path for e in index.sections}
    assert EXCLUDED_SENTINEL not in "\n".join(e.text for e in index.sections)


async def test_a_non_allowlisted_file_contributes_no_candidate_and_no_snippet(
    clone: Path,
):
    tool = make_tool(clone, allowlist=DEFAULT_ALLOWLIST)
    result = await tool.run(action="search", query="break-glass account")
    assert result.ok
    assert EXCLUDED_SENTINEL not in result.content
    assert "security/access.md" not in result.content


async def test_a_non_allowlisted_section_id_is_not_readable(clone: Path):
    allowed = index_of(clone, FULL_ALLOWLIST)
    secret = next(
        e for e in allowed.sections if e.rel_path == "security/access.md"
    )
    tool = make_tool(clone, allowlist=DEFAULT_ALLOWLIST)
    result = await tool.run(action="read", section_id=secret.section_id)
    assert not result.ok
    assert "unknown section" in (result.error or "").lower()
    assert EXCLUDED_SENTINEL not in (result.content or "")


@pytest.mark.parametrize("empty", [(), ("",), ("   ",), ("/",), (" ", "")])
async def test_an_empty_or_blank_allowlist_surfaces_nothing(clone: Path, empty):
    tool = make_tool(clone, allowlist=empty)
    assert tool.effective_allowlist == ()
    for arguments in (
        {"action": "search", "query": "prometheus"},
        {"action": "read", "section_id": "sec-0000000000000000"},
    ):
        result = await tool.run(**arguments)
        content = (result.content or "") + (result.error or "")
        assert "allowlist" in content.lower()
        assert "no documentation" in content.lower()
        for sentinel in ("Prometheus", "synthetic estate", EXCLUDED_SENTINEL):
            assert sentinel not in content


def test_an_entry_empty_after_normalization_is_discarded_not_broadened(clone: Path):
    tool = make_tool(clone, allowlist=("  ", "/", "devices/"))
    assert tool.effective_allowlist == ("devices/",)
    index = index_of(clone, ("  ", "/", "devices/"))
    assert {e.rel_path for e in index.sections} == {"devices/rp5.md", "devices/rp2.md"}


def test_allowlist_entries_are_relative_to_the_docs_root_not_the_mount(clone: Path):
    """`devices/` matches; the mount-prefixed spelling matches nothing."""
    docs_relative = index_of(clone, ("devices/",))
    assert {e.rel_path for e in docs_relative.sections} == {
        "devices/rp5.md",
        "devices/rp2.md",
    }
    mount_relative = index_of(clone, ("src/content/docs/devices/",))
    assert mount_relative.sections == ()


def test_the_allowlist_matches_on_folder_boundaries(clone: Path):
    """`devices` must not match a sibling named `devices-archive`."""
    archive = docs_root(clone) / "devices-archive"
    archive.mkdir()
    (archive / "old.md").write_text(
        "## Archived\n\nSYNTHETIC-ARCHIVE-SENTINEL\n", encoding="utf-8"
    )
    index = index_of(clone, ("devices",))
    rels = {e.rel_path for e in index.sections}
    assert rels == {"devices/rp5.md", "devices/rp2.md"}
    assert "SYNTHETIC-ARCHIVE-SENTINEL" not in "\n".join(e.text for e in index.sections)


def test_a_single_file_entry_allowlists_exactly_that_file(clone: Path):
    index = index_of(clone, ("index.mdx",))
    assert {e.rel_path for e in index.sections} == {"index.mdx"}


def test_filtering_composes_glob_first_then_allowlist(clone: Path):
    """An allowlist entry cannot re-admit what the glob already excluded.

    Both spellings are tried — the docs-root-relative `../README.md` and the
    clone-root-relative `README.md` — so neither an escape upward nor a walk that
    started too high can slip through this one.
    """
    index = index_of(
        clone,
        (
            "../README.md",
            "../package.json",
            "../outside/",
            "README.md",
            "package.json",
            "outside/",
            "node_modules/",
            ".git/",
            "devices/",
        ),
    )
    assert {e.rel_path for e in index.sections} == {"devices/rp5.md", "devices/rp2.md"}
    blob = "\n".join(e.text for e in index.sections)
    assert REPO_README_SENTINEL not in blob
    assert OUTSIDE_SENTINEL not in blob
    assert DEPENDENCY_SENTINEL not in blob
    assert GIT_METADATA_SENTINEL not in blob


# --- 5.7 search ----------------------------------------------------------


async def test_search_ranks_by_score_and_lists_the_required_fields(clone: Path):
    result = await make_tool(clone).run(action="search", query="scrape target")
    assert result.ok
    assert "services/monitoring.md" in result.content
    assert "Prometheus > Scrape targets" in result.content
    index = index_of(clone)
    target = next(
        e for e in index.sections if e.heading_path == ("Prometheus", "Scrape targets")
    )
    assert target.section_id in result.content
    # The heading-path boost puts the section titled "Scrape targets" first.
    body = result.content.split("\n")
    first_hit = next(line for line in body if line.startswith("1."))
    assert "Scrape targets" in first_hit


async def test_the_heading_path_boost_outranks_a_body_only_match(clone: Path):
    index = index_of(clone)
    heading_hit = next(
        e for e in index.sections if e.heading_path == ("Prometheus", "Scrape targets")
    )
    scores = make_tool(clone).score_sections("scrape targets", index.sections)
    ranked = [entry.section_id for entry, _ in scores]
    assert ranked[0] == heading_hit.section_id


def _hit_lines(content: str) -> list[str]:
    """Result lines of the form `<n>. …` — the numbered candidates."""
    return [
        line
        for line in content.splitlines()
        if line[:1].isdigit() and line[1:3] == ". " or line[:2].isdigit() and line[2:4] == ". "
    ]


async def test_the_configured_result_count_is_honoured(clone: Path):
    narrow = await make_tool(clone, search_result_count=3).run(
        action="search", query="synthetic"
    )
    assert len(_hit_lines(narrow.content)) == 3
    wider = await make_tool(clone, search_result_count=5).run(
        action="search", query="synthetic"
    )
    assert len(_hit_lines(wider.content)) == 5


async def test_a_regex_metacharacter_query_is_matched_literally(clone: Path):
    result = await make_tool(clone).run(action="search", query="(a+)+$")
    assert result.ok
    assert "services/monitoring.md" in result.content
    assert "Alerting" in result.content


async def test_the_query_is_never_compiled_as_a_pattern(clone: Path, monkeypatch):
    tool = make_tool(clone)
    await tool.run(action="search", query="warm")  # index built before the trap

    compiled: list[object] = []

    def refuse(pattern, *a, **kw):
        compiled.append(pattern)
        raise AssertionError(f"the query reached re.compile: {pattern!r}")

    monkeypatch.setattr(re, "compile", refuse)
    try:
        result = await tool.run(action="search", query="(a+)+$ [unclosed (")
    finally:
        monkeypatch.undo()
    assert compiled == []
    assert result.ok


def _module_source() -> str:
    import henk.tools.homelab_docs as module

    return Path(module.__file__).read_text(encoding="utf-8")


def test_the_module_imports_no_regex_engine():
    source = _module_source()
    assert "import re" not in source
    assert "re.compile" not in source
    assert "fnmatch" not in source


async def test_a_query_matching_nothing_is_distinguishable_from_an_empty_corpus(
    clone: Path,
):
    result = await make_tool(clone).run(action="search", query="zzzznotpresentzzzz")
    assert result.ok
    lowered = result.content.lower()
    assert "no section matched" in lowered
    assert "empty" not in lowered
    assert "section" in lowered


async def test_search_requires_a_query(clone: Path):
    result = await make_tool(clone).run(action="search")
    assert not result.ok
    assert "query" in (result.error or "").lower()


async def test_an_unknown_action_is_refused(clone: Path):
    result = await make_tool(clone).run(action="delete", section_id="x")
    assert not result.ok
    assert "action" in (result.error or "").lower()


# --- 5.8 truncation ------------------------------------------------------


async def test_an_oversized_section_is_truncated_and_says_so(clone: Path):
    index = index_of(clone)
    big = max(index.sections, key=lambda e: len(e.text.encode("utf-8")))
    assert len(big.text.encode("utf-8")) > 8000

    result = await make_tool(clone, read_byte_budget=8000).run(
        action="read", section_id=big.section_id
    )
    assert result.ok
    assert "truncated" in result.content.lower()
    # The notice names the section.
    assert " > ".join(big.heading_path) in result.content
    assert big.rel_path in result.content
    assert len(result.content.encode("utf-8")) < len(big.text.encode("utf-8")) + 2000


async def test_a_section_within_budget_is_whole_and_carries_no_notice(clone: Path):
    index = index_of(clone)
    small = next(
        e for e in index.sections if e.heading_path == ("Networking", "DNS")
    )
    assert len(small.text.encode("utf-8")) < 8000
    result = await make_tool(clone).run(action="read", section_id=small.section_id)
    assert result.ok
    assert "truncat" not in result.content.lower()
    assert small.text.strip() in result.content


async def test_truncation_never_splits_a_multibyte_character(clone: Path):
    page = docs_root(clone) / "devices" / "rp2.md"
    page.write_text(
        "## Wide\n\n" + ("é" * 400) + "\n", encoding="utf-8"
    )
    index = index_of(clone)
    wide = next(
        e for e in index.sections if e.rel_path == "devices/rp2.md" and e.heading_path == ("Wide",)
    )
    result = await make_tool(clone, read_byte_budget=101).run(
        action="read", section_id=wide.section_id
    )
    assert result.ok  # decoding did not explode
    assert "truncated" in result.content.lower()


async def test_read_requires_a_section_id(clone: Path):
    result = await make_tool(clone).run(action="read")
    assert not result.ok
    assert "section_id" in (result.error or "")


# --- tool shape ----------------------------------------------------------


def test_the_tool_is_read_only_and_declares_two_actions(clone: Path):
    tool = make_tool(clone)
    assert tool.name == "homelab_docs"
    assert tool.tool_class is ToolClass.READ_ONLY
    assert tool.parameters["properties"]["action"]["enum"] == ["search", "read"]
    assert tool.parameters["additionalProperties"] is False


async def test_the_tool_makes_no_network_call(clone: Path, monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("homelab_docs opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    tool = make_tool(clone)
    search = await tool.run(action="search", query="prometheus")
    assert search.ok
    index = index_of(clone)
    result = await tool.run(action="read", section_id=index.sections[0].section_id)
    assert result.ok


def test_the_module_imports_no_http_client():
    source = _module_source()
    assert "httpx" not in source
    assert "urllib" not in source
    assert "socket" not in source


async def test_the_tool_never_writes_into_the_corpus(clone: Path):
    before = {
        p: p.stat().st_mtime_ns for p in clone.rglob("*") if p.is_file() and not p.is_symlink()
    }
    tool = make_tool(clone)
    await tool.run(action="search", query="synthetic")
    index = index_of(clone)
    await tool.run(action="read", section_id=index.sections[0].section_id)
    after = {
        p: p.stat().st_mtime_ns for p in clone.rglob("*") if p.is_file() and not p.is_symlink()
    }
    assert before == after
