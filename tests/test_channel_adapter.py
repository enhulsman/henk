"""Channel-adapter contract tests (task 2.2), from specs/channel-adapter."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from henk.channel.allowlist import AllowlistFilter
from henk.channel.base import split_message
from henk.channel.signal import SignalAdapter, SignalBridgeError
from tests.conftest import FakeBridge, inbound

OWNER = "+31600000000"

# --- Owner-only allowlist -------------------------------------------------


def test_owner_dm_passes():
    allow = AllowlistFilter(OWNER)
    assert allow.allows(inbound("hi", sender=OWNER)) is True


def test_unknown_sender_dropped_and_logged(caplog):
    allow = AllowlistFilter(OWNER)
    with caplog.at_level(logging.WARNING, logger="henk.channel.allowlist"):
        allowed = allow.allows(inbound("hi", sender="+31699999999"))
    assert allowed is False
    assert any("non-owner" in r.message and "+31699999999" in r.message
               for r in caplog.records)


def test_group_message_ignored_and_logged(caplog):
    allow = AllowlistFilter(OWNER)
    with caplog.at_level(logging.WARNING, logger="henk.channel.allowlist"):
        allowed = allow.allows(inbound("hi", sender=OWNER, is_group=True))
    assert allowed is False
    assert any("group" in r.message for r in caplog.records)


def test_empty_sender_never_matches_owner():
    # A senderless envelope must never be treated as the owner (fail-closed).
    allow = AllowlistFilter(OWNER)
    assert allow.allows(inbound("hi", sender="")) is False


def test_empty_owner_id_refused_at_construction():
    with pytest.raises(ValueError):
        AllowlistFilter("")


# --- Signal transport via the bridge -------------------------------------


def _envelope(text, source=OWNER, group=False):
    data = {"message": text, "timestamp": 1690000000000}
    if group:
        data["groupInfo"] = {"groupId": "g1"}
    return {"envelope": {"source": source, "sourceUuid": "uuid-1", "dataMessage": data}}


async def _collect(adapter, limit):
    out = []
    async for msg in adapter.messages():
        out.append(msg)
        if len(out) >= limit:
            break
    return out


async def test_inbound_message_converted():
    bridge = FakeBridge([_envelope("hello")])
    adapter = SignalAdapter(bridge, account="+31611111111", owner=OWNER)
    msgs = await _collect(adapter, 1)
    assert msgs[0].text == "hello"
    assert msgs[0].sender == "uuid-1"
    assert msgs[0].is_group is False


async def test_non_data_envelope_skipped():
    receipt = {"envelope": {"source": OWNER, "receiptMessage": {"when": 1}}}
    bridge = FakeBridge([receipt, _envelope("real")])
    adapter = SignalAdapter(bridge, account="+31611111111", owner=OWNER)
    msgs = await _collect(adapter, 1)
    assert msgs[0].text == "real"


async def test_outbound_reply_sent():
    bridge = FakeBridge()
    adapter = SignalAdapter(bridge, account="+31611111111", owner=OWNER)
    await adapter.send("pong")
    assert bridge.sends == [(OWNER, "pong")]


async def test_bridge_unreachable_backs_off_without_crashing():
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    # First receive() raises; the retried stream yields a message.
    class FlakyBridge:
        def __init__(self):
            self.calls = 0
            self.sends = []

        async def receive(self):
            self.calls += 1
            if self.calls == 1:
                raise SignalBridgeError("connection refused")
            yield _envelope("recovered")

        async def send(self, recipient, text):
            self.sends.append((recipient, text))

    adapter = SignalAdapter(
        FlakyBridge(), account="+31611111111", owner=OWNER, sleep=fake_sleep
    )
    msgs = await _collect(adapter, 1)
    assert msgs[0].text == "recovered"
    assert slept, "adapter should have backed off after the bridge error"


async def test_send_retries_then_gives_up_without_crashing():
    async def fake_sleep(delay):
        return None

    class BrokenBridge:
        async def receive(self):
            if False:
                yield {}

        async def send(self, recipient, text):
            raise SignalBridgeError("down")

    adapter = SignalAdapter(
        BrokenBridge(), account="+3", owner=OWNER, sleep=fake_sleep, max_send_attempts=3
    )
    # Should not raise even though every attempt fails.
    await adapter.send("hi")


async def test_send_failure_is_surfaced_not_silently_truncated():
    async def nosleep(_):
        return None

    class PartialBridge:
        """Fails the first N send calls, then succeeds."""

        def __init__(self, fail_first):
            self.fail_first = fail_first
            self.calls = 0
            self.sends = []

        async def receive(self):
            if False:
                yield {}

        async def send(self, recipient, text):
            self.calls += 1
            if self.calls <= self.fail_first:
                raise SignalBridgeError("temp fail")
            self.sends.append((recipient, text))

    # 50 chars at safe_length 30 => two chunks. The first chunk's 3 attempts all
    # fail; the follow-up delivery-failure marker then succeeds.
    bridge = PartialBridge(fail_first=3)
    adapter = SignalAdapter(
        bridge,
        account="+3",
        owner=OWNER,
        sleep=nosleep,
        max_send_attempts=3,
        safe_length=30,
    )
    await adapter.send("a" * 50)

    assert len(bridge.sends) == 1  # the second chunk was NOT sent out of order
    assert "could not be delivered" in bridge.sends[0][1]  # owner is told


# --- Swappable channel-adapter contract (encapsulation) -------------------

# The Signal wire format (message-envelope field names, bridge receive/send
# routes) must live only in the Signal adapter. The bridge endpoint and account
# are *configuration* values — the spec wires a new adapter in "through
# configuration" — so config.py legitimately carries them and is not scanned.
SIGNAL_MODULE = Path("henk/channel/signal.py")
CONFIG_MODULE = Path("henk/config.py")
WIRE_FORMAT_TOKENS = ["dataMessage", "sourceUuid", "groupInfo", "receiptMessage"]


def test_signal_wire_format_stays_encapsulated():
    """No Signal wire-format specifics leak into neutral code."""
    repo_root = Path(__file__).resolve().parent.parent
    henk_dir = repo_root / "henk"
    offenders: list[str] = []
    for path in henk_dir.rglob("*.py"):
        rel = path.relative_to(repo_root)
        if rel in (SIGNAL_MODULE, CONFIG_MODULE):
            continue
        text = path.read_text()
        for token in WIRE_FORMAT_TOKENS:
            if token in text:
                offenders.append(f"{rel}: {token}")
    assert not offenders, f"Signal wire format leaked: {offenders}"


# --- Long replies delivered intact ---------------------------------------


def test_short_message_not_split():
    assert split_message("hello", 100) == ["hello"]


def test_long_message_split_at_boundaries_and_intact():
    paras = "\n\n".join(f"paragraph {i} " + "x" * 40 for i in range(20))
    chunks = split_message(paras, 100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == paras  # nothing truncated or reordered


def test_unbreakable_line_hard_split():
    text = "y" * 250
    chunks = split_message(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


def test_empty_message_yields_nothing():
    assert split_message("", 100) == []
