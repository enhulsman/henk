"""The single SQLite file behind memory and the capture inbox (design D2).

One database on the volume that already carries the audit JSONL, so this change
adds no deploy surface (secure-deployment spec). WAL mode keeps readers unblocked
by writers and bounds what a live backup of the file can catch mid-write.

Opening is **lazy**: ``build_runtime`` must assemble a full app without touching
the filesystem (a fresh install or a test environment has no ``/data``), so the
connection is created on first use and a failure surfaces as :class:`StoreError`
for the caller to report honestly.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable

from henk.store.errors import StoreError

logger = logging.getLogger("henk.store")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        memory_type TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memories_type_created "
    "ON memories(memory_type, created_at, id)",
    """
    CREATE TABLE IF NOT EXISTS inbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        created_at REAL NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_inbox_status_created "
    "ON inbox(status, created_at, id)",
)


class Store:
    """Owns the SQLite connection and the schema. Repositories borrow it."""

    def __init__(
        self, path: str | Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def clock(self) -> Callable[[], float]:
        return self._clock

    def connection(self) -> sqlite3.Connection:
        """Return the open connection, creating file + schema on first call."""
        if self._conn is not None:
            return self._conn
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            raise StoreError(f"cannot open the store at {self._path}: {exc}") from exc
        self._conn = conn
        return conn

    def journal_mode(self) -> str:
        row = self.connection().execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - best effort
                logger.warning("error closing the store", exc_info=True)
            self._conn = None
