"""Backend-seam invariance for the inbox (task 1.3), from specs/capture-inbox.

The `InboxStore` seam exists so the planned personal-inbox service can replace
the SQLite backend without touching agent logic (design D1). These contract
tests run unchanged against both the SQLite backend and an in-memory test double,
so the seam is proven by behaviour rather than asserted in a docstring. The
tool-level half of the same scenario lives in ``test_capture_inbox.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from henk.store import EmptyContentError, InboxItem, InboxPage, SqliteInboxStore, Store


class FakeInboxStore:
    """In-memory ``InboxStore`` — the stand-in for a future service backend."""

    def __init__(self, *, clock=None, fail: bool = False) -> None:
        self._items: list[InboxItem] = []
        self._next_id = 1
        self._clock = clock or (lambda: 0.0)
        self.fail = fail

    def append(self, text: str, *, source: str = "") -> InboxItem:
        if self.fail:
            from henk.store import StoreError

            raise StoreError("simulated inbox write failure")
        content = (text or "").strip()
        if not content:
            raise EmptyContentError("inbox text is empty")
        item = InboxItem(
            id=self._next_id,
            text=content,
            created_at=self._clock(),
            source=source,
            status="open",
        )
        self._next_id += 1
        self._items.append(item)
        return item

    def list_open(self, *, limit: int | None = 20) -> InboxPage:
        if self.fail:
            from henk.store import StoreError

            raise StoreError("simulated inbox read failure")
        open_items = [it for it in self._items if it.status == "open"]
        if limit is None:
            return InboxPage(items=tuple(open_items), newer_remainder=0)
        return InboxPage(
            items=tuple(open_items[:limit]),
            newer_remainder=max(0, len(open_items) - limit),
        )

    def mark_done(self, item_id: int) -> InboxItem | None:
        if self.fail:
            from henk.store import StoreError

            raise StoreError("simulated inbox write failure")
        for i, item in enumerate(self._items):
            if item.id == item_id and item.status == "open":
                done = replace(item, status="done")
                self._items[i] = done
                return done
        return None

    def get(self, item_id: int) -> InboxItem | None:
        for item in self._items:
            if item.id == item_id:
                return item
        return None


@dataclass
class _Backend:
    name: str
    store: object


@pytest.fixture(params=["sqlite", "double"])
def inbox(request, tmp_path: Path):
    if request.param == "sqlite":
        store = Store(tmp_path / "store" / "henk.db")
        yield SqliteInboxStore(store)
        store.close()
    else:
        yield FakeInboxStore()


# --- The contract both backends satisfy identically -----------------------


def test_append_then_read_back(inbox):
    item = inbox.append("buy bike lights", source="capture")
    page = inbox.list_open()
    assert [(it.id, it.text) for it in page.items] == [(item.id, "buy bike lights")]


def test_empty_text_rejected(inbox):
    with pytest.raises(EmptyContentError):
        inbox.append("  ")
    assert inbox.list_open().items == ()


def test_oldest_first_with_remainder(inbox):
    for i in range(25):
        inbox.append(f"item {i}")
    page = inbox.list_open(limit=20)
    assert [it.text for it in page.items] == [f"item {i}" for i in range(20)]
    assert page.newer_remainder == 5


def test_unlimited_listing(inbox):
    for i in range(25):
        inbox.append(f"item {i}")
    assert len(inbox.list_open(limit=None).items) == 25


def test_mark_done_excludes_from_open_listing(inbox):
    first = inbox.append("one")
    inbox.append("two")
    assert inbox.mark_done(first.id) is not None
    assert [it.text for it in inbox.list_open().items] == ["two"]


def test_mark_done_unknown_id_is_none(inbox):
    assert inbox.mark_done(4242) is None


def test_marking_done_twice_reports_the_second_as_unknown(inbox):
    item = inbox.append("one")
    assert inbox.mark_done(item.id) is not None
    assert inbox.mark_done(item.id) is None  # no longer an open item


def test_ids_are_unique_across_appends(inbox):
    ids = {inbox.append(f"item {i}").id for i in range(5)}
    assert len(ids) == 5
