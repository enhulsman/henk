"""Session-stats accumulation tests (deploy-verify defect follow-up).

The production adapter ``_SdkAgentSession`` shipped without ``stats()``, so the
audit record's ``tool_calls`` / ``usage`` / ``handoff_message_id`` were only ever
populated by test fakes — every real record had them empty/null. These tests pin
the accumulation logic against the real SDK message/block shapes (``ToolUseBlock``
= id/name/input, ``ToolResultBlock`` = tool_use_id/content, ``AssistantMessage``
carries ``model``, ``ResultMessage`` carries the query-total ``usage`` plus a
distinctive ``total_cost_usd``), and drive the adapter with a fake client so the
exact wiring that was missing is covered without the live SDK.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from henk.agent.sdk_session import _SdkAgentSession, _StatsAccumulator


# --- fake SDK message/block factories (shapes verified against claude_agent_sdk) ---

def _text(text: str) -> NS:
    return NS(content=[NS(text=text)])


def _assistant(model: str, *tool_uses: tuple[str, str]) -> NS:
    """AssistantMessage: text + ToolUseBlocks (id, name), carries model."""
    blocks = [NS(id=tid, name=name, input={}) for tid, name in tool_uses]
    return NS(content=blocks, model=model)


def _tool_results(*results) -> NS:
    """UserMessage carrying ToolResultBlocks (tool_use_id, content)."""
    blocks = [NS(tool_use_id=tid, content=content, is_error=False)
              for tid, content in results]
    return NS(content=blocks)


def _result(input_tokens: int, output_tokens: int) -> NS:
    """ResultMessage: query-total usage; total_cost_usd is the discriminator."""
    return NS(content=None, total_cost_usd=0.01,
              usage={"input_tokens": input_tokens, "output_tokens": output_tokens})


class _FakeClient:
    def __init__(self, turns: list[list]) -> None:
        self._turns = list(turns)
        self.connected = False
        self.queries: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def receive_response(self):
        for message in self._turns.pop(0):
            yield message

    async def disconnect(self) -> None:
        self.connected = False


# --- accumulator ---

def test_accumulator_collects_tool_calls_with_class_and_result_id():
    acc = _StatsAccumulator(
        {"homelab_health": "read-only", "publish_handoff": "notify-only"}
    )
    acc.observe(_assistant(
        "claude-sonnet-5",
        ("tu-1", "mcp__henk__homelab_health"),
        ("tu-2", "mcp__henk__publish_handoff"),
    ))
    acc.observe(_tool_results(
        ("tu-1", "all green"),
        ("tu-2", "handoff published (id: hf-99)"),
    ))
    stats = acc.snapshot()

    assert [c.name for c in stats.tool_calls] == ["homelab_health", "publish_handoff"]
    assert [c.tool_class for c in stats.tool_calls] == ["read-only", "notify-only"]
    handoff = stats.tool_calls[1]
    assert handoff.name == "publish_handoff"
    assert "hf-99" in (handoff.result_id or "")  # flows to handoff_message_id
    assert stats.model == "claude-sonnet-5"


def test_accumulator_reads_list_shaped_tool_result_content():
    # The in-process MCP adapter returns content as [{"type":"text","text":...}].
    acc = _StatsAccumulator({"publish_handoff": "notify-only"})
    acc.observe(_assistant("m", ("tu-1", "mcp__henk__publish_handoff")))
    acc.observe(_tool_results(
        ("tu-1", [{"type": "text", "text": "handoff published (id: hf-7)"}]),
    ))
    assert "hf-7" in (acc.snapshot().tool_calls[0].result_id or "")


def test_accumulator_sums_usage_across_turns():
    acc = _StatsAccumulator()
    acc.observe(_assistant("m"))
    acc.observe(_result(1000, 200))
    acc.observe(_assistant("m"))
    acc.observe(_result(500, 100))
    stats = acc.snapshot()
    assert stats.input_tokens == 1500
    assert stats.output_tokens == 300


def test_accumulator_ignores_text_and_missing_usage():
    acc = _StatsAccumulator()
    acc.observe(_text("just talking"))
    stats = acc.snapshot()
    assert stats.tool_calls == ()
    assert stats.input_tokens is None
    assert stats.output_tokens is None
    assert stats.model is None


def test_accumulator_unknown_tool_class_is_none():
    acc = _StatsAccumulator({})  # empty map
    acc.observe(_assistant("m", ("tu-1", "mcp__henk__notify")))
    call = acc.snapshot().tool_calls[0]
    assert call.name == "notify"
    assert call.tool_class is None


# --- adapter wiring (the exact gap: adapter had no stats()) ---

async def test_adapter_run_turn_returns_text_and_populates_stats():
    client = _FakeClient([[
        _assistant("claude-opus-4-8", ("tu-1", "mcp__henk__homelab_health")),
        _tool_results(("tu-1", "all green")),
        _text("Diagnosis: fine (high). Fix: none. Pickup: henk-pickup."),
        _result(1200, 300),
    ]])
    session = _SdkAgentSession(client, tool_classes={"homelab_health": "read-only"})

    reply = await session.run_turn("check health")
    assert "Diagnosis" in reply
    assert client.connected is True and client.queries == ["check health"]

    stats = session.stats()
    assert [c.name for c in stats.tool_calls] == ["homelab_health"]
    assert stats.tool_calls[0].tool_class == "read-only"
    assert stats.model == "claude-opus-4-8"
    assert stats.input_tokens == 1200 and stats.output_tokens == 300


async def test_adapter_stats_accumulate_across_two_turns():
    client = _FakeClient([
        [_assistant("m", ("tu-1", "mcp__henk__homelab_health")),
         _tool_results(("tu-1", "ok")), _text("one"), _result(100, 10)],
        [_assistant("m", ("tu-2", "mcp__henk__publish_handoff")),
         _tool_results(("tu-2", "handoff published (id: hf-2)")),
         _text("two"), _result(50, 5)],
    ])
    session = _SdkAgentSession(
        client, tool_classes={"homelab_health": "read-only",
                              "publish_handoff": "notify-only"}
    )
    await session.run_turn("a")
    await session.run_turn("b")
    stats = session.stats()
    assert [c.name for c in stats.tool_calls] == ["homelab_health", "publish_handoff"]
    assert stats.input_tokens == 150 and stats.output_tokens == 15
