"""Signal adapter over signal-cli-rest-api (json-rpc mode).

This is the ONLY module that may know Signal specifics: the bridge wire format,
Henk's account number, and signal-cli-rest-api endpoints. Everything above it
speaks the channel-neutral contract in ``henk.channel.base``.

The transport is injected as a ``SignalBridge`` so the adapter's conversion,
backoff, and splitting logic are testable without a live bridge or a websocket
library. The default bridge (``SignalCliRestBridge``) is deployment wiring and
lazily imports its websocket dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Awaitable, Callable, Optional, Protocol

from henk.channel.base import (
    DEFAULT_SAFE_LENGTH,
    InboundMessage,
    SendOutcome,
    split_message,
)

logger = logging.getLogger("henk.channel.signal")

#: The adapter's standing owner-facing notice for a reply that was cut off. Only
#: correct for the reply path — a proactive send's notice is its caller's, since
#: the adapter cannot know what was being sent (design D2).
REPLY_FAILURE_NOTICE = "[⚠ part of this reply could not be delivered]"


class SignalBridgeError(Exception):
    """Raised by a bridge when signal-cli-rest-api is unreachable or errors."""


class SignalBridge(Protocol):
    """Minimal transport contract the adapter depends on."""

    def receive(self) -> AsyncIterator[dict]:
        """Yield raw signal-cli-rest-api envelopes. May raise SignalBridgeError."""
        ...

    async def send(self, recipient: str, text: str) -> None:
        """Send one message. May raise SignalBridgeError."""
        ...


class SignalAdapter:
    """Adapts a ``SignalBridge`` to the channel-neutral ``ChannelAdapter``."""

    def __init__(
        self,
        bridge: SignalBridge,
        *,
        account: str,
        owner: str,
        safe_length: int = DEFAULT_SAFE_LENGTH,
        backoff_base: float = 1.0,
        max_backoff: float = 30.0,
        max_send_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bridge = bridge
        self._account = account
        self._owner = owner
        self._safe_length = safe_length
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._max_send_attempts = max_send_attempts
        self._sleep = sleep

    async def messages(self) -> AsyncIterator[InboundMessage]:
        """Yield inbound messages, reconnecting with backoff on bridge errors.

        A transport failure never propagates: it is logged, the adapter backs
        off, and the receive loop restarts. The agent process stays alive.
        """
        attempt = 0
        while True:
            try:
                async for envelope in self._bridge.receive():
                    message = self._convert(envelope)
                    if message is not None:
                        yield message
            except SignalBridgeError as exc:
                # Grow the backoff per consecutive error. We deliberately do NOT
                # reset on every yielded envelope: a bridge that flaps (one
                # message, then drops, repeatedly) would otherwise hot-reconnect
                # at the base delay forever instead of backing off.
                delay = min(
                    self._backoff_base * (2**attempt), self._max_backoff
                )
                attempt += 1
                logger.warning(
                    "signal bridge receive failed (%s); backing off %.1fs", exc, delay
                )
                await self._sleep(delay)
            else:
                # The receive stream ended cleanly; reset backoff, brief pause.
                attempt = 0
                await self._sleep(self._backoff_base)

    async def send(self, text: str) -> SendOutcome:
        """Reply path: the adapter's own standing notice describes a failure."""
        return await self._send_serialized(text, failure_notice=REPLY_FAILURE_NOTICE)

    async def send_proactive(
        self, text: str, *, failure_notice: str | None = None
    ) -> SendOutcome:
        """Agent-initiated path: the caller owns the owner-facing notice."""
        return await self._send_serialized(text, failure_notice=failure_notice)

    async def _send_serialized(
        self, text: str, *, failure_notice: str | None
    ) -> SendOutcome:
        """The one send sequence both operations share (design D3).

        Shared rather than one wrapper calling the other: re-entering the other
        operation would deadlock outright once a send mutex exists, and the notice
        must be emitted from inside the same sequence as the chunks it describes.
        """
        chunks = split_message(text, self._safe_length)
        if not chunks:
            # No chunk was attempted, so nothing was delivered — and no notice:
            # a banner claiming a delivery failure here would be false.
            return SendOutcome.FAILED
        for index, chunk in enumerate(chunks):
            if not await self._send_chunk(chunk):
                # A chunk failed permanently. Do NOT silently truncate: stop
                # (later chunks would arrive out of order) and tell the owner the
                # message was cut off, if this path has a notice to give.
                remaining = len(chunks) - index
                logger.error(
                    "send failed on chunk %d/%d; %d chunk(s) undelivered",
                    index + 1,
                    len(chunks),
                    remaining,
                )
                if failure_notice is not None:
                    # A single attempt, never the retry budget: a bridge that
                    # just refused three attempts will not take a fourth, and the
                    # notice's own failure is not worth more of the caller's
                    # latency. Its outcome is deliberately discarded — the notice
                    # never alters what this send reports.
                    await self._send_chunk(failure_notice, attempts=1)
                # The condition is "not delivered, having attempted a chunk" —
                # not "partial". A wholly-failed single-chunk send is the most
                # common failure shape, and a partial-only condition would drop
                # the notice exactly there.
                return SendOutcome.PARTIAL if index else SendOutcome.FAILED
        return SendOutcome.DELIVERED

    async def _send_chunk(self, chunk: str, *, attempts: int | None = None) -> bool:
        """Send one chunk with backoff. Returns True if delivered, False if given up.

        ``attempts`` overrides the configured retry budget (the failure notice
        gets exactly one try).
        """
        budget = self._max_send_attempts if attempts is None else attempts
        attempt = 0
        while True:
            try:
                await self._bridge.send(self._owner, chunk)
                return True
            except SignalBridgeError as exc:
                attempt += 1
                if attempt >= budget:
                    logger.error("giving up sending after %d attempts: %s", attempt, exc)
                    return False
                delay = min(
                    self._backoff_base * (2 ** (attempt - 1)), self._max_backoff
                )
                logger.warning("signal send failed (%s); retry in %.1fs", exc, delay)
                await self._sleep(delay)

    def _convert(self, envelope: dict) -> Optional[InboundMessage]:
        """Convert a signal-cli-rest-api envelope to a channel-neutral message.

        Returns ``None`` for envelopes that are not user text (receipts, typing
        indicators, empty data messages).
        """
        env = envelope.get("envelope", envelope)
        data = env.get("dataMessage")
        if not isinstance(data, dict):
            return None
        body = data.get("message")
        if not body:
            return None
        # DEPLOY-VERIFY (task 1.5/5.3): which identity Signal actually reports for
        # the owner (UUID vs E.164 number) must be confirmed against a real
        # envelope and matched to config `owner.id`, or the allowlist silently
        # drops every owner message. We prefer the stable UUID; do NOT loosen the
        # match to "either field" — that widens the allowlist. Set owner.id to
        # whatever this field emits, verified by the owner-accept smoke test.
        sender = env.get("sourceUuid") or env.get("source") or ""
        raw_ts = data.get("timestamp") or env.get("timestamp") or 0
        timestamp = float(raw_ts) / 1000.0 if raw_ts else 0.0
        is_group = "groupInfo" in data and data.get("groupInfo") is not None
        return InboundMessage(
            sender=sender, text=body, timestamp=timestamp, is_group=is_group
        )


#: Why every phase gets the configured value in full, rather than a share of it.
#:
#: httpx applies a timeout PER PHASE, so any phase left unset falls back to
#: httpx's own 5s default — an unbounded segment in a guarantee that is supposed
#: to be explicit. That was the original defect: an untimed client gave connect,
#: write and read 5s *each*, and 5s is shorter than signal-cli's send latency
#: under load, so a message the bridge had already accepted and delivered came
#: back as a failure to retry.
#:
#: A *total* request budget was specified first and cannot be built this way.
#: httpcore applies the read and write timeouts per socket OPERATION, inside
#: `while True` loops (`_receive_response_headers` → `_receive_event` →
#: `network_stream.read`), so a response arriving in many small reads is never
#: bounded by any sum. The only mechanism that bounds a whole request is
#: cancelling it in flight, which manufactures the "may already have been
#: delivered" ambiguity ``SendOutcome`` exists to describe rather than to create.
#: So the guarantee is per-phase and stated as such.
#:
#: The value therefore lands on `read` in full, which is the phase that carries
#: signal-cli's own processing time and the one the motivating bug lives in.
#: A scalar populates all four phases, `pool` included.


class SignalCliRestBridge:
    """Concrete ``SignalBridge`` over signal-cli-rest-api in json-rpc mode.

    Deployment wiring: receive is a persistent websocket (low latency, no
    polling); send is a REST POST. Both translate transport failures into
    ``SignalBridgeError`` so the adapter's backoff loop handles them. The
    websocket dependency is imported lazily so importing this module never
    requires it (tests drive the adapter through ``FakeBridge`` instead).

    Both timeouts are required rather than defaulted: ``open_timeout`` used to
    carry a constructor default the wiring never supplied, which is how the
    receive path ended up with a number nobody chose.
    """

    def __init__(
        self,
        base_url: str,
        account: str,
        *,
        send_timeout: float,
        open_timeout: float,
    ):
        self._base_url = base_url.rstrip("/")
        self._account = account
        self._send_timeout = send_timeout
        self._open_timeout = open_timeout

    def _build_client(self):
        """The ONLY place an HTTP client is constructed, so no phase goes unbounded.

        Deliberately not ``asyncio.wait_for`` around the POST — see the note
        above on why a request total is not specified.
        """
        import httpx  # lazy

        return httpx.AsyncClient(timeout=httpx.Timeout(self._send_timeout))

    async def receive(self) -> AsyncIterator[dict]:  # pragma: no cover - deploy path
        import websockets  # lazy: only needed at runtime

        ws_url = self._base_url.replace("http", "ws", 1) + (
            f"/v1/receive/{self._account}"
        )
        try:
            async with websockets.connect(
                ws_url, open_timeout=self._open_timeout
            ) as socket:
                async for raw in socket:
                    yield json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - normalise every failure
            raise SignalBridgeError(f"receive websocket failed: {exc}") from exc

    async def send(self, recipient: str, text: str) -> None:  # pragma: no cover
        payload = {
            "message": text,
            "number": self._account,
            "recipients": [recipient],
        }
        try:
            async with self._build_client() as client:
                resp = await client.post(f"{self._base_url}/v2/send", json=payload)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise SignalBridgeError(f"send failed: {exc}") from exc
