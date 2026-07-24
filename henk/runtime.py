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

from henk.agent.core import AgentCore
from henk.agent.sdk_session import SdkSessionFactory
from henk.app import App, Dispatcher
from henk.audit import AuditLog, read_audit_records
from henk.channel.allowlist import AllowlistFilter
from henk.channel.base import ChannelAdapter
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
from henk.tools import build_production_registry

logger = logging.getLogger("henk.runtime")

#: Bounded tail read for cadence rehydration. Far above any plausible 24h window
#: at Henk's few-events-per-week cadence, so rehydration stays a bounded startup
#: cost even without log rotation. (read_text still loads the file; a seek-based
#: tail is the future optimization if the log ever grows large.)
_REHYDRATE_LIMIT = 10_000


def _checkpoint_path(audit_path: str) -> Path:
    """The intake-offset cursor lives beside the audit log, on the same volume."""
    return Path(audit_path).parent / "intake-offset"


def build_runtime(config: Config) -> tuple[App, httpx.AsyncClient]:
    """Construct the wired App and the shared HTTP client (caller closes it).

    Nothing network-facing is opened here: the Signal bridge and the ntfy event
    stream connect lazily on first receive, and the SDK session is created
    per-conversation on demand.
    """
    client = httpx.AsyncClient()
    registry = build_production_registry(config, client)

    bridge = SignalCliRestBridge(config.signal.bridge_url, config.signal.account)
    adapter = SignalAdapter(
        bridge,
        account=config.signal.account,
        owner=config.owner.id,
        safe_length=config.signal.safe_length,
    )

    # The gate sends approval prompts over the same channel the owner uses.
    gate = ApprovalGate(
        adapter, timeout_seconds=config.agent.approval_timeout_seconds
    )
    factory = SdkSessionFactory(
        registry,
        gate,
        model=config.agent.model,
        system_prompt=config.agent.system_prompt,
    )

    audit = AuditLog(config.events.audit_path) if config.events.enabled else None

    # Durability wiring (design D1/D2): only when events are enabled. The
    # checkpoint store + cadence rehydration read the existing audit volume; both
    # reads are non-fatal if the volume is absent (fresh install / test env).
    checkpoint = None
    pipeline = None
    if config.events.enabled:
        checkpoint = OffsetCheckpoint(_checkpoint_path(config.events.audit_path))
        pipeline = _build_pipeline(config)
        # Reconstruct cooldown/cap/recurrence from the persisted log before the
        # coordinator consumes any event, so a restart does not re-arm cooldowns
        # or reset the daily cap. Best-effort: rehydration must never crash
        # startup, so an unreadable log or an unforeseen record shape logs and
        # falls back to empty cadence state (worst case: one restart re-alerts).
        try:
            pipeline.rehydrate(
                read_audit_records(
                    config.events.audit_path, limit=_REHYDRATE_LIMIT
                ),
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
    )
    dispatcher = Dispatcher(AllowlistFilter(config.owner.id), gate, core)

    coordinator = (
        _build_coordinator(config, core, audit, pipeline, checkpoint, adapter)
        if config.events.enabled
        else None
    )
    return App(adapter, dispatcher, core, coordinator=coordinator), client


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
    audit: AuditLog | None,
    pipeline: EventPipeline,
    checkpoint: OffsetCheckpoint,
    channel: ChannelAdapter,
) -> EventCoordinator:
    ev = config.events
    stream = NtfyEventStream(
        config.ntfy.base_url, ev.events_topic, token=config.secrets.ntfy_token
    )

    async def _notify_since_rejected() -> None:
        await channel.send(SINCE_REJECTED_NOTICE)

    # Seed intake from the durable checkpoint so the first subscribe resumes with
    # since=<offset> and replays events published while Henk was stopped (D1). If
    # the server rejects that cursor, intake replays all retained events rather
    # than retrying a value it can never resume from — and tells the owner.
    intake = EventIntake(
        stream,
        initial_offset=checkpoint.read(),
        on_since_rejected=_notify_since_rejected,
    )
    return EventCoordinator(
        intake, pipeline, core, debounce_seconds=ev.debounce_seconds, audit=audit
    )
