"""The single SQLite file behind memory, the capture inbox and reminders.

One database on the volume that already carries the audit JSONL, so this change
adds no deploy surface (secure-deployment spec). WAL mode keeps readers unblocked
by writers and bounds what a live backup of the file can catch mid-write.

Opening is **lazy**: ``build_runtime`` must assemble a full app without touching
the filesystem (a fresh install or a test environment has no ``/data``), so the
connection is created on first use and a failure surfaces as :class:`StoreError`
for the caller to report honestly.

Two things here are not obvious and are load-bearing:

- **Autocommit plus an explicit boundary** (reminders design D2). The connection
  is opened with ``isolation_level=None``, so no transaction exists unless
  :meth:`Store.transaction` opened one. Under pysqlite's default ``""`` the driver
  opens an implicit transaction before the first write and every repository's own
  ``commit()`` closes whatever its *caller* had open — which makes "these writes
  happen together or not at all" unimplementable.
- **There is no migration mechanism.** All DDL is ``CREATE TABLE IF NOT EXISTS``,
  so a column added to the statement below after the table exists on the deployed
  host is never created there. :func:`_check_reminders_columns` converts that from
  "code reads a column production does not have, silently" into a refusal to start
  that names the column. It is the enforcement of the rule, not a migration path.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

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
    # The COMPLETE final column set for reminders, delivery's five included
    # (reminders design D1). With no migration mechanism, a column added here
    # after the table exists on rp5 would never be created there — so the
    # reminder-delivery change adds no DDL, and this table is created once.
    """
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        due_at REAL NOT NULL,
        due_tz TEXT NOT NULL,
        input_spec TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        send_attempts INTEGER NOT NULL DEFAULT 0,
        delivered_at REAL,
        surfaced_at REAL,
        reported_at REAL
    )
    """,
    # Serves the pending listing here AND reminder-delivery's due selector.
    "CREATE INDEX IF NOT EXISTS idx_reminders_status_due "
    "ON reminders(status, due_at, id)",
)

#: Exactly the columns the statement above creates. The drift check compares the
#: live table against this, so the two cannot disagree without a test failing.
REMINDER_COLUMNS: tuple[str, ...] = (
    "id",
    "text",
    "due_at",
    "due_tz",
    "input_spec",
    "created_at",
    "source",
    "status",
    "next_attempt_at",
    "send_attempts",
    "delivered_at",
    "surfaced_at",
    "reported_at",
)


def _check_reminders_columns(conn: sqlite3.Connection) -> None:
    """Refuse to run against a ``reminders`` table this code does not recognize.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op against a pre-existing table, so a
    table created by an older build keeps its old column set forever. Reading a
    missing column then fails at the first query, on the deployed host only, where
    no test runs. Names both directions: a missing column means this build expects
    more than the file has, an unexpected one means the file was written by a build
    this one does not know about.
    """
    live = {str(row[1]) for row in conn.execute("PRAGMA table_info(reminders)")}
    if not live:  # pragma: no cover - the DDL above always creates it
        raise StoreError("the reminders table is missing after schema creation")
    expected = set(REMINDER_COLUMNS)
    missing = sorted(expected - live)
    unexpected = sorted(live - expected)
    if not missing and not unexpected:
        return
    parts = []
    if missing:
        parts.append(f"missing column(s): {', '.join(missing)}")
    if unexpected:
        parts.append(f"unexpected column(s): {', '.join(unexpected)}")
    raise StoreError(
        "the reminders table does not match this build's schema — "
        + "; ".join(parts)
        + ". There is no migration mechanism: the table must be recreated or the "
        "code reverted. Refusing to start rather than reading a column that is "
        "not there."
    )


class Store:
    """Owns the SQLite connection and the schema. Repositories borrow it."""

    def __init__(
        self, path: str | Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._conn: sqlite3.Connection | None = None
        #: Reentrancy depth for :meth:`transaction`. Counted per Store, on one
        #: shared connection — safe only while nothing dispatches a store call off
        #: the event loop (see the docstring on ``check_same_thread`` below).
        self._depth = 0
        #: Set when any scope, nested or not, leaves by exception. The outermost
        #: exit then rolls back even if the exception was caught in between.
        self._poisoned = False

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
            # isolation_level=None is autocommit: pysqlite issues no BEGIN of its
            # own, so the only transactions that exist are the ones
            # `transaction()` opened. check_same_thread=False is a historical
            # loosening and OVERSTATES the intent — the store is used from the
            # event loop thread only, no store call awaits, and `transaction()`
            # keeps its depth on the Store rather than per thread. Dispatching a
            # store call off the loop — onto a worker thread or an executor — would
            # interleave two transactions on this one connection;
            # tests/test_store_transaction.py greps for that and fails if it
            # appears (the grep's own pattern is why this comment spells it out in
            # prose rather than in code).
            conn = sqlite3.connect(
                str(self._path), check_same_thread=False, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            for statement in _SCHEMA:
                conn.execute(statement)
            # After the DDL and before any repository touches the table: a drift
            # check is only useful if nothing has queried the table yet.
            _check_reminders_columns(conn)
        except (sqlite3.Error, OSError) as exc:
            raise StoreError(f"cannot open the store at {self._path}: {exc}") from exc
        self._conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction, reentrant by depth, poisoned by any failure.

        ``BEGIN IMMEDIATE`` rather than deferred: a deferred transaction takes its
        write lock on the first write, which is the classic upgrade-deadlock shape.
        Taking it up front makes contention fail fast instead of half-way through.

        Only the outermost scope issues ``BEGIN`` and only the outermost scope
        commits — a nested ``with`` joins, which is what makes every repository
        method transaction-agnostic: standalone it is one atomic write, inside a
        caller's transaction it participates in theirs.

        A scope that leaves by exception **poisons** the transaction: the outermost
        exit rolls back even if the exception was caught in between. Half of a
        multi-write guarantee committed because an inner failure was swallowed is
        precisely the class of bug this API exists to prevent, and "the caller must
        re-raise" is a convention no test enforces.
        """
        conn = self.connection()
        if self._depth == 0:
            self._poisoned = False
            conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield conn
        except BaseException:
            # Set on the way out, before the exception propagates, so a caller
            # that catches it cannot unmark the transaction.
            self._poisoned = True
            raise
        finally:
            self._depth -= 1
            if self._depth == 0:
                poisoned, self._poisoned = self._poisoned, False
                try:
                    conn.execute("ROLLBACK" if poisoned else "COMMIT")
                except sqlite3.Error:
                    # A COMMIT that fails leaves the transaction open; roll it back
                    # so the connection is usable and the failure is not silently
                    # converted into a partial write.
                    logger.error(
                        "could not %s the store transaction",
                        "roll back" if poisoned else "commit",
                        exc_info=True,
                    )
                    if not poisoned:
                        try:
                            conn.execute("ROLLBACK")
                        except sqlite3.Error:  # pragma: no cover - best effort
                            pass
                        raise

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
        self._depth = 0
        self._poisoned = False
