"""publish_handoff — publish a triage handoff to the fixed deny-all handoffs topic.

Notify-class (design D7): publishing to a deny-all topic the owner controls is
the same capability already granted to ``notify``, so it needs no approval gate.
Like ``notify``, the topic/server are fixed at construction and the interface
exposes only the document — there is no destination parameter, so a handoff can
only ever land on the configured handoffs topic. The published body carries the
inherited ``[AI]`` label; the tool returns the ntfy message id so it lands in the
audit record's ``handoff_message_id``.

**Retention** (triage-quality design D8). After the topic accepts a publish (a
2xx), the document is also archived in the local store, with the identity keys,
rule keys and nodes of the session's incidents, **when the session was started by
an event**: that is, when the shared incident context is non-empty, which includes
an owner follow-up inside such a session. Those keys come from the application's
context, read here and written only by the core. The model cannot supply them,
because the interface still accepts only the document.

A retention failure is logged at error level and never changes the result: the
publish happened, and reporting a failure would make the model publish twice.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from henk.store.handoffs import format_handoff_result
from henk.tools.base import Tool, ToolClass, ToolResult
from henk.tools.notify import AI_LABEL

if TYPE_CHECKING:  # the events module imports henk.tools; avoid the cycle at runtime
    from henk.events.incident_context import IncidentContext, IncidentContextProvider
    from henk.store.handoffs import HandoffStore

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
        archive: "HandoffStore | Any | None" = None,
        incident_context: "IncidentContextProvider | None" = None,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._topic = topic
        self._token = token
        self._timeout = timeout
        # Both are optional: without either, nothing is ever retained, which is
        # exactly the pre-archive behaviour.
        self._archive = archive
        self._incident_context = incident_context

    async def _run(self, document: str) -> ToolResult:  # type: ignore[override]
        # Read the calling session's incidents BEFORE the await, so the handoff
        # is retained with the context it was published under.
        context = (
            self._incident_context.current()
            if self._incident_context is not None
            else None
        )
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
        except ValueError:  # ntfy returns JSON on publish; a 204 would not
            pass
        # Only reached after raise_for_status(): a failed publish retains nothing.
        self._retain(document, message_id, context)
        return ToolResult.success(format_handoff_result(message_id))

    def _retain(
        self, document: str, message_id: str, context: "IncidentContext | None"
    ) -> None:
        """Archive a published handoff; never raises, never changes the result."""
        if self._archive is None or context is None or context.empty:
            # No archive wired, or a session no event started: its handoff has no
            # incident keys and could never relate to a later incident.
            return
        try:
            self._archive.retain(
                document,
                message_id=message_id or None,
                identity_keys=context.identity_keys,
                rule_keys=context.rule_keys,
                nodes=context.nodes,
            )
        except Exception:
            # The id only, never the document: handoff text can carry memory
            # content, and logs are not where it belongs.
            logger.error(
                "handoff %s was published but could not be retained locally",
                message_id or "(no id)",
                exc_info=True,
            )
