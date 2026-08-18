"""The memory repository: short owner facts, type-namespaced and capped.

Two type namespaces with independent FIFO caps (design D3): ``pinned`` for
owner-authored facts (`/remember`) and ``agent`` for agent-authored ones
(`store_memory`). Over-limit content is rejected naming the limit — never
truncated — and eviction returns what it removed so the write's confirmation can
name it. Nothing here decides *who* may write; that is the gate's job.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Mapping

from henk.store.db import Store
from henk.store.errors import ContentTooLongError, EmptyContentError, StoreError

#: The two write paths, as type namespaces. Adding a third is a spec change.
PINNED = "pinned"
AGENT = "agent"
MEMORY_TYPES = (PINNED, AGENT)

#: Caps adopted from the proven in-house design (design D3).
DEFAULT_CAPS: Mapping[str, int] = {PINNED: 50, AGENT: 20}

#: Per-fact length limit. Bounds row size and the recall render; honest rejection.
DEFAULT_LENGTH_LIMIT = 500


@dataclass(frozen=True)
class Memory:
    id: int
    content: str
    memory_type: str
    created_at: float


@dataclass(frozen=True)
class MemoryWrite:
    """The result of one write: what was stored, and what the cap pushed out."""

    memory: Memory
    evicted: tuple[Memory, ...] = ()


def _row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=int(row["id"]),
        content=str(row["content"]),
        memory_type=str(row["memory_type"]),
        created_at=float(row["created_at"]),
    )


class MemoryStore:
    """Repository over the ``memories`` table."""

    def __init__(
        self,
        store: Store,
        *,
        caps: Mapping[str, int] | None = None,
        length_limit: int = DEFAULT_LENGTH_LIMIT,
    ) -> None:
        self._store = store
        self._caps = dict(DEFAULT_CAPS if caps is None else caps)
        self._length_limit = length_limit

    @property
    def length_limit(self) -> int:
        return self._length_limit

    def cap(self, memory_type: str) -> int:
        return self._caps.get(memory_type, 0)

    # --- writes -----------------------------------------------------------

    def add(self, content: str, memory_type: str = PINNED) -> MemoryWrite:
        """Store one fact, evicting the type's oldest rows if the cap demands it.

        Raises :class:`EmptyContentError` / :class:`ContentTooLongError` for
        content the store refuses, :class:`StoreError` for backend failures, and
        ``ValueError`` for an unknown type namespace.
        """
        if memory_type not in MEMORY_TYPES:
            raise ValueError(
                f"unknown memory type {memory_type!r}; expected one of "
                f"{', '.join(MEMORY_TYPES)}"
            )
        text = (content or "").strip()
        if not text:
            raise EmptyContentError("the memory text is empty; nothing was stored")
        if len(text) > self._length_limit:
            raise ContentTooLongError(self._length_limit, len(text))

        conn = self._store.connection()
        now = self._store.clock()
        try:
            cursor = conn.execute(
                "INSERT INTO memories (content, memory_type, created_at) "
                "VALUES (?, ?, ?)",
                (text, memory_type, now),
            )
            new_id = int(cursor.lastrowid or 0)
            evicted = self._trim_locked(conn, memory_type, protect_id=new_id)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise StoreError(f"could not store the memory: {exc}") from exc
        return MemoryWrite(
            memory=Memory(new_id, text, memory_type, now), evicted=tuple(evicted)
        )

    def delete_containing(self, needle: str) -> list[Memory]:
        """Delete every memory whose trimmed content contains ``needle``.

        Case-insensitive substring match; returns the removed rows so the reply
        can echo them (a mistaken bulk forget stays recoverable). An empty needle
        matches nothing — `/forget` with no text must never wipe the store.
        """
        token = (needle or "").strip().lower()
        if not token:
            return []
        conn = self._store.connection()
        try:
            rows = conn.execute(
                "SELECT id, content, memory_type, created_at FROM memories"
            ).fetchall()
            matches = [
                _row_to_memory(row)
                for row in rows
                if token in str(row["content"]).strip().lower()
            ]
            if matches:
                conn.executemany(
                    "DELETE FROM memories WHERE id = ?",
                    [(m.id,) for m in matches],
                )
                conn.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"could not delete memories: {exc}") from exc
        return matches

    def _trim_locked(
        self, conn: sqlite3.Connection, memory_type: str, *, protect_id: int
    ) -> list[Memory]:
        """Evict this type's oldest rows until its cap holds. Caller commits.

        ``protect_id`` keeps a just-inserted row safe from its own eviction when
        the cap is 1 or lower, so a write never silently succeeds as a no-op.
        """
        cap = self._caps.get(memory_type)
        if cap is None:
            return []
        rows = conn.execute(
            "SELECT id, content, memory_type, created_at FROM memories "
            "WHERE memory_type = ? ORDER BY created_at ASC, id ASC",
            (memory_type,),
        ).fetchall()
        overflow = len(rows) - max(cap, 1)
        if overflow <= 0:
            return []
        evicted: list[Memory] = []
        for row in rows:
            if overflow <= 0:
                break
            if int(row["id"]) == protect_id:
                continue
            evicted.append(_row_to_memory(row))
            overflow -= 1
        conn.executemany(
            "DELETE FROM memories WHERE id = ?", [(m.id,) for m in evicted]
        )
        return evicted

    # --- reads ------------------------------------------------------------

    def list_all(self) -> list[Memory]:
        """Every memory, newest first."""
        return self._select(
            "SELECT id, content, memory_type, created_at FROM memories "
            "ORDER BY created_at DESC, id DESC",
            (),
        )

    def list_by_type(self, memory_type: str) -> list[Memory]:
        """One type's memories, newest first."""
        return self._select(
            "SELECT id, content, memory_type, created_at FROM memories "
            "WHERE memory_type = ? ORDER BY created_at DESC, id DESC",
            (memory_type,),
        )

    def count(self, memory_type: str) -> int:
        conn = self._store.connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE memory_type = ?",
                (memory_type,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"could not count memories: {exc}") from exc
        return int(row[0])

    def _select(self, sql: str, params: tuple) -> list[Memory]:
        conn = self._store.connection()
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"could not read memories: {exc}") from exc
        return [_row_to_memory(row) for row in rows]
