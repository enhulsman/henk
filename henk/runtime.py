"""Production wiring: assemble the full runtime from config.

Kept separate from ``app.py`` (the reusable Dispatcher/App) so the concrete
choices — Signal transport, SDK factory, httpx client — live in one place and
``build_runtime`` can be constructed in a test without connecting anything.
"""

from __future__ import annotations

import httpx

from henk.agent.core import AgentCore
from henk.agent.sdk_session import SdkSessionFactory
from henk.app import App, Dispatcher
from henk.channel.allowlist import AllowlistFilter
from henk.channel.signal import SignalAdapter, SignalCliRestBridge
from henk.config import Config
from henk.gate.approval import ApprovalGate
from henk.tools import build_production_registry


def build_runtime(config: Config) -> tuple[App, httpx.AsyncClient]:
    """Construct the wired App and the shared HTTP client (caller closes it).

    Nothing network-facing is opened here: the Signal bridge connects lazily on
    first receive, and the SDK session is created per-conversation on demand.
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
    core = AgentCore(
        factory, adapter, idle_timeout_seconds=config.agent.idle_timeout_seconds
    )
    dispatcher = Dispatcher(AllowlistFilter(config.owner.id), gate, core)
    return App(adapter, dispatcher, core), client
