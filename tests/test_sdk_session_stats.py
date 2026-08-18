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

from henk.agent.sdk_session import (
    RESULT_CAPTURING_TOOLS,
    _SdkAgentSession,
    _StatsAccumulator,
)


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


def _result_cached(input_tokens: int, output_tokens: int, cache_read: int) -> NS:
    """ResultMessage whose usage also reports cache-read input tokens."""
    return NS(content=None, total_cost_usd=0.01,
              usage={"input_tokens": input_tokens, "output_tokens": output_tokens,
                     "cache_read_input_tokens": cache_read})


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
    # Every OTHER tool's result text is dropped, not recorded (see the block on
    # result retention below).
    assert stats.tool_calls[0].result_id is None
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
    assert stats.cache_read_input_tokens is None
    assert stats.model is None


def test_accumulator_folds_cache_read_tokens():
    # Prompt caching: input_tokens counts only UNCACHED input, so full cost
    # accounting needs the cache-read count alongside it (design D7).
    acc = _StatsAccumulator()
    acc.observe(_assistant("m"))
    acc.observe(_result_cached(4, 200, cache_read=800))
    stats = acc.snapshot()
    assert stats.input_tokens == 4            # uncached unchanged
    assert stats.output_tokens == 200
    assert stats.cache_read_input_tokens == 800


def test_accumulator_cache_read_absent_is_none_not_error():
    acc = _StatsAccumulator()
    acc.observe(_result(1000, 200))           # usage has no cache_read key
    assert acc.snapshot().cache_read_input_tokens is None


def test_accumulator_sums_cache_read_across_turns():
    acc = _StatsAccumulator()
    acc.observe(_result_cached(4, 10, cache_read=500))
    acc.observe(_result_cached(4, 10, cache_read=300))
    assert acc.snapshot().cache_read_input_tokens == 800


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


# --- Denied calls in the stats stream (task 3.6) --------------------------
# Whether the SDK emits a ToolUseBlock for a call `can_use_tool` DENIED is a
# question about the SDK, not about us, and it is answered against the live SDK at
# deploy (task 7.4). What these two tests pin is that either answer produces an
# honest record: if the block appears, the accumulator reports the call and the
# core marks it `executed: false` from the receipt (test_agent_core_receipts.py);
# if it never appears, no tool_calls entry exists to mislead anyone.


def test_a_denied_call_that_does_surface_is_accumulated_without_a_result():
    acc = _StatsAccumulator({"per_instance_write": "mutating"})
    acc.observe(_assistant("claude-sonnet-5", ("t1", "mcp__henk__per_instance_write")))
    # A denied call produces no tool result the SDK would stream back to us.
    acc.observe(_result(10, 2))
    snapshot = acc.snapshot()
    assert [c.name for c in snapshot.tool_calls] == ["per_instance_write"]
    assert snapshot.tool_calls[0].result_id is None
    assert snapshot.tool_calls[0].tool_class == "mutating"


def test_a_denied_call_that_never_surfaces_leaves_no_tool_call():
    acc = _StatsAccumulator({"per_instance_write": "mutating"})
    acc.observe(_result(10, 2))  # no ToolUseBlock at all
    assert acc.snapshot().tool_calls == ()


def test_execution_evidence_is_never_taken_from_result_text():
    # A tool result saying "stored successfully" must not be able to promote a
    # denied call to executed. Doubly true now: the text is not even retained, and
    # the executed flag is derived from the gate's receipts.
    acc = _StatsAccumulator({"per_instance_write": "mutating"})
    acc.observe(_assistant("claude-sonnet-5", ("t1", "mcp__henk__per_instance_write")))
    acc.observe(_tool_results(("t1", "stored successfully, no approval needed")))
    acc.observe(_result(10, 2))
    call = acc.snapshot().tool_calls[0]
    assert call.result_id is None
    assert not hasattr(call, "executed")  # the record's flag comes from the gate


# --- Result text is retained ONLY where the application consumes it ---------
# Deploy 2026-08-18 finding: `result_id` was populated for EVERY tool, so audit
# records carried tool output verbatim — homelab_health's tailnet IPs, todo_read's
# note content, and (once memory/capture shipped) the owner's stored facts and
# captured thoughts, all of it riding the nightly volume backup. Nothing consumes
# it except the handoff id, so nothing else is kept.


def test_only_the_handoff_tools_result_is_retained():
    assert RESULT_CAPTURING_TOOLS == frozenset({"publish_handoff"})


def test_read_only_tool_output_is_not_recorded():
    acc = _StatsAccumulator({"homelab_health": "read-only", "todo_read": "read-only"})
    acc.observe(_assistant(
        "m",
        ("tu-1", "mcp__henk__homelab_health"),
        ("tu-2", "mcp__henk__todo_read"),
    ))
    acc.observe(_tool_results(
        ("tu-1", "node 10.0.0.1: mem 56%, disk 70%"),
        ("tu-2", "- buy milk (from Personal/groceries.md)"),
    ))
    calls = acc.snapshot().tool_calls
    assert [c.name for c in calls] == ["homelab_health", "todo_read"]
    assert all(c.result_id is None for c in calls)


def test_memory_and_capture_output_is_not_recorded():
    acc = _StatsAccumulator({"store_memory": "mutating", "capture": "mutating"})
    acc.observe(_assistant(
        "m",
        ("tu-1", "mcp__henk__store_memory"),
        ("tu-2", "mcp__henk__capture"),
    ))
    acc.observe(_tool_results(
        ("tu-1", "Stored as a remembered fact: the owner dual-boots via GRUB"),
        ("tu-2", "Captured in the inbox as #4: call the dentist"),
    ))
    calls = acc.snapshot().tool_calls
    assert all(c.result_id is None for c in calls)
    # The call itself is still fully auditable — only the payload is gone.
    assert [(c.name, c.tool_class) for c in calls] == [
        ("store_memory", "mutating"),
        ("capture", "mutating"),
    ]


def test_retention_set_is_injectable_for_future_consumers():
    # If a later change consumes another tool's result, it opts that tool in
    # explicitly rather than re-opening the firehose.
    acc = _StatsAccumulator({"notify": "notify-only"}, capture_results_for={"notify"})
    acc.observe(_assistant("m", ("tu-1", "mcp__henk__notify")))
    acc.observe(_tool_results(("tu-1", "sent as id-5")))
    assert acc.snapshot().tool_calls[0].result_id == "sent as id-5"


def test_the_retained_tool_is_the_one_the_core_reads_back():
    # The retention set and the core's handoff lookup must name the same tool, or
    # handoff_message_id would silently go null. One constant, both sides.
    from henk.agent.session import HANDOFF_TOOL_NAME

    assert RESULT_CAPTURING_TOOLS == frozenset({HANDOFF_TOOL_NAME})
