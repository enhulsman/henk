"""Tool base types and the classified tool registry.

Every tool carries an explicit mutation class, and every *mutating* tool
additionally carries the two axes of the permission model (design D4/D10):

- an **authorization tier** — ``standing`` (execute without prompting, receipt
  always) or ``per-instance`` (inline approval, single-use, argument-bound). The
  third tier, ``never``, is the absence of registration: no enum value expresses
  it because the closed-toolset boundary already does.
- a **turn scope** — the turn types the action may run in, defaulting to
  owner-only, enforced by the gate against the turn's context and the session's
  taint.

Both are declared in code beside the tool, never in configuration: a tier or
scope grant is a security decision that must ride code review. The registry
refuses a tool without a classification, and a mutating tool without a tier or
with an empty scope — the approval-gate spec's startup requirements, enforced
structurally rather than by reviewer attention.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ToolClass(str, Enum):
    READ_ONLY = "read-only"
    NOTIFY_ONLY = "notify-only"
    MUTATING = "mutating"


class AuthorizationTier(str, Enum):
    """How a named mutating action is authorized (design D4).

    ``never`` is deliberately absent: an action nobody may take is simply not
    registered, and the default-deny closed-toolset hook denies it.
    """

    STANDING = "standing"
    PER_INSTANCE = "per-instance"


class TurnType(str, Enum):
    """The kinds of turn a tool can declare scope for (design D10).

    ``owner`` turns carry owner-authored input; ``event`` turns carry untrusted
    sensor payloads. A session that has processed an event turn stays tainted for
    its lifetime, so owner turns inside it are treated as event-adjacent for
    write purposes.
    """

    OWNER = "owner"
    EVENT = "event"


#: Mutating tools are owner-turn-only unless they say otherwise, so forgetting to
#: declare a scope fails closed rather than opening the event path.
DEFAULT_TURN_SCOPE: tuple[TurnType, ...] = (TurnType.OWNER,)


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
    (a JSON-schema dict) and implement ``_run``. A mutating subclass must also set
    ``authorization`` and may narrow or widen ``turn_scope``.
    """

    name: str = ""
    description: str = ""
    tool_class: ToolClass | None = None
    parameters: dict[str, Any] = {}
    #: Required for mutating tools; meaningless (and ignored) for the other classes.
    authorization: AuthorizationTier | None = None
    #: Turn types this tool may execute in. Owner-only by default (fail closed).
    turn_scope: tuple[TurnType, ...] = DEFAULT_TURN_SCOPE

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
        if tool.tool_class is ToolClass.MUTATING:
            if not isinstance(getattr(tool, "authorization", None), AuthorizationTier):
                raise ValueError(
                    f"mutating tool {name!r} has no valid authorization tier; set "
                    "authorization to an AuthorizationTier member (a tier grant "
                    "rides code review, so a string or a config value will not do)"
                )
            scope = tuple(getattr(tool, "turn_scope", ()) or ())
            if not scope or not all(isinstance(t, TurnType) for t in scope):
                raise ValueError(
                    f"mutating tool {name!r} has no valid turn scope; set "
                    "turn_scope to a non-empty tuple of TurnType members"
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

    def mutating(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.tool_class is ToolClass.MUTATING]
