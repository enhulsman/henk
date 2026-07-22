"""Shared test doubles and helpers."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx
import pytest

from henk.agent.session import AgentSession, SessionStats, ToolCallRecord
from henk.channel.base import InboundMessage


class FakeChannel:
    """Records everything sent; used as a channel-adapter test double."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


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
    """

    def __init__(self, script: list | None = None) -> None:
        self._script = list(script or [])
        self.sends: list[tuple[str, str]] = []

    async def receive(self) -> AsyncIterator[dict]:
        for item in self._script:
            if isinstance(item, Exception):
                raise item
            yield item

    async def send(self, recipient: str, text: str) -> None:
        self.sends.append((recipient, text))


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
