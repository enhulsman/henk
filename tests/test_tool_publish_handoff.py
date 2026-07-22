"""publish_handoff tests (task 2.3), from specs/triage-handoff.

Notify-class, fixed topic, [AI]-labelled, and — critically — NO destination
parameter: the tool interface exposes only the document, so the agent cannot
redirect a handoff anywhere but the configured handoffs topic. Cap-suppressed
incidents publish the same way (that path is the core's, tested in triage).
"""

from __future__ import annotations

import httpx

from henk.tools.base import ToolClass
from henk.tools.publish_handoff import PublishHandoffTool
from tests.conftest import mock_client


def _tool(handler) -> PublishHandoffTool:
    return PublishHandoffTool(
        mock_client(handler), base_url="http://vps:2586", topic="henk-handoffs",
        token="tok",
    )


def test_is_notify_class():
    tool = _tool(lambda req: httpx.Response(200, json={"id": "hf-1"}))
    assert tool.tool_class is ToolClass.NOTIFY_ONLY


def test_interface_has_no_destination_parameter():
    tool = _tool(lambda req: httpx.Response(200, json={"id": "hf-1"}))
    props = tool.parameters["properties"]
    assert set(props) == {"document"}                    # only the document
    for forbidden in ("topic", "server", "recipient", "url", "to"):
        assert forbidden not in props
    assert tool.parameters.get("additionalProperties") is False


async def test_publishes_to_fixed_topic_with_ai_label():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"id": "hf-99"})

    tool = _tool(handler)
    result = await tool.run(document="trigger; evidence; diagnosis; fix; pickup")
    assert result.ok
    assert seen["url"] == "http://vps:2586/henk-handoffs"  # fixed topic
    assert seen["body"].startswith("[AI]")                 # inherited AI label
    assert seen["auth"] == "Bearer tok"
    assert "hf-99" in result.content                       # message id returned


async def test_http_error_is_reported_not_raised():
    tool = _tool(lambda req: httpx.Response(500))
    result = await tool.run(document="x")
    assert result.ok is False
    assert "500" in (result.error or "")
