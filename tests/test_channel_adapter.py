"""Channel-adapter contract tests (task 2.2), from specs/channel-adapter."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from henk.channel.allowlist import AllowlistFilter
from henk.channel.base import SendOutcome, split_message
from henk.channel.signal import (
    REPLY_FAILURE_NOTICE,
    SignalAdapter,
    SignalBridgeError,
)
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


# --- Proactive owner-directed sends (channel-adapter delta, task 2.6) ------

# The proactive send is the same owner-directed primitive as a reply: it targets
# the configured owner with no arbitrary-recipient parameter (spec: "no recipient
# parameter beyond the configured owner identity"), works with no inbound trigger,
# and splits long messages in order like any reply.


#: Parameter names that would let a caller aim a send at anyone but the owner.
#: The exact-list assertions below catch an *added* parameter of any name; this
#: denylist catches a *rename* that keeps the arity. Neither subsumes the other.
RECIPIENT_DENYLIST = {
    "recipient",
    "to",
    "number",
    "sender",
    "phone",
    "uuid",
    "account",
}

#: Every send operation on the contract and on the Signal adapter, with the exact
#: parameter list it is allowed to expose.
SEND_OPERATIONS = {
    "send": ["text"],
    "send_proactive": ["text", "failure_notice"],
}


def test_no_send_operation_exposes_an_arbitrary_recipient():
    import inspect

    from henk.channel.base import ChannelAdapter

    for name, expected in SEND_OPERATIONS.items():
        for owner_type in (ChannelAdapter, SignalAdapter):
            operation = getattr(owner_type, name)
            params = [
                p for p in inspect.signature(operation).parameters if p != "self"
            ]
            assert params == expected, f"{owner_type.__name__}.{name}"
            leaked = RECIPIENT_DENYLIST & set(params)
            assert not leaked, f"{owner_type.__name__}.{name} exposes {leaked}"


async def test_proactive_send_reaches_owner_without_inbound():
    bridge = FakeBridge()  # nothing ever received
    adapter = SignalAdapter(bridge, account="+31611111111", owner=OWNER)
    outcome = await adapter.send_proactive("unprompted triage message")
    assert outcome is SendOutcome.DELIVERED
    assert bridge.sends == [(OWNER, "unprompted triage message")]


async def test_long_proactive_message_split_in_order():
    bridge = FakeBridge()
    adapter = SignalAdapter(bridge, account="+31611111111", owner=OWNER, safe_length=30)
    await adapter.send_proactive("x" * 20 + "\n\n" + "y" * 20)
    assert len(bridge.sends) == 2
    assert "".join(text for _, text in bridge.sends) == "x" * 20 + "\n\n" + "y" * 20
    assert all(recipient == OWNER for recipient, _ in bridge.sends)


async def test_reply_carries_the_adapters_notice_and_proactive_carries_none():
    # The adapter's standing banner says "part of this reply", which is simply
    # wrong for something that was never a reply — so a proactive send whose
    # caller supplied no notice is silent to the owner.
    reply_bridge = SelectiveBridge(fail_marker="payload")
    reply_outcome = await _adapter(reply_bridge).send("payload")
    assert reply_outcome is SendOutcome.FAILED
    assert [text for _, text in reply_bridge.sends] == [REPLY_FAILURE_NOTICE]

    quiet_bridge = SelectiveBridge(fail_marker="payload")
    quiet_outcome = await _adapter(quiet_bridge).send_proactive("payload")
    assert quiet_outcome is SendOutcome.FAILED
    assert quiet_bridge.sends == []
    # Only the payload's own attempts were made — no notice was even tried.
    assert all("payload" in text for _, text in quiet_bridge.attempts)


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


# --- Byte-measured splitting (channel-integrity, task 1.1) ----------------

# The limit is a *byte* budget, because bytes bound both the wire size and any
# client-side character limit while a character count bounds neither. The ASCII
# tests above are unchanged on purpose: for ASCII, byte length == character
# length, so they assert the same thing under either measurement.


def _blen(text: str) -> int:
    return len(text.encode("utf-8"))


def test_ascii_split_is_identical_under_byte_measurement():
    # Regression anchor for "ASCII behaviour unchanged": character-measured and
    # byte-measured splitting must agree exactly on pure ASCII.
    paras = "\n\n".join(f"paragraph {i} " + "x" * 40 for i in range(20))
    chunks = split_message(paras, 100)
    assert all(len(c) == _blen(c) for c in chunks)
    assert all(len(c) <= 100 for c in chunks)


def test_multibyte_text_near_the_limit_respects_the_byte_limit():
    # Each "é" is 2 UTF-8 bytes; a character-measured splitter would emit chunks
    # of ~2x the limit here.
    text = " ".join("é" * 20 for _ in range(30))
    chunks = split_message(text, 100)
    assert len(chunks) > 1
    assert all(_blen(c) <= 100 for c in chunks), [_blen(c) for c in chunks]
    assert "".join(chunks) == text


def test_emoji_text_respects_the_byte_limit():
    # Emoji are 4 UTF-8 bytes each: the worst case for a character count.
    text = "\n\n".join("🙂" * 30 for _ in range(10))
    chunks = split_message(text, 100)
    assert len(chunks) > 1
    assert all(_blen(c) <= 100 for c in chunks), [_blen(c) for c in chunks]
    assert "".join(chunks) == text


def test_split_never_divides_a_code_point():
    # Every chunk must be independently encodable/decodable: a cut inside a code
    # point cannot survive an encode/decode round trip.
    text = "🙂é" * 200
    chunks = split_message(text, 17)  # deliberately not a multiple of 4 or 6
    for chunk in chunks:
        assert chunk.encode("utf-8").decode("utf-8") == chunk
        assert _blen(chunk) <= 17
    assert "".join(chunks) == text


def test_unbreakable_multibyte_token_hard_splits_without_corruption():
    text = "🙂" * 60  # one token, no boundary anywhere, 240 bytes
    chunks = split_message(text, 30)
    assert len(chunks) > 1
    assert all(_blen(c) <= 30 for c in chunks)
    assert "".join(chunks) == text
    # 30 bytes holds 7 whole emoji (28 bytes); the 8th would overflow.
    assert all(len(c) <= 7 for c in chunks)


def test_limit_below_one_code_point_is_refused_not_looped():
    # Below 4 bytes no code point fits, the two guarantees (never split a code
    # point / reproduce the input) are jointly unsatisfiable, and a shrink loop
    # would find a zero-length cut and never advance on the send path.
    for limit in (0, -1, 1, 2, 3):
        with pytest.raises(ValueError):
            split_message("🙂ok", limit)


def test_limit_exactly_one_code_point_is_accepted():
    assert split_message("🙂🙂", 4) == ["🙂", "🙂"]


# --- Delivery outcome (channel-integrity, task 2.1) -----------------------

# `failed` means "delivery was not confirmed", never "nothing arrived": the
# bridge raises on any transport fault, including a response lost after the
# message was accepted and sent. A `partial` is never reported as success.


class SelectiveBridge:
    """A SignalBridge double that refuses any chunk containing ``fail_marker``.

    ``attempts`` records every call including refused ones, so a single-attempt
    notice is distinguishable from a retried chunk.
    """

    def __init__(self, fail_marker: str | None = None) -> None:
        self.fail_marker = fail_marker
        self.sends: list[tuple[str, str]] = []
        self.attempts: list[tuple[str, str]] = []

    async def receive(self):
        if False:
            yield {}

    async def send(self, recipient, text):
        self.attempts.append((recipient, text))
        if self.fail_marker is not None and self.fail_marker in text:
            raise SignalBridgeError("refused")
        self.sends.append((recipient, text))


async def _nosleep(_):
    return None


def _adapter(bridge, **kwargs):
    kwargs.setdefault("safe_length", 30)
    kwargs.setdefault("max_send_attempts", 3)
    return SignalAdapter(
        bridge, account="+31611111111", owner=OWNER, sleep=_nosleep, **kwargs
    )


#: 3 chunks at safe_length 30, the third carrying the failure marker.
THREE_CHUNKS = "a" * 25 + "\n\n" + "b" * 25 + "\n\n" + "FAILME"


async def test_healthy_send_reports_delivered():
    bridge = SelectiveBridge()
    outcome = await _adapter(bridge).send(THREE_CHUNKS)
    assert outcome is SendOutcome.DELIVERED
    assert len(bridge.sends) == 3
    assert "".join(text for _, text in bridge.sends) == THREE_CHUNKS


async def test_send_that_never_succeeds_reports_failed():
    bridge = SelectiveBridge(fail_marker="a")  # every chunk contains "a"
    outcome = await _adapter(bridge).send("a" * 10)
    assert outcome is SendOutcome.FAILED
    assert bridge.sends == []


async def test_partial_delivery_reports_partial_and_never_success():
    bridge = SelectiveBridge(fail_marker="FAILME")
    outcome = await _adapter(bridge).send(THREE_CHUNKS)
    assert outcome is SendOutcome.PARTIAL
    assert outcome is not SendOutcome.DELIVERED
    # The first two chunks landed; the third was abandoned, not reordered past.
    assert [text for _, text in bridge.sends][:2] == [
        "a" * 25 + "\n\n",
        "b" * 25 + "\n\n",
    ]
    assert "FAILME" not in [text for _, text in bridge.sends]


async def test_empty_send_is_not_delivered_and_emits_no_notice():
    bridge = SelectiveBridge()
    outcome = await _adapter(bridge).send("")
    assert outcome is not SendOutcome.DELIVERED
    # Nothing sent at all: no chunk was attempted, so a notice claiming a
    # delivery failure would be a lie.
    assert bridge.attempts == []
    assert bridge.sends == []


async def test_caller_ignoring_the_outcome_behaves_exactly_as_before():
    # The outcome is additive. A caller that discards it observes the same
    # deliveries and the same non-raising behaviour on both paths.
    healthy = SelectiveBridge()
    await _adapter(healthy).send("pong")
    assert healthy.sends == [(OWNER, "pong")]

    broken = SelectiveBridge(fail_marker="pong")
    await _adapter(broken).send("pong")  # must not raise
    # The undeliverable chunk is not delivered, and the owner-facing notice that
    # followed it before this change still follows it.
    assert "pong" not in [text for _, text in broken.sends]
    assert any("could not be delivered" in text for _, text in broken.sends)


# --- The failure notice (channel-integrity, task 2.4) ---------------------

# The condition is "any outcome other than `delivered`, where at least one chunk
# was attempted" — not "partial". It is one attempt, emitted inside the same send
# sequence immediately after the delivered chunks, and it never alters the
# reported outcome.


class FailAfterBridge:
    """Succeeds for the first N sends, then refuses everything.

    ``attempts`` records refused calls too, which is what makes a single-attempt
    notice distinguishable from a chunk that consumed the retry budget.
    """

    def __init__(self, succeed_first: int) -> None:
        self.succeed_first = succeed_first
        self.attempts: list[str] = []
        self.sends: list[str] = []

    async def receive(self):
        if False:
            yield {}

    async def send(self, recipient, text):
        self.attempts.append(text)
        if len(self.attempts) > self.succeed_first:
            raise SignalBridgeError("down")
        self.sends.append(text)


CALLER_NOTICE = "[⚠ part of this alert could not be delivered]"


async def test_caller_notice_follows_delivered_chunks_as_a_single_attempt():
    bridge = FailAfterBridge(succeed_first=1)
    outcome = await _adapter(bridge).send_proactive(
        THREE_CHUNKS, failure_notice=CALLER_NOTICE
    )
    assert outcome is SendOutcome.PARTIAL
    assert bridge.sends == ["a" * 25 + "\n\n"]  # only chunk 1 landed
    # Chunk 2 consumed the full retry budget (3); the notice got exactly one
    # attempt, and it came last — inside the same sequence, after the chunks.
    assert bridge.attempts[-1] == CALLER_NOTICE
    assert bridge.attempts.count(CALLER_NOTICE) == 1
    assert len(bridge.attempts) == 1 + 3 + 1


async def test_the_notice_never_alters_the_reported_outcome():
    # The notice fails too here. The outcome must still describe only the reply's
    # own chunks.
    bridge = FailAfterBridge(succeed_first=1)
    outcome = await _adapter(bridge).send(THREE_CHUNKS)
    assert outcome is SendOutcome.PARTIAL
    assert bridge.attempts[-1] == REPLY_FAILURE_NOTICE
    assert bridge.sends == ["a" * 25 + "\n\n"]


async def test_wholly_failed_single_chunk_reply_still_gets_the_notice():
    # The most common failure shape, and the one a `partial`-only condition would
    # silently drop.
    bridge = FailAfterBridge(succeed_first=0)
    outcome = await _adapter(bridge).send("one short reply")
    assert outcome is SendOutcome.FAILED
    assert bridge.attempts == ["one short reply"] * 3 + [REPLY_FAILURE_NOTICE]


# --- Explicit bridge timeouts (channel-integrity, task 3.1) ---------------

# The bridge used to build `httpx.AsyncClient()` with no timeout, taking httpx's
# 5s default — which applies PER TRANSPORT PHASE, so one POST could run to
# roughly 3x the number a reader would assume. A send that the bridge accepted
# and sent then reads as failed and is retried: duplicate delivery, from a value
# nobody chose and a ceiling nobody computed.


def _bridge(send_timeout=12.0, open_timeout=25.0):
    from henk.channel.signal import SignalCliRestBridge

    return SignalCliRestBridge(
        "http://signal-cli-rest-api:8080",
        "+31611111111",
        send_timeout=send_timeout,
        open_timeout=open_timeout,
    )


def test_bridge_client_timeout_comes_from_configuration():
    # Every phase httpx can spend time in is bounded. An unset phase silently
    # falls back to httpx's own 5s default, which is the defect being fixed.
    phases = _bridge(send_timeout=12.0)._build_client().timeout
    assert phases.connect is not None
    assert phases.read is not None
    assert phases.write is not None
    assert phases.pool is not None


def test_every_phase_carries_the_configured_value_in_full():
    # NOT a total, and deliberately not a fraction of one. httpcore applies the
    # read and write timeouts PER SOCKET OPERATION inside `while True` loops
    # (httpcore/_async/http11.py `_receive_response_headers` / `_receive_event`),
    # so no allocation across phases can bound a whole request — an earlier
    # version of this test asserted the four phases summed to the configured
    # value, which was true and strictly weaker than the guarantee it claimed.
    configured = 12.0
    phases = _bridge(send_timeout=configured)._build_client().timeout
    assert phases.connect == pytest.approx(configured)
    assert phases.read == pytest.approx(configured)
    assert phases.write == pytest.approx(configured)
    assert phases.pool == pytest.approx(configured)


def test_read_phase_gets_the_full_configured_value():
    # The motivating bug is bounded by READ: signal-cli's own send latency is
    # what the bridge waits on, so a read ceiling below that latency turns an
    # accepted message into a reported failure and a retried duplicate. A
    # fraction of the configured value here would raise httpx's 5s default only
    # marginally — which is what shipped in 0bfcc5b (6.0s of a nominal 10.0).
    for configured in (4.0, 10.0, 12.0):
        phases = _bridge(send_timeout=configured)._build_client().timeout
        assert phases.read == pytest.approx(configured), configured


def test_no_bridge_code_path_constructs_a_client_without_a_timeout():
    # An AST scan, because a second construction site is exactly the way this
    # guarantee would regress: the budget lives in one factory, and nothing else
    # in the module may build a client.
    import ast

    source = Path(__file__).resolve().parent.parent / SIGNAL_MODULE
    tree = ast.parse(source.read_text())
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "AsyncClient"
    ]
    assert len(constructions) == 1, [c.lineno for c in constructions]
    for call in constructions:
        keywords = {kw.arg for kw in call.keywords}
        assert "timeout" in keywords, (call.lineno, keywords)


def test_receive_connection_timeout_comes_from_configuration():
    # Previously a constructor default the wiring never supplied.
    assert _bridge(open_timeout=25.0)._open_timeout == 25.0


# --- Outbound send serialization (reminder-delivery, channel-adapter delta) ---
#
# Every test in this section drives `SignalAdapter`'s REAL lock over a bridge whose
# send actually yields control. `conftest.FakeChannel` is forbidden here and would be
# useless: it has no lock and never suspends mid-send, so it satisfies every
# serialization assertion below while serializing nothing. That is the defect the
# delta's "enforced by the adapter, not by caller convention" scenario exists to
# catch, and the reminders README names it explicitly as a trap this project's task
# lists forbid falling into.
#
# The scheduler is the first real second sender on this channel, which is why
# serialization lands in this change rather than in `channel-integrity`.


class SlowBridge:
    """A bridge whose every send suspends, so concurrent senders CAN interleave.

    One `await asyncio.sleep(0)` per send is what makes the interleaving
    deterministic rather than lucky: asyncio's ready queue is round-robin, so two
    gathered tasks each yielding once per chunk alternate strictly. Without the
    suspension there is no interleaving to prevent and the test would pass against a
    lockless adapter — which is precisely how a serialization test fools itself.
    """

    def __init__(self, fail_marker: str | None = None) -> None:
        #: Chunk texts in the order the transport actually saw them.
        self.sends: list[str] = []
        self.fail_marker = fail_marker

    async def receive(self):  # pragma: no cover - not exercised here
        if False:
            yield {}

    async def send(self, recipient, text):
        if self.fail_marker is not None and self.fail_marker in text:
            await asyncio.sleep(0)
            raise SignalBridgeError("refused")
        self.sends.append(text)
        await asyncio.sleep(0)


#: Three chunks each at safe_length 30, distinguishable by content alone — the
#: bridge sees only text, so the marker has to be in the payload.
MESSAGE_A = "\n\n".join("A" * 25 for _ in range(3))
MESSAGE_B = "\n\n".join("B" * 25 for _ in range(3))


def _tags(sends: list[str]) -> str:
    """Collapse recorded chunks to their sender's letter: 'AAABBB' or 'ABABAB'."""
    return "".join(text.strip()[0] for text in sends if text.strip())


def _is_contiguous(tags: str) -> bool:
    """True when no sender's chunks are split by another's — 'AAABBB', not 'ABABAB'."""
    return len(["" for i, c in enumerate(tags) if i == 0 or tags[i - 1] != c]) == len(
        set(tags)
    )


async def test_concurrent_multi_chunk_sends_do_not_interleave():
    bridge = SlowBridge()
    adapter = _adapter(bridge)
    outcomes = await asyncio.gather(
        adapter.send(MESSAGE_A), adapter.send(MESSAGE_B)
    )
    assert len(bridge.sends) == 6
    tags = _tags(bridge.sends)
    assert _is_contiguous(tags), f"chunks interleaved: {tags}"
    assert tags in ("AAABBB", "BBBAAA"), tags
    # Both senders report their OWN outcome, not a shared one.
    assert outcomes == [SendOutcome.DELIVERED, SendOutcome.DELIVERED]


async def test_a_reply_and_a_proactive_send_serialize_against_each_other():
    """The lock covers both operations, because they share one send sequence.

    This is the pairing that actually occurs: the scheduler's proactive delivery
    racing an owner reply. A lock on only one of the two paths would pass the
    reply-vs-reply test above and fail in production.
    """
    bridge = SlowBridge()
    adapter = _adapter(bridge)
    outcomes = await asyncio.gather(
        adapter.send(MESSAGE_A),
        adapter.send_proactive(MESSAGE_B, failure_notice="[notice]"),
    )
    tags = _tags(bridge.sends)
    assert _is_contiguous(tags), f"chunks interleaved: {tags}"
    assert outcomes == [SendOutcome.DELIVERED, SendOutcome.DELIVERED]


async def test_two_proactive_senders_serialize():
    bridge = SlowBridge()
    adapter = _adapter(bridge)
    await asyncio.gather(
        adapter.send_proactive(MESSAGE_A), adapter.send_proactive(MESSAGE_B)
    )
    assert _is_contiguous(_tags(bridge.sends))


async def test_the_failure_notice_lands_before_the_waiting_senders_first_chunk():
    """The notice cannot be separated from the chunks it describes.

    The delta already worded the notice as firing "within the same serialized
    sequence"; before the lock existed there was no sequence for it to be inside, so
    a waiting sender's chunks could land between the truncated message and the banner
    explaining it — leaving the owner reading an apology about a message that appears
    to have arrived intact.
    """
    # Sender A's third chunk carries the marker the bridge refuses.
    failing = "\n\n".join(["A" * 25, "A" * 25, "FAILME"])
    bridge = SlowBridge(fail_marker="FAILME")
    adapter = _adapter(bridge)
    outcomes = await asyncio.gather(
        adapter.send_proactive(failing, failure_notice="[NOTICE]"),
        adapter.send(MESSAGE_B),
    )
    texts = bridge.sends
    notice_at = next(i for i, t in enumerate(texts) if "[NOTICE]" in t)
    first_b = next(i for i, t in enumerate(texts) if t.strip().startswith("B"))
    assert notice_at < first_b, texts
    # A reports PARTIAL (a chunk landed, then one did not); B is unaffected.
    assert outcomes[0] is SendOutcome.PARTIAL
    assert outcomes[1] is SendOutcome.DELIVERED


async def test_a_waiting_send_is_delivered_not_dropped_or_truncated():
    bridge = SlowBridge()
    adapter = _adapter(bridge)
    outcomes = await asyncio.gather(
        *[adapter.send(MESSAGE_A), adapter.send(MESSAGE_B)]
    )
    assert all(o is SendOutcome.DELIVERED for o in outcomes)
    # Every sender's content arrives complete and in its own order.
    for letter, message in (("A", MESSAGE_A), ("B", MESSAGE_B)):
        mine = [t for t in bridge.sends if t.strip().startswith(letter)]
        assert "".join(mine) == message


async def test_a_failing_sender_does_not_strand_the_waiting_one():
    """A send that gives up must release the lock, including after its notice."""
    bridge = SlowBridge(fail_marker="FAILME")
    adapter = _adapter(bridge)
    outcomes = await asyncio.gather(
        adapter.send_proactive("FAILME", failure_notice="[NOTICE]"),
        adapter.send(MESSAGE_B),
    )
    assert outcomes[0] is SendOutcome.FAILED  # nothing landed: wholly failed
    assert outcomes[1] is SendOutcome.DELIVERED
    assert "".join(t for t in bridge.sends if t.strip().startswith("B")) == MESSAGE_B


async def test_an_exception_escaping_a_send_still_releases_the_lock():
    """Belt and braces: the lock is released on the error path too.

    `_send_chunk` normalises bridge errors, so an escaping exception means something
    unforeseen. If that leaked the lock, every later send on the process would hang
    forever — a deadlock is a worse failure than the exception that caused it.
    """

    class ExplodingBridge(SlowBridge):
        async def send(self, recipient, text):
            raise RuntimeError("something unforeseen")

    adapter = _adapter(ExplodingBridge())
    with pytest.raises(RuntimeError):
        await adapter.send("boom")
    # The adapter is still usable: a second send acquires the lock and completes.
    adapter._bridge = SlowBridge()
    assert await adapter.send(MESSAGE_A) is SendOutcome.DELIVERED


async def test_ten_concurrent_senders_all_stay_contiguous():
    """Scale past two, because a lock that serialises pairs may still admit a gap."""
    bridge = SlowBridge()
    adapter = _adapter(bridge)
    letters = "ABCDEFGHIJ"
    messages = {c: "\n\n".join(c * 25 for _ in range(3)) for c in letters}
    outcomes = await asyncio.gather(
        *[adapter.send(messages[c]) for c in letters]
    )
    assert all(o is SendOutcome.DELIVERED for o in outcomes)
    assert len(bridge.sends) == 30
    tags = _tags(bridge.sends)
    runs = [tags[i] for i in range(len(tags)) if i == 0 or tags[i - 1] != tags[i]]
    assert len(runs) == len(letters), f"a sender's chunks were split: {tags}"
    for c in letters:
        assert "".join(t for t in bridge.sends if t.strip().startswith(c)) == messages[c]


async def test_serialization_does_not_change_a_single_senders_behaviour():
    """The whole point of 4.3: additive for every existing caller."""
    bridge = SlowBridge()
    adapter = _adapter(bridge)
    assert await adapter.send(MESSAGE_A) is SendOutcome.DELIVERED
    assert "".join(bridge.sends) == MESSAGE_A
    # And the lock is re-entrant across sequential sends, not held after one returns.
    assert await adapter.send(MESSAGE_B) is SendOutcome.DELIVERED


def test_the_lock_is_the_whole_mechanism_and_nothing_more():
    """No hold timer, no chunk cap, no priority tier — each rejected in design.

    A bounded hold was rejected in `channel-integrity` D5 (its `failed` outcome is
    unreachable), a chunk cap likewise (it discards owner-requested content on the
    healthy path), and delivery-path priority in this change's D6 (it rescues nothing
    under a degraded bridge, and the cheap fix for a pathological reply is bounding
    the reply at its source). Recorded as a test so a future change adding one has to
    argue with the decision rather than around it.
    """
    import ast
    import inspect

    from henk.channel import signal as module

    source = inspect.getsource(module)
    tree = ast.parse(source)

    # Exactly one lock, and it is constructed in __init__ (instance state, so the
    # scheduler and the core must share one adapter — group 8 asserts that half).
    locks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Lock"
    ]
    assert len(locks) == 1, [n.lineno for n in locks]

    # `asyncio.wait_for` / `timeout` around the lock would be a hold timer.
    for name in ("wait_for", "timeout"):
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        ]
        assert offenders == [], f"asyncio.{name} looks like a hold timer: {offenders}"

    for forbidden in ("max_chunks", "chunk_cap", "priority", "hold_timeout"):
        assert forbidden not in source, f"{forbidden} was rejected in design"


def test_the_lock_wraps_the_shared_sequence_not_the_two_wrappers():
    """`send`/`send_proactive` must NOT take the lock (design D3's re-entry argument).

    They already share `_send_serialized`, and the notice has to be emitted from
    inside the same sequence as the chunks it describes. A lock in the wrappers
    instead would put the notice outside the critical section; a lock in BOTH would
    deadlock outright.
    """
    import ast
    import inspect

    from henk.channel.signal import SignalAdapter

    tree = ast.parse(inspect.getsource(SignalAdapter))
    holders = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.AsyncWith):
                for item in inner.items:
                    expr = item.context_expr
                    name = getattr(expr, "attr", None) or getattr(expr, "id", "")
                    if "lock" in str(name).lower():
                        holders.add(node.name)
    assert holders == {"_send_serialized"}, holders
