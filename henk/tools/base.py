"""Tool base types and the classified tool registry.

Every tool carries an explicit mutation class. The registry refuses to register
a tool without one — the approval-gate spec's "unclassified tool rejected at
startup" requirement, enforced structurally.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ToolClass(str, Enum):
    READ_ONLY = "read-only"
    NOTIFY_ONLY = "notify-only"
    MUTATING = "mutating"


@dataclass(frozen=True)
class ToolResult:
    """Outcome of a tool invocation. ``ok`` false always carries an ``error``."""

    ok: bool
    content: str = ""
    error: str | None = None

    @classmethod
    def success(cls, content: str) -> "ToolResult":
        return cls(ok=True, content=content)

    @classmethod
    def failure(cls, error: str) -> "ToolResult":
        return cls(ok=False, content="", error=error)


class Tool:
    """Base class for a Henk tool.

    Subclasses set ``name``, ``description``, ``tool_class``, ``parameters``
    (a JSON-schema dict) and implement ``_run``.
    """

    name: str = ""
    description: str = ""
    tool_class: ToolClass | None = None
    parameters: dict[str, Any] = {}

    async def run(self, **arguments: Any) -> ToolResult:
        return await self._run(**arguments)

    async def _run(self, **arguments: Any) -> ToolResult:  # pragma: no cover
        raise NotImplementedError


class ToolRegistry:
    """Holds classified tools; rejects any tool lacking a valid classification."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = getattr(tool, "name", "") or tool.__class__.__name__
        if not isinstance(getattr(tool, "tool_class", None), ToolClass):
            raise ValueError(
                f"tool {name!r} has no valid mutation classification; "
                "set tool_class to a ToolClass member"
            )
        if name in self._tools:
            raise ValueError(f"duplicate tool name {name!r}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def names(self) -> list[str]:
        return list(self._tools)

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def has_mutating(self) -> bool:
        return any(t.tool_class is ToolClass.MUTATING for t in self._tools.values())
