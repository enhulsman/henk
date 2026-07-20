"""todo_read tests (task 4.3), from specs/homelab-tools."""

from __future__ import annotations

import httpx

from henk.tools.base import ToolClass
from henk.tools.todo_read import TodoReadTool


def _make_tool(handler, token: str = "") -> tuple[TodoReadTool, list]:
    calls: list = []

    def recording(request):
        calls.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return TodoReadTool(client, base_url="http://vps:8089", token=token), calls


async def test_todos_fetched():
    def handler(request):
        return httpx.Response(
            200,
            json={"todos": [{"text": "buy milk", "done": False},
                            {"text": "pay rent", "done": True}]},
        )

    tool, _ = _make_tool(handler)
    result = await tool._run()
    assert result.ok
    assert "buy milk" in result.content
    assert "pay rent" in result.content


async def test_only_get_method_used():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"todos": []})

    tool, calls = _make_tool(handler)
    await tool._run()
    assert calls and all(r.method == "GET" for r in calls)


async def test_token_used_as_bearer():
    def handler(request):
        assert request.headers.get("authorization") == "Bearer todo-tk"
        return httpx.Response(200, json={"todos": []})

    tool, _ = _make_tool(handler, token="todo-tk")
    assert (await tool._run()).ok


async def test_timeout_is_honest():
    def handler(request):
        raise httpx.TimeoutException("slow", request=request)

    tool, _ = _make_tool(handler)
    result = await tool._run()
    assert result.ok is False
    assert "timed out" in (result.error or "")


async def test_non_2xx_is_honest():
    def handler(request):
        return httpx.Response(503)

    tool, _ = _make_tool(handler)
    result = await tool._run()
    assert result.ok is False
    assert "503" in (result.error or "")


def test_classification_is_read_only():
    assert TodoReadTool.tool_class is ToolClass.READ_ONLY
