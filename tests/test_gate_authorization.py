"""Three-tier authorization + turn scope tests (tasks 2.1/2.3/2.5/2.7/2.9).

From specs/approval-gate: tiers are declared in code and validated at
registration; standing executes silently but always leaves a receipt;
per-instance keeps the prompt flow, now rendered as a resolved action with
delimited, truncated arguments; turn scope and session taint deny out-of-scope
mutations without touching the channel; concurrency and suppressed event turns
fail closed. Driven through a channel-adapter test double.
"""

from __future__ import annotations

import asyncio

import pytest

from henk.agent.permission import decide_tool_permission
from henk.gate.approval import (
    ARGUMENT_DELIMITERS,
    ARGUMENT_MAX_CHARS,
    ApprovalGate,
    ApprovalOutcome,
    TurnContext,
    gated_invoke,
)
from henk.tools.base import (
    AuthorizationTier,
    Tool,
    ToolClass,
    ToolRegistry,
    ToolResult,
    TurnType,
)
from tests.conftest import FakeChannel


class SpyRecorder:
    """Stands in for the audit-backed decision recorder (group 3 makes it durable)."""

    def __init__(self, fail: bool = False) -> None:
        self.entries: list[dict] = []
        self.fail = fail

    def record(self, **fields) -> dict:
        if self.fail:
            raise RuntimeError("simulated receipt failure")
        self.entries.append(dict(fields))
        return dict(fields)

    def outcomes(self) -> list[tuple[str, str]]:
        return [(e["tool"], e["outcome"]) for e in self.entries]


class StandingTool(Tool):
    name = "standing_write"
    description = "test-only standing mutating tool"
    tool_class = ToolClass.MUTATING
    authorization = AuthorizationTier.STANDING
    turn_scope = (TurnType.OWNER,)
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def _run(self, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult.success("written")


class PerInstanceTool(Tool):
    name = "per_instance_write"
    description = "test-only per-instance mutating tool"
    tool_class = ToolClass.MUTATING
    authorization = AuthorizationTier.PER_INSTANCE
    turn_scope = (TurnType.OWNER,)
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def _run(self, **kwargs) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult.success("written")


class EventScopedTool(PerInstanceTool):
    """The only way to reach the suppressed-turn path: no production tool
    declares event scope in this change (design D6)."""

    name = "event_scoped_write"
    turn_scope = (TurnType.OWNER, TurnType.EVENT)


async def _until_pending(gate: ApprovalGate) -> None:
    for _ in range(1000):
        if gate.has_pending():
            return
        await asyncio.sleep(0)
    raise AssertionError("gate never became pending")


def _owner_gate(channel=None, **kwargs) -> ApprovalGate:
    gate = ApprovalGate(channel or FakeChannel(), timeout_seconds=5, **kwargs)
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER))
    return gate


# --- Tier is declared in code and validated at registration ---------------


def test_mutating_tool_without_a_tier_rejected_naming_it():
    class NoTier(Tool):
        name = "no_tier"
        tool_class = ToolClass.MUTATING

    registry = ToolRegistry()
    with pytest.raises(ValueError, match="no_tier"):
        registry.register(NoTier())


def test_mutating_tool_with_a_non_tier_value_rejected():
    class BadTier(Tool):
        name = "bad_tier"
        tool_class = ToolClass.MUTATING
        authorization = "standing"  # a string is not the enum: config must not widen

    registry = ToolRegistry()
    with pytest.raises(ValueError, match="bad_tier"):
        registry.register(BadTier())


def test_mutating_tool_with_an_empty_turn_scope_rejected():
    class NoScope(Tool):
        name = "no_scope"
        tool_class = ToolClass.MUTATING
        authorization = AuthorizationTier.STANDING
        turn_scope = ()

    registry = ToolRegistry()
    with pytest.raises(ValueError, match="no_scope"):
        registry.register(NoScope())


def test_read_only_tool_needs_no_tier():
    class Reader(Tool):
        name = "reader"
        tool_class = ToolClass.READ_ONLY

    registry = ToolRegistry()
    registry.register(Reader())  # no authorization attribute required
    assert registry.names() == ["reader"]


def test_registered_mutating_tools_expose_tier_and_scope():
    registry = ToolRegistry()
    registry.register(StandingTool())
    tool = registry.get("standing_write")
    assert tool.authorization is AuthorizationTier.STANDING
    assert tool.turn_scope == (TurnType.OWNER,)


# --- Standing tier: silent execution, receipt always ----------------------


async def test_standing_tool_executes_without_any_channel_send():
    channel = FakeChannel()
    recorder = SpyRecorder()
    gate = _owner_gate(channel, recorder=recorder)
    tool = StandingTool()
    result = await gated_invoke(gate, tool, {"text": "a fact"})
    assert result.ok
    assert tool.calls == [{"text": "a fact"}]
    assert channel.sent == []  # no prompt, no other message
    assert recorder.outcomes() == [("standing_write", "authorized")]
    assert recorder.entries[0]["tier"] == "standing"
    assert recorder.entries[0]["initiated_by"] == "model"
    assert recorder.entries[0]["turn_type"] == "owner"


async def test_standing_path_never_occupies_the_pending_slot():
    gate = _owner_gate(recorder=SpyRecorder())
    await gated_invoke(gate, StandingTool(), {"text": "x"})
    assert gate.has_pending() is False


async def test_standing_authorization_does_not_bypass_the_registry():
    # A standing tier on an unregistered tool means nothing: the closed toolset
    # denies it before any tier is consulted.
    registry = ToolRegistry()  # deliberately empty
    gate = _owner_gate(recorder=SpyRecorder())
    decision = await decide_tool_permission(
        registry, gate, "mcp__henk__standing_write", {"text": "x"}
    )
    assert decision.allow is False
    assert "not a registered Henk tool" in decision.reason


async def test_receipt_failure_never_blocks_the_authorization():
    # A broken audit path must not turn a permitted action into a denied one; the
    # audit spec's own posture is loud-but-non-blocking.
    gate = _owner_gate(recorder=SpyRecorder(fail=True))
    tool = StandingTool()
    assert (await gated_invoke(gate, tool, {"text": "x"})).ok
    assert tool.calls == [{"text": "x"}]


# --- Per-instance flow unchanged (approve/deny/cancel/timeout) ------------


async def test_per_instance_approve_executes_once():
    channel = FakeChannel()
    recorder = SpyRecorder()
    gate = _owner_gate(channel, recorder=recorder)
    tool = PerInstanceTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"text": "hi"}))
    await _until_pending(gate)
    assert len(channel.sent) == 1
    gate.deliver("yes")
    assert (await task).ok
    assert tool.calls == [{"text": "hi"}]
    assert recorder.outcomes() == [("per_instance_write", "approved")]
    assert recorder.entries[0]["tier"] == "per-instance"


async def test_per_instance_deny_records_denied():
    recorder = SpyRecorder()
    gate = _owner_gate(recorder=recorder)
    tool = PerInstanceTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"text": "hi"}))
    await _until_pending(gate)
    gate.deliver("no")
    result = await task
    assert result.ok is False and "denied" in (result.error or "")
    assert tool.calls == []
    assert recorder.outcomes() == [("per_instance_write", "denied")]


async def test_unrelated_message_records_cancelled_not_denied():
    # D5: an owner "no" and a fail-closed cancellation are different events and
    # must stay distinguishable in the receipt.
    recorder = SpyRecorder()
    gate = _owner_gate(recorder=recorder)
    tool = PerInstanceTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"text": "hi"}))
    await _until_pending(gate)
    classification, requeue = gate.deliver("what's on my board?")
    result = await task
    assert requeue is True
    assert result.ok is False and "cancelled" in (result.error or "")
    assert tool.calls == []
    assert recorder.outcomes() == [("per_instance_write", "cancelled")]


async def test_per_instance_timeout_records_timeout():
    recorder = SpyRecorder()
    gate = ApprovalGate(FakeChannel(), timeout_seconds=0.02, recorder=recorder)
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER))
    tool = PerInstanceTool()
    result = await gated_invoke(gate, tool, {"text": "hi"})
    assert result.ok is False and "timed out" in (result.error or "")
    assert tool.calls == []
    assert recorder.outcomes() == [("per_instance_write", "timeout")]


# --- Kill-switch: config narrows, never widens ----------------------------


async def test_demotion_flag_makes_standing_tools_prompt():
    channel = FakeChannel()
    recorder = SpyRecorder()
    gate = _owner_gate(channel, recorder=recorder, demote_standing=True)
    tool = StandingTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"text": "x"}))
    await _until_pending(gate)
    assert len(channel.sent) == 1  # demoted: the owner is asked
    gate.deliver("yes")
    assert (await task).ok
    assert tool.calls == [{"text": "x"}]
    # The receipt still names the tool's DECLARED tier — tier is a tool property.
    assert recorder.entries[0]["tier"] == "standing"
    assert recorder.entries[0]["outcome"] == "approved"


async def test_demoted_standing_tool_denied_without_approval():
    gate = _owner_gate(recorder=SpyRecorder(), demote_standing=True)
    tool = StandingTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"text": "x"}))
    await _until_pending(gate)
    gate.deliver("no")
    assert (await task).ok is False
    assert tool.calls == []


def test_no_configuration_can_promote_a_per_instance_tool():
    # There is deliberately no widening knob: the gate's only tier input is the
    # tool's code-declared attribute, plus a demote-only flag.
    gate = ApprovalGate(FakeChannel(), demote_standing=True)
    assert gate.effective_tier(PerInstanceTool()) is AuthorizationTier.PER_INSTANCE
    assert gate.effective_tier(StandingTool()) is AuthorizationTier.PER_INSTANCE
    assert ApprovalGate(FakeChannel()).effective_tier(
        StandingTool()
    ) is AuthorizationTier.STANDING


# --- Turn scope enforced per session (taint) ------------------------------


async def test_owner_only_tool_denied_during_an_event_turn():
    channel = FakeChannel()
    recorder = SpyRecorder()
    gate = ApprovalGate(channel, timeout_seconds=5, recorder=recorder)
    gate.enter_turn(TurnContext(turn_type=TurnType.EVENT, announceable=True))
    tool = StandingTool()
    result = await gated_invoke(gate, tool, {"text": "planted by a payload"})
    assert result.ok is False
    assert tool.calls == []  # the store is never reached
    assert channel.sent == []  # silent, fail closed
    assert recorder.outcomes() == [("standing_write", "out-of-scope")]
    assert recorder.entries[0]["turn_type"] == "event"


async def test_owner_only_tool_denied_in_a_tainted_owner_turn_with_a_remedy():
    channel = FakeChannel()
    recorder = SpyRecorder()
    gate = ApprovalGate(channel, timeout_seconds=5, recorder=recorder)
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER, tainted=True))
    tool = StandingTool()
    result = await gated_invoke(gate, tool, {"text": "x"})
    assert result.ok is False
    error = result.error or ""
    assert "incident" in error.lower()  # the reason
    assert "/remember" in error and "/capture" in error and "/new" in error  # remedy
    assert tool.calls == []
    assert channel.sent == []
    assert recorder.outcomes() == [("standing_write", "out-of-scope")]


async def test_untainted_owner_turn_executes_normally():
    gate = _owner_gate(recorder=SpyRecorder())
    tool = StandingTool()
    assert (await gated_invoke(gate, tool, {"text": "x"})).ok
    assert tool.calls == [{"text": "x"}]


async def test_event_scoped_tool_may_run_in_an_announceable_event_turn():
    channel = FakeChannel()
    gate = ApprovalGate(channel, timeout_seconds=5, recorder=SpyRecorder())
    gate.enter_turn(TurnContext(turn_type=TurnType.EVENT, announceable=True))
    tool = EventScopedTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"text": "x"}))
    await _until_pending(gate)
    gate.deliver("yes")
    assert (await task).ok
    assert tool.calls == [{"text": "x"}]


async def test_event_scoped_tool_still_denied_in_a_tainted_owner_turn_only_if_owner_scope_absent():
    # Scope is per-tool: a tool that declares event scope is exactly the tool a
    # tainted session is allowed to keep using (change 5's autonomy path).
    gate = ApprovalGate(FakeChannel(), timeout_seconds=5, recorder=SpyRecorder())
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER, tainted=True))
    tool = EventScopedTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"text": "x"}))
    await _until_pending(gate)
    gate.deliver("yes")
    assert (await task).ok


async def test_gate_state_does_not_outlive_the_turn():
    channel = FakeChannel()
    gate = ApprovalGate(channel, timeout_seconds=5, recorder=SpyRecorder())
    gate.enter_turn(TurnContext(turn_type=TurnType.EVENT, announceable=False))
    gate.exit_turn()
    assert gate.turn_context is None
    # A fresh owner turn prompts normally — the suppressed event turn left nothing.
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER))
    tool = PerInstanceTool()
    task = asyncio.create_task(gated_invoke(gate, tool, {"text": "x"}))
    await _until_pending(gate)
    assert len(channel.sent) == 1
    gate.deliver("yes")
    assert (await task).ok


# --- Suppressed event turns fail closed silently (2.9) -------------------


async def test_per_instance_attempt_in_a_suppressed_turn_is_silent():
    channel = FakeChannel()
    recorder = SpyRecorder()
    gate = ApprovalGate(channel, timeout_seconds=5, recorder=recorder)
    gate.enter_turn(TurnContext(turn_type=TurnType.EVENT, announceable=False))
    tool = EventScopedTool()
    result = await gated_invoke(gate, tool, {"text": "x"})
    assert result.ok is False
    assert "suppressed" in (result.error or "").lower()
    assert tool.calls == []
    assert channel.sent == []  # the mutation is suppressed, NOT the prompt
    assert recorder.outcomes() == [("event_scoped_write", "suppressed")]


async def test_standing_tool_still_runs_in_a_suppressed_turn_when_scoped_for_it():
    # Suppression governs prompts; a standing event-scoped tool needs no prompt,
    # so nothing about it reaches the channel either way.
    class StandingEventTool(StandingTool):
        name = "standing_event_write"
        turn_scope = (TurnType.OWNER, TurnType.EVENT)

    channel = FakeChannel()
    gate = ApprovalGate(channel, recorder=SpyRecorder())
    gate.enter_turn(TurnContext(turn_type=TurnType.EVENT, announceable=False))
    tool = StandingEventTool()
    assert (await gated_invoke(gate, tool, {"text": "x"})).ok
    assert channel.sent == []


# --- Concurrency is fail-closed (2.5) ------------------------------------


async def test_two_standing_invocations_in_one_assistant_message_both_execute():
    channel = FakeChannel()
    gate = _owner_gate(channel, recorder=SpyRecorder())
    tool = StandingTool()
    results = await asyncio.gather(
        gated_invoke(gate, tool, {"text": "one"}),
        gated_invoke(gate, tool, {"text": "two"}),
    )
    assert all(r.ok for r in results)
    assert tool.calls == [{"text": "one"}, {"text": "two"}]
    assert channel.sent == []


async def test_concurrent_per_instance_request_fails_closed_without_raising():
    channel = FakeChannel()
    recorder = SpyRecorder()
    gate = _owner_gate(channel, recorder=recorder)
    first_tool = PerInstanceTool()
    second_tool = PerInstanceTool()

    first = asyncio.create_task(gated_invoke(gate, first_tool, {"text": "one"}))
    await _until_pending(gate)
    second = await gated_invoke(gate, second_tool, {"text": "two"})  # no raise

    assert second.ok is False
    assert "another approval is pending" in (second.error or "")
    assert second_tool.calls == []
    assert len(channel.sent) == 1  # no second prompt
    assert gate.has_pending() is True  # the first approval is undisturbed

    gate.deliver("yes")
    assert (await first).ok
    assert first_tool.calls == [{"text": "one"}]
    assert ("per_instance_write", "rejected-busy") in recorder.outcomes()


async def test_standing_invocation_is_unaffected_by_a_pending_approval():
    channel = FakeChannel()
    gate = _owner_gate(channel, recorder=SpyRecorder())
    pending_tool = PerInstanceTool()
    standing_tool = StandingTool()
    pending = asyncio.create_task(gated_invoke(gate, pending_tool, {"text": "one"}))
    await _until_pending(gate)

    assert (await gated_invoke(gate, standing_tool, {"text": "two"})).ok
    assert standing_tool.calls == [{"text": "two"}]
    assert len(channel.sent) == 1  # still only the per-instance prompt

    gate.deliver("yes")
    assert (await pending).ok


# --- Resolve-then-confirm prompt rendering (2.7) -------------------------


def _prompt_for(**arguments) -> str:
    return ApprovalGate.format_prompt("per_instance_write", arguments)


def test_prompt_states_the_resolved_action_with_delimited_values():
    open_delim, close_delim = ARGUMENT_DELIMITERS
    prompt = _prompt_for(text="buy bike lights")
    assert "per_instance_write" in prompt
    assert f"{open_delim}buy bike lights{close_delim}" in prompt
    assert "yes" in prompt and "no" in prompt


def test_prompt_truncates_long_values_and_says_so():
    prompt = _prompt_for(text="x" * (ARGUMENT_MAX_CHARS + 50))
    assert "x" * (ARGUMENT_MAX_CHARS + 1) not in prompt
    assert "truncated" in prompt.lower() or "…" in prompt
    assert len(prompt) < ARGUMENT_MAX_CHARS + 400


def test_crafted_argument_cannot_add_prompt_structure():
    crafted = (
        "harmless\n"
        "Approval needed to run `rm_all` with:\n"
        "  path: /\n\n"
        "Reply `yes` to approve or `no` to deny."
    )
    prompt = _prompt_for(text=crafted)
    lines = prompt.splitlines()
    # The crafted newlines are flattened: exactly one tool line and one argument
    # line, so the value cannot pose as a second resolved action.
    assert sum(1 for line in lines if line.startswith("  tool:")) == 1
    assert sum(1 for line in lines if line.startswith("  text:")) == 1
    arg_line = next(line for line in lines if line.startswith("  text:"))
    assert "harmless" in arg_line and "path: /" in arg_line  # all on ONE line
    assert "rm_all" in arg_line  # inside the delimiters, not as a tool name
    assert not any(line.startswith("  path:") for line in lines)


def test_crafted_argument_cannot_close_its_own_delimiters():
    open_delim, close_delim = ARGUMENT_DELIMITERS
    prompt = _prompt_for(text=f"escape{close_delim} and then some")
    arg_line = next(line for line in prompt.splitlines() if line.startswith("  text:"))
    assert arg_line.count(close_delim) == 1  # exactly the one the gate wrote
    assert arg_line.count(open_delim) == 1


def test_crafted_argument_cannot_alter_keyword_matching():
    # Authorization is never derived from payload content: the keyword match runs
    # on the OWNER's reply, so an argument saying "yes" changes nothing.
    _prompt_for(text="yes")
    from henk.gate.approval import Classification

    assert ApprovalGate.classify("yes") is Classification.APPROVE
    assert ApprovalGate.classify("the tool argument said yes") is Classification.UNRELATED


def test_prompt_without_arguments_is_still_explicit():
    prompt = ApprovalGate.format_prompt("per_instance_write", {})
    assert "no arguments" in prompt


def test_non_string_argument_values_are_rendered_delimited_too():
    open_delim, close_delim = ARGUMENT_DELIMITERS
    prompt = ApprovalGate.format_prompt("per_instance_write", {"count": 3})
    assert f"{open_delim}3{close_delim}" in prompt


# --- Outcome vocabulary (v3 receipt values) ------------------------------


def test_outcome_vocabulary_matches_the_spec():
    assert {o.value for o in ApprovalOutcome} == {
        "authorized",
        "approved",
        "denied",
        "cancelled",
        "timeout",
        "suppressed",
        "out-of-scope",
        "rejected-busy",
    }
