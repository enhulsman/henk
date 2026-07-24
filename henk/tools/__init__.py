"""Henk's v1 tools plus the production registry builder."""

from __future__ import annotations

import logging

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

logger = logging.getLogger("henk.tools")


def build_production_registry(
    config: Config, client: httpx.AsyncClient
) -> ToolRegistry:
    """The production toolset: homelab_health + todo_read (read), notify, plus
    publish_handoff for triage. Zero mutating tools.

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
    return registry
