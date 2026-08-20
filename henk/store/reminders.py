"""The reminders repository: schedule, list, cancel, reinstate. Nothing deletes.

Every write method is **transaction-agnostic** (reminders design D2): it wraps its
work in ``store.transaction()`` and issues no ``commit()`` of its own, so calling it
standalone is one atomic write and calling it inside a caller's transaction joins
theirs. `reminder-delivery`'s pre-work / post-send transactions are built entirely
out of that property.

Three rules this module enforces structurally rather than by convention:

- **Nothing is ever deleted, and no stored text or due instant is ever rewritten.**
  A terminal status is a state change, not a removal, so what the owner asked for
  survives being cancelled. There is no `DELETE FROM reminders` and no update that
  touches ``text``, ``due_at``, ``due_tz``, ``input_spec`` or ``created_at``
  anywhere below, and a test asserts that against this file's source.
- **Every path into ``pending`` writes ``next_attempt_at``.** Delivery's selector is
  a query, so a null (or a stale sentinel) value makes a row permanently
  unselectable while it still reports itself as pending — a reminder that exists,
  says pending, and can never fire.
- **The cap check and the insert are one transaction.** Reading a count and then
  inserting outside a shared transaction is a check that proves nothing.

Reminder *text* rejections use the store's existing content-error taxonomy, so the
tool and command layers report them the same way they already report an over-long
memory: named limit, nothing stored, never a truncated variant.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from henk.store.db import Store
from henk.store.errors import (
    ContentTooLongError,
    EmptyContentError,
    InvalidContentError,
    StoreError,
)

PENDING = "pending"
DELIVERED = "delivered"
DELIVERED_LATE = "delivered-late"
MISSED = "missed"
CANCELLED = "cancelled"
ABANDONED = "abandoned"

#: The complete status vocabulary. `pending` and `cancelled` are the only two this
#: change writes; the rest are reminder-delivery's, listed here because the column
#: set is final and the vocabulary is part of it.
STATUSES: tuple[str, ...] = (
    PENDING,
    DELIVERED,
    DELIVERED_LATE,
    MISSED,
    CANCELLED,
    ABANDONED,
)

#: Sources, matching the two paths that can create a reminder.
SOURCE_TOOL = "tool"
SOURCE_COMMAND = "command"

#: How many reminders may be `pending` at once. Bounds accumulation from a model
#: that schedules more than the owner asked for; refused honestly, never silently.
DEFAULT_MAX_PENDING = 100

#: Per-reminder text limit. Rejected naming the limit, never truncated — a
#: silently shortened reminder is a wrong reminder.
DEFAULT_TEXT_LENGTH_LIMIT = 500

#: Oldest-due-first page size for `/reminders` and `reminders_read`.
DEFAULT_PAGE_SIZE = 20

#: Bound on the forensic ``input_spec`` column. Truncation here is **silent** by
#: design: a diagnostic column must never be the reason a valid schedule fails.
INPUT_SPEC_LIMIT = 64

_COLUMNS = (
    "id, text, due_at, due_tz, input_spec, created_at, source, status, "
    "next_attempt_at, send_attempts, delivered_at, surfaced_at, reported_at"
)


class ReminderCapReachedError(InvalidContentError):
    """The pending cap is already reached; nothing was stored."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        super().__init__(
            f"you already have {cap} reminders pending, which is the configured "
            f"limit of {cap}; nothing was scheduled — cancel one first"
        )


@dataclass(frozen=True)
class Reminder:
    """One stored reminder, every column included.

    The five delivery-half columns are carried here as well as in the table: a
    reader of this type should see the whole row, and `reminder-delivery` needs no
    second dataclass.
    """

    id: int
    text: str
    due_at: float
    due_tz: str
    input_spec: str
    created_at: float
    source: str
    status: str
    next_attempt_at: float
    send_attempts: int = 0
    delivered_at: float | None = None
    surfaced_at: float | None = None
    reported_at: float | None = None


@dataclass(frozen=True)
class ReminderPage:
    """One page of pending reminders plus how many due later were not shown."""

    items: tuple[Reminder, ...] = ()
    remainder: int = 0


def _row_to_reminder(row: sqlite3.Row) -> Reminder:
    def opt(key: str) -> float | None:
        value = row[key]
        return None if value is None else float(value)

    return Reminder(
        id=int(row["id"]),
        text=str(row["text"]),
        due_at=float(row["due_at"]),
        due_tz=str(row["due_tz"]),
        input_spec=str(row["input_spec"]),
        created_at=float(row["created_at"]),
        source=str(row["source"]),
        status=str(row["status"]),
        next_attempt_at=float(row["next_attempt_at"]),
        send_attempts=int(row["send_attempts"]),
        delivered_at=opt("delivered_at"),
        surfaced_at=opt("surfaced_at"),
        reported_at=opt("reported_at"),
    )


class ReminderStore:
    """Repository over the ``reminders`` table."""

    def __init__(
        self,
        store: Store,
        *,
        max_pending: int = DEFAULT_MAX_PENDING,
        text_length_limit: int = DEFAULT_TEXT_LENGTH_LIMIT,
        page_size: int = DEFAULT_PAGE_SIZE,
        input_spec_limit: int = INPUT_SPEC_LIMIT,
    ) -> None:
        self._store = store
        self._max_pending = max_pending
        self._text_length_limit = text_length_limit
        self._page_size = page_size
        self._input_spec_limit = input_spec_limit

    @property
    def max_pending(self) -> int:
        return self._max_pending

    @property
    def text_length_limit(self) -> int:
        return self._text_length_limit

    @property
    def page_size(self) -> int:
        return self._page_size

    @property
    def input_spec_limit(self) -> int:
        return self._input_spec_limit

    # --- writes -----------------------------------------------------------

    def schedule(
        self,
        text: str,
        *,
        due_at: float,
        due_tz: str,
        input_spec: str = "",
        source: str = SOURCE_TOOL,
    ) -> Reminder:
        """Store one pending reminder.

        Raises :class:`EmptyContentError` / :class:`ContentTooLongError` for text
        the store refuses, :class:`ReminderCapReachedError` at the pending cap, and
        :class:`StoreError` for a backend failure. Nothing is stored on any of them.
        """
        content = (text or "").strip()
        if not content:
            raise EmptyContentError("the reminder text is empty; nothing was stored")
        if len(content) > self._text_length_limit:
            raise ContentTooLongError(self._text_length_limit, len(content))
        # Silent truncation, and only here: the forensic column is worth having on
        # every row, and it is not worth failing a valid schedule for.
        spec = (input_spec or "")[: self._input_spec_limit]
        now = self._store.clock()
        try:
            with self._store.transaction() as conn:
                # Cap check and insert share this transaction, so the count cannot
                # be true when read and false when written.
                pending = self._count_pending_locked(conn)
                if pending >= self._max_pending:
                    raise ReminderCapReachedError(self._max_pending)
                new_id = self._insert_locked(
                    conn,
                    text=content,
                    due_at=float(due_at),
                    due_tz=str(due_tz),
                    input_spec=spec,
                    created_at=now,
                    source=source,
                )
        except sqlite3.Error as exc:
            raise StoreError(f"could not store the reminder: {exc}") from exc
        return Reminder(
            id=new_id,
            text=content,
            due_at=float(due_at),
            due_tz=str(due_tz),
            input_spec=spec,
            created_at=now,
            source=source,
            status=PENDING,
            # Written explicitly, not left to the column default: a pending row
            # becomes eligible for delivery when it is due.
            next_attempt_at=float(due_at),
        )

    def cancel(self, reminder_id: int) -> Reminder | None:
        """Set a *pending* reminder to `cancelled`. Returns None if none matched.

        A status change, never a delete: the row, its text and its due instant are
        all retained so `/reminders reinstate` can put it back and so the record of
        what the owner asked for survives.
        """
        return self._transition(reminder_id, from_status=PENDING, to_status=CANCELLED)

    def reinstate(self, reminder_id: int) -> Reminder | None:
        """Return a *cancelled* reminder to `pending`. Returns None if none matched.

        Subject to the pending cap (:class:`ReminderCapReachedError`), because
        reinstating is a path into `pending` like any other. The caller owns the
        past-due refusal: it needs the current instant and names `/remind` in its
        reply, both of which are policy above this layer.
        """
        return self._transition(
            reminder_id,
            from_status=CANCELLED,
            to_status=PENDING,
            enforce_cap=True,
        )

    def _transition(
        self,
        reminder_id: int,
        *,
        from_status: str,
        to_status: str,
        enforce_cap: bool = False,
    ) -> Reminder | None:
        """One status change plus its read-back, in one transaction.

        ``next_attempt_at`` is rewritten on any transition INTO `pending`, in the
        same UPDATE — so there is no path into `pending` that can forget it.
        """
        try:
            with self._store.transaction() as conn:
                if enforce_cap and to_status == PENDING:
                    if self._count_pending_locked(conn) >= self._max_pending:
                        raise ReminderCapReachedError(self._max_pending)
                if to_status == PENDING:
                    cursor = conn.execute(
                        "UPDATE reminders SET status = ?, "
                        "next_attempt_at = due_at "
                        "WHERE id = ? AND status = ?",
                        (to_status, reminder_id, from_status),
                    )
                else:
                    cursor = conn.execute(
                        "UPDATE reminders SET status = ? WHERE id = ? AND status = ?",
                        (to_status, reminder_id, from_status),
                    )
                if cursor.rowcount == 0:
                    return None
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM reminders WHERE id = ?", (reminder_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"could not update the reminder: {exc}") from exc
        return _row_to_reminder(row) if row is not None else None

    def _insert_locked(
        self,
        conn: sqlite3.Connection,
        *,
        text: str,
        due_at: float,
        due_tz: str,
        input_spec: str,
        created_at: float,
        source: str,
    ) -> int:
        """Insert one pending row. Runs inside the caller's transaction."""
        cursor = conn.execute(
            "INSERT INTO reminders (text, due_at, due_tz, input_spec, created_at, "
            "source, status, next_attempt_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (text, due_at, due_tz, input_spec, created_at, source, PENDING, due_at),
        )
        return int(cursor.lastrowid or 0)

    # --- reads ------------------------------------------------------------

    def get(self, reminder_id: int) -> Reminder | None:
        """Any reminder by id, terminal ones included (proves nothing is deleted)."""
        try:
            row = (
                self._store.connection()
                .execute(
                    f"SELECT {_COLUMNS} FROM reminders WHERE id = ?", (reminder_id,)
                )
                .fetchone()
            )
        except sqlite3.Error as exc:
            raise StoreError(f"could not read the reminder: {exc}") from exc
        return _row_to_reminder(row) if row is not None else None

    def list_pending(self, *, limit: int | None = -1) -> ReminderPage:
        """Pending reminders, oldest-due first, plus how many were not shown.

        ``limit`` defaults to the configured page size; pass ``None`` for every
        pending reminder. Oldest-due-first is the point: the next thing to happen
        is always visible, so the page bound never hides an imminent reminder.
        """
        effective = self._page_size if limit == -1 else limit
        try:
            rows = (
                self._store.connection()
                .execute(
                    f"SELECT {_COLUMNS} FROM reminders WHERE status = ? "
                    "ORDER BY due_at ASC, id ASC",
                    (PENDING,),
                )
                .fetchall()
            )
        except sqlite3.Error as exc:
            raise StoreError(f"could not read reminders: {exc}") from exc
        items = [_row_to_reminder(row) for row in rows]
        if effective is None:
            return ReminderPage(items=tuple(items), remainder=0)
        return ReminderPage(
            items=tuple(items[:effective]),
            remainder=max(0, len(items) - effective),
        )

    def count_pending(self) -> int:
        try:
            return self._count_pending_locked(self._store.connection())
        except sqlite3.Error as exc:
            raise StoreError(f"could not count reminders: {exc}") from exc

    @staticmethod
    def _count_pending_locked(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE status = ?", (PENDING,)
        ).fetchone()
        return int(row[0])


__all__ = [
    "ABANDONED",
    "CANCELLED",
    "ContentTooLongError",
    "DEFAULT_MAX_PENDING",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_TEXT_LENGTH_LIMIT",
    "DELIVERED",
    "DELIVERED_LATE",
    "EmptyContentError",
    "INPUT_SPEC_LIMIT",
    "MISSED",
    "PENDING",
    "Reminder",
    "ReminderCapReachedError",
    "ReminderPage",
    "ReminderStore",
    "SOURCE_COMMAND",
    "SOURCE_TOOL",
    "STATUSES",
]
