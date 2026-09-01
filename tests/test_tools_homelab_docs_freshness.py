"""homelab_docs — freshness stamp, index invalidation, availability, the two gates.

Tasks 5.9-5.13 of the `read-depth` change.

The stamp contract these tests pin is the one `deploy/homelab-docs-stamp.sh` (task
8.3) has to write; it is spelled out in the module docstring of
`henk/tools/homelab_docs.py` and in `notes/apply-decisions-docs.md`.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from henk.config import Config, ConfigError
from henk.tools.homelab_docs import STAMP_FILENAME, HomelabDocsTool, build_index
from tests.corpus_fixture import (
    DEFAULT_ALLOWLIST,
    FRESH_NOW,
    FULL_ALLOWLIST,
    STAMP_PULLED_AT,
    build_corpus,
    docs_root,
    hours,
    stamp_path,
    write_stamp,
)
from tests.test_config import _minimal_raw


class Clock:
    """A settable clock, so a test can advance time without touching the corpus."""

    def __init__(self, now: float = FRESH_NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_tool(clone: Path, clock: Clock | None = None, **overrides) -> HomelabDocsTool:
    kwargs = dict(
        path=str(clone),
        allowlist=FULL_ALLOWLIST,
        stamp_max_age_seconds=93600.0,
        read_byte_budget=8000,
        search_result_count=5,
        clock=clock or Clock(),
    )
    kwargs.update(overrides)
    return HomelabDocsTool(**kwargs)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    return build_corpus(tmp_path)


def some_section_id(clone: Path, allowlist=FULL_ALLOWLIST) -> str:
    index = build_index(clone, allowlist)
    return next(
        e for e in index.sections if e.heading_path == ("Networking", "DNS")
    ).section_id


async def both_results(tool: HomelabDocsTool, clone: Path):
    """Run both actions; every freshness rule applies to both."""
    search = await tool.run(action="search", query="resolver")
    read = await tool.run(action="read", section_id=some_section_id(clone))
    return search, read


# --- 5.9 freshness -------------------------------------------------------


async def test_a_fresh_result_states_the_commit_and_the_pull_age(clone: Path):
    tool = make_tool(clone)
    for result in await both_results(tool, clone):
        assert result.ok
        assert "5ynth3t1" in result.content  # the commit
        assert "2026-08-30T09:14:07Z" in result.content  # committed_at
        assert "2h" in result.content  # pulled two hours before the clock
        assert "STALE" not in result.content
        assert "UNKNOWN" not in result.content


async def test_past_the_bound_the_result_is_marked_stale_and_still_served(clone: Path):
    tool = make_tool(clone, Clock(FRESH_NOW + hours(37)))
    search, read = await both_results(tool, clone)
    for result in (search, read):
        assert result.ok
        assert "STALE" in result.content
        assert "39h" in result.content  # 2h at the stamp + 37h advanced
        assert "26h" in result.content  # the configured bound, named
    assert "invented resolver" in read.content  # content still served
    assert "monitoring" in search.content.lower() or "rp5" in search.content


async def test_exactly_at_the_bound_is_not_yet_stale(clone: Path):
    tool = make_tool(clone, Clock(FRESH_NOW + hours(24)))  # 26h since the pull
    result = await tool.run(action="search", query="resolver")
    assert "STALE" not in result.content


async def test_one_second_past_the_bound_is_stale(clone: Path):
    tool = make_tool(clone, Clock(FRESH_NOW + hours(24) + 1))
    result = await tool.run(action="search", query="resolver")
    assert "STALE" in result.content


async def test_a_missing_stamp_yields_freshness_unknown_and_still_serves(clone: Path):
    stamp_path(clone).unlink()
    tool = make_tool(clone)
    search, read = await both_results(tool, clone)
    for result in (search, read):
        assert result.ok
        assert "UNKNOWN" in result.content
        assert STAMP_FILENAME in result.content
        assert "missing" in result.content.lower()
        assert "STALE" not in result.content
    assert "invented resolver" in read.content


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "[]",
        '{"pulled_at": "yesterday"}',
        '{"pulled_at": ""}',
        '{"commit": "abc"}',
        '{"pulled_at": 1756699211}',
    ],
)
async def test_an_unparseable_stamp_yields_freshness_unknown(clone: Path, payload: str):
    stamp_path(clone).write_text(payload, encoding="utf-8")
    tool = make_tool(clone)
    for result in await both_results(tool, clone):
        assert result.ok
        assert "UNKNOWN" in result.content
        assert "current" in result.content.lower()  # "must not be treated as current"


async def test_no_result_presents_unstamped_content_as_current(clone: Path):
    stamp_path(clone).unlink()
    result = await make_tool(clone).run(action="search", query="resolver")
    assert "must not be treated as current" in result.content


async def test_an_old_commit_with_a_recent_pull_is_not_stale(clone: Path):
    write_stamp(
        clone,
        commit="0ldc0mm1t000000000000000000000000000abc",
        committed_at="2026-03-04T11:22:33Z",
        pulled_at=STAMP_PULLED_AT,
    )
    result = await make_tool(clone).run(action="search", query="resolver")
    assert result.ok
    assert "STALE" not in result.content
    assert "2026-03-04T11:22:33Z" in result.content  # the commit age is reported
    assert "181d" in result.content or "180d" in result.content


async def test_an_unparseable_commit_date_does_not_make_the_pull_unknown(clone: Path):
    write_stamp(clone, committed_at="whenever", pulled_at=STAMP_PULLED_AT)
    result = await make_tool(clone).run(action="search", query="resolver")
    assert result.ok
    assert "STALE" not in result.content
    assert "UNKNOWN" not in result.content


@pytest.mark.parametrize(
    "pulled_at",
    ["2026-09-01T04:00:11Z", "2026-09-01T06:00:11+02:00", "2026-09-01T04:00:11.500Z"],
)
async def test_the_stamp_accepts_the_offset_forms_the_writer_can_emit(
    clone: Path, pulled_at: str
):
    write_stamp(clone, pulled_at=pulled_at)
    result = await make_tool(clone).run(action="search", query="resolver")
    assert result.ok
    assert "UNKNOWN" not in result.content
    assert "STALE" not in result.content


async def test_a_naive_pull_time_is_read_as_utc_not_as_local_time(clone: Path):
    """The writer emits UTC; a `Z`-less value must not drift with the host zone."""
    write_stamp(clone, pulled_at="2026-09-01T04:00:11")
    result = await make_tool(clone).run(action="search", query="resolver")
    assert result.ok
    assert "UNKNOWN" not in result.content
    assert "2h" in result.content


# --- 5.10 the silent pull failure ---------------------------------------


async def test_a_silently_stopped_pull_becomes_visible_as_staleness(clone: Path):
    """Nothing about the corpus changes; only the clock moves."""
    clock = Clock()
    tool = make_tool(clone, clock)
    fresh = await tool.run(action="search", query="resolver")
    assert "STALE" not in fresh.content

    corpus_before = sorted(
        (p.relative_to(clone).as_posix(), p.stat().st_mtime_ns)
        for p in clone.rglob("*")
        if p.is_file() and not p.is_symlink()
    )
    clock.now += hours(48)
    stale = await tool.run(action="search", query="resolver")
    corpus_after = sorted(
        (p.relative_to(clone).as_posix(), p.stat().st_mtime_ns)
        for p in clone.rglob("*")
        if p.is_file() and not p.is_symlink()
    )

    assert corpus_before == corpus_after  # the dead updater changed nothing
    assert "STALE" in stale.content
    assert "50h" in stale.content
    assert stale.ok  # still answers, loudly


# --- 5.11 index invalidation --------------------------------------------


async def test_a_changed_pull_stamp_rebuilds_the_index(clone: Path):
    tool = make_tool(clone)
    first = await tool.run(action="search", query="SYNTHETIC-NEW-PAGE-SENTINEL")
    assert "no section matched" in first.content.lower()

    (docs_root(clone) / "services" / "new-service.md").write_text(
        "## Brand new\n\nSYNTHETIC-NEW-PAGE-SENTINEL lives here now.\n", encoding="utf-8"
    )
    write_stamp(clone, pulled_at="2026-09-01T05:00:11Z")

    second = await tool.run(action="search", query="SYNTHETIC-NEW-PAGE-SENTINEL")
    assert "new-service.md" in second.content
    assert "Brand new" in second.content


async def test_an_unchanged_stamp_reuses_the_index(clone: Path):
    tool = make_tool(clone)
    await tool.run(action="search", query="resolver")
    builds = tool.index_build_count
    for _ in range(5):
        await tool.run(action="search", query="resolver")
        await tool.run(action="read", section_id=some_section_id(clone))
    assert tool.index_build_count == builds


async def test_an_unchanged_stamp_hides_a_corpus_edit_until_the_stamp_moves(
    clone: Path,
):
    """The host writes the stamp last, so an unstamped edit is deliberately unseen."""
    tool = make_tool(clone)
    await tool.run(action="search", query="resolver")

    (docs_root(clone) / "services" / "sneaky.md").write_text(
        "## Sneaky\n\nSYNTHETIC-UNSTAMPED-SENTINEL\n", encoding="utf-8"
    )
    unseen = await tool.run(action="search", query="SYNTHETIC-UNSTAMPED-SENTINEL")
    assert "no section matched" in unseen.content.lower()

    write_stamp(clone, pulled_at="2026-09-01T05:30:00Z")
    seen = await tool.run(action="search", query="SYNTHETIC-UNSTAMPED-SENTINEL")
    assert "sneaky.md" in seen.content


async def test_an_unstamped_corpus_is_re_read_every_call(clone: Path):
    """With no stamp there is no change signal, so the index cannot be trusted."""
    stamp_path(clone).unlink()
    tool = make_tool(clone)
    await tool.run(action="search", query="resolver")
    builds = tool.index_build_count
    await tool.run(action="search", query="resolver")
    assert tool.index_build_count > builds


# --- 5.12 availability ---------------------------------------------------


def test_enabling_the_corpus_without_a_path_fails_startup():
    raw = _minimal_raw("+31600000000")
    raw["homelab_docs"] = {"enabled": True}
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(raw, env={})
    assert "homelab_docs.path" in str(exc.value)
    assert "homelab_docs.enabled" in str(exc.value)


def test_host_state_is_not_a_startup_failure(tmp_path: Path):
    """A configured path that does not exist loads fine — D11 layer 2."""
    raw = _minimal_raw("+31600000000")
    raw["homelab_docs"] = {"enabled": True, "path": str(tmp_path / "absent")}
    config = Config.from_dict(raw, env={})
    assert config.homelab_docs.enabled
    assert config.homelab_docs.path.endswith("absent")


@pytest.mark.parametrize("action", ["search", "read"])
async def test_a_missing_corpus_directory_errors_per_call_naming_path_and_condition(
    tmp_path: Path, action: str
):
    absent = tmp_path / "not-mounted"
    tool = make_tool(absent)
    arguments = {"action": action}
    arguments["query" if action == "search" else "section_id"] = "anything"
    result = await tool.run(**arguments)
    assert not result.ok
    assert str(absent) in (result.error or "")
    assert "missing" in (result.error or "").lower()
    assert result.content == ""


async def test_an_unconfigured_path_errors_per_call(tmp_path: Path):
    result = await make_tool(tmp_path, path="").run(action="search", query="x")
    assert not result.ok
    assert "homelab_docs.path" in (result.error or "")


async def test_a_corpus_without_the_docs_subpath_names_that_condition(tmp_path: Path):
    bare = tmp_path / "bare-clone"
    bare.mkdir()
    (bare / "README.md").write_text("# nothing here\n", encoding="utf-8")
    result = await make_tool(bare).run(action="search", query="x")
    assert not result.ok
    assert str(bare) in (result.error or "")
    assert "src/content/docs" in (result.error or "")


async def test_an_unreadable_corpus_directory_errors_per_call(clone: Path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    docs = docs_root(clone)
    original = docs.stat().st_mode
    os.chmod(docs, 0o000)
    try:
        result = await make_tool(clone).run(action="search", query="resolver")
    finally:
        os.chmod(docs, stat.S_IMODE(original))
    assert not result.ok
    assert str(clone) in (result.error or "")
    assert "readable" in (result.error or "").lower()


async def test_an_empty_corpus_is_never_mistaken_for_no_match(tmp_path: Path):
    empty = tmp_path / "empty-clone"
    (empty / "src" / "content" / "docs").mkdir(parents=True)
    write_stamp(empty)
    result = await make_tool(empty).run(action="search", query="resolver")
    assert not result.ok
    error = (result.error or "").lower()
    assert "empty" in error or "no indexable documentation" in error
    assert "no section matched" not in error
    assert str(empty) in (result.error or "")


async def test_runtime_loss_behaves_exactly_like_startup_absence(
    clone: Path, tmp_path: Path
):
    import shutil

    tool = make_tool(clone)
    warm = await tool.run(action="search", query="resolver")
    assert warm.ok
    ident = some_section_id(clone)

    shutil.rmtree(clone)

    startup_absent = make_tool(clone)
    for after_loss, never_there in (
        (await tool.run(action="search", query="resolver"),
         await startup_absent.run(action="search", query="resolver")),
        (await tool.run(action="read", section_id=ident),
         await startup_absent.run(action="read", section_id=ident)),
    ):
        assert not after_loss.ok
        assert after_loss.error == never_there.error


async def test_a_file_that_becomes_unreadable_mid_operation_errors(clone: Path):
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    tool = make_tool(clone)
    ident = some_section_id(clone)
    assert (await tool.run(action="read", section_id=ident)).ok

    page = docs_root(clone) / "devices" / "rp5.md"
    original = page.stat().st_mode
    os.chmod(page, 0o000)
    try:
        result = await tool.run(action="read", section_id=ident)
    finally:
        os.chmod(page, stat.S_IMODE(original))

    assert not result.ok
    assert "devices/rp5.md" in (result.error or "")
    assert result.content == ""
    assert "invented resolver" not in (result.error or "")


async def test_a_deleted_file_errors_rather_than_substituting_content(clone: Path):
    tool = make_tool(clone)
    ident = some_section_id(clone)
    assert (await tool.run(action="read", section_id=ident)).ok
    (docs_root(clone) / "devices" / "rp5.md").unlink()
    result = await tool.run(action="read", section_id=ident)
    assert not result.ok
    assert "devices/rp5.md" in (result.error or "")
    assert result.content == ""


async def test_a_section_that_vanished_from_a_still_readable_file_errors(clone: Path):
    tool = make_tool(clone)
    ident = some_section_id(clone)
    page = docs_root(clone) / "devices" / "rp5.md"
    page.write_text("## Only heading\n\nEverything else went away.\n", encoding="utf-8")
    result = await tool.run(action="read", section_id=ident)
    assert not result.ok
    assert "Everything else went away" not in (result.error or "")
    assert result.content == ""


# --- 5.13 the two gates are distinguishable ------------------------------


async def test_corpus_unavailable_and_allowlist_empty_read_differently(
    clone: Path, tmp_path: Path
):
    unavailable = await make_tool(tmp_path / "absent").run(
        action="search", query="resolver"
    )
    denied = await make_tool(clone, allowlist=()).run(action="search", query="resolver")

    assert not unavailable.ok
    assert denied.ok  # present and readable, just nothing in scope

    unavailable_text = (unavailable.error or "") + unavailable.content
    denied_text = (denied.error or "") + denied.content

    assert "allowlist" not in unavailable_text.lower()
    assert "allowlist" in denied_text.lower()
    assert "missing" in unavailable_text.lower()
    assert "missing" not in denied_text.lower()
    assert unavailable_text != denied_text


async def test_an_empty_allowlist_is_not_reported_as_an_empty_corpus(clone: Path):
    denied = await make_tool(clone, allowlist=()).run(action="search", query="resolver")
    text = denied.content + (denied.error or "")
    assert "no documentation" in text.lower()
    assert "empty corpus" not in text.lower()
    assert "no indexable documentation" not in text.lower()


async def test_an_allowlist_that_matches_nothing_names_the_allowlist_not_the_corpus(
    clone: Path,
):
    result = await make_tool(clone, allowlist=("nowhere/",)).run(
        action="search", query="resolver"
    )
    text = result.content + (result.error or "")
    assert "allowlist" in text.lower()
    assert "no indexable documentation" not in text.lower()


async def test_a_missing_corpus_wins_over_an_empty_allowlist(tmp_path: Path):
    """Both gates shut: report the one the owner has to fix first."""
    result = await make_tool(tmp_path / "absent", allowlist=()).run(
        action="search", query="resolver"
    )
    assert not result.ok
    assert "missing" in (result.error or "").lower()


async def test_the_allowlisted_scope_is_reported_not_the_corpus_wide_total(clone: Path):
    """Like `todo_read`, the count that reaches the owner is the allowlisted one."""
    narrow = await make_tool(clone, allowlist=("index.mdx",)).run(
        action="search", query="zzzznotpresentzzzz"
    )
    wide = await make_tool(clone, allowlist=DEFAULT_ALLOWLIST).run(
        action="search", query="zzzznotpresentzzzz"
    )
    assert narrow.content != wide.content
    assert "1 file" in narrow.content
