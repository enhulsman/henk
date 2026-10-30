"""approval-reactions group 2: reactions coming in (tasks 2.4, 2.6).

From the channel-adapter delta: *Inbound reactions are normalised and bound to
Henk's own messages*, *Owner-only allowlist* (reactions, no acknowledgement), and
*Swappable channel-adapter contract* (reactions go only to the gate; *The prompt
reference is never audited or put in a turn*). From the approval-gate delta:
*Reactions from anyone but the owner*, *Other reactions are ignored* (no turn is
queued), *Taking back a reaction after execution*. Design D3, D4.

Envelopes are shaped as ``notes/bridge-facts.md`` records signal-cli's json-rpc
reaction: ``dataMessage.reaction`` with ``emoji``, ``targetAuthor``,
``targetAuthorNumber``, ``targetAuthorUuid``, ``targetSentTimestamp`` (int) and
``isRemove``, and ``dataMessage.message`` null. Numbers are placeholders.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from henk.agent.commands import OwnerCommands
from henk.agent.core import AgentCore
from henk.agent.permission import decide_tool_permission
from henk.agent.recall import MemoryRecall
from henk.app import App, Dispatcher
from henk.audit import AuditLog, MutationReceipts
from henk.channel.acknowledge import OwnerAcknowledgement
from henk.channel.allowlist import AllowlistFilter
from henk.channel.base import InboundMessage, InboundReaction
from henk.channel.signal import TYPING_REFRESH_SECONDS, SignalAdapter
from henk.config import StoreConfig
from henk.gate.approval import ApprovalGate, Classification, TurnContext, gated_invoke
from henk.store import build_stores
from henk.tools.base import ToolRegistry, TurnType
from tests.conftest import FakeBridge
from tests.test_approval_reactions import ARGS, LabelledTool, PromptChannel
from tests.test_gate_authorization import SpyRecorder
from tests.test_prompt_rendering import CAL_PROMPT

OWNER = "+31600000000"
ACCOUNT = "+31611111111"
STRANGER = "00000000-0000-4000-8000-000000000000"
THUMBS_UP = "\U0001F44D"
THUMBS_DOWN = "\U0001F44E"
T = 1761830400123
FAIL_FAST = 2.0
ALLOWLIST_LOGGER = "henk.channel.allowlist"


def _reaction_env(
    emoji: str = THUMBS_UP,
    target_ts=T,
    *,
    target_author_number: str | None = ACCOUNT,
    target_author: str | None = ACCOUNT,
    target_author_uuid: str | None = "00000000-0000-4000-8000-000000000000",
    is_remove: bool = False,
    group: bool = False,
    source: str = OWNER,
    timestamp: int = 1761830500000,
) -> dict:
    reaction = {"emoji": emoji, "isRemove": is_remove}
    if target_ts is not ...:
        reaction["targetSentTimestamp"] = target_ts
    if target_author is not None:
        reaction["targetAuthor"] = target_author
    if target_author_number is not None:
        reaction["targetAuthorNumber"] = target_author_number
    if target_author_uuid is not None:
        reaction["targetAuthorUuid"] = target_author_uuid
    data = {"timestamp": timestamp, "message": None, "reaction": reaction}
    if group:
        data["groupInfo"] = {"groupId": "g1"}
    return {
        "envelope": {
            "source": source,
            "sourceNumber": source,
            "sourceUuid": source,
            "timestamp": timestamp,
            "dataMessage": data,
        }
    }


def _dm(text: str, timestamp: int, *, source: str = OWNER) -> dict:
    return {
        "envelope": {
            "source": source,
            "sourceUuid": source,
            "timestamp": timestamp,
            "dataMessage": {"message": text, "timestamp": timestamp},
        }
    }


def _adapter(bridge) -> SignalAdapter:
    return SignalAdapter(bridge, account=ACCOUNT, owner=OWNER)


async def _first(adapter: SignalAdapter, n: int = 1) -> list:
    stream = adapter.messages()
    try:
        return [await asyncio.wait_for(anext(stream), FAIL_FAST) for _ in range(n)]
    finally:
        await stream.aclose()


async def _converted(envelope: dict):
    """The first item the adapter yields for ``envelope``. A DM follows it so a
    dropped reaction shows up as the DM coming first, never as a hang."""
    [item] = await _first(_adapter(FakeBridge([envelope, _dm("after", 1)])))
    return item


def _reaction(emoji=THUMBS_UP, target="prompt-1", *, sender=OWNER, removed=False,
              is_group=False) -> InboundReaction:
    return InboundReaction(sender=sender, emoji=emoji, target_ref=target,
                           removed=removed, is_group=is_group)


async def _until(pred, tries: int = 5000) -> None:
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not met in time")


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# =============================================================================
# 2.4 Reaction envelopes become channel-neutral reactions
# =============================================================================


async def test_the_owner_reacting_to_henks_message_yields_a_bound_reaction():
    # Scenario: Owner reacts to Henk's prompt.
    item = await _converted(_reaction_env())
    assert isinstance(item, InboundReaction)
    assert not isinstance(item, InboundMessage)
    assert item == InboundReaction(
        sender=OWNER, emoji=THUMBS_UP, target_ref=str(T), removed=False, is_group=False
    )


@pytest.mark.parametrize("returned", [T, str(T)], ids=["int-response", "string-response"])
async def test_the_target_reference_equals_the_prompt_reference_for_the_same_timestamp(returned):
    # Minted by one function from the same integer, whatever JSON type the send
    # response carried: equality cannot fail on form.
    bridge = FakeBridge([_reaction_env(target_ts=T), _dm("after", 1)])
    bridge.send_results = [returned]
    adapter = _adapter(bridge)
    sent = await adapter.send_prompt(CAL_PROMPT)
    [reaction] = await _first(adapter)
    assert sent.ref is not None
    assert reaction.target_ref == sent.ref


async def test_a_string_target_timestamp_mints_the_same_reference():
    item = await _converted(_reaction_env(target_ts=str(T)))
    assert isinstance(item, InboundReaction)
    assert item.target_ref == str(T)


async def test_a_reaction_on_the_owners_own_message_has_no_target():
    # Scenario: Reaction on the owner's own message.
    item = await _converted(
        _reaction_env(target_author_number=OWNER, target_author=OWNER, target_author_uuid=None)
    )
    assert isinstance(item, InboundReaction)
    assert item.target_ref is None


@pytest.mark.parametrize(
    "number, author, uuid",
    [
        (None, ACCOUNT, "00000000-0000-4000-8000-000000000000"),
        (None, None, "00000000-0000-4000-8000-000000000000"),
        ("", ACCOUNT, None),
        ("+15550100000", ACCOUNT, None),
    ],
    ids=["number-absent-author-matches", "uuid-only", "number-empty", "number-differs"],
)
async def test_only_target_author_number_binds_the_target(number, author, uuid):
    # D3: no loosening to "either field". Only targetAuthorNumber equal to Henk's
    # configured account mints a target; anything else fails closed.
    item = await _converted(
        _reaction_env(target_author_number=number, target_author=author, target_author_uuid=uuid)
    )
    assert isinstance(item, InboundReaction)
    assert item.target_ref is None


@pytest.mark.parametrize("target_ts", [..., None, 0, True, "abc", -5], ids=repr)
async def test_a_reaction_without_a_usable_target_timestamp_has_no_target(target_ts):
    item = await _converted(_reaction_env(target_ts=target_ts))
    assert isinstance(item, InboundReaction)
    assert item.target_ref is None


async def test_a_removal_is_flagged():
    item = await _converted(_reaction_env(is_remove=True))
    assert isinstance(item, InboundReaction)
    assert item.removed is True


async def test_a_group_reaction_is_flagged():
    item = await _converted(_reaction_env(group=True))
    assert isinstance(item, InboundReaction)
    assert item.is_group is True


async def test_skin_tones_are_passed_through_for_the_gate_to_normalise():
    toned = THUMBS_UP + "\U0001F3FD"
    item = await _converted(_reaction_env(emoji=toned))
    assert isinstance(item, InboundReaction)
    assert item.emoji == toned


async def test_a_reaction_envelope_is_never_an_inbound_message():
    # Reactions never reach the core as text: the stream is reaction, then the DM,
    # and nothing else.
    bridge = FakeBridge([_reaction_env(), _dm("real", 2)])
    items = await _first(_adapter(bridge), 2)
    assert [type(i).__name__ for i in items] == ["InboundReaction", "InboundMessage"]
    assert items[1].text == "real"


# =============================================================================
# 2.6 The allowlist applies to reactions
# =============================================================================


def test_the_owners_reaction_passes_the_allowlist():
    assert AllowlistFilter(OWNER).allows(_reaction()) is True


def test_a_strangers_reaction_is_dropped_and_logged(caplog):
    # Scenario: Unknown sender reacts.
    with caplog.at_level(logging.WARNING, logger=ALLOWLIST_LOGGER):
        allowed = AllowlistFilter(OWNER).allows(_reaction(sender=STRANGER))
    assert allowed is False
    assert any("non-owner" in r.getMessage() and STRANGER in r.getMessage()
               for r in caplog.records)


def test_a_group_reaction_from_the_owner_is_dropped_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger=ALLOWLIST_LOGGER):
        allowed = AllowlistFilter(OWNER).allows(_reaction(is_group=True))
    assert allowed is False
    assert any("group" in r.getMessage() for r in caplog.records)


def test_a_senderless_reaction_never_matches_the_owner():
    assert AllowlistFilter(OWNER).allows(_reaction(sender="")) is False


# =============================================================================
# 2.6 The dispatcher routes reactions to the gate only
# =============================================================================


class SpyGate:
    """Stands in for the gate so the dispatcher's routing is observable."""

    def __init__(self, pending: bool) -> None:
        self.pending = pending
        self.reactions: list[InboundReaction] = []
        self.texts: list[str] = []

    def has_pending(self) -> bool:
        return self.pending

    def deliver(self, text: str):
        self.texts.append(text)
        return Classification.UNRELATED, True

    def deliver_reaction(self, reaction: InboundReaction) -> Classification:
        self.reactions.append(reaction)
        return Classification.IGNORED


class SpyCore:
    def __init__(self) -> None:
        self.submitted: list = []

    async def submit(self, text) -> None:
        self.submitted.append(text)


@pytest.mark.parametrize(
    "reaction",
    [_reaction(sender=STRANGER), _reaction(is_group=True), _reaction(sender="")],
    ids=["stranger", "owner-in-a-group", "senderless"],
)
async def test_a_non_owner_or_group_reaction_reaches_neither_gate_nor_core(reaction, caplog):
    # Scenarios: Reactions from anyone but the owner; Unknown sender reacts.
    gate, core = SpyGate(pending=True), SpyCore()
    dispatcher = Dispatcher(AllowlistFilter(OWNER), gate, core)
    with caplog.at_level(logging.WARNING, logger=ALLOWLIST_LOGGER):
        await asyncio.wait_for(dispatcher.on_inbound(reaction), FAIL_FAST)
    assert gate.reactions == [] and gate.texts == []
    assert core.submitted == []
    assert any(r.name == ALLOWLIST_LOGGER for r in caplog.records)


async def test_an_owner_reaction_while_pending_goes_to_the_gate_only():
    gate, core = SpyGate(pending=True), SpyCore()
    dispatcher = Dispatcher(AllowlistFilter(OWNER), gate, core)
    reaction = _reaction()
    await asyncio.wait_for(dispatcher.on_inbound(reaction), FAIL_FAST)
    assert gate.reactions == [reaction]
    assert gate.texts == []  # never classified as typed text
    assert core.submitted == []


async def test_an_owner_reaction_with_nothing_pending_never_reaches_the_core():
    gate, core = SpyGate(pending=False), SpyCore()
    dispatcher = Dispatcher(AllowlistFilter(OWNER), gate, core)
    await asyncio.wait_for(dispatcher.on_inbound(_reaction()), FAIL_FAST)
    assert core.submitted == []
    assert gate.texts == []


async def test_a_reaction_gets_no_read_receipt():
    # Owner-only allowlist: "a reaction SHALL receive no acknowledgement".
    bridge = FakeBridge()
    adapter = SignalAdapter(bridge, account=ACCOUNT, owner=OWNER)
    gate = ApprovalGate(adapter, timeout_seconds=5)
    core = SpyCore()
    acknowledgement = OwnerAcknowledgement(
        adapter, timeout=0.5, refresh_seconds=TYPING_REFRESH_SECONDS, paused=gate.has_pending
    )
    dispatcher = Dispatcher(AllowlistFilter(OWNER), gate, core, acknowledgement=acknowledgement)
    await asyncio.wait_for(dispatcher.on_inbound(_reaction(target=str(T))), FAIL_FAST)
    assert bridge.ack_attempts == [] and bridge.receipts == []
    assert core.submitted == []


# --- With the real gate and core: ignored reactions queue no turn -------------


class _ToolSession:
    def __init__(self, registry, gate, tool, turns):
        self._registry, self._gate, self._tool, self._turns = registry, gate, tool, turns

    async def run_turn(self, text: str) -> str:
        self._turns.append(text)
        if text.endswith("mutate"):
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
        self.created = 0

    def create(self):
        self.created += 1
        return _ToolSession(*self._args, self.turns)


def _wired():
    channel = PromptChannel()
    gate = ApprovalGate(channel, timeout_seconds=5, recorder=SpyRecorder())
    channel.gate = gate
    tool = LabelledTool()
    registry = ToolRegistry()
    registry.register(tool)
    factory = _ToolFactory(registry, gate, tool)
    core = AgentCore(factory, channel)
    dispatcher = Dispatcher(AllowlistFilter(OWNER), gate, core)
    return SimpleNamespace(channel=channel, gate=gate, tool=tool, factory=factory,
                           core=core, dispatcher=dispatcher)


def _msg(text: str) -> InboundMessage:
    return InboundMessage(sender=OWNER, text=text, timestamp=0.0)


async def test_ignored_reactions_while_pending_queue_no_turn_and_a_later_thumbs_up_approves():
    # Scenario: Other reactions are ignored ... no turn is queued, and a later
    # valid reaction still resolves it. Through the dispatcher and the core.
    w = _wired()
    worker = asyncio.create_task(w.core.run())
    try:
        await w.dispatcher.on_inbound(_msg("mutate"))
        await _until(lambda: w.channel.returned_refs and w.gate.has_pending())
        for reaction in (
            _reaction("❤️"),
            _reaction(THUMBS_UP, "prompt-0"),
            _reaction(THUMBS_UP, removed=True),
            _reaction(THUMBS_UP, None),
            _reaction(THUMBS_UP, sender=STRANGER),
        ):
            await asyncio.wait_for(w.dispatcher.on_inbound(reaction), FAIL_FAST)
        for _ in range(20):
            await asyncio.sleep(0)
        assert w.gate.has_pending() is True
        assert w.core._queue.qsize() == 0
        assert w.tool.calls == []

        await w.dispatcher.on_inbound(_reaction(THUMBS_UP + "\U0001F3FF"))
        await _until(lambda: "did mutate" in w.channel.sent)
    finally:
        await _cancel(worker)
    assert w.tool.calls == [ARGS]
    assert w.factory.turns == ["mutate"]  # no reaction ever became a turn


async def test_taking_back_a_reaction_after_execution_sends_nothing_and_queues_nothing():
    # Scenario: Taking back a reaction after execution, end to end.
    w = _wired()
    worker = asyncio.create_task(w.core.run())
    try:
        await w.dispatcher.on_inbound(_msg("mutate"))
        await _until(lambda: w.channel.returned_refs and w.gate.has_pending())
        await w.dispatcher.on_inbound(_reaction(THUMBS_UP))
        await _until(lambda: "did mutate" in w.channel.sent)
        sent, edits = list(w.channel.sent), list(w.channel.edits)

        await w.dispatcher.on_inbound(_reaction(THUMBS_UP, removed=True))
        await w.dispatcher.on_inbound(_reaction(THUMBS_UP))  # and a stray re-add
        for _ in range(50):
            await asyncio.sleep(0)
    finally:
        await _cancel(worker)
    assert w.tool.calls == [ARGS]
    assert w.channel.sent == sent and w.channel.edits == edits
    assert w.factory.turns == ["mutate"]


# =============================================================================
# 2.6 The prompt reference is never audited, persisted or put in a turn
# =============================================================================

#: The prompt's sent timestamp. Thirteen digits in 2032: no clock value, id or
#: other timestamp of this run can contain it, so a hit can only be the reference.
PROMPT_SENTINEL = 1987654322987
DM_TS = 1700000000701
REACTION_TS = 1700000000777
MEMORY = "the owner keeps the spare Pi in the hallway cupboard"
DRIVE_BOUND = 5.0


class QueueBridge(FakeBridge):
    """A FakeBridge whose receive stream is fed by the test as the run goes."""

    def __init__(self) -> None:
        super().__init__(hold_open=True)
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def receive(self):
        while True:
            yield await self.inbox.get()


async def test_the_prompt_reference_is_never_audited_persisted_or_put_in_a_turn(tmp_path: Path):
    # Scenario: The prompt reference is never audited or put in a turn. The owner
    # asks, Henk prompts (the bridge reports PROMPT_SENTINEL), the owner reacts 👍
    # on it, the prompt is edited, the tool runs and Henk replies. Then every file
    # and every turn is scanned for the reference.
    bridge = QueueBridge()
    bridge.send_results = [PROMPT_SENTINEL]  # the first send is the prompt
    adapter = SignalAdapter(bridge, account=ACCOUNT, owner=OWNER)
    audit_path = tmp_path / "audit" / "audit.jsonl"
    audit = AuditLog(audit_path)
    receipts = MutationReceipts(audit)
    store_path = tmp_path / "audit" / "henk-store.db"
    stores = build_stores(StoreConfig(path=str(store_path)))
    stores.memories.add(MEMORY)
    gate = ApprovalGate(adapter, timeout_seconds=5, recorder=receipts)
    acknowledgement = OwnerAcknowledgement(
        adapter, timeout=0.5, refresh_seconds=TYPING_REFRESH_SECONDS, paused=gate.has_pending
    )
    tool = LabelledTool()
    registry = ToolRegistry()
    registry.register(tool)
    factory = _ToolFactory(registry, gate, tool)
    core = AgentCore(
        factory,
        adapter,
        audit=audit,
        gate=gate,
        receipts=receipts,
        commands=OwnerCommands(memories=stores.memories, inbox=stores.inbox, receipts=receipts),
        recall=MemoryRecall(stores.memories),
        working_indicator=acknowledgement.working,
    )
    dispatcher = Dispatcher(AllowlistFilter(OWNER), gate, core, acknowledgement=acknowledgement)
    app = App(adapter, dispatcher, core)

    def prompted() -> bool:
        return any(r["returned"] == PROMPT_SENTINEL for r in bridge.requests)

    async def drive() -> None:
        task = asyncio.create_task(app.run())
        try:
            await bridge.inbox.put(_dm("mutate", DM_TS))
            await _until(prompted)
            await _until(gate.has_pending)
            for _ in range(10):
                await asyncio.sleep(0)
            await bridge.inbox.put(_reaction_env(target_ts=PROMPT_SENTINEL, timestamp=REACTION_TS))
            await _until(lambda: any(text == "did mutate" for _, text in bridge.sends))
            await _until(lambda: bridge.typing.count(("stop", OWNER)) == 1)
        finally:
            await _cancel(task)

    try:
        await asyncio.wait_for(drive(), DRIVE_BOUND)
    finally:
        stores.store.close()

    needle = str(PROMPT_SENTINEL)
    # It reached the edit request, so the scan is not vacuous.
    edits = [r for r in bridge.requests if r["edit_timestamp"] is not None]
    assert [e["edit_timestamp"] for e in edits] == [PROMPT_SENTINEL]
    assert edits[0]["text"].startswith("Approved: Write a test thing, ")
    assert tool.calls == [ARGS]
    # The reaction got no read receipt; only the DM did.
    assert bridge.receipts == [(OWNER, DM_TS)]

    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    [authorization] = [r for r in records if r["record_type"] == "authorization"]
    assert authorization["outcome"] == "approved"
    assert authorization["detail"] == "answered by reaction"
    files = sorted(p for p in tmp_path.rglob("*") if p.is_file())
    assert audit_path in files and store_path in files
    assert MEMORY.encode() in store_path.read_bytes()
    leaked = [str(p) for p in files if needle.encode() in p.read_bytes()]
    assert leaked == [], f"the prompt reference was persisted in {leaked}"
    assert factory.turns and all(needle not in turn for turn in factory.turns)
    assert len(factory.turns) == 1  # the reaction was never a turn


# =============================================================================
# 2.1 at the gate: a reaction is only ever classified, never requeued
# =============================================================================


async def test_deliver_reaction_returns_a_classification_not_a_requeue_flag():
    # A reaction never becomes a turn, so unlike `deliver` there is nothing to
    # requeue: the return is the Classification alone.
    channel = PromptChannel()
    gate = ApprovalGate(channel, timeout_seconds=5, recorder=SpyRecorder())
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER))
    task = asyncio.create_task(gated_invoke(gate, LabelledTool(), dict(ARGS)))
    await _until(lambda: channel.returned_refs and gate.has_pending())
    result = gate.deliver_reaction(_reaction("❤️"))
    assert result is Classification.IGNORED
    gate.deliver("no")
    await asyncio.wait_for(task, FAIL_FAST)
