"""Production wiring: assemble the full runtime from config.

Kept separate from ``app.py`` (the reusable Dispatcher/App) so the concrete
choices — Signal transport, SDK factory, httpx client, ntfy event stream — live
in one place and ``build_runtime`` can be constructed in a test without
connecting anything.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from henk.agent.commands import OwnerCommands
from henk.agent.core import AgentCore
from henk.agent.recall import MemoryRecall
from henk.agent.sdk_session import SdkSessionFactory
from henk.app import App, Dispatcher
from henk.audit import (
    AuditLog,
    MutationReceipts,
    ReminderReceipts,
    read_audit_records,
)
from henk.channel.allowlist import AllowlistFilter
from henk.channel.base import ChannelAdapter, SendOutcome
from henk.channel.signal import SignalAdapter, SignalCliRestBridge
from henk.config import Config
from henk.events.checkpoint import OffsetCheckpoint
from henk.events.coordinator import EventCoordinator
from henk.events.intake import (
    SINCE_REJECTED_NOTICE,
    EventIntake,
    NtfyEventStream,
)
from henk.events.pipeline import EventPipeline, PipelineConfig
from henk.gate.approval import ApprovalGate
from henk.reminders.note import DeliveredReminderNote
from henk.reminders.scheduler import ReminderScheduler
from henk.reminders.timeparse import TimeResolver
from henk.store import build_stores
from henk.tools import build_production_registry, build_time_resolver

logger = logging.getLogger("henk.runtime")

#: Bounded tail read for cadence rehydration. Far above any plausible 24h window
#: at Henk's few-events-per-week cadence, so rehydration stays a bounded startup
#: cost even without log rotation. (read_text still loads the file; a seek-based
#: tail is the future optimization if the log ever grows large.)
_REHYDRATE_LIMIT = 10_000

#: The transport read timeout is a redundant FLOOR under intake's own per-frame
#: liveness budget, not a replacement for it: httpx's read timeout resets on any
#: received bytes, so a peer dribbling newlines defeats it. Set above the deadline
#: so intake's watchdog is always what fires first, leaving this as the backstop
#: that turns a broken hand-rolled watchdog into a bounded reconnect.
_READ_TIMEOUT_MULTIPLE = 2


def _checkpoint_path(audit_path: str) -> Path:
    """The intake-offset cursor lives beside the audit log, on the same volume."""
    return Path(audit_path).parent / "intake-offset"


def build_runtime(config: Config) -> tuple[App, httpx.AsyncClient]:
    """Construct the wired App and the shared HTTP client (caller closes it).

    Nothing network-facing is opened here: the Signal bridge and the ntfy event
    stream connect lazily on first receive, and the SDK session is created
    per-conversation on demand.
    """
    # NOTE: this shared tool client carries no explicit timeout, so it takes
    # httpx's 5s PER-PHASE default. That is the same defect the Signal bridge's
    # explicit total fixes, left in place deliberately: the tool endpoints have
    # their own per-endpoint `timeout_seconds` in config and no measured budget
    # here, so choosing one is a separate change rather than a silent guess.
    client = httpx.AsyncClient()
    # One store, shared: the tools, the owner commands and recall must all read and
    # write the same repositories, or `/remember` and `store_memory` would disagree
    # about what Henk knows. Nothing is opened here — Store connects lazily.
    stores = build_stores(config.store, config.reminders)

    # Audit is constructed UNCONDITIONALLY (design D11) — see the note below — but
    # the reminder receipts need it before the registry is built, because the tools
    # take the lifecycle-record writer as a constructor argument.
    audit = AuditLog(config.audit.path)
    receipts = MutationReceipts(audit)
    reminder_receipts = ReminderReceipts(audit)

    # ONE resolver for the whole runtime, or None when reminders are disabled. The
    # tools, the owner commands and the per-turn time header all close over this
    # same instance: a due time rendered by two resolvers could differ, and the owner
    # would be the one left adjudicating which is right.
    resolver = build_time_resolver(config)
    registry = build_production_registry(
        config,
        client,
        stores=stores,
        resolver=resolver,
        reminder_receipts=reminder_receipts,
    )

    # Every timeout comes from config, none from a constructor default: an
    # unsupplied default is how the receive path's 30s went unchosen.
    bridge = SignalCliRestBridge(
        config.signal.bridge_url,
        config.signal.account,
        send_timeout=config.signal.send_timeout_seconds,
        open_timeout=config.signal.open_timeout_seconds,
    )
    adapter = SignalAdapter(
        bridge,
        account=config.signal.account,
        owner=config.owner.id,
        safe_length=config.signal.safe_length,
    )

    # Audit was constructed above, UNCONDITIONALLY (design D11). It used to appear
    # only when events were enabled — a leftover of arriving with that change. With
    # mutating tools in the registry, every supported configuration must produce
    # receipts, the rollback path (`events.enabled: false`) included.
    #
    # The gate sends approval prompts over the same channel the owner uses. The
    # demotion flag is the only config input it takes, and it only narrows; the
    # recorder is what makes every decision it takes durable at decision time.
    gate = ApprovalGate(
        adapter,
        timeout_seconds=config.agent.approval_timeout_seconds,
        demote_standing=config.gate.demote_standing,
        recorder=receipts,
    )
    factory = SdkSessionFactory(
        registry,
        gate,
        model=config.agent.model,
        system_prompt=config.agent.system_prompt,
    )

    # Durability wiring (design D1/D2): only when events are enabled. The
    # checkpoint store + cadence rehydration read the existing audit volume; both
    # reads are non-fatal if the volume is absent (fresh install / test env).
    checkpoint = None
    pipeline = None
    if config.events.enabled:
        checkpoint = OffsetCheckpoint(_checkpoint_path(config.audit.path))
        pipeline = _build_pipeline(config)
        # Reconstruct cooldown/cap/recurrence from the persisted log before the
        # coordinator consumes any event, so a restart does not re-arm cooldowns
        # or reset the daily cap. Best-effort: rehydration must never crash
        # startup, so an unreadable log or an unforeseen record shape logs and
        # falls back to empty cadence state (worst case: one restart re-alerts).
        try:
            pipeline.rehydrate(
                read_audit_records(config.audit.path, limit=_REHYDRATE_LIMIT),
                now=time.time(),
            )
        except Exception:
            logger.warning(
                "cadence rehydration failed; starting with empty cadence state",
                exc_info=True,
            )

    core = AgentCore(
        factory,
        adapter,
        idle_timeout_seconds=config.agent.idle_timeout_seconds,
        audit=audit,
        model=config.agent.model,
        checkpoint=checkpoint,
        # Wire recurrence framing live: a triage that publishes a handoff records
        # its id for the next re-fire of that identity (removes dead note_handoff).
        handoff_sink=pipeline.note_handoff if pipeline is not None else None,
        # The core frames every agent turn for the gate (turn type, announceability,
        # session taint) — without this the gate cannot enforce turn scope (D10).
        gate=gate,
        # Fans model-initiated receipts into the session record's approvals[].
        receipts=receipts,
        # Owner commands run app-side, before any session exists, and write their
        # own receipts — they are owner-authored input, so no gate is involved (D8).
        commands=OwnerCommands(
            memories=stores.memories,
            inbox=stores.inbox,
            # The SAME repository and resolver instances the tools got. Passed only
            # when the capability is enabled, so all four reminder commands reply
            # honestly rather than half-working when it is off.
            reminders=stores.reminders if config.reminders.enabled else None,
            resolver=resolver,
            receipts=receipts,
            reminder_receipts=reminder_receipts,
            inbox_page_size=config.store.inbox_page_size,
        ),
        # Memory recall for the first owner turn of each session (D3).
        recall=MemoryRecall(stores.memories, limit=config.store.recall_render_limit),
        # The per-turn current-time header, composed from the same resolver — so the
        # time the model reasons from and the time the owner is told read identically.
        # None when reminders are disabled, and then owner-turn composition is
        # byte-identical to before this change.
        time_header=_time_header(resolver),
        # The delivered-reminder block: what the scheduler sent, told back to Henk on
        # the owner's next turn. Same repository and resolver as everything else.
        deliveries=_delivery_note(config, stores, resolver),
    )
    dispatcher = Dispatcher(AllowlistFilter(config.owner.id), gate, core)

    coordinator = (
        _build_coordinator(config, core, audit, pipeline, checkpoint, adapter)
        if config.events.enabled
        else None
    )
    # The scheduler is handed `adapter` — the SAME instance the core holds, not a
    # second one over the same bridge. The send lock is instance state, so a second
    # adapter would serialize nothing while passing every serialization test.
    scheduler = (
        ReminderScheduler(
            stores.reminders,
            adapter,
            config=config.reminders,
            resolver=resolver,
            receipts=reminder_receipts,
        )
        if config.reminders.enabled and resolver is not None
        else None
    )
    return (
        App(
            adapter,
            dispatcher,
            core,
            coordinator=coordinator,
            scheduler=scheduler,
        ),
        client,
    )


def _delivery_note(config: Config, stores, resolver: TimeResolver | None):
    """The owner-turn delivered-reminder provider, or None when reminders are off.

    Off means off: with no provider the core's owner-turn composition is byte-identical
    to what it was before this capability existed, which is what makes the disabled
    deploy a genuine no-op rather than a nearly-no-op.
    """
    if not config.reminders.enabled or resolver is None:
        return None
    return DeliveredReminderNote(
        stores.reminders,
        resolver,
        window_seconds=config.reminders.note_window_seconds,
        max_items=config.reminders.note_max_items,
        clock=resolver.current_instant,
    )


def _time_header(resolver: TimeResolver | None):
    """A zero-argument header composer, or None when reminders are disabled.

    Reads the clock once per call, which is once per owner turn — the header has to
    reflect the moment of *this* turn, not the session's start, or a relative time
    the model composes from it means something the owner did not say.
    """
    if resolver is None:
        return None
    return lambda: resolver.time_header(resolver.current_instant())


def _build_pipeline(config: Config) -> EventPipeline:
    ev = config.events
    return EventPipeline(
        PipelineConfig(
            debounce_seconds=ev.debounce_seconds,
            cooldown_seconds=ev.cooldown_seconds,
            recurrence_window_seconds=ev.recurrence_window_seconds,
            cap_per_24h=ev.cap_per_24h,
            cooldown_overrides=ev.cooldown_overrides,
        )
    )


def _build_coordinator(
    config: Config,
    core: AgentCore,
    audit: AuditLog,
    pipeline: EventPipeline,
    checkpoint: OffsetCheckpoint,
    channel: ChannelAdapter,
) -> EventCoordinator:
    ev = config.events
    stream = NtfyEventStream(
        config.ntfy.base_url,
        ev.events_topic,
        token=config.secrets.ntfy_token,
        read_timeout=ev.liveness_deadline_seconds * _READ_TIMEOUT_MULTIPLE,
    )

    async def _notify_since_rejected() -> None:
        # Proactive: an unprompted operator alert, not a reply. Single chunk, so
        # there is nothing to be cut off and no failure notice to supply.
        outcome = await channel.send_proactive(SINCE_REJECTED_NOTICE)
        if outcome != SendOutcome.DELIVERED:
            logger.error(
                "since-rejected notice not delivered (outcome=%s)",
                getattr(outcome, "value", outcome),
            )

    # Seed intake from the durable checkpoint so the first subscribe resumes with
    # since=<offset> and replays events published while Henk was stopped (D1). If
    # the server rejects that cursor, intake replays all retained events rather
    # than retrying a value it can never resume from — and tells the owner.
    #
    # The liveness watchdog: intake abandons a subscription that delivers no
    # proof-of-life frame within the deadline, so a half-open socket stops being
    # observationally identical to a healthy quiet tailnet. Config-driven, and the
    # deadline's ordering against the server's keepalive interval is validated at
    # load time.
    intake = EventIntake(
        stream,
        initial_offset=checkpoint.read(),
        on_since_rejected=_notify_since_rejected,
        liveness_deadline=ev.liveness_deadline_seconds,
        liveness_report_interval=ev.liveness_report_interval_seconds,
    )
    return EventCoordinator(
        intake, pipeline, core, debounce_seconds=ev.debounce_seconds, audit=audit
    )
