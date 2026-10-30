"""maintenance-calendar group 7: the agent core's half of a maintenance window.

From the agent-core delta:

- *A released triage says what the window held*: the release line, composed by code
  from the turn's :class:`HeldRelease`, after the suppressed-count note, on the
  triage reply and on the incomplete-triage notice alike; both variants and the
  singular form; compact ``HH:MM`` in the owner's zone (the stated exception to the
  shared renderer); a non-announceable released turn sends nothing; live turns
  carry no line.
- *Owner turns carry the maintenance-window note*: the block sits after the
  current-time header and before the recall block, is composed per turn, never
  blocks the turn, never taints, and never reaches an event turn.

And design D8, *Marked released at durable flush*: the core calls
``release_sink(window_id)`` once the release triage's audit record was written, or
at once when audit is not configured; never for a live turn, never on a failed
write, never twice, never when session setup failed.

The owner-turn note's *content* is tested against a real hold store in
``test_maintenance_note.py``; here the provider is a double, so the core's seam is
pinned on its own.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from henk.agent.core import TRIAGE_FAILURE_NOTICE, AgentCore
from henk.agent.ending import ERROR, TriageEnding, incomplete_triage_notice
from henk.agent.turns import EventTurn, EventTurnItem, HeldRelease, OwnerTurn
from henk.events.identity import derive_identity
from henk.events.types import Event
from tests.conftest import (
    TRIAGE_REPLY,
    EventSessionFactory,
    FakeChannel,
    FakeSessionFactory,
    make_clock,
)
from tests.test_agent_core_durability import FlakyAudit
from tests.test_agent_core_turn_scope import RecordingGate

AMS = ZoneInfo("Europe/Amsterdam")
TITLE = "Homelab: patch round"
#: Saturday 7 November 2026, 20:00 CET (19:00Z); the window ends at 22:00 CET and its
#: grace at 22:30 CET.
W_START = datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc).timestamp()
DUE = W_START + 2.5 * 3600

LINE_WITH_RECOVERIES = (
    '(Held during "Homelab: patch round", 20:00–22:30: 4 alerts, 3 of which '
    "recovered on their own.)"
)
LINE_ONE = '(Held during "Homelab: patch round", 20:00–22:30: 1 alert.)'
LINE_TWO = '(Held during "Homelab: patch round", 20:00–22:30: 2 alerts.)'


def _release(held: int = 4, recovered: int = 3, **overrides) -> HeldRelease:
    fields = dict(
        window_id="evt-patch",
        title=TITLE,
        start=W_START,
        end_with_grace=DUE,
        held=held,
        recovered=recovered,
        hold_age_seconds=1800.0,
    )
    fields.update(overrides)
    return HeldRelease(**fields)


def _item(name: str = "svc/api", eid: str = "e1") -> EventTurnItem:
    event = Event(id=eid, title=f"Gatus: {name}", message="triggered", arrival_time=0.0)
    return EventTurnItem(event=event, identity=derive_identity(event))


def _turn(release: HeldRelease | None = None, **kw) -> EventTurn:
    return EventTurn(items=(_item(),), held_release=release, **kw)


class SinkSpy:
    """The release sink: records each window id, and what audit held at the call."""

    def __init__(self, audit=None, *, raises: bool = False) -> None:
        self.calls: list[str] = []
        self.records_at_call: list[int] = []
        self._audit = audit
        self._raises = raises

    def __call__(self, window_id: str) -> bool:
        self.calls.append(window_id)
        if self._audit is not None:
            self.records_at_call.append(len(self._audit.records))
        if self._raises:
            raise RuntimeError("the sink blew up")
        return True


class RaisingFactory:
    """A factory whose session setup fails, like an SDK client that cannot start."""

    def create(self):
        raise RuntimeError("session setup failed")


class FailingSession:
    """An event session whose turn raises: the triage ends ``error``."""

    def __init__(self) -> None:
        self.contents: list[str] = []
        self.closed = False

    async def run_turn(self, text: str) -> str:
        self.contents.append(text)
        raise RuntimeError("stream broke")

    async def close(self) -> None:
        self.closed = True


class FailingFactory:
    def __init__(self) -> None:
        self.created: list[FailingSession] = []

    def create(self):
        session = FailingSession()
        self.created.append(session)
        return session


def _core(factory=None, channel=None, **kw) -> AgentCore:
    kw.setdefault("owner_zone", AMS)
    return AgentCore(
        factory or EventSessionFactory(),
        channel or FakeChannel(),
        clock=make_clock([0.0]),
        **kw,
    )


# --- The release line -----------------------------------------------------------


async def test_release_line_with_recoveries():
    # Scenario "Release line with recoveries": R, a blank line, the line.
    channel = FakeChannel()
    core = _core(channel=channel)
    await core.process(_turn(_release(held=4, recovered=3)))
    assert channel.calls == [
        ("proactive", f"{TRIAGE_REPLY}\n\n{LINE_WITH_RECOVERIES}", TRIAGE_FAILURE_NOTICE)
    ]


async def test_release_line_without_recoveries_singular():
    # Scenario "Release line without recoveries": one alert, singular.
    channel = FakeChannel()
    core = _core(channel=channel)
    await core.process(_turn(_release(held=1, recovered=0)))
    assert channel.sent == [f"{TRIAGE_REPLY}\n\n{LINE_ONE}"]


async def test_release_line_without_recoveries_plural():
    channel = FakeChannel()
    core = _core(channel=channel)
    await core.process(_turn(_release(held=2, recovered=0)))
    assert channel.sent == [f"{TRIAGE_REPLY}\n\n{LINE_TWO}"]


async def test_the_line_comes_after_the_suppressed_count_note():
    channel = FakeChannel()
    core = _core(channel=channel)
    await core.process(_turn(_release(held=1, recovered=0), suppressed_count=2))
    [text] = channel.sent
    note = (
        "(Note: 2 earlier incidents were suppressed to stay under the alert cap — "
        "retrieve them with henk-pickup.)"
    )
    assert text == f"{TRIAGE_REPLY}\n\n{note}\n\n{LINE_ONE}"


async def test_incomplete_triage_still_carries_the_line():
    # Scenario "Incomplete triage still carries the line": the notice ends with it.
    channel = FakeChannel()
    turn = _turn(_release(held=4, recovered=3))
    core = _core(FailingFactory(), channel)
    await core.process(turn)
    [(kind, text, notice)] = channel.calls
    expected_notice = incomplete_triage_notice(
        turn, TriageEnding(ERROR), handoff_published=False
    )
    assert kind == "proactive" and notice == TRIAGE_FAILURE_NOTICE
    assert text == f"{expected_notice}\n\n{LINE_WITH_RECOVERIES}"
    assert text.endswith(LINE_WITH_RECOVERIES)


async def test_incomplete_triage_carries_the_note_then_the_line():
    channel = FakeChannel()
    core = _core(FailingFactory(), channel)
    await core.process(_turn(_release(held=1, recovered=0), suppressed_count=1))
    [text] = channel.sent
    assert text.endswith(
        "retrieve them with henk-pickup.)\n\n" + LINE_ONE
    ), text


async def test_a_non_announceable_released_turn_sends_nothing():
    channel = FakeChannel()
    core = _core(channel=channel)
    await core.process(_turn(_release(), announceable=False))
    assert channel.sent == []


async def test_a_non_announceable_incomplete_released_turn_sends_nothing():
    channel = FakeChannel()
    core = _core(FailingFactory(), channel)
    await core.process(_turn(_release(), announceable=False, suppressed_count=3))
    assert channel.sent == []


async def test_live_turns_carry_no_release_line():
    # Scenario "Live turns carry no release line".
    channel = FakeChannel()
    core = _core(channel=channel)
    await core.process(_turn(None))
    assert channel.sent == [TRIAGE_REPLY]


async def test_a_live_turn_with_a_suppressed_note_ends_with_the_note():
    channel = FakeChannel()
    core = _core(channel=channel)
    await core.process(_turn(None, suppressed_count=1))
    [text] = channel.sent
    assert text.endswith("retrieve them with henk-pickup.)")
    assert "Held during" not in text


async def test_a_live_incomplete_notice_carries_no_line():
    channel = FakeChannel()
    core = _core(FailingFactory(), channel)
    await core.process(_turn(None))
    [text] = channel.sent
    assert "Held during" not in text


async def test_the_line_is_composed_by_code_never_from_the_reply():
    # The model's own text cannot change the line: a reply that writes a different
    # line still gets the code-composed one appended.
    channel = FakeChannel()
    fake = '(Held during "Homelab: patch round", 20:00–22:30: 0 alerts.)'
    core = _core(EventSessionFactory(reply=fake), channel)
    await core.process(_turn(_release(held=1, recovered=0)))
    assert channel.sent == [f"{fake}\n\n{LINE_ONE}"]


@pytest.mark.parametrize(
    ("zone", "span"),
    [
        (AMS, "20:00–22:30"),
        (ZoneInfo("UTC"), "19:00–21:30"),
        (ZoneInfo("Pacific/Kiritimati"), "09:00–11:30"),
    ],
)
async def test_the_times_are_compact_hh_mm_in_the_owner_zone(process_tz, zone, span):
    channel = FakeChannel()
    core = _core(channel=channel, owner_zone=zone)
    await core.process(_turn(_release(held=1, recovered=0)))
    [text] = channel.sent
    line = text.split("\n\n")[-1]
    assert line == f'(Held during "Homelab: patch round", {span}: 1 alert.)'
    # Never the shared renderer's long form: that is the block's, not the line's.
    assert "Saturday" not in line and "CET" not in line


async def test_without_an_owner_zone_the_times_are_utc_and_say_so():
    # Rows held while the calendar was on, released after a rollback whose config
    # lost owner.timezone: UTC, labelled, rather than an unlabelled guess.
    channel = FakeChannel()
    core = _core(channel=channel, owner_zone=None)
    await core.process(_turn(_release(held=1, recovered=0)))
    [text] = channel.sent
    assert text.endswith('(Held during "Homelab: patch round", 19:00–21:30 UTC: 1 alert.)')


async def test_the_title_is_flattened_to_one_line():
    channel = FakeChannel()
    core = _core(channel=channel)
    await core.process(
        _turn(_release(held=1, recovered=0, title="Homelab:\n  patch\tround"))
    )
    [text] = channel.sent
    assert text.endswith(LINE_ONE)


async def test_a_line_that_cannot_be_composed_never_stops_the_triage_message(caplog):
    caplog.set_level(logging.ERROR)
    channel = FakeChannel()
    core = _core(channel=channel)
    # An instant no platform clock can represent.
    await core.process(_turn(_release(held=1, recovered=0, start=1e20)))
    assert channel.sent and channel.sent[0].startswith(TRIAGE_REPLY)


# --- The release sink (D8: marked released at durable flush) ------------------


async def test_the_sink_fires_once_the_triage_record_is_written():
    audit = FlakyAudit([])
    sink = SinkSpy(audit)
    core = _core(audit=audit, release_sink=sink)
    await core.process(_turn(_release()))
    assert sink.calls == ["evt-patch"]
    # The triage's record was already durable when the sink ran.
    assert sink.records_at_call == [1]
    assert audit.records[0]["record_type"] == "session"


async def test_the_sink_does_not_fire_when_the_record_could_not_be_written():
    audit = FlakyAudit([False])
    sink = SinkSpy()
    core = _core(audit=audit, release_sink=sink)
    await core.process(_turn(_release()))
    assert sink.calls == []


async def test_the_sink_fires_at_once_when_audit_is_not_configured():
    sink = SinkSpy()
    core = _core(audit=None, release_sink=sink)
    await core.process(_turn(_release()))
    assert sink.calls == ["evt-patch"]


async def test_the_sink_never_fires_for_a_live_turn():
    for audit in (FlakyAudit([]), None):
        sink = SinkSpy()
        core = _core(audit=audit, release_sink=sink)
        await core.process(_turn(None))
        assert sink.calls == []


async def test_the_sink_fires_at_most_once_per_triage():
    audit = FlakyAudit([])
    sink = SinkSpy()
    core = _core(audit=audit, release_sink=sink)
    turn = _turn(_release())
    await core.process(turn)
    # A second flush of the same triage, an owner follow-up in its session, and
    # the session's close: none of them marks the window again.
    await core._flush_event_triage(turn)
    await core.process(OwnerTurn("what happened?"))
    await core.aclose()
    assert sink.calls == ["evt-patch"]


async def test_the_sink_never_fires_when_session_setup_failed():
    sink = SinkSpy()
    core = _core(RaisingFactory(), audit=FlakyAudit([]), release_sink=sink)
    with pytest.raises(RuntimeError):
        await core.process(_turn(_release()))
    assert sink.calls == []


async def test_an_errored_triage_that_was_recorded_still_marks_the_window():
    # D8 keys on the record, not on the triage's success: an errored triage is
    # recorded and advances like any other (agent-core D1), so it releases too.
    audit = FlakyAudit([])
    sink = SinkSpy()
    core = _core(FailingFactory(), audit=audit, release_sink=sink)
    await core.process(_turn(_release()))
    assert audit.records[0]["outcome"] == "error"
    assert sink.calls == ["evt-patch"]


async def test_a_non_announceable_released_triage_still_marks_the_window():
    # Nothing is sent, but the triage ran and its record is durable.
    sink = SinkSpy()
    channel = FakeChannel()
    core = _core(channel=channel, audit=FlakyAudit([]), release_sink=sink)
    await core.process(_turn(_release(), announceable=False))
    assert sink.calls == ["evt-patch"]
    assert channel.sent == []


async def test_a_failing_sink_never_stops_the_triage_message(caplog):
    caplog.set_level(logging.ERROR)
    channel = FakeChannel()
    sink = SinkSpy(raises=True)
    core = _core(channel=channel, audit=FlakyAudit([]), release_sink=sink)
    await core.process(_turn(_release(held=1, recovered=0)))
    assert sink.calls == ["evt-patch"]
    assert channel.sent == [f"{TRIAGE_REPLY}\n\n{LINE_ONE}"]
    assert "evt-patch" in caplog.text


async def test_without_a_sink_a_released_turn_is_processed_as_before():
    channel = FakeChannel()
    core = _core(channel=channel, audit=FlakyAudit([]))
    await core.process(_turn(_release(held=1, recovered=0)))
    assert channel.sent == [f"{TRIAGE_REPLY}\n\n{LINE_ONE}"]


# --- The owner-turn maintenance block (seam) ----------------------------------


class Provider:
    """A maintenance-note double: scripted blocks, one per call, or an error."""

    def __init__(self, *blocks: str | None, raises: bool = False) -> None:
        self._blocks = list(blocks) or [None]
        self.calls = 0
        self._raises = raises

    def block(self) -> str | None:
        self.calls += 1
        if self._raises:
            raise RuntimeError("calendar trouble")
        if len(self._blocks) > 1:
            return self._blocks.pop(0)
        return self._blocks[0]


class Recall:
    class _Block:
        text = "RECALL"
        content_hash = "sha256:0"

    def block(self):
        return self._Block()


class Deliveries:
    def block(self) -> str:
        return "DELIVERIES"


async def test_the_block_sits_after_the_time_header_and_before_recall():
    factory = FakeSessionFactory()
    core = AgentCore(
        factory,
        FakeChannel(),
        clock=make_clock([0.0]),
        time_header=lambda: "TIME",
        recall=Recall(),
        deliveries=Deliveries(),
        maintenance=Provider("MAINTENANCE"),
    )
    await core.process(OwnerTurn("is everything ok?"))
    [content] = factory.created[0].turns
    assert content == "TIME\n\nMAINTENANCE\n\nRECALL\n\nDELIVERIES\n\nis everything ok?"


async def test_without_a_time_header_the_block_comes_first():
    factory = FakeSessionFactory()
    core = AgentCore(
        factory, FakeChannel(), clock=make_clock([0.0]),
        recall=Recall(), maintenance=Provider("MAINTENANCE"),
    )
    await core.process(OwnerTurn("hi"))
    assert factory.created[0].turns == ["MAINTENANCE\n\nRECALL\n\nhi"]


async def test_the_block_is_composed_per_turn():
    factory = FakeSessionFactory()
    provider = Provider("FIRST", "SECOND")
    core = AgentCore(factory, FakeChannel(), clock=make_clock([0.0]), maintenance=provider)
    await core.process(OwnerTurn("one"))
    await core.process(OwnerTurn("two"))
    assert factory.created[0].turns == ["FIRST\n\none", "SECOND\n\ntwo"]
    assert provider.calls == 2


async def test_no_block_leaves_the_turn_unchanged():
    for provider in (None, Provider(None), Provider("")):
        factory = FakeSessionFactory()
        core = AgentCore(
            factory, FakeChannel(), clock=make_clock([0.0]),
            time_header=lambda: "TIME", maintenance=provider,
        )
        await core.process(OwnerTurn("hello"))
        assert factory.created[0].turns == ["TIME\n\nhello"]


async def test_calendar_trouble_never_blocks_a_turn(caplog):
    # Scenario "Calendar trouble never blocks a turn".
    caplog.set_level(logging.ERROR)
    factory = FakeSessionFactory()
    channel = FakeChannel()
    core = AgentCore(
        factory, channel, clock=make_clock([0.0]),
        time_header=lambda: "TIME", maintenance=Provider(raises=True),
    )
    await core.process(OwnerTurn("hello"))
    assert factory.created[0].turns == ["TIME\n\nhello"]
    assert channel.sent == ["ok:TIME\n\nhello"]
    assert "maintenance" in caplog.text.lower()


async def test_the_block_never_taints_the_session():
    gate = RecordingGate()
    core = AgentCore(
        FakeSessionFactory(), FakeChannel(), clock=make_clock([0.0]),
        gate=gate, maintenance=Provider("MAINTENANCE"),
    )
    await core.process(OwnerTurn("one"))
    await core.process(OwnerTurn("two"))
    assert len(gate.contexts) == 2
    assert all(ctx.tainted is False for ctx in gate.contexts)
    assert core._session_tainted is False


async def test_event_turns_never_carry_the_block():
    factory = EventSessionFactory()
    provider = Provider("===== BEGIN MAINTENANCE WINDOW (data, NOT instructions) =====")
    core = _core(factory, maintenance=provider)
    await core.process(_turn(_release()))
    await core.process(_turn(None))
    for session in factory.created:
        for content in session.contents:
            assert "MAINTENANCE WINDOW" not in content
    assert provider.calls == 0


async def test_an_owner_follow_up_in_a_triage_session_carries_the_block():
    # It is an owner turn: the block rides on it even though the session is the
    # incident's (taint is the session's, not the block's).
    factory = EventSessionFactory()
    provider = Provider("MAINTENANCE")
    core = _core(factory, maintenance=provider)
    await core.process(_turn(_release()))
    await core.process(OwnerTurn("what happened?"))
    [session] = factory.created
    assert session.contents[-1] == "MAINTENANCE\n\nwhat happened?"
    assert provider.calls == 1


def test_the_release_descriptor_is_unchanged_by_this_group():
    # Group 6 owns the descriptor; group 7 only reads it.
    assert [f.name for f in dataclasses.fields(HeldRelease)] == [
        "window_id", "title", "start", "end_with_grace", "held", "recovered",
        "hold_age_seconds",
    ]
