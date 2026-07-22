"""Incident-triage tests (task 2.2), from specs/incident-triage. SDK mocked.

Triageable → triage session + proactive message when announceable; cap-overflow
→ full session, handoff + audit, NO Signal, suppressed count surfaced later; arc
present recorded, arc-miss recorded without blocking delivery; recurrence framing;
no timer sends; owner reply continues the triage session.
"""

from __future__ import annotations

import json
from pathlib import Path

from henk.agent.core import AgentCore
from henk.audit import AuditLog
from henk.agent.turns import EventTurn, EventTurnItem
from henk.events.identity import derive_identity
from henk.events.types import Event
from tests.conftest import (
    EventSessionFactory,
    FakeChannel,
    handoff_stats,
    make_clock,
)


def _item(title: str, message: str = "", *, recurrence: bool = False,
          prior_ref: str | None = None, eid: str = "e1") -> EventTurnItem:
    event = Event(id=eid, title=title, message=message, arrival_time=0.0)
    return EventTurnItem(
        event=event,
        identity=derive_identity(event),
        recurrence=recurrence,
        prior_handoff_ref=prior_ref,
    )


def _turn(*items, announceable=True, suppressed=0) -> EventTurn:
    return EventTurn(items=tuple(items), announceable=announceable, suppressed_count=suppressed)


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- Announceable triage ---------------------------------------------------


async def test_triageable_announceable_sends_and_audits_as_event(tmp_path):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    factory = EventSessionFactory(stats=handoff_stats("hf-42"))
    core = AgentCore(factory, channel, clock=make_clock([0]), audit=audit, model="m")
    await core.process(_turn(_item("Gatus: svc/api", "triggered")))
    await core.aclose()  # flush the session record

    assert len(channel.sent) == 1                     # proactive message delivered
    recs = _records(tmp_path / "a.jsonl")
    assert len(recs) == 1
    r = recs[0]
    assert r["trigger"] == "event"
    assert r["handoff_message_id"] == "hf-42"         # handoff published
    assert r["triage_arc_complete"] is True
    assert r["event"][0]["identity_key"] == "gatus:svc/api"


# --- Cap-overflow: triage + handoff + audit, but no Signal -----------------


async def test_cap_overflow_triages_and_hands_off_without_sending(tmp_path):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    factory = EventSessionFactory(stats=handoff_stats("hf-7"))
    core = AgentCore(factory, channel, clock=make_clock([0]), audit=audit)
    await core.process(_turn(_item("Gatus: svc/api"), announceable=False))
    await core.aclose()

    assert channel.sent == []                          # Signal suppressed
    assert factory.create_count == 1                   # triage session still ran
    r = _records(tmp_path / "a.jsonl")[0]
    assert r["announceable"] is False
    assert r["handoff_message_id"] == "hf-7"           # handoff still published


async def test_suppressed_count_surfaces_on_next_announceable_message():
    channel = FakeChannel()
    factory = EventSessionFactory()
    core = AgentCore(factory, channel, clock=make_clock([0]))
    await core.process(_turn(_item("Gatus: svc/api"), announceable=True, suppressed=2))
    assert "2 earlier incident" in channel.sent[0]
    assert "suppressed" in channel.sent[0]
    assert "henk-pickup" in channel.sent[0]


# --- Triage arc compliance -------------------------------------------------


async def test_arc_complete_recorded_true(tmp_path):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    factory = EventSessionFactory()  # default reply carries the full arc
    core = AgentCore(factory, channel, clock=make_clock([0]), audit=audit)
    await core.process(_turn(_item("Gatus: svc/api")))
    await core.aclose()
    assert _records(tmp_path / "a.jsonl")[0]["triage_arc_complete"] is True


async def test_arc_miss_still_delivers_and_records_false(tmp_path):
    channel = FakeChannel()
    audit = AuditLog(tmp_path / "a.jsonl")
    # Reply missing the Fix line → arc incomplete, but must still be delivered.
    incomplete = "Diagnosis: disk filling (confidence: high)\nPickup: henk-pickup"
    factory = EventSessionFactory(reply=incomplete)
    core = AgentCore(factory, channel, clock=make_clock([0]), audit=audit)
    await core.process(_turn(_item("Gatus: svc/api")))
    await core.aclose()
    assert channel.sent == [incomplete]                        # delivered anyway
    assert _records(tmp_path / "a.jsonl")[0]["triage_arc_complete"] is False


# --- Recurrence framing ----------------------------------------------------


async def test_recurrence_frames_turn_and_references_prior_handoff():
    channel = FakeChannel()
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(factory, channel, clock=make_clock([0]))
    item = _item("Gatus: svc/api", recurrence=True, prior_ref="hf-prev")
    await core.process(_turn(item))
    content = factory.created[0].contents[0]
    assert "Recurrence" in content
    assert "hf-prev" in content


# --- Cadence is condition-triggered ---------------------------------------


async def test_no_sends_without_a_triggering_turn():
    channel = FakeChannel()
    factory = EventSessionFactory()
    AgentCore(factory, channel, clock=make_clock([0]))
    # Construction + no queued turns → nothing is ever sent (no timer path).
    assert channel.sent == []
    assert factory.create_count == 0


# --- Owner reply continues the triage session -----------------------------


async def test_owner_reply_continues_the_triage_session():
    channel = FakeChannel()
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(factory, channel, clock=make_clock([0, 0, 1, 1]))
    await core.process(_turn(_item("Gatus: svc/api")))      # triage session
    await core.process("what does the backup log say?")     # owner follow-up
    assert factory.create_count == 1                        # SAME session
    assert len(factory.created[0].contents) == 2            # follow-up ran in it
