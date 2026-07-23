"""Henk's v1 tools plus the production registry builder."""

from __future__ import annotations

import httpx

from henk.config import Config
from henk.tools.base import Tool, ToolClass, ToolRegistry, ToolResult
from henk.tools.homelab_health import HomelabHealthTool
from henk.tools.notify import NotifyTool
from henk.tools.publish_handoff import PublishHandoffTool
from henk.tools.taiga_read import TaigaReadTool
from henk.tools.todo_read import TodoReadTool

__all__ = [
    "Tool",
    "ToolClass",
    "ToolRegistry",
    "ToolResult",
    "HomelabHealthTool",
    "TaigaReadTool",
    "TodoReadTool",
    "NotifyTool",
    "PublishHandoffTool",
    "build_production_registry",
]


def build_production_registry(
    config: Config, client: httpx.AsyncClient
) -> ToolRegistry:
    """The v1 production toolset: homelab_health (read) + notify, plus
    publish_handoff for triage. Zero mutating tools.

    ``taiga_read`` and ``todo_read`` are BOTH deliberately NOT registered: each
    backs onto a store that mixes personal and work/Anamata content, so wiring it
    safely needs source-side scoping plus a client-side allowlist (Tier-W
    posture: never surface work data).

    - ``taiga_read``: the Taiga instance holds mixed personal/work projects; needs
      a dedicated account scoped to personal projects (server-side) + a client-side
      project-id allowlist.
    - ``todo_read``: pulled after 5.3 deploy-verify caught it surfacing work/Anamata
      todos into a triage handoff. It also raw-dumped the whole response — the
      obsidian-todo-api returns a note-grouped dict the tool never parsed — so a
      note-path allowlist plus a rewrite of the summariser are both required.

    Both are deferred to a dedicated personal-data-scoping change; their classes
    and tests are kept for that follow-up.
    """
    registry = ToolRegistry()
    registry.register(
        HomelabHealthTool(
            client,
            gatus_url=config.gatus.base_url,
            prometheus_url=config.prometheus.base_url,
            timeout=config.gatus.timeout_seconds,
        )
    )
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
    return registry
