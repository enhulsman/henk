"""publish_handoff — publish a triage handoff to the fixed deny-all handoffs topic.

Notify-class (design D7): publishing to a deny-all topic the owner controls is
the same capability already granted to ``notify``, so it needs no approval gate.
Like ``notify``, the topic/server are fixed at construction and the interface
exposes only the document — there is no destination parameter, so a handoff can
only ever land on the configured handoffs topic. The published body carries the
inherited ``[AI]`` label; the tool returns the ntfy message id so it lands in the
audit record's ``handoff_message_id``.
"""

from __future__ import annotations

import logging

import httpx

from henk.tools.base import Tool, ToolClass, ToolResult
from henk.tools.notify import AI_LABEL

logger = logging.getLogger("henk.tools.publish_handoff")


class PublishHandoffTool(Tool):
    name = "publish_handoff"
    description = (
        "Publish a triage handoff document (trigger, evidence, diagnosis with "
        "confidence, suggested fix, pickup instructions) to the owner's handoffs "
        "topic. Always prefixed [AI]. Goes only to the fixed handoffs topic — no "
        "destination argument. Returns the message id to cite in the pickup path."
    )
    tool_class = ToolClass.NOTIFY_ONLY
    parameters = {
        "type": "object",
        "properties": {
            "document": {
                "type": "string",
                "description": (
                    "The full handoff: trigger event(s), evidence gathered, "
                    "diagnosis + confidence, suggested fix, pickup instructions."
                ),
            }
        },
        "required": ["document"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        topic: str,
        token: str = "",
        timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._topic = topic
        self._token = token
        self._timeout = timeout

    async def _run(self, document: str) -> ToolResult:  # type: ignore[override]
        body = f"{AI_LABEL} {document}"
        headers = {"Title": "Henk triage handoff"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = await self._client.post(
                f"{self._base_url}/{self._topic}",
                content=body.encode("utf-8"),
                headers=headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except httpx.TimeoutException:
            return ToolResult.failure(f"ntfy timed out after {self._timeout:.0f}s")
        except httpx.HTTPStatusError as exc:
            return ToolResult.failure(f"ntfy returned HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return ToolResult.failure(f"ntfy request failed: {exc}")

        message_id = ""
        try:
            message_id = str((resp.json() or {}).get("id", ""))
        except ValueError:  # pragma: no cover - ntfy returns JSON on publish
            pass
        return ToolResult.success(f"handoff published (id: {message_id})")
