"""notify tests (task 4.4), from specs/homelab-tools."""

from __future__ import annotations

import inspect

import httpx
import pytest

from henk.tools.base import ToolClass
from henk.tools.notify import AI_LABEL, NotifyTool


def _make_tool(handler, token: str = "") -> tuple[NotifyTool, list]:
    calls: list = []

    def recording(request):
        calls.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return (
        NotifyTool(client, base_url="http://vps:2586", topic="henk", token=token),
        calls,
    )


async def test_notification_sent_and_labeled():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = request.content.decode()
        return httpx.Response(200)

    tool, _ = _make_tool(handler)
    result = await tool._run(message="disk almost full")
    assert result.ok
    assert captured["body"].startswith(AI_LABEL)
    assert "disk almost full" in captured["body"]
    assert captured["path"] == "/henk"  # fixed topic


def test_interface_has_no_destination_parameter():
    props = NotifyTool.parameters["properties"]
    assert set(props) == {"message"}
    assert NotifyTool.parameters.get("additionalProperties") is False
    # The runtime signature also refuses an alternate destination argument.
    sig = inspect.signature(NotifyTool._run)
    assert list(sig.parameters) == ["self", "message"]


async def test_alternate_destination_impossible_at_runtime():
    tool, _ = _make_tool(lambda r: httpx.Response(200))
    with pytest.raises(TypeError):
        await tool._run(message="hi", topic="other")  # type: ignore[call-arg]


async def test_backend_error_is_honest():
    def handler(request):
        return httpx.Response(500)

    tool, _ = _make_tool(handler)
    result = await tool._run(message="x")
    assert result.ok is False
    assert "500" in (result.error or "")


def test_classification_is_notify_only():
    assert NotifyTool.tool_class is ToolClass.NOTIFY_ONLY
