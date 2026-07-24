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

One deliberate exception to "pure transport adapter": when the server rejects the
persisted resume point, intake awaits an injected ``on_since_rejected`` callback
that reaches the owner's channel (design D8). That send briefly blocks the intake
loop; it is best-effort and bounded by the adapter's own timeouts, and it fires at
most once per process.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Optional, Protocol

from henk.events.types import Event

logger = logging.getLogger("henk.events.intake")

#: ntfy's "replay everything still retained" cursor. Used as the recovery value
#: when the server rejects our checkpoint (see :class:`EventIntake`), because an
#: over-replay is absorbed by cooldown/cap whereas a cold subscribe silently
#: drops every event published while Henk was down.
RETENTION_REPLAY_SINCE = "all"

#: One-shot operator alert when the persisted checkpoint is unusable. Henk runs
#: unattended, so a cursor the server refuses must not be a log-only event.
SINCE_REJECTED_NOTICE = (
    "⚠️ Henk: the saved event checkpoint was rejected by ntfy, so intake fell "
    "back to replaying everything still retained. Some incidents may re-notify. "
    "No events were dropped, but the checkpoint file is worth a look."
)


class EventStreamError(Exception):
    """Raised by a transport when the ntfy subscription is unreachable or errors.

    ``status`` carries the HTTP status when the failure was an HTTP error
    response, so the intake loop can tell a rejected *resume point* (400) from an
    ordinary transport blip and recover instead of retrying the same bad value.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


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
        initial_offset: str | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_base: float = 1.0,
        max_backoff: float = 30.0,
        on_since_rejected: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._stream = stream
        self._on_since_rejected = on_since_rejected
        # Bounds the since-rejection recovery: the first is immediate and tells
        # the owner, later ones are paced and silent (mirrors the core's
        # durability latch -- an unattended agent must not storm the channel).
        self._recoveries = 0
        self._clock = clock
        self._sleep = sleep
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        # Seeded from the durable checkpoint on restart so the first subscribe
        # resumes with since=<offset> and replays events published while stopped
        # (design D1). None on the first ever start → cold subscribe, no since.
        self._last_id: str | None = initial_offset

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
                    # A delivered event proves the stream is healthy. Note this is
                    # the ONLY reset: a since-rejection recovery installs a fresh
                    # cursor but inherits the existing backoff penalty, because
                    # `attempt` tracks transport health, not cursor validity.
                    attempt = 0
                    yield event
            except EventStreamError as exc:
                if self._is_since_rejection(exc):
                    # The server refuses our resume point (a malformed checkpoint
                    # -- ntfy 400s anything it cannot parse). Retrying it would
                    # wedge intake forever, silently, which is the exact failure
                    # this change exists to prevent. Fall back to a full retention
                    # replay: bounded, and cooldown/cap absorb the repeats.
                    self._recoveries += 1
                    logger.error(
                        "ntfy rejected since=%s (HTTP %s); replaying all retained "
                        "events instead so intake cannot wedge on a bad checkpoint "
                        "(recovery #%d)",
                        self._last_id,
                        exc.status,
                        self._recoveries,
                    )
                    self._last_id = RETENTION_REPLAY_SINCE
                    if self._recoveries == 1:
                        # The expected case: one bad checkpoint, one notice, and
                        # an immediate reconnect since the sentinel is known-valid.
                        await self._notify_since_rejected()
                        continue
                    # A cursor that keeps being rejected would otherwise re-download
                    # the whole retention window in a tight loop -- DM-storming the
                    # owner and rewriting a suppression record per replayed event
                    # into the unrotated audit log. Notify once, then pace it by
                    # falling through to the normal backoff.
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

    def _is_since_rejection(self, exc: EventStreamError) -> bool:
        """True when the server rejected the *resume point* specifically.

        Requires a 400 AND a ``since`` we could actually be blamed for: a cold
        subscribe sends none, and the sentinel is already the fallback, so in
        both cases there is nothing to recover to and the normal backoff applies.
        """
        return (
            exc.status == 400
            and self._last_id is not None
            and self._last_id != RETENTION_REPLAY_SINCE
        )

    async def _notify_since_rejected(self) -> None:
        if self._on_since_rejected is None:
            return
        try:
            await self._on_since_rejected()
        except Exception:  # noqa: BLE001 - best effort; must never kill intake
            logger.warning("failed to send since-rejected notice", exc_info=True)

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
        except httpx.HTTPStatusError as exc:
            # Keep the status: the intake loop needs it to tell a rejected resume
            # point (400) from an outage it should simply retry.
            raise EventStreamError(
                f"ntfy subscribe failed: {exc}", status=exc.response.status_code
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalise every failure
            raise EventStreamError(f"ntfy subscribe failed: {exc}") from exc
