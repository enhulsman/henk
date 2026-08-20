"""Henk's durable store: one SQLite file carrying memories, the inbox and reminders.

Lives on the volume that already holds the audit JSONL — no new volume, port,
socket, ACL grant, or secret (secure-deployment spec). Three repositories share the
file: :class:`MemoryStore` (capped, type-namespaced owner facts),
:class:`SqliteInboxStore` (append-only capture inbox behind the
:class:`InboxStore` seam, so a future personal-inbox service can take over
without touching agent logic — design D1) and :class:`ReminderStore` (one-shot
reminders, nothing ever deleted).

:meth:`Store.transaction` is the boundary all three share: an explicit
``BEGIN IMMEDIATE`` context manager on an autocommit connection, so "these writes
happen together or not at all" is expressible and no repository can commit a
transaction it did not open (reminders design D2).
"""

from henk.store.db import Store
from henk.store.factory import HenkStores, build_stores
from henk.store.errors import (
    ContentTooLongError,
    EmptyContentError,
    InvalidContentError,
    StoreError,
)
from henk.store.inbox import (
    DEFAULT_PAGE_SIZE,
    DONE,
    OPEN,
    InboxItem,
    InboxPage,
    InboxStore,
    SqliteInboxStore,
    format_created_at,
)
from henk.store.reminders import (
    ABANDONED,
    CANCELLED,
    DELIVERED,
    DELIVERED_LATE,
    INPUT_SPEC_LIMIT,
    MISSED,
    PENDING,
    SOURCE_COMMAND,
    SOURCE_TOOL,
    STATUSES,
    Reminder,
    ReminderCapReachedError,
    ReminderPage,
    ReminderStore,
)
from henk.store.memory import (
    AGENT,
    DEFAULT_CAPS,
    DEFAULT_LENGTH_LIMIT,
    MEMORY_TYPES,
    PINNED,
    Memory,
    MemoryStore,
    MemoryWrite,
)

__all__ = [
    "ABANDONED",
    "AGENT",
    "HenkStores",
    "build_stores",
    "ContentTooLongError",
    "DEFAULT_CAPS",
    "DEFAULT_LENGTH_LIMIT",
    "DEFAULT_PAGE_SIZE",
    "DONE",
    "EmptyContentError",
    "InboxItem",
    "InboxPage",
    "InboxStore",
    "InvalidContentError",
    "MEMORY_TYPES",
    "Memory",
    "MemoryStore",
    "MemoryWrite",
    "OPEN",
    "PINNED",
    "CANCELLED",
    "DELIVERED",
    "DELIVERED_LATE",
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
    "SqliteInboxStore",
    "Store",
    "format_created_at",
    "StoreError",
]
