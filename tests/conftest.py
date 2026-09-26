"""Shared test doubles and helpers."""

from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncIterator

import httpx
import pytest

from henk.agent.session import AgentSession, SessionStats, ToolCallRecord
from henk.channel.base import InboundMessage, SendOutcome


class FakeChannel:
    """Records everything sent; used as a channel-adapter test double.

    ``sent`` is the ordered, text-only history appended by BOTH send operations,
    so assertions about what the owner saw are indifferent to which path carried
    it. ``calls`` is the same history with the path and the caller-supplied
    failure notice attached, for assertions that are specifically about the
    reply-vs-proactive distinction.

    Both operations report ``DELIVERED``: this is a cooperative double, and a
    test that needs a failing send must exercise the real adapter over a failing
    bridge (that is the defect this contract exists to surface).

    The acknowledgement operations record into ``acks`` only, never into
    ``sent`` or ``calls``: they are not messages, and every existing assertion
    over those two lists must stay exactly as it was. They always report
    accepted, for the same cooperative reason.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        #: (kind, text, failure_notice) where kind is "reply" or "proactive".
        self.calls: list[tuple[str, str, str | None]] = []
        #: ("receipt", channel_ref) | ("start", None) | ("stop", None), in order.
        self.acks: list[tuple[str, str | None]] = []

    async def send(self, text: str) -> SendOutcome:
        self.sent.append(text)
        self.calls.append(("reply", text, None))
        return SendOutcome.DELIVERED

    async def send_proactive(
        self, text: str, *, failure_notice: str | None = None
    ) -> SendOutcome:
        self.sent.append(text)
        self.calls.append(("proactive", text, failure_notice))
        return SendOutcome.DELIVERED

    async def acknowledge(self, channel_ref: str | None) -> bool:
        self.acks.append(("receipt", channel_ref))
        return True

    async def start_working(self) -> bool:
        self.acks.append(("start", None))
        return True

    async def stop_working(self) -> bool:
        self.acks.append(("stop", None))
        return True


class RecordingSession:
    """A fake AgentSession that echoes a scripted reply and records turns."""

    def __init__(self, reply: str = "ok", *, fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.turns: list[str] = []
        self.closed = False

    async def run_turn(self, text: str) -> str:
        self.turns.append(text)
        if self.fail:
            raise RuntimeError("simulated SDK failure")
        return f"{self.reply}:{text}"

    async def close(self) -> None:
        self.closed = True


class FakeSessionFactory:
    """Creates RecordingSessions and counts how many times create() was called."""

    def __init__(self, reply: str = "ok", *, fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.created: list[RecordingSession] = []

    def create(self) -> AgentSession:
        session = RecordingSession(self.reply, fail=self.fail)
        self.created.append(session)
        return session

    @property
    def create_count(self) -> int:
        return len(self.created)


#: A well-formed triage reply carrying the full arc (diagnosis+confidence/fix/pickup).
TRIAGE_REPLY = (
    "The health ETL looks stalled.\n"
    "Diagnosis: HealthEtl job stopped emitting (confidence: moderate)\n"
    "Fix: restart the health-etl unit on rp5\n"
    "Pickup: full handoff on henk-handoffs — run henk-pickup"
)


class EventSession:
    """AgentSession fake that records the text of each turn and exposes stats.

    Records the *content* passed to ``run_turn`` (so tests can assert triage
    framing on event turns and its absence on owner turns) and returns a scripted
    reply. ``stats`` feeds the audit record.
    """

    def __init__(self, reply: str = TRIAGE_REPLY, stats: SessionStats | None = None):
        self.reply = reply
        self.contents: list[str] = []
        self.closed = False
        self._stats = stats

    async def run_turn(self, text: str) -> str:
        self.contents.append(text)
        return self.reply

    async def close(self) -> None:
        self.closed = True

    def stats(self) -> SessionStats | None:
        return self._stats


class EventSessionFactory:
    """Creates EventSessions with a shared scripted reply + stats."""

    def __init__(self, reply: str = TRIAGE_REPLY, stats: SessionStats | None = None):
        self.reply = reply
        self.stats = stats
        self.created: list[EventSession] = []

    def create(self) -> AgentSession:
        session = EventSession(self.reply, self.stats)
        self.created.append(session)
        return session

    @property
    def create_count(self) -> int:
        return len(self.created)


def handoff_stats(result_id: str = "hf-1", model: str = "claude-sonnet-5") -> SessionStats:
    """Stats for a session that gathered evidence and published a handoff."""
    return SessionStats(
        tool_calls=(
            ToolCallRecord("homelab_health", "read-only"),
            ToolCallRecord("publish_handoff", "notify-only", result_id),
        ),
        model=model,
        input_tokens=1200,
        output_tokens=300,
    )


class FakeBridge:
    """A SignalBridge double: yields scripted envelopes, records sends.

    ``script`` is a list where each item is either a dict (an envelope to yield)
    or an Exception instance (raised at that point in the receive stream).

    Acknowledgement operations (owner-acknowledgement task 2.1) record into
    ``receipts`` and ``typing`` only once the request has been ACCEPTED, i.e.
    after any fault for that operation has cleared, so "recorded" means "the
    bridge completed it" and an ordering assertion over ``typing`` is an
    assertion about completions. ``ack_attempts`` records every call, refused or
    hung ones included, which is what makes "exactly one attempt" checkable.

    ``ack_faults`` is keyed ``"receipt"``, ``"start"`` or ``"stop"``: an
    exception instance is raised on every call of that operation, and an
    ``asyncio.Event`` is awaited first (a hang until the test sets it, or
    forever). Faults are per operation, so a test can hang only the stop; sends
    are never affected.

    ``hold_open``: once the script is exhausted, ``receive()`` blocks forever
    instead of returning. A clean stream end makes ``SignalAdapter.messages()``
    sleep and call ``receive()`` again, which replays the same script, so an
    ``App.run`` test would otherwise see its envelopes over and over and never
    settle. The default keeps every existing test's behaviour.
    """

    def __init__(self, script: list | None = None, *, hold_open: bool = False) -> None:
        self._script = list(script or [])
        self.sends: list[tuple[str, str]] = []
        #: (recipient, timestamp) per accepted read receipt.
        self.receipts: list[tuple[str, int]] = []
        #: ("start" | "stop", recipient) per accepted typing-indicator request.
        self.typing: list[tuple[str, str]] = []
        #: (operation, recipient) per acknowledgement call, accepted or not.
        self.ack_attempts: list[tuple[str, str]] = []
        self.ack_faults: dict[str, Exception | asyncio.Event] = {}
        self.hold_open = hold_open

    async def receive(self) -> AsyncIterator[dict]:
        for item in self._script:
            if isinstance(item, Exception):
                raise item
            yield item
        if self.hold_open:
            await asyncio.Event().wait()

    async def send(self, recipient: str, text: str) -> None:
        self.sends.append((recipient, text))

    async def _ack(self, operation: str, recipient: str) -> None:
        self.ack_attempts.append((operation, recipient))
        fault = self.ack_faults.get(operation)
        if isinstance(fault, asyncio.Event):
            await fault.wait()
        elif fault is not None:
            raise fault

    async def send_receipt(self, recipient: str, timestamp: int) -> None:
        await self._ack("receipt", recipient)
        self.receipts.append((recipient, timestamp))

    async def start_typing(self, recipient: str) -> None:
        await self._ack("start", recipient)
        self.typing.append(("start", recipient))

    async def stop_typing(self, recipient: str) -> None:
        await self._ack("stop", recipient)
        self.typing.append(("stop", recipient))


def make_clock(values: list[float]):
    """Return a callable that yields successive values, holding the last."""
    it = iter(values)
    last = [values[0] if values else 0.0]

    def clock() -> float:
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]

    return clock


def mock_client(handler) -> httpx.AsyncClient:
    """Build an AsyncClient backed by a MockTransport calling ``handler``."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def inbound(text: str, sender: str = "+31600000000", *, is_group: bool = False):
    return InboundMessage(sender=sender, text=text, timestamp=0.0, is_group=is_group)


@pytest.fixture
def fake_channel() -> FakeChannel:
    return FakeChannel()


# --- Process-timezone guard (reminders design D8a) ------------------------

#: The process default zone is a separate hazard from the zone *database*, and the
#: larger one, because it fails asymmetrically: a bare `datetime.now()`, a
#: zone-less `fromtimestamp(t)`, a bare `.astimezone()`, or `.timestamp()` on a
#: naive value all read it silently. The development host resolves as
#: Europe/Amsterdam and a slim container with no TZ is UTC, so the likeliest slip
#: is green locally and two hours wrong on rp5 only.
#:
#: `Pacific/Kiritimati` (+14) is the hostile value that earns its place: a leak
#: there changes the *date*, not merely the hour, so it fails assertions an
#: hour-only offset would slip past. `Europe/Amsterdam` is included as the
#: false-negative control — a leak is invisible under it, which is the whole point.
HOSTILE_PROCESS_ZONES = ("UTC", "Pacific/Kiritimati", "Europe/Amsterdam")


@pytest.fixture(params=HOSTILE_PROCESS_ZONES)
def process_tz(request) -> str:
    """Run the test body under each hostile process timezone in turn.

    Every clock-touching surface uses this — the resolver, the renderer, the
    reminder owner commands and the per-turn time header — because the dispatcher
    is where `datetime.now()` is most idiomatic to write, not just the resolver.
    The suite is the guard; the image's `TZ=UTC` is only the floor under it.
    """
    original = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    try:
        yield request.param
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()
