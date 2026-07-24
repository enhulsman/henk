"""Per-triage audit flush + durable checkpoint advance (task 1.5), from
specs/audit-log + specs/agent-core + design D1/D3.

The event-triage audit record is written at triage completion with the session
left open (so it survives a SIGKILL and does not conflate two incidents), and
the durable intake checkpoint advances to the batch offset ONLY after that
record is durable — an errored triage records ``outcome="error"`` and still
advances; a failed audit write leaves the cursor so the event replays. A
suppression-only batch advances via a ``CheckpointMarker`` on the same queue.
"""

from __future__ import annotations

import json
from pathlib import Path

from henk.agent.core import AgentCore
from henk.agent.session import SessionStats, ToolCallRecord
from henk.agent.turns import CheckpointMarker, EventTurn, EventTurnItem
from henk.audit import AuditLog
from henk.events.identity import derive_identity
from henk.events.types import Event
from tests.conftest import EventSessionFactory, FakeChannel, handoff_stats, make_clock


def _item(title: str, eid: str = "e1") -> EventTurnItem:
    event = Event(id=eid, title=title, message="", arrival_time=0.0)
    return EventTurnItem(event=event, identity=derive_identity(event))


def _turn(*items: EventTurnItem, announceable: bool = True, offset: str | None = None) -> EventTurn:
    return EventTurn(items=tuple(items), announceable=announceable, offset=offset)


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class RecordingCheckpoint:
    def __init__(self, ok: bool = True) -> None:
        self.writes: list[str] = []
        self._ok = ok

    def write(self, offset: str) -> bool:
        self.writes.append(offset)
        return self._ok


class FlakyAudit:
    """Audit double whose write() returns scripted ok values (True after exhausted)."""

    def __init__(self, results: list[bool]) -> None:
        self._results = list(results)
        self.records: list[dict] = []

    def write(self, record) -> bool:
        ok = self._results.pop(0) if self._results else True
        if ok:
            self.records.append(dict(record))
        return ok


class GrowingStatsSession:
    """Event session whose cumulative stats grow per turn, like the real SDK.

    Turn 1 (triage): homelab_health + publish_handoff(hf-1), 1000/200 tokens.
    Turn 2+ (interrogation): + one homelab_health, +300/+50 tokens.
    """

    def __init__(self) -> None:
        from tests.conftest import TRIAGE_REPLY

        self.reply = TRIAGE_REPLY
        self.contents: list[str] = []
        self.closed = False
        self._turns = 0

    async def run_turn(self, text: str) -> str:
        self.contents.append(text)
        self._turns += 1
        return self.reply

    async def close(self) -> None:
        self.closed = True

    def stats(self) -> SessionStats:
        calls = [
            ToolCallRecord("homelab_health", "read-only"),
            ToolCallRecord("publish_handoff", "notify-only", "hf-1"),
        ]
        inp, outp = 1000, 200
        if self._turns >= 2:
            calls.append(ToolCallRecord("homelab_health", "read-only"))
            inp, outp = inp + 300, outp + 50
        return SessionStats(tool_calls=tuple(calls), model="m",
                            input_tokens=inp, output_tokens=outp)


class GrowingStatsFactory:
    def __init__(self) -> None:
        self.created: list[GrowingStatsSession] = []

    def create(self):
        s = GrowingStatsSession()
        self.created.append(s)
        return s

    @property
    def create_count(self) -> int:
        return len(self.created)


class FailingSession:
    """An AgentSession whose triage turn raises (poison event)."""

    def __init__(self) -> None:
        self.closed = False

    async def run_turn(self, text: str) -> str:
        raise RuntimeError("triage boom")

    async def close(self) -> None:
        self.closed = True

    def stats(self):
        return None


class FailingFactory:
    def __init__(self) -> None:
        self.created: list[FailingSession] = []

    def create(self):
        s = FailingSession()
        self.created.append(s)
        return s


# --- Per-triage flush: prompt, open-session, hard-kill survivable ----------


async def test_triage_record_written_at_completion_with_session_open(tmp_path):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    factory = EventSessionFactory(stats=handoff_stats("hf-1"))
    core = AgentCore(factory, channel, clock=make_clock([0]), audit=audit)
    await core.process(_turn(_item("Gatus: svc/api")))
    # No aclose(): a SIGKILL now would still leave the record on disk.
    recs = _records(tmp_path / "a.jsonl")
    assert len(recs) == 1 and recs[0]["trigger"] == "event"
    assert factory.created[0].closed is False  # session stays open for interrogation


async def test_two_event_turns_two_distinct_records_no_conflation(tmp_path):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    factory = EventSessionFactory(stats=handoff_stats("hf"))
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1]), audit=audit)
    await core.process(_turn(_item("Gatus: svc/a", eid="a")))
    await core.process(_turn(_item("Gatus: svc/b", eid="b")))
    recs = _records(tmp_path / "a.jsonl")
    assert len(recs) == 2  # one per incident (displace → separate sessions)
    assert recs[0]["event"][0]["identity_key"] == "gatus:svc/a"
    assert recs[1]["event"][0]["identity_key"] == "gatus:svc/b"


async def test_no_double_write_when_event_session_later_closes(tmp_path):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    factory = EventSessionFactory(stats=handoff_stats("hf"))
    core = AgentCore(factory, channel, clock=make_clock([0]), audit=audit)
    await core.process(_turn(_item("Gatus: svc/api")))
    await core.aclose()  # record already flushed at triage → no duplicate on close
    assert len(_records(tmp_path / "a.jsonl")) == 1


# --- Checkpoint advance is gated on the record being durable ---------------


async def test_checkpoint_advances_after_record_is_durable(tmp_path):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    cp = RecordingCheckpoint()
    factory = EventSessionFactory(stats=handoff_stats("hf"))
    core = AgentCore(factory, channel, clock=make_clock([0]), audit=audit, checkpoint=cp)
    await core.process(_turn(_item("Gatus: svc/api"), offset="e5"))
    assert cp.writes == ["e5"]  # advanced to the batch offset


async def test_checkpoint_not_advanced_when_audit_write_fails(tmp_path):
    channel = FakeChannel()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    audit = AuditLog(blocker / "a.jsonl")  # parent is a file → write() returns False
    cp = RecordingCheckpoint()
    factory = EventSessionFactory(stats=handoff_stats("hf"))
    core = AgentCore(factory, channel, clock=make_clock([0]), audit=audit, checkpoint=cp)
    await core.process(_turn(_item("Gatus: svc/api"), offset="e5"))
    assert cp.writes == []  # gate: no durable record → no advance → event replays


async def test_errored_triage_records_error_then_advances(tmp_path):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    cp = RecordingCheckpoint()
    core = AgentCore(FailingFactory(), channel, clock=make_clock([0]), audit=audit, checkpoint=cp)
    await core.process(_turn(_item("Gatus: svc/api"), offset="e5"))
    recs = _records(tmp_path / "a.jsonl")
    assert len(recs) == 1 and recs[0]["outcome"] == "error"
    assert recs[0]["event"][0]["identity_key"] == "gatus:svc/api"  # not an anonymous blank
    assert cp.writes == ["e5"]  # poison event does not reprocess forever


async def test_checkpoint_marker_advances_and_creates_no_session():
    channel = FakeChannel()
    cp = RecordingCheckpoint()
    factory = EventSessionFactory()
    core = AgentCore(factory, channel, clock=make_clock([0]), checkpoint=cp)
    await core.process(CheckpointMarker(offset="e9"))
    assert cp.writes == ["e9"]
    assert factory.create_count == 0  # a marker is bookkeeping, not a turn


# --- Recurrence-ref wiring (removes the dead note_handoff) ------------------


# --- CRITICAL: cursor must never leapfrog a non-durable event ---------------


async def test_checkpoint_never_leapfrogs_a_failed_flush_via_marker(tmp_path):
    channel = FakeChannel()
    audit = FlakyAudit([False])  # batch N's triage-record write fails
    cp = RecordingCheckpoint()
    factory = EventSessionFactory(stats=handoff_stats("hf"))
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1]),
                     audit=audit, checkpoint=cp)
    await core.process(_turn(_item("Gatus: svc/api"), offset="e5"))  # N: write fails
    await core.process(CheckpointMarker(offset="e9"))                 # N+1: marker
    assert cp.writes == []  # latched → the marker cannot leapfrog the failed event


async def test_failed_flush_latches_and_blocks_a_later_successful_triage(tmp_path):
    channel = FakeChannel()
    audit = FlakyAudit([False, True])  # N fails, N+1 succeeds
    cp = RecordingCheckpoint()
    factory = EventSessionFactory(stats=handoff_stats("hf"))
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1, 2, 2]),
                     audit=audit, checkpoint=cp)
    await core.process(_turn(_item("Gatus: svc/a", eid="a"), offset="e1"))  # fails
    await core.process(_turn(_item("Gatus: svc/b", eid="b"), offset="e2"))  # succeeds
    assert cp.writes == []           # latch blocks the later success too
    assert len(audit.records) == 1   # N+1's record IS written (triage still ran)


async def test_latch_sends_one_shot_degraded_notification(tmp_path):
    channel = FakeChannel()
    audit = FlakyAudit([False, False])  # two genuine failures
    cp = RecordingCheckpoint()
    factory = EventSessionFactory(stats=handoff_stats("hf"))
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1, 2, 2]),
                     audit=audit, checkpoint=cp)
    # announceable=False → replies suppressed, so the only channel send is the notice.
    await core.process(_turn(_item("Gatus: svc/a", eid="a"), announceable=False, offset="e1"))
    await core.process(_turn(_item("Gatus: svc/b", eid="b"), announceable=False, offset="e2"))
    assert len(channel.sent) == 1                    # one-shot despite two failures
    assert "restart" in channel.sent[0].lower()      # the degraded-durability notice


async def test_no_audit_event_turn_sends_no_degraded_notice():
    # M4 makes a no-audit flush return ok=False by design; that must NOT be treated
    # as a genuine failure (no latch, no notice).
    channel = FakeChannel()
    factory = EventSessionFactory()
    core = AgentCore(factory, channel, clock=make_clock([0]))  # audit=None, checkpoint=None
    await core.process(_turn(_item("Gatus: svc/api"), announceable=False))
    assert channel.sent == []


# --- MAJOR #2: owner interrogation audited as its own delta record ----------


async def test_owner_interrogation_recorded_as_separate_delta_record(tmp_path):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    factory = GrowingStatsFactory()
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1]),
                     audit=audit, model="m")
    await core.process(_turn(_item("Gatus: svc/api")))   # triage → record 1 (flushed now)
    await core.process("what does the backup log say?")  # interrogation, same session
    await core.aclose()                                  # flush the owner record
    recs = _records(tmp_path / "a.jsonl")
    assert len(recs) == 2
    triage, interro = recs[0], recs[1]
    assert factory.create_count == 1  # same session (interrogation continuity)
    # Triage record: full triage activity.
    assert triage["trigger"] == "event"
    assert [c["name"] for c in triage["tool_calls"]] == ["homelab_health", "publish_handoff"]
    assert triage["handoff_message_id"] == "hf-1"
    assert triage["usage"]["input_tokens"] == 1000
    # Interrogation record: ONLY its own delta — no double-count, no borrowed handoff.
    assert interro["trigger"] == "owner-message"
    assert [c["name"] for c in interro["tool_calls"]] == ["homelab_health"]
    assert interro["usage"]["input_tokens"] == 300   # not 1300
    assert interro["usage"]["output_tokens"] == 50   # not 250
    assert interro["handoff_message_id"] is None


async def test_handoff_id_pushed_to_recurrence_sink(tmp_path):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    calls: list[tuple[str, str]] = []
    factory = EventSessionFactory(stats=handoff_stats("hf-9"))
    core = AgentCore(
        factory, channel, clock=make_clock([0]), audit=audit,
        handoff_sink=lambda key, ref: calls.append((key, ref)),
    )
    await core.process(_turn(_item("Gatus: svc/api")))
    assert calls == [("gatus:svc/api", "hf-9")]  # wired for the next batch's recurrence
