"""Production wiring: assemble the full runtime from config.

Kept separate from ``app.py`` (the reusable Dispatcher/App) so the concrete
choices — Signal transport, SDK factory, httpx client, ntfy event stream — live
in one place and ``build_runtime`` can be constructed in a test without
connecting anything.
"""

from __future__ import annotations

import httpx

from henk.agent.core import AgentCore
from henk.agent.sdk_session import SdkSessionFactory
from henk.app import App, Dispatcher
from henk.audit import AuditLog
from henk.channel.allowlist import AllowlistFilter
from henk.channel.signal import SignalAdapter, SignalCliRestBridge
from henk.config import Config
from henk.events.coordinator import EventCoordinator
from henk.events.intake import EventIntake, NtfyEventStream
from henk.events.pipeline import EventPipeline, PipelineConfig
from henk.gate.approval import ApprovalGate
from henk.tools import build_production_registry


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
    core = AgentCore(
        factory,
        adapter,
        idle_timeout_seconds=config.agent.idle_timeout_seconds,
        audit=audit,
        model=config.agent.model,
    )
    dispatcher = Dispatcher(AllowlistFilter(config.owner.id), gate, core)

    coordinator = _build_coordinator(config, core, audit) if config.events.enabled else None
    return App(adapter, dispatcher, core, coordinator=coordinator), client


def _build_coordinator(config: Config, core: AgentCore, audit: AuditLog | None):
    ev = config.events
    stream = NtfyEventStream(
        config.ntfy.base_url, ev.events_topic, token=config.secrets.ntfy_token
    )
    intake = EventIntake(stream)
    pipeline = EventPipeline(
        PipelineConfig(
            debounce_seconds=ev.debounce_seconds,
            cooldown_seconds=ev.cooldown_seconds,
            recurrence_window_seconds=ev.recurrence_window_seconds,
            cap_per_24h=ev.cap_per_24h,
            cooldown_overrides=ev.cooldown_overrides,
        )
    )
    return EventCoordinator(
        intake, pipeline, core, debounce_seconds=ev.debounce_seconds, audit=audit
    )
