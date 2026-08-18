"""Store-foundation tests (task 1.1), from specs/memory-store + specs/capture-inbox.

The store is the durability substrate for both new capabilities: one SQLite file
on the existing audit volume, WAL mode, two tables. These tests drive the
repositories directly — the tool/command layers ride on top and are covered in
their own modules.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from henk.store import (
    ContentTooLongError,
    EmptyContentError,
    MemoryStore,
    SqliteInboxStore,
    Store,
    StoreError,
)


def _store(tmp_path: Path, **kwargs) -> Store:
    return Store(tmp_path / "store" / "henk.db", **kwargs)


def _memories(tmp_path: Path, **kwargs) -> MemoryStore:
    return MemoryStore(_store(tmp_path), **kwargs)


# --- Schema, file layout, WAL ---------------------------------------------


def test_store_creates_its_file_and_parent_directory(tmp_path: Path):
    store = _store(tmp_path)
    store.connection()
    assert (tmp_path / "store" / "henk.db").exists()
    store.close()


def test_store_uses_wal_mode(tmp_path: Path):
    # WAL keeps readers unblocked by writers and bounds torn-read exposure for the
    # live backup that already covers this volume (design D2).
    store = _store(tmp_path)
    assert store.journal_mode() == "wal"
    store.close()


def test_unopenable_store_raises_store_error(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    store = Store(blocker / "henk.db")
    with pytest.raises(StoreError):
        store.connection()


# --- Memories: types, length limit, caps, FIFO ----------------------------


def test_memory_added_and_listed_with_its_type(tmp_path: Path):
    memories = _memories(tmp_path)
    write = memories.add("the workstation dual-boots via GRUB", "pinned")
    assert write.memory.content == "the workstation dual-boots via GRUB"
    assert write.memory.memory_type == "pinned"
    assert write.evicted == ()
    assert [m.content for m in memories.list_all()] == [
        "the workstation dual-boots via GRUB"
    ]


def test_memory_types_are_separate_namespaces(tmp_path: Path):
    memories = _memories(tmp_path)
    memories.add("owner fact", "pinned")
    memories.add("agent fact", "agent")
    assert memories.count("pinned") == 1
    assert memories.count("agent") == 1
    assert {m.memory_type for m in memories.list_all()} == {"pinned", "agent"}


def test_unknown_memory_type_rejected(tmp_path: Path):
    memories = _memories(tmp_path)
    with pytest.raises(ValueError, match="memory type"):
        memories.add("x", "sneaky")


def test_over_limit_fact_rejected_naming_the_limit_and_stores_nothing(tmp_path: Path):
    memories = _memories(tmp_path, length_limit=20)
    with pytest.raises(ContentTooLongError) as exc:
        memories.add("x" * 21, "pinned")
    assert exc.value.limit == 20
    assert "20" in str(exc.value)
    assert memories.list_all() == []  # never a truncated variant


def test_at_limit_fact_accepted(tmp_path: Path):
    memories = _memories(tmp_path, length_limit=20)
    memories.add("x" * 20, "pinned")
    assert memories.count("pinned") == 1


def test_empty_and_whitespace_content_rejected(tmp_path: Path):
    memories = _memories(tmp_path)
    for text in ("", "   ", "\n\t "):
        with pytest.raises(EmptyContentError):
            memories.add(text, "pinned")
    assert memories.list_all() == []


def test_content_is_stored_trimmed(tmp_path: Path):
    memories = _memories(tmp_path)
    write = memories.add("  padded fact  ", "pinned")
    assert write.memory.content == "padded fact"


def test_fifo_eviction_at_the_cap_returns_the_evicted_content(tmp_path: Path):
    memories = _memories(tmp_path, caps={"pinned": 3, "agent": 2})
    for i in range(3):
        memories.add(f"fact {i}", "pinned")
    write = memories.add("fact 3", "pinned")
    assert [m.content for m in write.evicted] == ["fact 0"]  # oldest first out
    assert memories.count("pinned") == 3  # cap holds exactly
    assert [m.content for m in memories.list_by_type("pinned")] == [
        "fact 3",
        "fact 2",
        "fact 1",
    ]


def test_types_are_capped_independently(tmp_path: Path):
    memories = _memories(tmp_path, caps={"pinned": 3, "agent": 1})
    memories.add("agent fact", "agent")
    for i in range(3):
        memories.add(f"pinned {i}", "pinned")
    # The agent type sat at its cap while pinned filled and then evicted its own.
    write = memories.add("pinned 3", "pinned")
    assert [m.content for m in write.evicted] == ["pinned 0"]
    assert memories.count("agent") == 1
    assert memories.list_by_type("agent")[0].content == "agent fact"


def test_eviction_in_one_type_never_touches_another(tmp_path: Path):
    memories = _memories(tmp_path, caps={"pinned": 1, "agent": 1})
    memories.add("pinned keeper", "pinned")
    write = memories.add("agent fact", "agent")
    assert write.evicted == ()  # a different type at its cap is irrelevant
    assert memories.count("pinned") == 1


def test_list_all_is_newest_first(tmp_path: Path):
    memories = _memories(tmp_path)
    memories.add("first", "pinned")
    memories.add("second", "pinned")
    assert [m.content for m in memories.list_all()] == ["second", "first"]


# --- Memories: deletion by substring with echo ----------------------------


def test_delete_containing_is_case_insensitive_and_echoes_removals(tmp_path: Path):
    memories = _memories(tmp_path)
    memories.add("the BACKUP job runs at 03:00", "pinned")
    memories.add("backup target is the vps", "agent")
    memories.add("unrelated fact", "pinned")
    removed = memories.delete_containing("backup")
    assert {m.content for m in removed} == {
        "the BACKUP job runs at 03:00",
        "backup target is the vps",
    }
    assert [m.content for m in memories.list_all()] == ["unrelated fact"]


def test_delete_containing_no_match_changes_nothing(tmp_path: Path):
    memories = _memories(tmp_path)
    memories.add("a fact", "pinned")
    assert memories.delete_containing("quantum") == []
    assert memories.count("pinned") == 1


# --- Restart survival -----------------------------------------------------


def test_memories_survive_a_store_reopen(tmp_path: Path):
    first = _memories(tmp_path)
    first.add("survives restarts", "pinned")
    first._store.close()

    second = _memories(tmp_path)
    assert [m.content for m in second.list_all()] == ["survives restarts"]


# --- Inbox: append, oldest-first drain, done ------------------------------


def test_inbox_append_returns_an_open_item_with_an_id(tmp_path: Path):
    inbox = SqliteInboxStore(_store(tmp_path))
    item = inbox.append("buy bike lights", source="owner-command")
    assert item.id > 0
    assert item.text == "buy bike lights"
    assert item.status == "open"
    assert item.source == "owner-command"


def test_inbox_rejects_empty_text(tmp_path: Path):
    inbox = SqliteInboxStore(_store(tmp_path))
    with pytest.raises(EmptyContentError):
        inbox.append("   ")
    assert inbox.list_open().items == ()


def test_inbox_lists_oldest_first_with_a_newer_remainder_count(tmp_path: Path):
    inbox = SqliteInboxStore(_store(tmp_path))
    for i in range(25):
        inbox.append(f"item {i}")
    page = inbox.list_open(limit=20)
    assert [it.text for it in page.items] == [f"item {i}" for i in range(20)]
    assert page.newer_remainder == 5


def test_inbox_list_all_returns_every_open_item(tmp_path: Path):
    inbox = SqliteInboxStore(_store(tmp_path))
    for i in range(25):
        inbox.append(f"item {i}")
    page = inbox.list_open(limit=None)
    assert len(page.items) == 25
    assert page.newer_remainder == 0


def test_inbox_mark_done_hides_the_item_without_deleting_it(tmp_path: Path):
    inbox = SqliteInboxStore(_store(tmp_path))
    item = inbox.append("drain me")
    done = inbox.mark_done(item.id)
    assert done is not None and done.status == "done"
    assert inbox.list_open().items == ()
    assert inbox.get(item.id) is not None  # archived, not deleted


def test_inbox_mark_done_unknown_id_returns_none(tmp_path: Path):
    inbox = SqliteInboxStore(_store(tmp_path))
    assert inbox.mark_done(9999) is None


def test_inbox_has_no_cap_and_never_evicts(tmp_path: Path):
    inbox = SqliteInboxStore(_store(tmp_path))
    for i in range(120):
        inbox.append(f"item {i}")
    page = inbox.list_open(limit=None)
    assert len(page.items) == 120
    assert page.items[0].text == "item 0"  # the oldest is still reachable


def test_inbox_items_survive_a_store_reopen(tmp_path: Path):
    store = _store(tmp_path)
    SqliteInboxStore(store).append("persisted thought")
    store.close()

    reopened = SqliteInboxStore(_store(tmp_path))
    assert [it.text for it in reopened.list_open().items] == ["persisted thought"]


def test_memories_and_inbox_share_one_database_file(tmp_path: Path):
    store = _store(tmp_path)
    MemoryStore(store).add("a fact", "pinned")
    SqliteInboxStore(store).append("a thought")
    assert list((tmp_path / "store").iterdir())  # single db (plus WAL sidecars)
    names = {p.name.split("-")[0] for p in (tmp_path / "store").iterdir()}
    assert names == {"henk.db"}
    store.close()
