"""taiga_read tests (task 4.2), from specs/homelab-tools."""

from __future__ import annotations

import httpx

from henk.tools.base import ToolClass
from henk.tools.taiga_read import TaigaReadTool


def _make_tool(handler, token: str = "") -> tuple[TaigaReadTool, list]:
    calls: list = []

    def recording(request):
        calls.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return (
        TaigaReadTool(client, base_url="http://rp5:8000", token=token),
        calls,
    )


async def test_board_contents_fetched():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {"subject": "Story A", "status_extra_info": {"name": "In progress"}},
                {"subject": "Story B", "status_extra_info": {"name": "New"}},
            ],
        )

    tool, calls = _make_tool(handler)
    result = await tool._run(operation="list_user_stories", project_id=1)
    assert result.ok
    assert "Story A" in result.content
    assert "In progress" in result.content
    assert calls[0].method == "GET"


async def test_write_operation_impossible_no_request_made():
    def handler(request):  # pragma: no cover - must never be reached
        return httpx.Response(200, json={})

    tool, calls = _make_tool(handler)
    result = await tool._run(operation="create_user_story", project_id=1)
    assert result.ok is False
    assert "read-only" in (result.error or "")
    assert calls == []  # no request reached Taiga


async def test_only_get_requests_used():
    def handler(request):
        return httpx.Response(200, json=[])

    tool, calls = _make_tool(handler)
    await tool._run(operation="list_projects")
    assert all(r.method == "GET" for r in calls)


async def test_token_sent_as_bearer():
    def handler(request):
        assert request.headers.get("authorization") == "Bearer tk"
        return httpx.Response(200, json=[])

    tool, _ = _make_tool(handler, token="tk")
    result = await tool._run(operation="list_projects")
    assert result.ok


async def test_http_error_is_honest():
    def handler(request):
        return httpx.Response(500, json={})

    tool, _ = _make_tool(handler)
    result = await tool._run(operation="list_projects")
    assert result.ok is False
    assert "500" in (result.error or "")


async def test_missing_project_id_rejected_before_request():
    def handler(request):  # pragma: no cover
        return httpx.Response(200, json=[])

    tool, calls = _make_tool(handler)
    result = await tool._run(operation="list_user_stories")
    assert result.ok is False
    assert calls == []


def test_classification_is_read_only():
    assert TaigaReadTool.tool_class is ToolClass.READ_ONLY
