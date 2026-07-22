"""Outbound-only ntfy subscription to the events topic (design D1, D10).

``EventIntake`` opens a streaming subscription through an injected
:class:`EventStream` transport and yields :class:`~henk.events.types.Event`
objects. It tracks the last-seen message id and, on any transport failure,
backs off and reconnects with ``since=<last id>`` so events published during an
outage are received exactly once (bounded replay — same reconnect discipline as
the Signal bridge). A transport error never propagates out of :meth:`events`:
the reactive owner-DM path lives in a different loop and must stay functional
even while intake is failing (event-intake spec: intake failures are non-fatal).

The concrete transport (:class:`NtfyEventStream`) is deployment wiring and is
exercised at deploy, not in unit tests — tests drive a fake stream.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Optional, Protocol

from henk.events.types import Event

logger = logging.getLogger("henk.events.intake")


class EventStreamError(Exception):
    """Raised by a transport when the ntfy subscription is unreachable or errors."""


class EventStream(Protocol):
    """Minimal transport contract: yield raw ntfy frames from ``since`` onward."""

    def subscribe(self, since: str | None) -> AsyncIterator[dict]:
        """Yield raw ntfy JSON frames. May raise EventStreamError."""
        ...


class EventIntake:
    """Adapts an :class:`EventStream` into a resilient stream of ``Event``s."""

    def __init__(
        self,
        stream: EventStream,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_base: float = 1.0,
        max_backoff: float = 30.0,
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._sleep = sleep
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._last_id: str | None = None

    async def events(self) -> AsyncIterator[Event]:
        """Yield events forever, reconnecting with backoff + ``since`` on error."""
        attempt = 0
        while True:
            try:
                async for raw in self._stream.subscribe(since=self._last_id):
                    event = self._convert(raw)
                    if event is None:
                        continue
                    self._last_id = event.id or self._last_id
                    attempt = 0  # a delivered event proves the stream is healthy
                    yield event
            except EventStreamError as exc:
                delay = min(self._backoff_base * (2**attempt), self._max_backoff)
                attempt += 1
                logger.warning(
                    "event stream failed (%s); reconnecting from since=%s in %.1fs",
                    exc,
                    self._last_id,
                    delay,
                )
                await self._sleep(delay)
            else:
                # Clean end of stream (rare for a long poll): brief pause, resume.
                await self._sleep(self._backoff_base)

    def _convert(self, raw: Mapping[str, Any]) -> Optional[Event]:
        """Convert a raw ntfy frame to an ``Event``; skip control frames.

        ntfy's JSON stream interleaves ``open``/``keepalive`` control frames with
        ``message`` frames; only the latter carry an event.
        """
        if raw.get("event") != "message":
            return None
        return Event(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            message=str(raw.get("message", "")),
            arrival_time=self._clock(),
            raw=dict(raw),
        )


class NtfyEventStream:  # pragma: no cover - deploy path (needs live ntfy + httpx)
    """Concrete ``EventStream`` over ntfy's newline-delimited JSON stream.

    Long-lived ``GET /{topic}/json?since=<id>`` with the scoped bearer token.
    Every transport failure is normalised to ``EventStreamError`` so the intake
    backoff loop handles it. httpx is imported lazily so importing this module
    never requires it.
    """

    def __init__(
        self, base_url: str, topic: str, *, token: str = "", open_timeout: float = 30.0
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._topic = topic
        self._token = token
        self._open_timeout = open_timeout

    async def subscribe(self, since: str | None) -> AsyncIterator[dict]:
        import json

        import httpx

        params = {"since": since} if since else {}
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        url = f"{self._base_url}/{self._topic}/json"
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET", url, params=params, headers=headers
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.strip():
                            yield json.loads(line)
        except Exception as exc:  # noqa: BLE001 - normalise every failure
            raise EventStreamError(f"ntfy subscribe failed: {exc}") from exc
