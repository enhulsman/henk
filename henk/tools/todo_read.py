"""todo_read — read-only todos from obsidian-todo-api (vps:8089), GET only."""

from __future__ import annotations

import logging

import httpx

from henk.tools.base import Tool, ToolClass, ToolResult

logger = logging.getLogger("henk.tools.todo_read")


class TodoReadTool(Tool):
    name = "todo_read"
    description = "Fetch current todos from obsidian-todo-api. Read-only, no arguments."
    tool_class = ToolClass.READ_ONLY
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        token: str = "",
        path: str = "/api/todos",
        timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._path = path if path.startswith("/") else f"/{path}"
        self._timeout = timeout

    async def _run(self) -> ToolResult:  # type: ignore[override]
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            resp = await self._client.get(
                f"{self._base_url}{self._path}",
                headers=headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            return ToolResult.failure(
                f"obsidian-todo-api timed out after {self._timeout:.0f}s"
            )
        except httpx.HTTPStatusError as exc:
            return ToolResult.failure(
                f"obsidian-todo-api returned HTTP {exc.response.status_code}"
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult.failure(f"obsidian-todo-api request failed: {exc}")

        return ToolResult.success(self._summarize(data))

    @staticmethod
    def _summarize(data: object) -> str:
        items = data
        if isinstance(data, dict):
            items = data.get("todos", data.get("items", []))
        if isinstance(items, list):
            lines = [f"{len(items)} todo(s)"]
            for item in items:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("title") or item.get("task")
                    done = item.get("done") or item.get("completed")
                    mark = "x" if done else " "
                    lines.append(f"- [{mark}] {text}")
                else:
                    lines.append(f"- {item}")
            return "\n".join(lines)
        return str(data)
