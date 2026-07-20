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

from henk.channel.base import DEFAULT_SAFE_LENGTH, InboundMessage, split_message

logger = logging.getLogger("henk.channel.signal")


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
                    attempt = 0  # healthy traffic resets backoff
                    message = self._convert(envelope)
                    if message is not None:
                        yield message
            except SignalBridgeError as exc:
                delay = min(
                    self._backoff_base * (2**attempt), self._max_backoff
                )
                attempt += 1
                logger.warning(
                    "signal bridge receive failed (%s); backing off %.1fs", exc, delay
                )
                await self._sleep(delay)
            else:
                # The receive stream ended cleanly; brief pause, then reconnect.
                await self._sleep(self._backoff_base)

    async def send(self, text: str) -> None:
        for chunk in split_message(text, self._safe_length):
            await self._send_chunk(chunk)

    async def _send_chunk(self, chunk: str) -> None:
        attempt = 0
        while True:
            try:
                await self._bridge.send(self._owner, chunk)
                return
            except SignalBridgeError as exc:
                attempt += 1
                if attempt >= self._max_send_attempts:
                    logger.error("giving up sending after %d attempts: %s", attempt, exc)
                    return
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
        sender = env.get("sourceUuid") or env.get("source") or ""
        raw_ts = data.get("timestamp") or env.get("timestamp") or 0
        timestamp = float(raw_ts) / 1000.0 if raw_ts else 0.0
        is_group = "groupInfo" in data and data.get("groupInfo") is not None
        return InboundMessage(
            sender=sender, text=body, timestamp=timestamp, is_group=is_group
        )


class SignalCliRestBridge:
    """Concrete ``SignalBridge`` over signal-cli-rest-api in json-rpc mode.

    Deployment wiring: receive is a persistent websocket (low latency, no
    polling); send is a REST POST. Both translate transport failures into
    ``SignalBridgeError`` so the adapter's backoff loop handles them. The
    websocket dependency is imported lazily so importing this module never
    requires it (tests drive the adapter through ``FakeBridge`` instead).
    """

    def __init__(self, base_url: str, account: str, *, open_timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._account = account
        self._open_timeout = open_timeout

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
        import httpx  # lazy

        payload = {
            "message": text,
            "number": self._account,
            "recipients": [recipient],
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{self._base_url}/v2/send", json=payload)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise SignalBridgeError(f"send failed: {exc}") from exc
