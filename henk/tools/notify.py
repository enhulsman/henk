"""notify — notify-only push to a single fixed ntfy topic, always ``[AI]``-labelled.

The topic and server are fixed at construction from config; the tool interface
exposes only ``message``. There is no topic/server/recipient parameter, so the
agent cannot redirect a notification anywhere but the owner's own ntfy topic
(brief constraint 5 / homelab-tools spec).
"""

from __future__ import annotations

import logging

import httpx

from henk.tools.base import Tool, ToolClass, ToolResult

logger = logging.getLogger("henk.tools.notify")

AI_LABEL = "[AI]"


class NotifyTool(Tool):
    name = "notify"
    description = (
        "Send the owner a push notification via ntfy. Always prefixed [AI]. "
        "Goes only to the owner's fixed topic — no destination argument."
    )
    tool_class = ToolClass.NOTIFY_ONLY
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Notification text."}
        },
        "required": ["message"],
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

    async def _run(self, message: str) -> ToolResult:  # type: ignore[override]
        body = f"{AI_LABEL} {message}"
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
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

        return ToolResult.success("notification sent")
