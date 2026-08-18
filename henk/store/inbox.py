"""The capture inbox: append, oldest-first drain, mark done. No eviction, ever.

:class:`InboxStore` is the seam from design D1 — the tools and commands know only
these three operations, so the planned personal-inbox service can replace the
SQLite backend without touching agent logic. Silently evicting a captured thought
is worse than unbounded growth (short text rows), so there is no cap and no code
path that edits or deletes item text.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from henk.store.db import Store
from henk.store.errors import EmptyContentError, StoreError

OPEN = "open"
DONE = "done"

#: Default page size for the oldest-first drain (`inbox_read`, `/inbox`).
DEFAULT_PAGE_SIZE = 20


@dataclass(frozen=True)
class InboxItem:
    id: int
    text: str
    created_at: float
    source: str = ""
    status: str = OPEN


@dataclass(frozen=True)
class InboxPage:
    """One page of open items plus the count of newer ones not shown."""

    items: tuple[InboxItem, ...] = ()
    newer_remainder: int = 0


@runtime_checkable
class InboxStore(Protocol):
    """The storage seam. A future service backend implements exactly this."""

    def append(self, text: str, *, source: str = "") -> InboxItem: ...

    def list_open(self, *, limit: int | None = DEFAULT_PAGE_SIZE) -> InboxPage: ...

    def mark_done(self, item_id: int) -> InboxItem | None: ...


def format_created_at(created_at: float) -> str:
    """Render an item's capture time for a human (and for the model).

    Lives beside the item type because both read-back surfaces — the `inbox_read`
    tool and the `/inbox` command — must render it the same way; a raw epoch float
    tells the owner nothing and invites the model to invent a date.
    """
    try:
        stamp = datetime.fromtimestamp(float(created_at), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - absurd clocks
        return "unknown time"
    return stamp.strftime("%Y-%m-%d %H:%M UTC")


def _row_to_item(row: sqlite3.Row) -> InboxItem:
    return InboxItem(
        id=int(row["id"]),
        text=str(row["text"]),
        created_at=float(row["created_at"]),
        source=str(row["source"]),
        status=str(row["status"]),
    )


class SqliteInboxStore:
    """The v1 backend: the ``inbox`` table in Henk's own SQLite store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def append(self, text: str, *, source: str = "") -> InboxItem:
        content = (text or "").strip()
        if not content:
            raise EmptyContentError("the capture text is empty; nothing was stored")
        conn = self._store.connection()
        now = self._store.clock()
        try:
            cursor = conn.execute(
                "INSERT INTO inbox (text, created_at, source, status) "
                "VALUES (?, ?, ?, ?)",
                (content, now, source, OPEN),
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise StoreError(f"could not store the capture: {exc}") from exc
        return InboxItem(
            id=int(cursor.lastrowid or 0),
            text=content,
            created_at=now,
            source=source,
            status=OPEN,
        )

    def list_open(self, *, limit: int | None = DEFAULT_PAGE_SIZE) -> InboxPage:
        """The ``limit`` OLDEST open items, plus how many newer ones exist.

        Oldest-first is the point: an inbox is a queue to drain, so the head is
        always visible and the page bound never becomes de-facto eviction.
        """
        conn = self._store.connection()
        try:
            rows = conn.execute(
                "SELECT id, text, created_at, source, status FROM inbox "
                "WHERE status = ? ORDER BY created_at ASC, id ASC",
                (OPEN,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"could not read the inbox: {exc}") from exc
        items = [_row_to_item(row) for row in rows]
        if limit is None:
            return InboxPage(items=tuple(items), newer_remainder=0)
        return InboxPage(
            items=tuple(items[:limit]),
            newer_remainder=max(0, len(items) - limit),
        )

    def mark_done(self, item_id: int) -> InboxItem | None:
        """Archive one open item. Returns None when no OPEN item has that id."""
        conn = self._store.connection()
        try:
            cursor = conn.execute(
                "UPDATE inbox SET status = ? WHERE id = ? AND status = ?",
                (DONE, item_id, OPEN),
            )
            conn.commit()
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT id, text, created_at, source, status FROM inbox WHERE id = ?",
                (item_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            conn.rollback()
            raise StoreError(f"could not update the inbox: {exc}") from exc
        return _row_to_item(row) if row is not None else None

    def get(self, item_id: int) -> InboxItem | None:
        """Any item by id, done ones included (proves done ≠ deleted)."""
        conn = self._store.connection()
        try:
            row = conn.execute(
                "SELECT id, text, created_at, source, status FROM inbox WHERE id = ?",
                (item_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"could not read the inbox: {exc}") from exc
        return _row_to_item(row) if row is not None else None
