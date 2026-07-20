"""taiga_read — read-only Taiga access.

Uses the Taiga REST API's read endpoints directly (the design-D4 fallback, taken
because the MCP transport availability is a deploy-time probe, task 1.5). The
read-only posture is identical either way: only ``get_*`` / ``list_*`` operations
exist here. There is no code path to any create/update/assign operation, so a
write attempt fails before any request reaches Taiga.
"""

from __future__ import annotations

import logging

import httpx

from henk.tools.base import Tool, ToolClass, ToolResult

logger = logging.getLogger("henk.tools.taiga_read")

# operation name -> (path template, whether project_id is required)
_READ_OPERATIONS: dict[str, tuple[str, bool]] = {
    "list_projects": ("/api/v1/projects", False),
    "list_user_stories": ("/api/v1/userstories", True),
    "list_tasks": ("/api/v1/tasks", True),
    "list_issues": ("/api/v1/issues", True),
    "get_user_story": ("/api/v1/userstories/{id}", False),
}


class TaigaReadTool(Tool):
    name = "taiga_read"
    description = (
        "Read Taiga board data (projects, user stories, tasks, issues). "
        "Read-only: no create/update/assign operations exist."
    )
    tool_class = ToolClass.READ_ONLY
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": sorted(_READ_OPERATIONS),
                "description": "Which read operation to run.",
            },
            "project_id": {
                "type": "integer",
                "description": "Project id (required for list_user_stories/tasks/issues).",
            },
            "id": {
                "type": "integer",
                "description": "Object id (required for get_user_story).",
            },
        },
        "required": ["operation"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        token: str = "",
        timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def _run(  # type: ignore[override]
        self,
        operation: str,
        project_id: int | None = None,
        id: int | None = None,
    ) -> ToolResult:
        if operation not in _READ_OPERATIONS:
            # Write-capable or unknown operation: reject before any HTTP request.
            return ToolResult.failure(
                f"unsupported operation {operation!r}; taiga_read is read-only "
                f"(allowed: {', '.join(sorted(_READ_OPERATIONS))})"
            )

        path_template, needs_project = _READ_OPERATIONS[operation]
        params: dict[str, int] = {}
        if needs_project:
            if project_id is None:
                return ToolResult.failure(f"{operation} requires project_id")
            params["project"] = project_id
        if "{id}" in path_template:
            if id is None:
                return ToolResult.failure(f"{operation} requires id")
            path_template = path_template.format(id=id)

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            resp = await self._client.get(
                f"{self._base_url}{path_template}",
                params=params,
                headers=headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            return ToolResult.failure(f"Taiga timed out after {self._timeout:.0f}s")
        except httpx.HTTPStatusError as exc:
            return ToolResult.failure(
                f"Taiga returned HTTP {exc.response.status_code}"
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult.failure(f"Taiga request failed: {exc}")

        return ToolResult.success(self._summarize(operation, data))

    @staticmethod
    def _summarize(operation: str, data: object) -> str:
        if isinstance(data, list):
            lines = [f"{operation}: {len(data)} item(s)"]
            for item in data:
                if isinstance(item, dict):
                    subject = item.get("subject") or item.get("name") or item.get("id")
                    status = item.get("status_extra_info", {})
                    status_name = (
                        status.get("name") if isinstance(status, dict) else None
                    )
                    lines.append(
                        f"- {subject}" + (f" [{status_name}]" if status_name else "")
                    )
            return "\n".join(lines)
        if isinstance(data, dict):
            subject = data.get("subject") or data.get("name") or data.get("id")
            return f"{operation}: {subject}"
        return f"{operation}: {data!r}"
