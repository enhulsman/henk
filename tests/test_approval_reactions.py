"""approval-reactions group 2: the gate side (tasks 2.1, 2.2, 2.3).

From the approval-gate delta: *Inline approval over the channel* (reactions, the
undeclared-key refusal, the single-message bound), *A decided prompt records its
outcome* (the edit, its ordering, the receipt detail), *Mutating tools declare an
owner-facing label*, and the kill-switch and reminder-delivery scenarios that now
name reactions. Design D2, D4, D6, D7, D8.

Driven through a channel double (``PromptChannel``) that records prompts and edits,
can withhold a prompt's reference or an edit's completion until the test releases
it, and writes every prompt, edit and tool run to one shared ``log`` so ordering is
a list-order assertion. The gate never sees Signal: references here are opaque
strings (``prompt-1``), exactly as the contract promises neutral code.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from henk.agent.core import AgentCore
from henk.agent.permission import decide_tool_permission
from henk.app import Dispatcher
from henk.channel.allowlist import AllowlistFilter
from henk.channel.base import (
    ApprovalPrompt,
    InboundMessage,
    InboundReaction,
    PromptOutcome,
    PromptSent,
    SendOutcome,
)
from henk.gate.approval import (
    ARGUMENT_MAX_CHARS,
    ApprovalGate,
    ApprovalOutcome,
    Classification,
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
from tests.test_gate_authorization import SpyRecorder

OWNER = "+31600000000"
FAIL_FAST = 2.0

THUMBS_UP = "\U0001F44D"
THUMBS_DOWN = "\U0001F44E"
VS16 = "️"
SKIN_TONES = ["\U0001F3FB", "\U0001F3FC", "\U0001F3FD", "\U0001F3FE", "\U0001F3FF"]

#: Every form of 👍 that must approve: bare, with VS16, each skin tone, and a
#: skin tone followed by VS16 (both normalisation steps at once).
APPROVING = (
    [THUMBS_UP, THUMBS_UP + VS16]
    + [THUMBS_UP + tone for tone in SKIN_TONES]
    + [THUMBS_UP + SKIN_TONES[2] + VS16]
)
DENYING = (
    [THUMBS_DOWN, THUMBS_DOWN + VS16]
    + [THUMBS_DOWN + tone for tone in SKIN_TONES]
    + [THUMBS_DOWN + SKIN_TONES[4] + VS16]
)
#: Emoji that are not 👍 or 👎 after normalisation. A doubled thumb and a bare
#: modifier are the shapes a "starts with" or "contains" match would let through.
OTHER_EMOJI = [
    "❤️",  # red heart
    "\U0001F44C",  # OK hand
    "\U0001F602",  # tears of joy
    THUMBS_UP + THUMBS_UP,
    THUMBS_DOWN + THUMBS_UP,
    SKIN_TONES[0],
    VS16,
    "",
    "yes",
]

ARGS = {"title": "Homelab: patch round", "when": "Sat 2026-11-07 14:00"}
LABEL = "Write a test thing"
PROMPT = ApprovalPrompt(
    label=LABEL,
    tool_name="labelled_write",
    fields=(("title", "Homelab: patch round"), ("when", "Sat 2026-11-07 14:00")),
)

VERBS = {
    ApprovalOutcome.APPROVED: "Approved",
    ApprovalOutcome.DENIED: "Denied",
    ApprovalOutcome.CANCELLED: "Cancelled",
    ApprovalOutcome.TIMEOUT: "Timed out",
}


# --- Doubles -----------------------------------------------------------------


class PromptChannel(FakeChannel):
    """A FakeChannel whose prompt and edit can be held, failed or refused.

    ``refs`` (when given) supplies each prompt's reference in turn, ``None``
    included. ``prompt_outcome`` / ``too_large`` shape what ``send_prompt``
    reports; a non-delivered prompt is recorded in ``prompts`` (it was asked for)
    but never in ``sent`` (the owner never saw it). ``prompt_hold`` / ``edit_hold``
    suspend the operation until set. ``pending_at_edit`` records whether the gate
    still held its pending slot when each edit began. ``edit_raises`` makes
    ``edit_prompt`` raise after recording the attempt, as a broken adapter might.
    """

    def __init__(
        self,
        *,
        log: list | None = None,
        refs: list | None = None,
        prompt_outcome: SendOutcome = SendOutcome.DELIVERED,
        too_large: bool = False,
        edit_result: bool = True,
        edit_raises: bool = False,
    ) -> None:
        super().__init__()
        self.log = log if log is not None else []
        self._refs = list(refs) if refs is not None else None
        self.prompt_outcome = prompt_outcome
        self.too_large = too_large
        self.edit_result = edit_result
        self.edit_raises = edit_raises
        self.prompt_hold: asyncio.Event | None = None
        self.edit_hold: asyncio.Event | None = None
        self.prompt_started = asyncio.Event()
        self.edit_started = asyncio.Event()
        self.returned_refs: list[str | None] = []
        self.gate: ApprovalGate | None = None
        self.pending_at_edit: list[bool] = []

    async def send_prompt(self, prompt: ApprovalPrompt) -> PromptSent:
        self.log.append(("prompt", prompt.tool_name))
        self.prompts.append(prompt)
        self.prompt_started.set()
        if self.prompt_hold is not None:
            await self.prompt_hold.wait()
        if self._refs is not None:
            ref = self._refs.pop(0)
        else:
            ref = f"prompt-{len(self.prompts)}"
        if self.prompt_outcome is not SendOutcome.DELIVERED:
            self.returned_refs.append(ref)
            return PromptSent(self.prompt_outcome, ref, too_large=self.too_large)
        self.sent.append(f"[prompt {prompt.tool_name}]")
        self.returned_refs.append(ref)
        return PromptSent(SendOutcome.DELIVERED, ref)

    async def edit_prompt(self, ref: str, outcome: PromptOutcome) -> bool:
        self.log.append(("edit", outcome.verb))
        if self.gate is not None:
            self.pending_at_edit.append(self.gate.has_pending())
        self.edit_started.set()
        if self.edit_hold is not None:
            await self.edit_hold.wait()
        self.edits.append((ref, outcome))
        if self.edit_raises:
            raise RuntimeError("simulated edit failure")
        return self.edit_result


class LabelledTool(Tool):
    name = "labelled_write"
    description = "test-only per-instance mutating tool with a label"
    label = LABEL
    tool_class = ToolClass.MUTATING
    authorization = AuthorizationTier.PER_INSTANCE
    turn_scope = (TurnType.OWNER,)
    parameters = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "when": {"type": "string"}},
    }

    def __init__(self, log: list | None = None) -> None:
        self.calls: list[dict] = []
        self.log = log

    async def _run(self, **kwargs) -> ToolResult:
        if self.log is not None:
            self.log.append(("run", self.name))
        self.calls.append(kwargs)
        return ToolResult.success("written")


class LabelledStandingTool(LabelledTool):
    name = "labelled_standing_write"
    label = "Write a standing thing"
    authorization = AuthorizationTier.STANDING


class LabelledEventTool(LabelledTool):
    name = "labelled_event_write"
    turn_scope = (TurnType.OWNER, TurnType.EVENT)


def _gate(channel: PromptChannel, **kwargs) -> ApprovalGate:
    kwargs.setdefault("timeout_seconds", 5)
    gate = ApprovalGate(channel, **kwargs)
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER))
    channel.gate = gate
    return gate


def _reaction(
    emoji: str = THUMBS_UP,
    target: str | None = "prompt-1",
    *,
    removed: bool = False,
    sender: str = OWNER,
    is_group: bool = False,
) -> InboundReaction:
    return InboundReaction(
        sender=sender, emoji=emoji, target_ref=target, removed=removed, is_group=is_group
    )


async def _until(pred, tries: int = 5000) -> None:
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not met in time")


async def _prompted(gate: ApprovalGate, channel: PromptChannel, count: int = 1) -> None:
    """Wait until the ``count``-th prompt's reference has been handed back and the
    gate is waiting on the owner. A few extra yields let the gate store the ref."""
    await _until(lambda: len(channel.returned_refs) >= count and gate.has_pending())
    for _ in range(5):
        await asyncio.sleep(0)


def _invoke(gate: ApprovalGate, tool: Tool, args: dict | None = None) -> asyncio.Task:
    return asyncio.create_task(gated_invoke(gate, tool, dict(ARGS if args is None else args)))


def _detail(entry: dict) -> str | None:
    return entry.get("detail")


# =============================================================================
# 2.1 Approve and deny by reaction
# =============================================================================


@pytest.mark.parametrize("emoji", APPROVING, ids=[e.encode("unicode_escape").decode() for e in APPROVING])
async def test_a_thumbs_up_on_the_pending_prompt_approves_and_executes_once(emoji):
    # Scenario: Owner approves by reaction, with or without a skin-tone modifier.
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder)
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)

    assert gate.deliver_reaction(_reaction(emoji)) is Classification.APPROVE
    result = await asyncio.wait_for(task, FAIL_FAST)

    assert result.ok
    assert tool.calls == [ARGS]  # exactly once, with the arguments shown
    assert recorder.outcomes() == [("labelled_write", "approved")]
    assert gate.has_pending() is False


@pytest.mark.parametrize("emoji", DENYING, ids=[e.encode("unicode_escape").decode() for e in DENYING])
async def test_a_thumbs_down_on_the_pending_prompt_denies(emoji):
    # Scenario: Owner denies by reaction.
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder)
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)

    assert gate.deliver_reaction(_reaction(emoji)) is Classification.DENY
    result = await asyncio.wait_for(task, FAIL_FAST)

    assert result.ok is False
    assert "denied by owner" in (result.error or "")
    assert tool.calls == []
    assert recorder.outcomes() == [("labelled_write", "denied")]


# =============================================================================
# 2.1 Ignored reactions leave the approval pending
# =============================================================================

IGNORED_CASES = (
    [pytest.param(_reaction(e), id=f"emoji-{e.encode('unicode_escape').decode()}") for e in OTHER_EMOJI]
    + [
        pytest.param(_reaction(THUMBS_UP, "prompt-0"), id="thumbs-up-on-an-earlier-prompt"),
        pytest.param(_reaction(THUMBS_UP, "1761830400000"), id="thumbs-up-on-another-message"),
        pytest.param(_reaction(THUMBS_UP, None), id="thumbs-up-with-no-target"),
        pytest.param(_reaction(THUMBS_DOWN, "prompt-0"), id="thumbs-down-on-an-earlier-prompt"),
        pytest.param(_reaction(THUMBS_UP, removed=True), id="thumbs-up-removed"),
        pytest.param(_reaction(THUMBS_DOWN, removed=True), id="thumbs-down-removed"),
        pytest.param(
            _reaction(THUMBS_UP + SKIN_TONES[1], removed=True), id="toned-thumbs-up-removed"
        ),
    ]
)


@pytest.mark.parametrize("reaction", IGNORED_CASES)
async def test_an_ignored_reaction_leaves_the_approval_pending_and_resolvable(reaction):
    # Scenario: Other reactions are ignored. The approval stays pending, nothing
    # runs, nothing is edited, and a later keyword still resolves it.
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder)
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)

    assert gate.deliver_reaction(reaction) is Classification.IGNORED
    for _ in range(20):
        await asyncio.sleep(0)
    assert gate.has_pending() is True
    assert task.done() is False
    assert tool.calls == []
    assert channel.edits == []
    assert recorder.entries == []

    gate.deliver("yes")
    assert (await asyncio.wait_for(task, FAIL_FAST)).ok
    assert tool.calls == [ARGS]
    assert recorder.outcomes() == [("labelled_write", "approved")]


async def test_a_valid_reaction_still_resolves_after_ignored_ones():
    channel = PromptChannel()
    gate = _gate(channel, recorder=SpyRecorder())
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)

    gate.deliver_reaction(_reaction("❤️"))
    gate.deliver_reaction(_reaction(THUMBS_DOWN, removed=True))
    gate.deliver_reaction(_reaction(THUMBS_DOWN, "prompt-0"))
    assert gate.deliver_reaction(_reaction(THUMBS_UP)) is Classification.APPROVE
    assert (await asyncio.wait_for(task, FAIL_FAST)).ok
    assert tool.calls == [ARGS]


def test_a_reaction_with_nothing_ever_pending_is_ignored():
    # Scenario: A reaction with nothing pending does nothing (never pending).
    channel = PromptChannel()
    gate = _gate(channel)
    assert gate.deliver_reaction(_reaction(THUMBS_UP)) is Classification.IGNORED
    assert gate.has_pending() is False
    assert channel.sent == [] and channel.edits == []


async def test_a_reaction_after_a_timeout_is_ignored():
    # Scenario: A reaction with nothing pending does nothing (timed out).
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder, timeout_seconds=0.02)
    tool = LabelledTool()
    result = await asyncio.wait_for(gated_invoke(gate, tool, dict(ARGS)), FAIL_FAST)
    assert result.ok is False
    edits_before = list(channel.edits)

    assert gate.deliver_reaction(_reaction(THUMBS_UP)) is Classification.IGNORED
    assert tool.calls == []
    assert recorder.outcomes() == [("labelled_write", "timeout")]
    assert channel.edits == edits_before


async def test_a_prompt_sent_without_a_reference_is_logged(caplog):
    # Live on rp5 2026-10-31 a prompt went out without a reference and nothing
    # said so: reactions were ignored and the edit skipped in silence. The gate
    # warns, naming the approval reference and never the channel's.
    channel = PromptChannel(refs=[None])
    gate = _gate(channel, recorder=SpyRecorder())
    tool = LabelledTool()
    with caplog.at_level(logging.INFO, logger="henk.gate"):
        task = _invoke(gate, tool)
        await _prompted(gate, channel)
        gate.deliver("yes")
        assert (await asyncio.wait_for(task, FAIL_FAST)).ok
    warnings = [
        r.getMessage() for r in caplog.records
        if r.name == "henk.gate" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, warnings
    assert "no reference" in warnings[0] and "appr-" in warnings[0]


async def test_a_prompt_sent_with_a_reference_logs_no_warning(caplog):
    channel = PromptChannel()
    gate = _gate(channel, recorder=SpyRecorder())
    tool = LabelledTool()
    with caplog.at_level(logging.INFO, logger="henk.gate"):
        task = _invoke(gate, tool)
        await _prompted(gate, channel)
        gate.deliver("yes")
        assert (await asyncio.wait_for(task, FAIL_FAST)).ok
    assert not [
        r for r in caplog.records
        if r.name == "henk.gate" and r.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize("target", [None, "prompt-1"], ids=["no-target", "some-target"])
async def test_a_prompt_sent_without_a_reference_ignores_every_reaction(target):
    # D2/D4: no ref from the channel means reactions to that prompt are ignored,
    # keywords still work, and it is never edited. The None-target case is the
    # trap: `None == None` would match a reaction with no target to a prompt with
    # no reference.
    channel = PromptChannel(refs=[None])
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder)
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)

    assert gate.deliver_reaction(_reaction(THUMBS_UP, target)) is Classification.IGNORED
    for _ in range(20):
        await asyncio.sleep(0)
    assert gate.has_pending() is True and tool.calls == []

    gate.deliver("yes")
    assert (await asyncio.wait_for(task, FAIL_FAST)).ok
    assert tool.calls == [ARGS]
    assert channel.edits == []  # nothing to edit: the prompt has no reference
    assert _detail(recorder.entries[0]) == "answered by keyword"


async def test_a_reaction_that_arrives_before_the_reference_is_stored_is_ignored():
    # D4 race: the phone shows the prompt before the bridge's response returns.
    # The reaction names the right message, but the gate does not know that yet;
    # it fails closed, and a later valid reaction approves.
    channel = PromptChannel()
    channel.prompt_hold = asyncio.Event()
    gate = _gate(channel, recorder=SpyRecorder())
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await asyncio.wait_for(channel.prompt_started.wait(), FAIL_FAST)
    assert gate.has_pending() is True

    assert gate.deliver_reaction(_reaction(THUMBS_UP, "prompt-1")) is Classification.IGNORED

    channel.prompt_hold.set()
    await _prompted(gate, channel)
    assert task.done() is False and tool.calls == []
    assert gate.deliver_reaction(_reaction(THUMBS_UP, "prompt-1")) is Classification.APPROVE
    assert (await asyncio.wait_for(task, FAIL_FAST)).ok
    assert tool.calls == [ARGS]


async def test_a_second_thumbs_up_authorizes_nothing():
    # Scenario: Approval is single-use and argument-bound. A second 👍 on the
    # decided prompt is ignored, and it cannot answer the NEXT prompt either.
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder)
    tool = LabelledTool()

    first = _invoke(gate, tool)
    await _prompted(gate, channel)
    gate.deliver_reaction(_reaction(THUMBS_UP, "prompt-1"))
    assert (await asyncio.wait_for(first, FAIL_FAST)).ok

    assert gate.deliver_reaction(_reaction(THUMBS_UP, "prompt-1")) is Classification.IGNORED
    assert tool.calls == [ARGS]

    second = _invoke(gate, tool)
    await _prompted(gate, channel, count=2)
    assert channel.returned_refs == ["prompt-1", "prompt-2"]
    assert gate.deliver_reaction(_reaction(THUMBS_UP, "prompt-1")) is Classification.IGNORED
    for _ in range(20):
        await asyncio.sleep(0)
    assert second.done() is False and gate.has_pending() is True
    assert tool.calls == [ARGS]

    gate.deliver("no")
    assert (await asyncio.wait_for(second, FAIL_FAST)).ok is False
    assert tool.calls == [ARGS]
    assert recorder.outcomes() == [
        ("labelled_write", "approved"),
        ("labelled_write", "denied"),
    ]


async def test_taking_back_a_thumbs_up_after_execution_does_nothing():
    # Scenario: Taking back a reaction after execution.
    channel = PromptChannel()
    gate = _gate(channel, recorder=SpyRecorder())
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)
    gate.deliver_reaction(_reaction(THUMBS_UP))
    assert (await asyncio.wait_for(task, FAIL_FAST)).ok
    sent, edits = list(channel.sent), list(channel.edits)

    assert gate.deliver_reaction(_reaction(THUMBS_UP, removed=True)) is Classification.IGNORED
    assert tool.calls == [ARGS]  # not reversed, not repeated
    assert channel.sent == sent and channel.edits == edits  # nothing sent


@pytest.mark.parametrize("reaction_first", [True, False], ids=["reaction-first", "keyword-first"])
async def test_the_first_answer_wins_outcome_and_detail(reaction_first):
    # Scenario: First answer wins. Back to back, no yield between them: the first
    # routed decides the outcome AND the receipt detail; the second resolves nothing.
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder)
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)

    if reaction_first:
        gate.deliver_reaction(_reaction(THUMBS_UP))
        gate.deliver("no")
    else:
        gate.deliver("no")
        gate.deliver_reaction(_reaction(THUMBS_UP))
    result = await asyncio.wait_for(task, FAIL_FAST)

    [entry] = recorder.entries
    if reaction_first:
        assert result.ok and tool.calls == [ARGS]
        assert entry["outcome"] == "approved"
        assert _detail(entry) == "answered by reaction"
        assert [o.verb for _, o in channel.edits] == ["Approved"]
    else:
        assert result.ok is False and tool.calls == []
        assert entry["outcome"] == "denied"
        assert _detail(entry) == "answered by keyword"
        assert [o.verb for _, o in channel.edits] == ["Denied"]


async def test_kill_switch_demoted_standing_tool_approves_by_reaction():
    # Scenario: Kill-switch demotes standing tools (owner approval by keyword or
    # reaction); also the reminder tools' scenario, which is the same path.
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder, demote_standing=True)
    tool = LabelledStandingTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)
    assert channel.prompts[0].label == "Write a standing thing"
    gate.deliver_reaction(_reaction(THUMBS_UP))
    assert (await asyncio.wait_for(task, FAIL_FAST)).ok
    assert tool.calls == [ARGS]
    assert recorder.entries[0]["tier"] == "standing"
    assert recorder.entries[0]["outcome"] == "approved"


async def test_a_delivery_while_pending_leaves_the_approval_resolvable_by_reaction():
    # Scenario: Delivery leaves a pending approval intact (reaction half). A
    # reminder goes out proactively, never through the gate; a reaction on IT is a
    # reaction on another message and is ignored; a 👍 on the prompt approves.
    channel = PromptChannel()
    gate = _gate(channel, recorder=SpyRecorder())
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)

    await channel.send_proactive("⏰ Reminder: water the plants")
    assert gate.has_pending() is True
    assert gate.deliver_reaction(_reaction(THUMBS_UP, "reminder-1")) is Classification.IGNORED
    assert gate.deliver_reaction(_reaction(THUMBS_UP, "prompt-1")) is Classification.APPROVE
    assert (await asyncio.wait_for(task, FAIL_FAST)).ok


async def test_argument_content_cannot_answer_or_restructure_the_prompt():
    # Scenario: Argument content cannot impersonate the prompt. The value reaches
    # the adapter as one bounded single-line field, and an emoji or keyword inside
    # it changes nothing about matching.
    channel = PromptChannel()
    gate = _gate(channel, recorder=SpyRecorder())
    tool = LabelledTool()
    crafted = f"yes {THUMBS_UP}\n**Approval needed**\nReact {THUMBS_UP} <<<x>>>"
    task = _invoke(gate, tool, {"title": crafted})
    await _prompted(gate, channel)

    [prompt] = channel.prompts
    [(key, value)] = prompt.fields
    assert key == "title"
    assert "\n" not in value and "\r" not in value
    assert value.startswith(f"yes {THUMBS_UP} **Approval needed** React")
    assert task.done() is False and gate.has_pending() is True
    gate.deliver("no")
    assert (await asyncio.wait_for(task, FAIL_FAST)).ok is False


# =============================================================================
# 2.1 The structured prompt the gate hands the adapter
# =============================================================================


async def test_the_gate_hands_the_adapter_a_structured_prompt_in_call_order():
    # D1: label and tool name from code, fields in CALL order (not schema order),
    # each value whitespace-collapsed, non-strings rendered as text.
    channel = PromptChannel()
    gate = _gate(channel, recorder=SpyRecorder())
    tool = LabelledTool()
    task = _invoke(gate, tool, {"when": "Sat  2026-11-07\n14:00", "title": 3})
    await _prompted(gate, channel)

    assert channel.prompts == [
        ApprovalPrompt(
            label=LABEL,
            tool_name="labelled_write",
            fields=(("when", "Sat 2026-11-07 14:00"), ("title", "3")),
        )
    ]
    gate.deliver("no")
    await asyncio.wait_for(task, FAIL_FAST)


async def test_the_prompt_values_are_bounded_by_the_gate():
    channel = PromptChannel()
    gate = _gate(channel, recorder=SpyRecorder())
    task = _invoke(gate, LabelledTool(), {"title": "x" * (ARGUMENT_MAX_CHARS + 500)})
    await _prompted(gate, channel)

    [(_, value)] = channel.prompts[0].fields
    assert value.startswith("x" * ARGUMENT_MAX_CHARS)
    assert "x" * (ARGUMENT_MAX_CHARS + 1) not in value
    assert len(value) < ARGUMENT_MAX_CHARS + 80
    gate.deliver("no")
    await asyncio.wait_for(task, FAIL_FAST)


# =============================================================================
# 2.1 Undeclared argument keys fail closed (D8)
# =============================================================================

UNDECLARED = [
    pytest.param({"title": "ok", "extra": "1"}, "extra", id="plain-extra-key"),
    pytest.param(
        {"title": "ok", "when\nApproval needed: rm_all": "x"},
        "when Approval needed: rm_all",
        id="key-with-a-line-break",
    ),
    pytest.param({"Title": "case differs"}, "Title", id="case-variant"),
]


@pytest.mark.parametrize("args, shown_key", UNDECLARED)
async def test_an_undeclared_argument_key_cancels_without_a_prompt(args, shown_key):
    # Scenario: An undeclared argument key fails closed. A short timeout: the
    # refusal must come before any wait for the owner.
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder, timeout_seconds=0.1)
    tool = LabelledTool()
    result = await asyncio.wait_for(gated_invoke(gate, tool, args), FAIL_FAST)

    assert result.ok is False
    assert tool.calls == []
    assert channel.prompts == [] and channel.sent == []  # nothing was asked
    assert gate.has_pending() is False
    [entry] = recorder.entries
    assert entry["outcome"] == "cancelled"
    assert entry["detail"] == "undeclared argument"
    error = result.error or ""
    assert shown_key in error  # names the key, whitespace-collapsed
    assert "\n" not in error
    assert "not a parameter" in error.lower()
    assert "unrelated" not in error.lower()  # never the unrelated-message reason
    assert "not executed" in error.lower() or "nothing was" in error.lower()
    assert channel.edits == []


async def test_an_undeclared_key_is_bounded_before_it_is_echoed():
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder, timeout_seconds=0.1)
    long_key = "k" * (ARGUMENT_MAX_CHARS * 3)
    result = await gated_invoke(gate, LabelledTool(), {long_key: "v"})
    assert result.ok is False
    error = result.error or ""
    assert "k" * 20 in error  # it is named...
    assert "k" * (ARGUMENT_MAX_CHARS + 1) not in error  # ...but bounded
    assert recorder.entries[0]["detail"] == "undeclared argument"
    assert channel.prompts == []


async def test_a_tool_declaring_no_properties_refuses_every_key():
    class Bare(LabelledTool):
        name = "bare_write"
        parameters = {"type": "object"}

    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder, timeout_seconds=0.1)
    result = await gated_invoke(gate, Bare(), {"title": "x"})
    assert result.ok is False
    assert channel.prompts == []
    assert _detail(recorder.entries[0]) == "undeclared argument"


# =============================================================================
# 2.3 The single-message bound
# =============================================================================


async def test_a_prompt_too_large_for_one_message_cancels_with_its_own_reason():
    # Scenario: A prompt too large for one message fails closed.
    channel = PromptChannel(prompt_outcome=SendOutcome.FAILED, too_large=True, refs=[None])
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder, timeout_seconds=0.1)
    tool = LabelledTool()
    result = await asyncio.wait_for(gated_invoke(gate, tool, dict(ARGS)), FAIL_FAST)

    assert result.ok is False
    assert tool.calls == []
    assert channel.sent == []
    assert gate.has_pending() is False
    error = (result.error or "").lower()
    assert "too large to show in one approval prompt" in error
    assert "executed" in error
    assert "unrelated" not in error
    [entry] = recorder.entries
    assert entry["outcome"] == "cancelled"
    assert entry["detail"] == "prompt too large"
    assert channel.edits == []


async def test_an_ordinary_failed_prompt_send_is_not_called_too_large():
    channel = PromptChannel(prompt_outcome=SendOutcome.FAILED, refs=[None])
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder, timeout_seconds=0.1)
    result = await asyncio.wait_for(gated_invoke(gate, LabelledTool(), dict(ARGS)), FAIL_FAST)
    assert result.ok is False
    [entry] = recorder.entries
    assert entry["outcome"] == "cancelled"
    assert _detail(entry) != "prompt too large"
    assert "too large" not in (result.error or "").lower()
    assert channel.edits == []


async def test_a_failed_prompt_send_carries_its_own_reason_and_no_detail():
    # A gate-caused cancel: its own reason, never the unrelated-message one, and
    # no receipt detail.
    channel = PromptChannel(prompt_outcome=SendOutcome.FAILED, refs=[None])
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder, timeout_seconds=0.1)
    result = await asyncio.wait_for(gated_invoke(gate, LabelledTool(), dict(ARGS)), FAIL_FAST)
    assert result.error == (
        "delivery of the approval prompt could not be confirmed; the action was not "
        "executed"
    )
    [entry] = recorder.entries
    assert entry["outcome"] == "cancelled"
    assert "detail" not in entry


# =============================================================================
# 2.2 The outcome edit
# =============================================================================


async def _decide(gate, channel, how: str) -> None:
    if how == "keyword-yes":
        gate.deliver("yes")
    elif how == "reaction-up":
        gate.deliver_reaction(_reaction(THUMBS_UP + SKIN_TONES[3]))
    elif how == "keyword-no":
        gate.deliver("no")
    elif how == "reaction-down":
        gate.deliver_reaction(_reaction(THUMBS_DOWN))
    elif how == "unrelated":
        gate.deliver("what's on my board?")
    elif how == "timeout":
        pass  # the short timeout decides
    else:  # pragma: no cover
        raise AssertionError(how)


DECISIONS = [
    pytest.param("keyword-yes", "Approved", "approved", "answered by keyword", id="approved-by-keyword"),
    pytest.param("reaction-up", "Approved", "approved", "answered by reaction", id="approved-by-reaction"),
    pytest.param("keyword-no", "Denied", "denied", "answered by keyword", id="denied-by-keyword"),
    pytest.param("reaction-down", "Denied", "denied", "answered by reaction", id="denied-by-reaction"),
    pytest.param("unrelated", "Cancelled", "cancelled", None, id="cancelled-by-unrelated"),
    pytest.param("timeout", "Timed out", "timeout", None, id="timed-out"),
]


@pytest.mark.parametrize("how, verb, outcome, detail", DECISIONS)
async def test_each_decided_outcome_edits_the_prompt_and_carries_its_detail(
    how, verb, outcome, detail
):
    # Scenarios: Approval edits the prompt; Each non-executing outcome edits the
    # prompt; The receipt names how the owner answered.
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder, timeout_seconds=0.05 if how == "timeout" else 5)
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)
    await _decide(gate, channel, how)
    result = await asyncio.wait_for(task, FAIL_FAST)

    assert channel.edits == [("prompt-1", PromptOutcome(verb=verb, prompt=PROMPT))]
    [entry] = recorder.entries
    assert entry["outcome"] == outcome
    assert _detail(entry) == detail
    assert result.ok is (outcome == "approved")
    assert tool.calls == ([ARGS] if outcome == "approved" else [])


def test_the_outcome_verbs_are_the_owners_words():
    # The verb vocabulary is owner-facing text: exactly these four.
    assert sorted(VERBS.values()) == ["Approved", "Cancelled", "Denied", "Timed out"]


@pytest.mark.parametrize("how", ["keyword-yes", "reaction-up"])
async def test_the_edit_lands_before_the_tool_runs(how):
    # Scenario: Approval edits the prompt ... before the tool executes. One shared
    # log, so the ordering is the order things happened.
    log: list = []
    channel = PromptChannel(log=log)
    gate = _gate(channel, recorder=SpyRecorder())
    tool = LabelledTool(log=log)
    task = _invoke(gate, tool)
    await _prompted(gate, channel)
    await _decide(gate, channel, how)
    assert (await asyncio.wait_for(task, FAIL_FAST)).ok
    assert log == [("prompt", "labelled_write"), ("edit", "Approved"), ("run", "labelled_write")]


@pytest.mark.parametrize("how", ["keyword-yes", "keyword-no", "unrelated"])
async def test_the_pending_slot_is_released_before_the_edit_and_the_result_waits_for_it(how):
    # D2: release first, then await the edit, then return. A held edit proves both:
    # the gate is no longer pending while it runs, and the decision has not
    # returned yet (so the tool cannot have run and no result has flowed back).
    channel = PromptChannel()
    channel.edit_hold = asyncio.Event()
    gate = _gate(channel, recorder=SpyRecorder())
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)
    await _decide(gate, channel, how)

    await asyncio.wait_for(channel.edit_started.wait(), FAIL_FAST)
    assert channel.pending_at_edit == [False]
    assert gate.has_pending() is False
    for _ in range(20):
        await asyncio.sleep(0)
    assert task.done() is False
    assert tool.calls == []

    channel.edit_hold.set()
    await asyncio.wait_for(task, FAIL_FAST)


async def test_a_failed_edit_changes_nothing(caplog):
    # Scenario: A failed edit changes nothing.
    channel = PromptChannel(edit_result=False)
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder)
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)
    with caplog.at_level(logging.INFO, logger="henk.gate"):
        gate.deliver("yes")
        result = await asyncio.wait_for(task, FAIL_FAST)

    assert result.ok
    assert tool.calls == [ARGS]  # exactly once
    [entry] = recorder.entries
    assert entry["outcome"] == "approved"
    assert entry["detail"] == "answered by keyword"
    assert len(channel.edits) == 1  # one attempt, no retry
    assert any(
        r.name == "henk.gate" and r.levelno >= logging.WARNING and "edit" in r.getMessage().lower()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


async def test_an_edit_that_raises_changes_nothing(caplog):
    # Scenario: A failed edit changes nothing, for an adapter that breaks its
    # never-raise contract: the action still runs once and the receipt stands.
    channel = PromptChannel(edit_raises=True)
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder)
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)
    with caplog.at_level(logging.INFO, logger="henk.gate"):
        gate.deliver_reaction(_reaction(THUMBS_UP))
        result = await asyncio.wait_for(task, FAIL_FAST)

    assert result.ok
    assert tool.calls == [ARGS]
    [entry] = recorder.entries
    assert entry["outcome"] == "approved"
    assert entry["detail"] == "answered by reaction"
    assert len(channel.edits) == 1
    assert any(
        r.name == "henk.gate" and r.levelno >= logging.WARNING and "edit" in r.getMessage().lower()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize(
    "answer, outcome", [("yes", "approved"), ("no", "denied")], ids=["approved", "denied"]
)
async def test_cancelling_the_turn_during_the_edit_keeps_the_receipt(answer, outcome):
    # The receipt is written at decision time, before the edit is awaited: a turn
    # cancelled while the edit is in flight (shutdown, /new) still leaves exactly
    # one receipt with how the owner answered, and the tool never runs.
    channel = PromptChannel()
    channel.edit_hold = asyncio.Event()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder)
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)
    gate.deliver(answer)
    await asyncio.wait_for(channel.edit_started.wait(), FAIL_FAST)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tool.calls == []
    [entry] = recorder.entries
    assert entry["outcome"] == outcome
    assert entry["detail"] == "answered by keyword"


async def test_a_second_thumbs_up_before_the_slot_is_released_is_ignored():
    # The decision is made but the gate task has not yet run to release the slot:
    # a second 👍 (or a 👎) on the same prompt answers nothing and says so.
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder)
    tool = LabelledTool()
    task = _invoke(gate, tool)
    await _prompted(gate, channel)

    assert gate.deliver_reaction(_reaction(THUMBS_UP)) is Classification.APPROVE
    assert gate.has_pending() is True  # no yield yet: the slot is still held
    assert gate.deliver_reaction(_reaction(THUMBS_UP)) is Classification.IGNORED
    assert gate.deliver_reaction(_reaction(THUMBS_DOWN)) is Classification.IGNORED
    assert (await asyncio.wait_for(task, FAIL_FAST)).ok
    assert tool.calls == [ARGS]
    [entry] = recorder.entries
    assert entry["outcome"] == "approved"
    assert entry["detail"] == "answered by reaction"


# --- Decisions that sent no prompt edit nothing ------------------------------


async def test_a_standing_authorization_edits_nothing():
    channel = PromptChannel()
    gate = _gate(channel, recorder=SpyRecorder())
    assert (await gated_invoke(gate, LabelledStandingTool(), dict(ARGS))).ok
    assert channel.prompts == [] and channel.edits == []


async def test_a_rejected_busy_request_edits_nothing():
    channel = PromptChannel()
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder)
    first = _invoke(gate, LabelledTool())
    await _prompted(gate, channel)
    second = await gated_invoke(gate, LabelledTool(), dict(ARGS))
    assert second.ok is False
    assert channel.edits == []  # the busy refusal edits nothing...
    gate.deliver("yes")
    await asyncio.wait_for(first, FAIL_FAST)
    # ...and the first prompt's own edit is the only one.
    assert [(ref, o.verb) for ref, o in channel.edits] == [("prompt-1", "Approved")]
    assert [e["outcome"] for e in recorder.entries] == ["rejected-busy", "approved"]


async def test_a_suppressed_decision_edits_nothing():
    channel = PromptChannel()
    gate = ApprovalGate(channel, timeout_seconds=5, recorder=SpyRecorder())
    gate.enter_turn(TurnContext(turn_type=TurnType.EVENT, announceable=False))
    result = await gated_invoke(gate, LabelledEventTool(), dict(ARGS))
    assert result.ok is False
    assert channel.prompts == [] and channel.edits == []


async def test_an_out_of_scope_decision_edits_nothing():
    channel = PromptChannel()
    gate = ApprovalGate(channel, timeout_seconds=5, recorder=SpyRecorder())
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER, tainted=True))
    result = await gated_invoke(gate, LabelledTool(), dict(ARGS))
    assert result.ok is False
    assert channel.prompts == [] and channel.edits == []


@pytest.mark.parametrize("ref", [None, "prompt-1"], ids=["no-ref", "with-ref"])
async def test_a_prompt_that_could_not_be_sent_edits_nothing(ref):
    # Even if a failed send somehow carried a reference: the prompt was not
    # confirmed sent, so there is nothing to turn into a record.
    channel = PromptChannel(prompt_outcome=SendOutcome.FAILED, refs=[ref])
    recorder = SpyRecorder()
    gate = _gate(channel, recorder=recorder, timeout_seconds=0.1)
    tool = LabelledTool()
    result = await asyncio.wait_for(gated_invoke(gate, tool, dict(ARGS)), FAIL_FAST)
    assert result.ok is False and tool.calls == []
    assert recorder.entries[0]["outcome"] == "cancelled"
    assert channel.edits == []


# =============================================================================
# 2.2 A message during the edit is not swallowed (Dispatcher + core)
# =============================================================================


class _ToolSession:
    """Runs the labelled tool through the REAL permission path on 'mutate'."""

    def __init__(self, registry, gate, tool, turns):
        self._registry, self._gate, self._tool, self._turns = registry, gate, tool, turns

    async def run_turn(self, text: str) -> str:
        self._turns.append(text)
        if text == "mutate":
            decision = await decide_tool_permission(
                self._registry, self._gate, f"mcp__henk__{self._tool.name}", dict(ARGS)
            )
            if decision.allow:
                await self._tool.run(**ARGS)
                return "did mutate"
            return f"blocked: {decision.reason}"
        return f"echo:{text}"

    async def close(self) -> None:
        pass


class _ToolFactory:
    def __init__(self, registry, gate, tool):
        self._args = (registry, gate, tool)
        self.turns: list[str] = []

    def create(self):
        return _ToolSession(*self._args, self.turns)


def _msg(text: str) -> InboundMessage:
    return InboundMessage(sender=OWNER, text=text, timestamp=0.0)


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.parametrize("during", ["yes", "what's on my board?"])
async def test_a_message_during_the_edit_becomes_a_turn_and_the_action_runs_once(during):
    # Scenario: A message during the edit is not swallowed. The slot is released
    # before the edit, so the dispatcher sees nothing pending and queues it.
    channel = PromptChannel()
    channel.edit_hold = asyncio.Event()
    gate = ApprovalGate(channel, timeout_seconds=5)
    channel.gate = gate
    tool = LabelledTool()
    registry = ToolRegistry()
    registry.register(tool)
    factory = _ToolFactory(registry, gate, tool)
    core = AgentCore(factory, channel)
    dispatcher = Dispatcher(AllowlistFilter(OWNER), gate, core)

    worker = asyncio.create_task(core.run())
    try:
        await dispatcher.on_inbound(_msg("mutate"))
        await _prompted(gate, channel)
        await dispatcher.on_inbound(_msg("yes"))
        await asyncio.wait_for(channel.edit_started.wait(), FAIL_FAST)

        await dispatcher.on_inbound(_msg(during))  # while the edit is in flight
        channel.edit_hold.set()
        await _until(lambda: f"echo:{during}" in channel.sent)
    finally:
        await _cancel(worker)

    assert tool.calls == [ARGS]  # exactly once
    assert factory.turns == ["mutate", during]
    assert channel.sent.index("did mutate") < channel.sent.index(f"echo:{during}")


# =============================================================================
# 2.3 Labels: declared in code, required at registration
# =============================================================================


@pytest.mark.parametrize(
    "label", ["", "   ", None, 7], ids=["default-empty", "blank", "none", "not-a-string"]
)
def test_registration_refuses_a_mutating_tool_without_a_label(label):
    # Scenario: Mutating tool without a label rejected at startup.
    class Unlabelled(LabelledTool):
        name = "unlabelled_write"

    Unlabelled.label = label
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="unlabelled_write"):
        registry.register(Unlabelled())


def test_a_label_is_not_required_of_read_only_or_notify_only_tools():
    class Reader(Tool):
        name = "reader"
        tool_class = ToolClass.READ_ONLY

    class Notifier(Tool):
        name = "notifier"
        tool_class = ToolClass.NOTIFY_ONLY

    registry = ToolRegistry()
    registry.register(Reader())
    registry.register(Notifier())
    assert registry.names() == ["reader", "notifier"]


def test_a_labelled_mutating_tool_registers():
    registry = ToolRegistry()
    registry.register(LabelledTool())
    assert registry.get("labelled_write").label == LABEL


def _production_label_cases():
    from henk.tools.calendar import CalendarCreateEventTool
    from henk.tools.capture import CaptureTool
    from henk.tools.memory import StoreMemoryTool
    from henk.tools.reminders import CancelReminderTool, RemindTool

    return [
        (CalendarCreateEventTool, "calendar_create_event", "Add a calendar event"),
        (StoreMemoryTool, "store_memory", "Remember a fact"),
        (CaptureTool, "capture", "Add to your inbox"),
        (RemindTool, "remind", "Set a reminder"),
        (CancelReminderTool, "cancel_reminder", "Cancel a reminder"),
    ]


@pytest.mark.parametrize(
    "cls, name, label", _production_label_cases(), ids=lambda v: v if isinstance(v, str) else None
)
def test_the_production_labels_are_the_owners_table_verbatim(cls, name, label):
    # Proposal decision 1, verbatim. Declared on the class itself: fixed in code,
    # never derived from model output or set per instance.
    assert cls.name == name
    assert cls.tool_class is ToolClass.MUTATING
    assert vars(cls).get("label") == label


def test_every_production_mutating_tool_is_labelled():
    # The five above are the whole mutating set: a sixth would need a label too,
    # and this names it rather than letting registration be the first to notice.
    labelled = {name for _, name, _ in _production_label_cases()}
    import henk.tools as tools_pkg
    import importlib
    import pkgutil

    mutating: set[str] = set()
    for info in pkgutil.iter_modules(tools_pkg.__path__):
        module = importlib.import_module(f"henk.tools.{info.name}")
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, Tool)
                and value.__module__ == module.__name__
                and getattr(value, "tool_class", None) is ToolClass.MUTATING
                and value.name
            ):
                mutating.add(value.name)
    assert mutating == labelled
