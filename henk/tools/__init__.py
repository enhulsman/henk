"""Henk's v1 tools plus the production registry builder."""

from __future__ import annotations

import logging

import httpx

from henk.config import Config
from henk.store import HenkStores, build_stores
from henk.tools.base import (
    AuthorizationTier,
    Tool,
    ToolClass,
    ToolRegistry,
    ToolResult,
    TurnType,
)
from henk.tools.capture import CaptureTool, InboxReadTool
from henk.tools.homelab_health import HomelabHealthTool
from henk.tools.memory import StoreMemoryTool
from henk.tools.notify import NotifyTool
from henk.tools.publish_handoff import PublishHandoffTool
from henk.tools.taiga_read import TaigaReadTool
from henk.tools.todo_read import TodoReadTool

__all__ = [
    "AuthorizationTier",
    "Tool",
    "ToolClass",
    "ToolRegistry",
    "ToolResult",
    "TurnType",
    "CaptureTool",
    "HomelabHealthTool",
    "InboxReadTool",
    "StoreMemoryTool",
    "TaigaReadTool",
    "TodoReadTool",
    "NotifyTool",
    "PublishHandoffTool",
    "build_production_registry",
]

logger = logging.getLogger("henk.tools")


def build_production_registry(
    config: Config,
    client: httpx.AsyncClient,
    *,
    stores: HenkStores | None = None,
) -> ToolRegistry:
    """The production toolset: reads, notify-class sends, and the durable writes.

    Mutating tools live here now (approval-gate delta, owner-blessed reversal of
    "v1 ships no mutating tools"): ``store_memory`` and ``capture`` write into
    Henk's own capped/append-only stores, both at the **standing** tier and both
    **owner-turn-only**, so they execute without a prompt but never during an event
    turn, never in a session an incident has touched, and never without a durable
    receipt. ``inbox_read`` is their read-only counterpart.

    ``stores`` should be passed by any caller that also uses the repositories
    itself — the runtime does, because `/remember` and recall must read and write
    the same instances the tool does. When omitted, a fresh set is built from
    config (correct, but a second connection to the same file).

    ``todo_read`` is registered behind a **default-deny note-path allowlist**
    (personal-data-scoping). The obsidian vault mixes personal and work/Anamata
    notes, so the tool surfaces only todos whose source note matches an allowlisted
    folder-boundary prefix and drops everything else; an empty/unset allowlist
    surfaces nothing (fail closed). Registering with an empty effective allowlist is
    safe but useless, so a startup WARNING is emitted in that case.

    ``taiga_read`` remains deliberately NOT registered (fast-follow): the Taiga
    instance holds mixed personal/work projects, so it needs the same default-deny
    allowlist — keyed on **project id** — plus a server-side prerequisite (a Taiga
    read account scoped to personal projects) that does not exist yet. It MUST NOT be
    registered until that project-id filter is implemented. Its class and tests are
    kept for that follow-up.
    """
    stores = stores or build_stores(config.store)
    registry = ToolRegistry()
    registry.register(
        HomelabHealthTool(
            client,
            gatus_url=config.gatus.base_url,
            prometheus_url=config.prometheus.base_url,
            timeout=config.gatus.timeout_seconds,
        )
    )
    todo_read = TodoReadTool(
        client,
        base_url=config.todo.base_url,
        token=config.secrets.todo_token,
        timeout=config.todo.timeout_seconds,
        note_allowlist=config.personal_data.todo_note_allowlist,
    )
    if not todo_read.effective_allowlist:
        logger.warning(
            "todo_read registered but always empty — no allowlist configured "
            "(personal_data.todo_note_allowlist); it will surface nothing"
        )
    registry.register(todo_read)
    registry.register(
        NotifyTool(
            client,
            base_url=config.ntfy.base_url,
            topic=config.ntfy.topic,
            token=config.secrets.ntfy_token,
            timeout=config.ntfy.timeout_seconds,
        )
    )
    # publish_handoff rides the same single ntfy credential (write on handoffs).
    # Registered unconditionally so the enumerated toolset matches the registry;
    # it is only ever exercised by triage, which only runs when events.enabled.
    registry.register(
        PublishHandoffTool(
            client,
            base_url=config.ntfy.base_url,
            topic=config.events.handoffs_topic,
            token=config.secrets.ntfy_token,
            timeout=config.ntfy.timeout_seconds,
        )
    )
    registry.register(StoreMemoryTool(stores.memories))
    registry.register(CaptureTool(stores.inbox))
    registry.register(
        InboxReadTool(stores.inbox, page_size=config.store.inbox_page_size)
    )
    return registry
