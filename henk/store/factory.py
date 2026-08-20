"""One place that decides how the store's pieces fit together.

All three repositories share a single :class:`Store` (one SQLite file), and the
runtime needs the *same* instances the tool registry got — the memory repository
backs `store_memory`, `/remember` and recall alike, and the reminder repository backs
`remind`, `cancel_reminder`, `reminders_read` and the `/remind` / `/reminders`
commands. Building them here keeps that single ownership visible instead of scattered
across the wiring.
"""

from __future__ import annotations

from dataclasses import dataclass

from henk.store.db import Store
from henk.store.inbox import SqliteInboxStore
from henk.store.memory import MemoryStore
from henk.store.reminders import ReminderStore


@dataclass(frozen=True)
class HenkStores:
    """The store and its three repositories, built once and shared."""

    store: Store
    memories: MemoryStore
    inbox: SqliteInboxStore
    reminders: ReminderStore


def build_stores(store_config, reminders_config=None) -> HenkStores:
    """Build the store and repositories from a ``StoreConfig``.

    Opens nothing: :class:`Store` connects lazily, so this is safe at import/wiring
    time on a host with no ``/data`` volume.

    The reminder repository is built **whether or not reminders are enabled**. Its
    bounds come from ``RemindersConfig`` (defaults when none is supplied), and
    building it is inert: the table is created by the same lazy schema path either
    way, no tool is registered when the capability is off, and stored rows stay
    untouched — which is what makes re-enabling restore access to them.
    """
    from henk.config import RemindersConfig

    reminders_config = reminders_config or RemindersConfig()
    store = Store(store_config.path)
    return HenkStores(
        store=store,
        memories=MemoryStore(
            store,
            caps=store_config.memory_caps,
            length_limit=store_config.fact_length_limit,
        ),
        inbox=SqliteInboxStore(store),
        reminders=ReminderStore(
            store,
            max_pending=reminders_config.max_pending,
            text_length_limit=reminders_config.text_length_limit,
            page_size=reminders_config.page_size,
        ),
    )
