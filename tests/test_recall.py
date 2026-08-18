"""Recall-block tests (task 4.4), from specs/memory-store + specs/agent-core.

Continuity by rebuild: the store is rendered into the first OWNER turn of every
session as a delimited data block, bounded so a full store cannot dominate the
prompt, hashed so the audit trail shows which memory state a session saw, and
never mixed into the untrusted-sensor-data path of an event turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from henk.agent.core import AgentCore
from henk.agent.recall import (
    RECALL_BEGIN,
    RECALL_END_PREFIX,
    MemoryRecall,
    render_recall_block,
)
from henk.agent.turns import EventTurn, EventTurnItem
from henk.audit import AuditLog
from henk.events.identity import derive_identity
from henk.events.types import Event
from henk.store import MemoryStore, Store
from tests.conftest import EventSessionFactory, FakeChannel, make_clock


class _Mem:
    """Minimal memory stand-in: rendering depends only on these four fields."""

    def __init__(self, id, content, memory_type="pinned", created_at=0.0):
        self.id = id
        self.content = content
        self.memory_type = memory_type
        self.created_at = created_at


def _memories(tmp_path: Path, **kwargs) -> MemoryStore:
    return MemoryStore(Store(tmp_path / "store" / "henk.db"), **kwargs)


# --- Rendering ------------------------------------------------------------


def test_empty_store_renders_nothing():
    assert render_recall_block([]) is None


def test_block_is_delimited_and_framed_as_data_not_instructions():
    block = render_recall_block([_Mem(1, "the vps runs ntfy")])
    assert block is not None
    assert block.text.startswith(RECALL_BEGIN)
    assert RECALL_END_PREFIX in block.text
    lowered = block.text.lower()
    assert "not instructions" in lowered  # framed as remembered fact, not command
    assert "the vps runs ntfy" in block.text


def test_facts_are_grouped_by_type_newest_first_within_each_group():
    memories = [
        _Mem(1, "old pinned", "pinned", created_at=1.0),
        _Mem(2, "new pinned", "pinned", created_at=3.0),
        _Mem(3, "agent fact", "agent", created_at=2.0),
    ]
    text = render_recall_block(memories).text
    assert text.index("pinned") < text.index("agent fact")  # owner facts first
    assert text.index("new pinned") < text.index("old pinned")


def test_block_carries_a_short_content_hash():
    block = render_recall_block([_Mem(1, "a fact")])
    assert block.content_hash
    assert len(block.content_hash) <= 16
    assert block.content_hash in block.text  # visible in the closing delimiter


def test_hash_changes_with_content_and_is_stable_for_identical_renders():
    one = render_recall_block([_Mem(1, "a fact")])
    same = render_recall_block([_Mem(1, "a fact")])
    other = render_recall_block([_Mem(1, "a different fact")])
    assert one.content_hash == same.content_hash
    assert one.content_hash != other.content_hash


def test_render_bound_omits_the_oldest_and_says_how_many():
    memories = [
        _Mem(i, f"fact {i} " + "x" * 90, "pinned", created_at=float(i))
        for i in range(40)
    ]
    block = render_recall_block(memories, limit=1000)
    assert len(block.text) <= 1000
    assert block.omitted > 0
    assert str(block.omitted) in block.text
    assert "omitted" in block.text.lower()
    # The oldest went first, the newest is always present.
    assert "fact 39" in block.text
    assert "fact 0 " not in block.text


def test_render_bound_never_deletes_from_the_store(tmp_path: Path):
    memories = _memories(tmp_path, length_limit=200)
    for i in range(30):
        memories.add(f"fact {i} " + "y" * 100, "pinned")
    block = render_recall_block(memories.list_all(), limit=1000)
    assert block.omitted > 0
    assert memories.count("pinned") == 30  # the store is untouched


def test_unbounded_store_renders_every_fact():
    memories = [_Mem(i, f"fact {i}", "pinned", created_at=float(i)) for i in range(5)]
    block = render_recall_block(memories, limit=8000)
    assert block.omitted == 0
    for i in range(5):
        assert f"fact {i}" in block.text


def test_memory_recall_reads_the_store(tmp_path: Path):
    memories = _memories(tmp_path)
    memories.add("stored fact", "pinned")
    block = MemoryRecall(memories, limit=8000).block()
    assert "stored fact" in block.text


def test_memory_recall_propagates_a_read_failure(tmp_path: Path):
    from henk.store import StoreError

    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    recall = MemoryRecall(MemoryStore(Store(blocker / "henk.db")))
    with pytest.raises(StoreError):
        recall.block()  # the caller decides what to do; never a silent empty block


# --- Injection into turns -------------------------------------------------


class _StubRecall:
    def __init__(self, block=None, fail: bool = False) -> None:
        self._block = block
        self.fail = fail
        self.calls = 0

    def block(self):
        self.calls += 1
        if self.fail:
            from henk.store import StoreError

            raise StoreError("simulated read failure")
        return self._block


def _item(eid: str = "e1") -> EventTurnItem:
    event = Event(id=eid, title="Gatus: svc/api", message="", arrival_time=0.0)
    return EventTurnItem(event=event, identity=derive_identity(event))


def _block(text="MEMORY BLOCK", digest="hash123", omitted=0):
    from henk.agent.recall import RecallBlock

    return RecallBlock(text=text, content_hash=digest, omitted=omitted)


async def test_first_owner_turn_is_prefixed_with_the_block():
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(
        factory,
        FakeChannel(),
        clock=make_clock([0, 0]),
        recall=_StubRecall(_block()),
    )
    await core.process("what's up?")
    content = factory.created[0].contents[0]
    assert content.startswith("MEMORY BLOCK")
    assert content.endswith("what's up?")


async def test_the_block_is_injected_once_per_session():
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(
        factory,
        FakeChannel(),
        clock=make_clock([0, 0, 1, 1]),
        recall=_StubRecall(_block()),
    )
    await core.process("first")
    await core.process("second")
    contents = factory.created[0].contents
    assert contents[0].startswith("MEMORY BLOCK")
    assert "MEMORY BLOCK" not in contents[1]


async def test_a_new_session_gets_the_block_again():
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(
        factory,
        FakeChannel(),
        clock=make_clock([0, 0, 1, 1, 2, 2]),
        recall=_StubRecall(_block()),
    )
    await core.process("first")
    await core.process("/new")
    await core.process("after reset")
    assert factory.created[1].contents[0].startswith("MEMORY BLOCK")


async def test_event_turns_never_carry_the_block():
    factory = EventSessionFactory()
    core = AgentCore(
        factory, FakeChannel(), clock=make_clock([0]), recall=_StubRecall(_block())
    )
    await core.process(EventTurn(items=(_item(),), announceable=True))
    assert "MEMORY BLOCK" not in factory.created[0].contents[0]


async def test_owner_followup_in_an_event_started_session_gets_the_block():
    # The common path in an event-active homelab: keying on the first owner TURN
    # rather than session creation is what keeps this from silently missing recall.
    factory = EventSessionFactory()
    core = AgentCore(
        factory, FakeChannel(), clock=make_clock([0]), recall=_StubRecall(_block())
    )
    await core.process(EventTurn(items=(_item(),), announceable=True))
    await core.process("what did you find?")
    contents = factory.created[0].contents
    assert "MEMORY BLOCK" not in contents[0]  # the event turn did not
    assert contents[1].startswith("MEMORY BLOCK")  # the owner follow-up did


async def test_empty_store_injects_nothing():
    factory = EventSessionFactory(reply="ok")
    recall = _StubRecall(None)
    core = AgentCore(
        factory, FakeChannel(), clock=make_clock([0, 0]), recall=recall
    )
    await core.process("hello")
    assert factory.created[0].contents == ["hello"]


async def test_injected_hash_lands_in_the_session_audit_record(tmp_path: Path):
    import json

    path = tmp_path / "audit.jsonl"
    core = AgentCore(
        EventSessionFactory(reply="ok"),
        FakeChannel(),
        clock=make_clock([0, 0]),
        audit=AuditLog(path),
        recall=_StubRecall(_block(digest="deadbeef")),
    )
    await core.process("hello")
    await core.aclose()
    record = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ][0]
    assert record["memory_hash"] == "deadbeef"


async def test_no_block_means_a_null_memory_hash(tmp_path: Path):
    import json

    path = tmp_path / "audit.jsonl"
    core = AgentCore(
        EventSessionFactory(reply="ok"),
        FakeChannel(),
        clock=make_clock([0, 0]),
        audit=AuditLog(path),
        recall=_StubRecall(None),
    )
    await core.process("hello")
    await core.aclose()
    record = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ][0]
    assert record["memory_hash"] is None


# --- Failure modes (task 4.5) --------------------------------------------


async def test_unreadable_store_does_not_block_the_turn(caplog):
    import logging

    factory = EventSessionFactory(reply="ok")
    channel = FakeChannel()
    recall = _StubRecall(fail=True)
    core = AgentCore(factory, channel, clock=make_clock([0, 0]), recall=recall)
    with caplog.at_level(logging.ERROR, logger="henk.agent"):
        await core.process("hello")
    assert factory.created[0].contents == ["hello"]  # no block, but the turn ran
    assert channel.sent == ["ok"]
    assert any("recall" in r.message for r in caplog.records)


async def test_a_failed_read_is_retried_on_the_next_turn():
    # A transient read failure must not permanently cost this session its memory.
    factory = EventSessionFactory(reply="ok")
    recall = _StubRecall(fail=True)
    core = AgentCore(factory, FakeChannel(), clock=make_clock([0, 0, 1, 1]), recall=recall)
    await core.process("first")
    recall.fail = False
    recall._block = _block()
    await core.process("second")
    assert factory.created[0].contents[1].startswith("MEMORY BLOCK")


async def test_a_stored_memory_is_present_in_recall_after_a_restart(tmp_path: Path):
    # memory-store scenario, end to end across a process boundary: the whole point
    # of the store is that continuity survives a restart rather than depending on a
    # longer idle window.
    from henk.agent.commands import OwnerCommands

    first = _memories(tmp_path)
    OwnerCommands(memories=first).handle("/remember the pi5 hosts gatus")
    first._store.close()  # no graceful app shutdown involved

    reopened = _memories(tmp_path)
    block = MemoryRecall(reopened).block()
    assert block is not None
    assert "the pi5 hosts gatus" in block.text
