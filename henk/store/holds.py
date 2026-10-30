"""Held events and their maintenance windows (maintenance-calendar D7, D8).

Two tables in the shared SQLite file, created by :mod:`henk.store.db`:

- ``hold_windows``: one row per window that has held at least one event. Inserted
  with that first event (``INSERT OR IGNORE``), so its ``end`` is the end as known
  then, the **frozen end**. Only the release's extension rule moves it.
  ``released_at`` is set once, by an update guarded on ``released_at IS NULL``,
  which is what makes a window's ``hold-release`` record unique.
- ``held_events``: one row per held ntfy event, keyed on the event id, so a replay
  of an already-held event inserts nothing.

This repository is dumb storage: which window an event is held against, whether a
window is open, and how a release groups the rows are decided in
:mod:`henk.events.holds`. Every backend failure is a :class:`StoreError`, which the
caller turns into fail-open behaviour.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from henk.store.db import Store
from henk.store.errors import StoreError

#: Outcomes of :meth:`HoldStore.hold`.
HELD = "held"
DUPLICATE = "duplicate"
CLOSED = "closed"


@dataclass(frozen=True)
class HoldWindow:
    """One ``hold_windows`` row. Times are epoch seconds; ``end`` excludes grace."""

    window_id: str
    title: str
    start: float
    end: float
    released_at: float | None


@dataclass(frozen=True)
class HeldEvent:
    """One ``held_events`` row: everything a release rebuilds the event from."""

    event_id: str
    window_id: str
    identity_key: str
    state: str
    title: str
    message: str
    raw_json: str
    published_at: float
    arrival_time: float
    held_at: float


@dataclass(frozen=True)
class HoldInsert:
    """What :meth:`HoldStore.hold` did.

    ``status`` is :data:`HELD` (a new row), :data:`DUPLICATE` (the event id was
    already held, nothing written) or :data:`CLOSED` (the window is already
    released, nothing written). ``window_end`` is the window's frozen end when
    the event was held, else None.
    """

    status: str
    window_end: float | None = None


_WINDOW_SELECT = 'SELECT window_id, title, start, "end", released_at FROM hold_windows'
_EVENT_SELECT = (
    "SELECT event_id, window_id, identity_key, state, title, message, raw_json, "
    "published_at, arrival_time, held_at FROM held_events"
)
#: Publish time first; ties (ntfy's `time` is whole seconds) in insertion order,
#: which is ntfy's delivery order.
_EVENT_ORDER = " ORDER BY published_at, rowid"


def _window(row: sqlite3.Row) -> HoldWindow:
    released = row["released_at"]
    return HoldWindow(
        window_id=str(row["window_id"]),
        title=str(row["title"]),
        start=float(row["start"]),
        end=float(row["end"]),
        released_at=None if released is None else float(released),
    )


def _event(row: sqlite3.Row) -> HeldEvent:
    return HeldEvent(
        event_id=str(row["event_id"]),
        window_id=str(row["window_id"]),
        identity_key=str(row["identity_key"]),
        state=str(row["state"]),
        title=str(row["title"]),
        message=str(row["message"]),
        raw_json=str(row["raw_json"]),
        published_at=float(row["published_at"]),
        arrival_time=float(row["arrival_time"]),
        held_at=float(row["held_at"]),
    )


def _placeholders(values: list[str]) -> str:
    return ", ".join("?" for _ in values)


class HoldStore:
    """Repository over ``hold_windows`` and ``held_events``. Borrows the Store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @property
    def store(self) -> Store:
        return self._store

    # --- reads --------------------------------------------------------------

    def known_event_ids(self, event_ids: Iterable[str]) -> set[str]:
        """The subset of ``event_ids`` already held (the duplicate check)."""
        ids = sorted({str(i) for i in event_ids if i})
        if not ids:
            return set()
        try:
            rows = self._store.connection().execute(
                f"SELECT event_id FROM held_events WHERE event_id IN ({_placeholders(ids)})",
                ids,
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"could not read held events: {exc}") from exc
        return {str(r[0]) for r in rows}

    def windows(self, window_ids: Iterable[str]) -> dict[str, HoldWindow]:
        """The stored rows for these window ids; ids never held are absent."""
        ids = sorted({str(i) for i in window_ids})
        if not ids:
            return {}
        try:
            rows = self._store.connection().execute(
                f"{_WINDOW_SELECT} WHERE window_id IN ({_placeholders(ids)})", ids
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"could not read hold windows: {exc}") from exc
        return {w.window_id: w for w in map(_window, rows)}

    def unreleased_windows(self) -> list[HoldWindow]:
        """Every unreleased window that holds at least one event, by end then id."""
        try:
            rows = self._store.connection().execute(
                f"{_WINDOW_SELECT} AS w WHERE released_at IS NULL AND EXISTS ("
                "SELECT 1 FROM held_events AS e WHERE e.window_id = w.window_id"
                ') ORDER BY "end", window_id'
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"could not read hold windows: {exc}") from exc
        return [_window(r) for r in rows]

    def held_events(self, window_id: str) -> list[HeldEvent]:
        """The window's held events, by publish time then arrival order."""
        try:
            rows = self._store.connection().execute(
                f"{_EVENT_SELECT} WHERE window_id = ?{_EVENT_ORDER}", (window_id,)
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"could not read held events: {exc}") from exc
        return [_event(r) for r in rows]

    # --- writes -------------------------------------------------------------

    def hold(
        self,
        *,
        window_id: str,
        window_title: str,
        window_start: float,
        window_end: float,
        event_id: str,
        identity_key: str,
        state: str,
        title: str,
        message: str,
        raw_json: str,
        published_at: float,
        arrival_time: float,
        held_at: float,
    ) -> HoldInsert:
        """Hold one event against one window, in one transaction.

        The duplicate check comes first, so a replayed event never creates a
        window row of its own. The window row is inserted only if new, and its
        stored (frozen) end is what the caller gets back. A released window takes
        nothing.
        """
        try:
            with self._store.transaction() as conn:
                if conn.execute(
                    "SELECT 1 FROM held_events WHERE event_id = ?", (event_id,)
                ).fetchone():
                    return HoldInsert(DUPLICATE)
                conn.execute(
                    'INSERT OR IGNORE INTO hold_windows (window_id, title, start, "end", '
                    "released_at) VALUES (?, ?, ?, ?, NULL)",
                    (window_id, window_title, float(window_start), float(window_end)),
                )
                row = conn.execute(
                    'SELECT "end", released_at FROM hold_windows WHERE window_id = ?',
                    (window_id,),
                ).fetchone()
                if row["released_at"] is not None:
                    return HoldInsert(CLOSED)
                conn.execute(
                    "INSERT INTO held_events (event_id, window_id, identity_key, state, "
                    "title, message, raw_json, published_at, arrival_time, held_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        window_id,
                        identity_key,
                        state,
                        title,
                        message,
                        raw_json,
                        float(published_at),
                        float(arrival_time),
                        float(held_at),
                    ),
                )
                return HoldInsert(HELD, float(row["end"]))
        except sqlite3.Error as exc:
            raise StoreError(f"could not hold event: {exc}") from exc

    def extend(self, window_id: str, end: float) -> bool:
        """Move an unreleased window's frozen end (the extension rule). True if moved."""
        try:
            with self._store.transaction() as conn:
                cursor = conn.execute(
                    'UPDATE hold_windows SET "end" = ? '
                    "WHERE window_id = ? AND released_at IS NULL",
                    (float(end), window_id),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise StoreError(f"could not extend hold window: {exc}") from exc

    def mark_released(self, window_id: str, at: float) -> list[HeldEvent] | None:
        """Set ``released_at`` once. The window's rows if this call marked it, else None.

        Guarded on ``released_at IS NULL``: a second call, for whatever reason,
        changes nothing and returns None, so the caller writes no second
        ``hold-release`` record. The rows are read in the same transaction, so the
        counts the caller records are exactly the rows that were released.
        """
        try:
            with self._store.transaction() as conn:
                cursor = conn.execute(
                    "UPDATE hold_windows SET released_at = ? "
                    "WHERE window_id = ? AND released_at IS NULL",
                    (float(at), window_id),
                )
                if cursor.rowcount != 1:
                    return None
                rows = conn.execute(
                    f"{_EVENT_SELECT} WHERE window_id = ?{_EVENT_ORDER}", (window_id,)
                ).fetchall()
                return [_event(r) for r in rows]
        except sqlite3.Error as exc:
            raise StoreError(f"could not mark hold window released: {exc}") from exc
