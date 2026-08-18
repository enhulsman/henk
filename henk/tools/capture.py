"""The capture verb and its read-back, both over the `InboxStore` seam.

`capture` is mutating, **standing**, **owner-turn-only** — the same containment
argument as `store_memory` (design D4): an append-only write into a Henk-local
store that cannot leave the container, denied in event turns and in any turn of a
tainted session, receipted every time. When the planned personal-inbox service
replaces the backend, that tier must be re-litigated: "cannot leave the container"
does not survive the swap (design D1).

`inbox_read` is read-only and drains oldest-first, because an inbox is a queue: the
head must always be visible so the page bound never becomes de-facto eviction.

Neither tool knows anything about SQLite — they speak only append / list / mark
done, which is what makes the backend swappable without touching agent logic.
"""

from __future__ import annotations

import logging

from henk.store import (
    DEFAULT_PAGE_SIZE,
    EmptyContentError,
    StoreError,
    format_created_at,
)
from henk.tools.base import (
    AuthorizationTier,
    Tool,
    ToolClass,
    ToolResult,
    TurnType,
)

logger = logging.getLogger("henk.tools.capture")

#: Upper bound on what one `inbox_read` call may return. The inbox is unbounded by
#: design, so an unclamped limit could pull the entire history into a prompt.
MAX_READ_LIMIT = 200


class CaptureTool(Tool):
    name = "capture"
    description = (
        "Capture a passing thought, task, or idea into the owner's durable inbox "
        "so it is not lost. Use it whenever the owner says something is worth "
        "keeping for later, or asks you to note something down. One thought per "
        "call, in the owner's own words where possible."
    )
    tool_class = ToolClass.MUTATING
    authorization = AuthorizationTier.STANDING
    turn_scope = (TurnType.OWNER,)
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The thought to capture, as one short line.",
            }
        },
        "required": ["text"],
    }

    def __init__(self, inbox, *, source: str = "capture-tool") -> None:
        self._inbox = inbox
        self._source = source

    async def _run(self, text: str = "", **_extra) -> ToolResult:
        try:
            item = self._inbox.append(text, source=self._source)
        except EmptyContentError:
            return ToolResult.failure(
                "nothing was captured: the text was empty. Call this with the "
                "thought to capture."
            )
        except StoreError as exc:
            # Never report a failed capture as success — a thought the owner
            # believes is saved and is not is worse than an honest error.
            logger.error("capture write failed: %s", exc)
            return ToolResult.failure(
                f"nothing was captured: the inbox could not be written ({exc})."
            )
        return ToolResult.success(f"Captured in the inbox as #{item.id}: {item.text}")


class InboxReadTool(Tool):
    name = "inbox_read"
    description = (
        "List the oldest open items in the owner's capture inbox, plus a count of "
        "any newer ones. Read-only: use it to answer questions about what is in "
        "the inbox or what to pick up next."
    )
    tool_class = ToolClass.READ_ONLY
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": (
                    "How many of the oldest open items to return (default 20)."
                ),
            }
        },
    }

    def __init__(self, inbox, *, page_size: int = DEFAULT_PAGE_SIZE) -> None:
        self._inbox = inbox
        self._page_size = page_size

    async def _run(self, limit: int | None = None, **_extra) -> ToolResult:
        effective = self._page_size if limit is None else limit
        try:
            effective = max(1, min(int(effective), MAX_READ_LIMIT))
        except (TypeError, ValueError):
            effective = self._page_size
        try:
            page = self._inbox.list_open(limit=effective)
        except StoreError as exc:
            # An unreadable inbox is not an empty one, and must never be reported
            # as one: the owner would conclude their captures are gone.
            logger.error("inbox_read failed: %s", exc)
            return ToolResult.failure(
                f"the inbox could not be read ({exc}); this is a failure, not an "
                "empty inbox."
            )
        if not page.items:
            return ToolResult.success("The inbox has no open items.")
        lines = [f"{len(page.items)} open inbox item(s), oldest first:"]
        lines.extend(
            f"- #{item.id} (captured {format_created_at(item.created_at)}): "
            f"{item.text}"
            for item in page.items
        )
        if page.newer_remainder:
            lines.append(f"...and {page.newer_remainder} newer item(s) not shown.")
        return ToolResult.success("\n".join(lines))
