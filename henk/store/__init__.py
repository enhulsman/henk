"""Henk's durable store: one SQLite file carrying memories and the capture inbox.

Lives on the volume that already holds the audit JSONL — no new volume, port,
socket, ACL grant, or secret (secure-deployment spec). Two repositories share the
file: :class:`MemoryStore` (capped, type-namespaced owner facts) and
:class:`SqliteInboxStore` (append-only capture inbox behind the
:class:`InboxStore` seam, so a future personal-inbox service can take over
without touching agent logic — design D1).
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
    "SqliteInboxStore",
    "Store",
    "format_created_at",
    "StoreError",
]
