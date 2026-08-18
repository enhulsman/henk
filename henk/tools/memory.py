"""The `store_memory` tool: the agent's own write path into memory.

Mutating, **standing** tier, **owner-turn-only**. The standing grant is argued on
containment, not on saved attention (design D4): this is an append-only write into
a capped, Henk-local SQLite store that cannot leave the container, it is denied in
event turns and in any turn of a session an event turn has touched, and every
invocation leaves a durable receipt. Prompting for it would spend owner attention
on a write the owner can undo with one `/forget`.
"""

from __future__ import annotations

import logging

from henk.store import AGENT, ContentTooLongError, EmptyContentError, StoreError
from henk.tools.base import (
    AuthorizationTier,
    Tool,
    ToolClass,
    ToolResult,
    TurnType,
)

logger = logging.getLogger("henk.tools.memory")


class StoreMemoryTool(Tool):
    name = "store_memory"
    description = (
        "Remember one short fact about the owner or their homelab for future "
        "conversations. Use it for durable facts worth recalling later (how "
        "something is set up, a standing preference), not for chit-chat or for "
        "anything the owner said in passing. One fact per call, phrased so it "
        "still makes sense months from now."
    )
    tool_class = ToolClass.MUTATING
    authorization = AuthorizationTier.STANDING
    turn_scope = (TurnType.OWNER,)
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact to remember, as one short sentence.",
            }
        },
        "required": ["content"],
    }

    def __init__(self, memories) -> None:
        self._memories = memories

    async def _run(self, content: str = "", **_extra) -> ToolResult:
        try:
            write = self._memories.add(content, AGENT)
        except EmptyContentError:
            return ToolResult.failure(
                "nothing was stored: the fact was empty. Call this with the text "
                "of the fact to remember."
            )
        except ContentTooLongError as exc:
            return ToolResult.failure(f"nothing was stored: {exc}")
        except StoreError as exc:
            # Never claim success on a failed write: the model would tell the owner
            # something is remembered when it is not.
            logger.error("store_memory write failed: %s", exc)
            return ToolResult.failure(
                f"nothing was stored: the memory store could not be written ({exc})."
            )

        message = f"Stored as a remembered fact: {write.memory.content}"
        if write.evicted:
            evicted = "; ".join(m.content for m in write.evicted)
            message += (
                f"\nThe agent-memory cap was reached, so the oldest fact was "
                f"dropped to make room: {evicted}"
            )
        return ToolResult.success(message)
