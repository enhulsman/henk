"""Henk's v1 tools plus the production registry builder."""

from __future__ import annotations

import httpx

from henk.config import Config
from henk.tools.base import Tool, ToolClass, ToolRegistry, ToolResult
from henk.tools.homelab_health import HomelabHealthTool
from henk.tools.notify import NotifyTool
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
    "build_production_registry",
]


def build_production_registry(
    config: Config, client: httpx.AsyncClient
) -> ToolRegistry:
    """The v1 production toolset: three read tools + notify. Zero mutating tools."""
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
        TaigaReadTool(
            client,
            base_url=config.taiga.base_url,
            token=config.secrets.taiga_token,
            timeout=config.taiga.timeout_seconds,
        )
    )
    registry.register(
        TodoReadTool(
            client,
            base_url=config.todo.base_url,
            token=config.secrets.todo_token,
            timeout=config.todo.timeout_seconds,
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
    return registry
