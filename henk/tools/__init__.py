"""Henk's v1 tools plus the production registry builder."""

from __future__ import annotations

import logging

import httpx

from henk.config import Config
from henk.reminders.timeparse import TimeResolver
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
from henk.tools.homelab_docs import HomelabDocsTool
from henk.tools.homelab_health import HomelabHealthTool
from henk.tools.homelab_query import HomelabQueryTool
from henk.tools.memory import StoreMemoryTool
from henk.tools.notify import NotifyTool
from henk.tools.publish_handoff import PublishHandoffTool
from henk.tools.reminders import (
    REMINDER_TOOL_NAMES,
    CancelReminderTool,
    RemindersReadTool,
    RemindTool,
)
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
    "HomelabDocsTool",
    "HomelabHealthTool",
    "HomelabQueryTool",
    "InboxReadTool",
    "StoreMemoryTool",
    "TaigaReadTool",
    "TodoReadTool",
    "NotifyTool",
    "PublishHandoffTool",
    "REMINDER_TOOL_NAMES",
    "CancelReminderTool",
    "RemindersReadTool",
    "RemindTool",
    "build_time_resolver",
    "build_production_registry",
]

logger = logging.getLogger("henk.tools")


def build_time_resolver(config: Config) -> TimeResolver | None:
    """The one resolver the tools AND the owner commands share, or None when off.

    One instance, because a due time rendered by two resolvers could differ and the
    owner would have to adjudicate. ``config.owner.zone`` is guaranteed present here:
    config load refuses `reminders.enabled` without `owner.timezone`, naming both.
    """
    if not config.reminders.enabled:
        return None
    zone = config.owner.zone
    if zone is None:  # pragma: no cover - config load already refuses this
        raise ValueError(
            "reminders are enabled but owner.timezone is unset; config load should "
            "have refused this"
        )
    return TimeResolver(
        zone,
        horizon_days=config.reminders.horizon_days,
        clock_skew_tolerance_seconds=config.reminders.clock_skew_tolerance_seconds,
    )


def build_production_registry(
    config: Config,
    client: httpx.AsyncClient,
    *,
    stores: HenkStores | None = None,
    resolver: TimeResolver | None = None,
    reminder_receipts=None,
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

    ``homelab_query`` and ``homelab_docs`` are the read-depth pair, each behind its
    own flag and each read-only. ``homelab_query`` ships **enabled**; ``homelab_docs``
    ships **disabled** and registers on host state alone being bad, so a broken
    corpus is a per-call error rather than a silently absent tool.

    ``taiga_read`` remains deliberately NOT registered (fast-follow): the Taiga
    instance holds mixed personal/work projects, so it needs the same default-deny
    allowlist — keyed on **project id** — plus a server-side prerequisite (a Taiga
    read account scoped to personal projects) that does not exist yet. It MUST NOT be
    registered until that project-id filter is implemented. Its class and tests are
    kept for that follow-up.
    """
    stores = stores or build_stores(config.store, config.reminders)
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
    # Reminders register ONLY when enabled, and the capability ships disabled. A
    # build that accepts "remind me at six", echoes a confident confirmation and
    # then says nothing at six has spent the owner's trust on a promise it
    # structurally cannot keep — delivery is the `reminder-delivery` change. With the
    # flag off the registry is byte-for-byte the pre-change one.
    if config.reminders.enabled:
        resolver = resolver or build_time_resolver(config)
        registry.register(
            RemindTool(stores.reminders, resolver, receipts=reminder_receipts)
        )
        registry.register(
            CancelReminderTool(stores.reminders, resolver, receipts=reminder_receipts)
        )
        # Read-only, so it bypasses the gate and takes no receipt.
        registry.register(RemindersReadTool(stores.reminders, resolver))
    # Read depth. Both halves are read-only and both are behind their own flag,
    # because they stage differently: the queries ride the `tag:henk` egress
    # `homelab_health` already uses (rp5:8080, vps:9090), so they ship ON with no
    # host provisioning to wait for; the corpus needs a clone, a pull timer and a
    # path allowlist on rp5 first, so it ships OFF and the owner flips it last.
    if config.homelab_query.enabled:
        # Both backends keep the timeout their own endpoint section declares —
        # no new timeout key, so `homelab_health` and `homelab_query` cannot time
        # out at different bounds against the same backend. Nothing is discovered
        # here: `endpoint_history`'s domain is read at FIRST USE, which is what
        # keeps `build_runtime`'s "nothing network-facing is opened here" true.
        registry.register(
            HomelabQueryTool(
                client,
                gatus_url=config.gatus.base_url,
                prometheus_url=config.prometheus.base_url,
                gatus_timeout=config.gatus.timeout_seconds,
                prometheus_timeout=config.prometheus.timeout_seconds,
                max_points=config.homelab_query.query_range_max_points,
            )
        )
    if config.homelab_docs.enabled:
        # Registered on HOST STATE alone being bad — a missing, empty, unreadable
        # or unstamped corpus still registers and fails honestly per call (design
        # D11 layer 2). An absent tool produces no honest failure at all: the model
        # would answer documentation questions from its priors with no marker that
        # the corpus was unreachable. Only a CONFIG error (enabled with no path)
        # refuses, and it does so at load, before this function is reached.
        docs = HomelabDocsTool(
            path=config.homelab_docs.path,
            allowlist=config.personal_data.docs_path_allowlist,
            stamp_max_age_seconds=config.homelab_docs.stamp_max_age_seconds,
            read_byte_budget=config.homelab_docs.read_byte_budget,
            search_result_count=config.homelab_docs.search_result_count,
        )
        if not docs.effective_allowlist:
            logger.warning(
                "homelab_docs registered but always empty — no allowlist "
                "configured (personal_data.docs_path_allowlist); it will surface "
                "nothing"
            )
        registry.register(docs)
    return registry
