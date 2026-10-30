"""Henk's v1 tools plus the production registry builder."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from henk.calendar.client import CalendarClient
from henk.calendar.token import AccessTokenSource
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
from henk.tools.calendar import (
    CALENDAR_TOOL_NAMES,
    ApprovalReferences,
    CalendarCreateEventTool,
    CalendarUpcomingTool,
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
from henk.tools.sessions_read import SessionsReadTool
from henk.tools.taiga_read import TaigaReadTool
from henk.tools.todo_read import TodoReadTool

if TYPE_CHECKING:  # pragma: no cover - typing only
    from henk.audit import CalendarReceipts

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
    "SessionsReadTool",
    "StoreMemoryTool",
    "TaigaReadTool",
    "TodoReadTool",
    "NotifyTool",
    "PublishHandoffTool",
    "REMINDER_TOOL_NAMES",
    "CancelReminderTool",
    "RemindersReadTool",
    "RemindTool",
    "CALENDAR_TOOL_NAMES",
    "ApprovalReferences",
    "CalendarCreateEventTool",
    "CalendarUpcomingTool",
    "build_calendar_client",
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


def build_calendar_client(
    config: Config, client: httpx.AsyncClient
) -> CalendarClient | None:
    """The one calendar client the tools AND the window provider share, or None.

    One instance, because it carries the one access-token cache: two clients would
    each sign in, and a 401 seen by one would not refresh the other. Constructing
    it contacts nobody; the first call signs in. ``config.secrets.google_calendar_key``
    and ``config.owner.timezone`` are guaranteed here: config load refuses
    ``calendar.enabled`` without either.
    """
    if not config.calendar.enabled:
        return None
    key = config.secrets.google_calendar_key
    zone = config.owner.timezone
    if key is None or zone is None:  # pragma: no cover - config load refuses this
        raise ValueError(
            "the calendar is enabled without its key or owner.timezone; config "
            "load should have refused this"
        )
    tokens = AccessTokenSource(
        key, client, timeout=config.calendar.timeout_seconds
    )
    return CalendarClient(
        client,
        tokens,
        calendar_id=config.calendar.calendar_id,
        time_zone=zone,
        creator_allowlist=config.personal_data.calendar_creator_allowlist,
        timeout=config.calendar.timeout_seconds,
    )


def build_production_registry(
    config: Config,
    client: httpx.AsyncClient,
    *,
    stores: HenkStores | None = None,
    resolver: TimeResolver | None = None,
    reminder_receipts=None,
    incident_context=None,
    calendar_client: CalendarClient | None = None,
    calendar_receipts: "CalendarReceipts | None" = None,
    calendar_references: ApprovalReferences | None = None,
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

    ``sessions_read`` is behind its own flag *and* behind a second key, and the
    two-key shape is the point: the workstation estate mixes the owner's personal
    and work sessions, so the capability needs both ``sessions.enabled`` and a
    non-empty ``personal_data.session_project_allowlist`` before it can surface
    anything (design D1/D12). It ships **disabled**, because nothing it reads
    exists until the workstation publisher, the write-only ntfy user and the topic
    grant have been provisioned by hand; on rp5 the flag and the allowlist are set
    **together**, and setting only the flag is a tool that can report nothing but
    "no sessions are in scope". That half-applied state registers anyway, with the
    same startup WARNING ``todo_read`` uses, because a loud useless tool is easier
    to diagnose than a silently absent one. No new URL, timeout or secret key: the
    topic is read over ``endpoints.ntfy`` with the credential Henk already holds.

    ``taiga_read`` remains deliberately NOT registered (fast-follow): the Taiga
    instance holds mixed personal/work projects, so it needs the same default-deny
    allowlist — keyed on **project id** — plus a server-side prerequisite (a Taiga
    read account scoped to personal projects) that does not exist yet. It MUST NOT be
    registered until that project-id filter is implemented. Its class and tests are
    kept for that follow-up.

    ``calendar_upcoming`` and ``calendar_create_event`` register ONLY when
    ``calendar.enabled`` (maintenance-calendar), last, and with the flag off the
    registry is the pre-change one and no Google host is contacted. Like the
    resolver, ``calendar_client`` should be passed by any caller that also uses it
    itself — the runtime does, because the window provider must share the one
    token cache — and is built from config when omitted (construction contacts
    nobody, so the replay's refusing transport is fine). ``calendar_receipts``
    writes the ``calendar`` record after a confirmed insert, and
    ``calendar_references`` is the tap on the gate's recorder that hands the create
    tool its approval's reference; the runtime passes both. With receipts and no
    references, the create tool refuses rather than write an unlinkable record.
    An empty ``personal_data.calendar_creator_allowlist`` admits only the events
    Henk created, which is safe but probably not intended, so it logs the same kind
    of startup WARNING ``todo_read`` does.
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
    #
    # It also archives what it published (triage-quality D8), into the shared
    # store, with the incidents read from `incident_context`. That provider must
    # be the SAME instance the agent core writes; with none passed, a fresh one is
    # used, which stays empty, so nothing is ever archived. Imported here rather
    # than at module level because henk.events.incident_context imports henk.tools.
    if incident_context is None:
        from henk.events.incident_context import IncidentContextProvider

        incident_context = IncidentContextProvider()
    registry.register(
        PublishHandoffTool(
            client,
            base_url=config.ntfy.base_url,
            topic=config.events.handoffs_topic,
            token=config.secrets.ntfy_token,
            timeout=config.ntfy.timeout_seconds,
            archive=stores.handoffs,
            incident_context=incident_context,
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
    if config.sessions.enabled:
        sessions_read = SessionsReadTool(
            client,
            base_url=config.ntfy.base_url,
            topic=config.sessions.topic,
            token=config.secrets.ntfy_token,
            timeout=config.ntfy.timeout_seconds,
            allowlist=config.personal_data.session_project_allowlist,
            stale_after_seconds=config.sessions.stale_after_seconds,
            lookback_seconds=config.sessions.lookback_seconds,
        )
        if not sessions_read.effective_allowlist:
            logger.warning(
                "sessions_read registered but always empty — no allowlist "
                "configured (personal_data.session_project_allowlist); it will "
                "surface nothing"
            )
        registry.register(sessions_read)
    if config.calendar.enabled:
        calendar_client = calendar_client or build_calendar_client(config, client)
        zone = config.owner.zone
        if not config.personal_data.calendar_creator_allowlist:
            logger.warning(
                "calendar tools registered with an empty creator allowlist "
                "(personal_data.calendar_creator_allowlist); only events Henk "
                "created are admitted"
            )
        registry.register(
            CalendarUpcomingTool(
                calendar_client,
                calendar=config.calendar,
                zone=zone,
                events_enabled=config.events.enabled,
            )
        )
        registry.register(
            CalendarCreateEventTool(
                calendar_client,
                calendar=config.calendar,
                zone=zone,
                events_enabled=config.events.enabled,
                receipts=calendar_receipts,
                references=calendar_references,
            )
        )
    return registry
