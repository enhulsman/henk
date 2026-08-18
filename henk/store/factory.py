"""One place that decides how the store's pieces fit together.

Both repositories share a single :class:`Store` (one SQLite file), and the runtime
needs the *same* instances the tool registry got — the memory repository backs
`store_memory`, `/remember` and recall alike. Building them here keeps that single
ownership visible instead of scattered across the wiring.
"""

from __future__ import annotations

from dataclasses import dataclass

from henk.store.db import Store
from henk.store.inbox import SqliteInboxStore
from henk.store.memory import MemoryStore


@dataclass(frozen=True)
class HenkStores:
    """The store and its two repositories, built once and shared."""

    store: Store
    memories: MemoryStore
    inbox: SqliteInboxStore


def build_stores(store_config) -> HenkStores:
    """Build the store and repositories from a ``StoreConfig``.

    Opens nothing: :class:`Store` connects lazily, so this is safe at import/wiring
    time on a host with no ``/data`` volume.
    """
    store = Store(store_config.path)
    return HenkStores(
        store=store,
        memories=MemoryStore(
            store,
            caps=store_config.memory_caps,
            length_limit=store_config.fact_length_limit,
        ),
        inbox=SqliteInboxStore(store),
    )
