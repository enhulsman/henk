"""Receipts in session records (tasks 3.4–3.6), from specs/audit-log.

The verified defect this fixes: ``session_record`` has always accepted
``approvals``, and the core has never passed it — every production record carried
``approvals: []`` while the spec required the decisions. With standing-tier tools
in the registry that gap stops being cosmetic, so these tests pin the whole chain:
the gate reports, the recorder makes it durable, the core threads it into the
session record, and ``tool_calls.executed`` is derived from those receipts rather
than guessed from result text.
"""

from __future__ import annotations

import json
from pathlib import Path

from henk.agent.core import AgentCore
from henk.agent.session import SessionStats, ToolCallRecord
from henk.agent.turns import EventTurn, EventTurnItem
from henk.audit import AuditLog, MutationReceipts
from henk.events.identity import derive_identity
from henk.events.types import Event
from henk.gate.approval import ApprovalGate, TurnContext, gated_invoke
from henk.tools.base import TurnType
from tests.conftest import EventSessionFactory, FakeChannel, make_clock
from tests.test_gate_authorization import PerInstanceTool, StandingTool


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _sessions(path: Path) -> list[dict]:
    return [r for r in _records(path) if r["record_type"] == "session"]


def _authorizations(path: Path) -> list[dict]:
    return [r for r in _records(path) if r["record_type"] == "authorization"]


def _item(title: str = "Gatus: svc/api", eid: str = "e1") -> EventTurnItem:
    event = Event(id=eid, title=title, message="", arrival_time=0.0)
    return EventTurnItem(event=event, identity=derive_identity(event))


class MutatingToolSession:
    """Session fake that invokes a mutating tool through the gate mid-turn.

    Closer to the real thing than a stats stub: the receipt has to arrive while
    the core's accumulator for THIS turn is the live one, which is the property
    the acc-rotation scoping test depends on.
    """

    def __init__(self, tool, gate, arguments=None, stats=None, reply="done") -> None:
        self._tool = tool
        self._gate = gate
        self._arguments = arguments or {"text": "a fact"}
        self._stats = stats
        self.reply = reply
        self.contents: list[str] = []
        self.closed = False

    async def run_turn(self, text: str) -> str:
        self.contents.append(text)
        await gated_invoke(self._gate, self._tool, self._arguments)
        return self.reply

    async def close(self) -> None:
        self.closed = True

    def stats(self):
        return self._stats


class MutatingToolFactory:
    def __init__(self, tool, gate, *, stats=None, reply="done") -> None:
        self._tool = tool
        self._gate = gate
        self._stats = stats
        self._reply = reply
        self.created: list[MutatingToolSession] = []

    def create(self):
        session = MutatingToolSession(
            self._tool, self._gate, stats=self._stats, reply=self._reply
        )
        self.created.append(session)
        return session


def _wire(tmp_path: Path, tool, *, stats=None, demote_standing=False):
    """A core wired exactly like production: audit → receipts → gate → core."""
    channel = FakeChannel()
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    receipts = MutationReceipts(audit)
    gate = ApprovalGate(
        channel, timeout_seconds=5, recorder=receipts, demote_standing=demote_standing
    )
    factory = MutatingToolFactory(tool, gate, stats=stats)
    core = AgentCore(
        factory,
        channel,
        audit=audit,
        receipts=receipts,
        gate=gate,
        clock=make_clock([0, 0, 1, 1, 2, 2]),
    )
    return core, gate, channel, path


# --- approvals[] is never empty when a mutating tool was invoked ----------


async def test_standing_invocation_lands_in_the_session_record(tmp_path: Path):
    tool = StandingTool()
    stats = SessionStats(
        tool_calls=(ToolCallRecord("standing_write", "mutating"),),
        model="claude-sonnet-5",
    )
    core, _gate, _channel, path = _wire(tmp_path, tool, stats=stats)
    await core.process("remember this")
    await core.aclose()

    session = _sessions(path)[0]
    assert session["approvals"] == [
        {
            "tool": "standing_write",
            "tier": "standing",
            "outcome": "authorized",
            "initiated_by": "model",
            "reference": "appr-1",
        }
    ]
    assert session["approvals"], "the verified defect: approvals was always empty"


async def test_a_mutating_invocation_always_leaves_a_receipt_before_the_record(
    tmp_path: Path,
):
    tool = StandingTool()
    core, _gate, _channel, path = _wire(tmp_path, tool)
    await core.process("remember this")
    # The authorization record is durable BEFORE the session's own record exists.
    assert [r["record_type"] for r in _records(path)] == ["authorization"]
    await core.aclose()
    assert [r["record_type"] for r in _records(path)] == ["authorization", "session"]


async def test_read_only_calls_produce_no_authorization_records(tmp_path: Path):
    from tests.test_approval_gate import ReadTool

    stats = SessionStats(tool_calls=(ToolCallRecord("read_thing", "read-only"),))
    core, _gate, _channel, path = _wire(tmp_path, ReadTool(), stats=stats)
    await core.process("how are things?")
    await core.aclose()
    assert _authorizations(path) == []
    assert _sessions(path)[0]["approvals"] == []


# --- executed flag derived from receipts, never from result text ----------


async def test_executed_flag_true_for_an_authorized_mutating_call(tmp_path: Path):
    stats = SessionStats(
        tool_calls=(
            ToolCallRecord("homelab_health", "read-only"),
            ToolCallRecord("standing_write", "mutating"),
        )
    )
    core, _gate, _channel, path = _wire(tmp_path, StandingTool(), stats=stats)
    await core.process("remember this")
    await core.aclose()
    calls = {c["name"]: c for c in _sessions(path)[0]["tool_calls"]}
    assert calls["standing_write"]["executed"] is True
    assert calls["homelab_health"]["executed"] is True  # bypasses the gate entirely


async def test_executed_flag_false_for_a_denied_mutating_call(tmp_path: Path):
    # Whether the SDK surfaces a DENIED call as a ToolUseBlock at all is a
    # deploy-verified question (task 7.4). This pins the branch that matters when
    # it does: the flag comes from the receipt, so a denied call is never reported
    # as executed — and no tool-result text is consulted to decide it.
    stats = SessionStats(tool_calls=(ToolCallRecord("per_instance_write", "mutating"),))
    core, gate, _channel, path = _wire(tmp_path, PerInstanceTool(), stats=stats)

    import asyncio

    task = asyncio.create_task(core.process("write something"))
    for _ in range(1000):
        if gate.has_pending():
            break
        await asyncio.sleep(0)
    gate.deliver("no")
    await task
    await core.aclose()

    session = _sessions(path)[0]
    assert session["tool_calls"][0]["executed"] is False
    assert session["approvals"][0]["outcome"] == "denied"


async def test_an_unreceipted_mutating_call_is_not_reported_as_executed(
    tmp_path: Path, caplog
):
    # Structurally impossible (the gate records every decision), so if it ever
    # happens the record must not claim execution — and it must be noisy.
    import logging

    stats = SessionStats(tool_calls=(ToolCallRecord("standing_write", "mutating"),))
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "audit.jsonl")
    core = AgentCore(
        EventSessionFactory(reply="ok", stats=stats),
        channel,
        audit=audit,
        clock=make_clock([0, 0]),
    )
    with caplog.at_level(logging.WARNING, logger="henk.agent"):
        await core.process("hello")
        await core.aclose()
    session = _sessions(tmp_path / "audit.jsonl")[0]
    assert session["tool_calls"][0]["executed"] is False
    assert any("without an authorization receipt" in r.message for r in caplog.records)


# --- acc rotation: a triage's receipts stay in the triage's record --------


async def test_triage_receipts_do_not_leak_into_the_continuation_record(
    tmp_path: Path,
):
    # An owner interrogation continuing an event session gets its own record. A
    # receipt from the triage turn must not reappear there — an approval must be
    # attributable to exactly one record.
    channel = FakeChannel()
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    receipts = MutationReceipts(audit)
    gate = ApprovalGate(channel, timeout_seconds=5, recorder=receipts)

    class EventScopedStanding(StandingTool):
        name = "event_scoped_standing"
        turn_scope = (TurnType.OWNER, TurnType.EVENT)

    tool = EventScopedStanding()
    factory = MutatingToolFactory(
        tool,
        gate,
        stats=SessionStats(tool_calls=(ToolCallRecord("event_scoped_standing",
                                                      "mutating"),)),
        reply=(
            "Diagnosis: disk pressure (confidence: high)\n"
            "Fix: prune images\nPickup: see the handoff"
        ),
    )
    core = AgentCore(
        factory,
        channel,
        audit=audit,
        receipts=receipts,
        gate=gate,
        clock=make_clock([0, 0, 1, 1]),
    )

    await core.process(EventTurn(items=(_item(),), announceable=True))
    await core.process("what did you find?")
    await core.aclose()

    triage, continuation = _sessions(path)[0], _sessions(path)[1]
    assert len(triage["approvals"]) == 1
    assert triage["approvals"][0]["tool"] == "event_scoped_standing"
    # The follow-up turn's own invocation is out of scope (owner-only? no — this
    # tool declares event scope, so it runs), but whatever it recorded belongs to
    # the continuation alone.
    assert len(continuation["approvals"]) == 1
    assert continuation["approvals"][0]["reference"] != triage["approvals"][0][
        "reference"
    ]


async def test_out_of_scope_denial_is_receipted_in_the_triage_record(tmp_path: Path):
    # incident-triage delta: a mutating attempt during triage is denied silently
    # and leaves an `out-of-scope` receipt; the record shows no mutating execution.
    tool = StandingTool()  # owner-turn-only
    channel = FakeChannel()
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    receipts = MutationReceipts(audit)
    gate = ApprovalGate(channel, timeout_seconds=5, recorder=receipts)
    factory = MutatingToolFactory(
        tool,
        gate,
        stats=SessionStats(tool_calls=(ToolCallRecord("standing_write", "mutating"),)),
        reply=(
            "Diagnosis: disk pressure (confidence: high)\n"
            "Fix: prune images\nPickup: see the handoff"
        ),
    )
    core = AgentCore(
        factory, channel, audit=audit, receipts=receipts, gate=gate,
        clock=make_clock([0, 0]),
    )
    await core.process(EventTurn(items=(_item(),), announceable=True))

    triage = _sessions(path)[0]
    assert triage["approvals"][0]["outcome"] == "out-of-scope"
    assert triage["tool_calls"][0]["executed"] is False
    assert tool.calls == []
    assert channel.sent  # the triage reply itself, but no approval prompt
    assert not any("Approval needed" in s for s in channel.sent)
