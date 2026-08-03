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
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import (
    Any,
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Protocol,
)

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

#: Stable identifier on the liveness-trip log line. A trip is *behaviourally*
#: indistinguishable from any other transport failure (same control flow, so it
#: cannot lose events) and *observationally* distinguishable: trip counts and
#: inter-trip intervals are extracted later by matching this token. It is a
#: contract — reword the sentence around it freely, never the token itself.
LIVENESS_TRIP_MARKER = "intake-liveness-trip"


def _fmt_ts(value: float | None) -> str:
    """Wall-clock stamp for the owner-facing lines; ``never`` before the first."""
    if value is None:
        return "never"
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")


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
    """Minimal transport contract: yield raw ntfy frames from ``since`` onward.

    An async *generator*, not merely an iterator: the intake loop closes each
    connection's generator explicitly (see :meth:`EventIntake.events`), because a
    liveness trip abandons it while it is suspended mid-read.
    """

    def subscribe(self, since: str | None) -> AsyncGenerator[dict, None]:
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
        mono_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout_ctx: Callable[
            [float], AbstractAsyncContextManager[Any]
        ] = asyncio.timeout,
        backoff_base: float = 1.0,
        max_backoff: float = 30.0,
        liveness_deadline: float = 135.0,
        liveness_report_interval: float = 3600.0,
        on_since_rejected: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._stream = stream
        self._on_since_rejected = on_since_rejected
        # Bounds the since-rejection recovery: the first is immediate and tells
        # the owner, later ones are paced and silent (mirrors the core's
        # durability latch -- an unattended agent must not storm the channel).
        self._recoveries = 0
        self._clock = clock
        # Liveness arithmetic must be monotonic or an NTP step trips or stalls the
        # watchdog (same split as EventCoordinator: wall-clock for anything
        # displayed or persisted, monotonic for in-process intervals).
        self._mono_clock = mono_clock
        self._sleep = sleep
        # Substituted as a PAIR with mono_clock: asyncio.timeout reads the event
        # loop clock, so a fake clock beside the real context manager is
        # incoherent. Takes a remaining budget in seconds, not an absolute
        # deadline -- an absolute loop time cannot be expressed through a relative
        # seam, and it would put the arithmetic out of reach of the tests.
        self._timeout_ctx = timeout_ctx
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._liveness_deadline = liveness_deadline
        self._liveness_report_interval = liveness_report_interval
        # Seeded from the durable checkpoint on restart so the first subscribe
        # resumes with since=<offset> and replays events published while stopped
        # (design D1). None on the first ever start → cold subscribe, no since.
        self._last_id: str | None = initial_offset
        # --- liveness state ---------------------------------------------------
        # The budget anchor (monotonic) and the exposed timestamp (wall-clock) are
        # the same concept held twice, and they deliberately DIVERGE at one point:
        # re-establishing the budget before a subscribe moves the anchor, but a
        # reconnect is not evidence of delivery, so the exposed timestamp goes
        # stale until a real proof-of-life frame arrives.
        self._last_proof_of_life_mono = mono_clock()
        self._last_proof_of_life_at = clock()
        self._last_reconnect_at: float | None = None
        # The backoff penalty tracks transport health across the process, so it
        # lives here rather than as a local in events() -- liveness_state() and
        # the periodic line both report it.
        self._penalty = 0
        self._first_proof_seen = False
        self._last_report_mono = mono_clock()
        self._frames_since_report = 0

    async def events(self) -> AsyncIterator[Event]:
        """Yield events forever, reconnecting with backoff + ``since`` on error.

        The retrieval loop is desugared out of ``async for`` because that form has
        no syntactic place for a per-retrieval timeout, and wrapping the whole
        ``async for`` instead would put the scope around the consumer's ``yield``
        — raising ``CancelledError`` into the consumer rather than being handled
        here. The path of least resistance is the fatal one and *looks* compliant.
        """
        connected = False
        while True:
            if connected:
                self._last_reconnect_at = self._clock()
            connected = True
            # The liveness budget is (re-)established HERE: immediately before the
            # subscribe and AFTER any backoff sleep above. Established once
            # outside this loop, every post-trip connection inherits an expired
            # budget and dies before its first frame -- permanently, silently, and
            # capped at max backoff (measured: zero events after the first trip).
            # Anchored before the sleep, a 30s backoff eats 30s of the window.
            self._last_proof_of_life_mono = self._mono_clock()
            agen = self._stream.subscribe(since=self._last_id)
            try:
                try:
                    while True:
                        remaining = self._liveness_deadline - (
                            self._mono_clock() - self._last_proof_of_life_mono
                        )
                        try:
                            # Scoped to frame RETRIEVAL only. Keyed on the budget
                            # remaining since the last proof of life, not a full
                            # window per retrieval: the latter restarts on `open`
                            # frames, so it is keyed on any frame and measurably
                            # never fires under an `open` flood.
                            async with self._timeout_ctx(remaining):
                                raw = await agen.__anext__()
                        except StopAsyncIteration:
                            # Caught INSIDE events(): this is itself an async
                            # generator, so a StopAsyncIteration escaping its body
                            # becomes "RuntimeError: async generator raised
                            # StopAsyncIteration".
                            break
                        except TimeoutError as exc:
                            # TimeoutError is not an EventStreamError, so the
                            # handler below would miss it: it would escape
                            # events(), kill the coordinator's pump task and leave
                            # run() blocked on queue.get() forever -- the first
                            # trip would silently kill intake. Normalise at the
                            # point it fires, with status=None so
                            # _is_since_rejection can never misread it and
                            # _recoveries is untouched.
                            raise EventStreamError(
                                f"{LIVENESS_TRIP_MARKER}: nothing delivered in the "
                                f"remaining {remaining:.0f}s of the "
                                f"{self._liveness_deadline:.0f}s liveness deadline "
                                f"[{self._liveness_summary()}]",
                                status=None,
                            ) from exc

                        # Liveness accounting reads raw["event"] ONLY, and runs
                        # BEFORE the _convert/continue guard so control frames
                        # reach it. It must never write _last_id: ntfy control
                        # frames carry an `id`, and writing one into the cursor is
                        # either 400ed (retention replay + owner DM) or accepted,
                        # silently skipping every message since.
                        if raw.get("event") != "open":
                            self._note_proof_of_life()

                        event = self._convert(raw)
                        if event is None:
                            continue
                        self._last_id = event.id or self._last_id
                        yield event
                        # Advance the anchor AFTER the yield returns, so consumer
                        # latency is not charged against liveness: liveness
                        # measures whether the stream is delivering, not how fast
                        # Henk processes what it delivers. (Keepalives are never
                        # yielded, so they advance it at classification above.)
                        self._last_proof_of_life_mono = self._mono_clock()
                except EventStreamError as exc:
                    if self._is_since_rejection(exc):
                        # The server refuses our resume point (a malformed
                        # checkpoint -- ntfy 400s anything it cannot parse).
                        # Retrying it would wedge intake forever, silently, which
                        # is the exact failure this change exists to prevent. Fall
                        # back to a full retention replay: bounded, and
                        # cooldown/cap absorb the repeats.
                        self._recoveries += 1
                        logger.error(
                            "ntfy rejected since=%s (HTTP %s); replaying all "
                            "retained events instead so intake cannot wedge on a "
                            "bad checkpoint (recovery #%d)",
                            self._last_id,
                            exc.status,
                            self._recoveries,
                        )
                        self._last_id = RETENTION_REPLAY_SINCE
                        if self._recoveries == 1:
                            # The expected case: one bad checkpoint, one notice,
                            # and an immediate reconnect since the sentinel is
                            # known-valid.
                            await self._notify_since_rejected()
                            continue
                        # A cursor that keeps being rejected would otherwise
                        # re-download the whole retention window in a tight loop --
                        # DM-storming the owner and rewriting a suppression record
                        # per replayed event into the unrotated audit log. Notify
                        # once, then pace it by falling through to the backoff.
                    await self._backoff(f"event stream failed ({exc})")
                else:
                    # Clean end of stream (rare for a long poll). It takes the
                    # backoff path unconditionally: a healthy stream's keepalives
                    # have already zeroed the penalty, so this still costs only the
                    # base delay, while a connection that opened and delivered
                    # nothing escalates instead of retrying forever at a fixed
                    # interval. No per-connection "did this deliver?" flag needed.
                    await self._backoff("event stream ended", level=logging.INFO)
            finally:
                # Broad on purpose. `await` inside a `finally` spanning a `yield`
                # is legal (it is `yield` there that raises "async generator
                # ignored GeneratorExit"). The narrow form -- closing only on the
                # timeout and error paths -- leaks on consumer abandonment, which
                # is the most-travelled path and the only one where aclose() has
                # real work to do, every other having closed the generator by
                # exception already. This fires when the OUTER generator is closed,
                # which is why the coordinator's pump must hold and close it too.
                await agen.aclose()

    def _note_proof_of_life(self) -> None:
        """Record a proof-of-life frame: any frame whose ``event`` is not ``open``.

        An ``open`` frame proves a connection was accepted; it does not prove the
        stream is delivering. Keyed on the frame's type, never on its position
        relative to an ``open``.
        """
        now_mono = self._mono_clock()
        self._last_proof_of_life_mono = now_mono
        self._last_proof_of_life_at = self._clock()
        # Reset the penalty here, not on a delivered event: a keepalive IS
        # transport health, and without this a failure that raised the penalty
        # followed by a reconnect into a quiet period leaves the counter parked, so
        # the next genuine blip starts at maximum backoff. (A since-rejection
        # recovery still inherits the penalty -- it installs a fresh cursor, which
        # is not evidence about the transport.)
        self._penalty = 0
        self._frames_since_report += 1
        if not self._first_proof_seen:
            self._first_proof_seen = True
            self._last_report_mono = now_mono
            self._frames_since_report = 0
            logger.info(
                "intake liveness: first proof-of-life frame; %s",
                self._liveness_summary(),
            )
        elif now_mono - self._last_report_mono >= self._liveness_report_interval:
            logger.info(
                "intake liveness: still delivering; %d proof-of-life frames in the "
                "last %.0fs; %s",
                self._frames_since_report,
                now_mono - self._last_report_mono,
                self._liveness_summary(),
            )
            self._last_report_mono = now_mono
            self._frames_since_report = 0

    def liveness_state(self) -> dict[str, Any]:
        """Last proof of life, last reconnect, and the current backoff penalty.

        A test seam and a hook for a future in-process reader — **not** the
        owner-facing surface. There is no status tool, admin command or
        coordinator passthrough, so out of process the log emissions are what is
        readable; a Signal-exposed liveness tool is its own change.
        """
        return {
            "last_proof_of_life_at": self._last_proof_of_life_at,
            "last_reconnect_at": self._last_reconnect_at,
            "backoff_penalty": self._penalty,
        }

    def _liveness_summary(self) -> str:
        return (
            "last proof-of-life %s (%.0fs ago), last reconnect %s, penalty %d"
            % (
                _fmt_ts(self._last_proof_of_life_at),
                max(0.0, self._clock() - self._last_proof_of_life_at),
                _fmt_ts(self._last_reconnect_at),
                self._penalty,
            )
        )

    async def _backoff(self, reason: str, *, level: int = logging.WARNING) -> None:
        """Log the end of a connection and pace the reconnect.

        Shared by every path that ends a connection, because the clean-end path
        has no exception to log: without a *reason* parameter the naive
        implementation reuses the error path and reports "event stream failed" on
        every healthy clean end. ``reason`` is also where the trip's stable
        identifier rides, so it must be passed through verbatim.
        """
        delay = min(self._backoff_base * (2**self._penalty), self._max_backoff)
        self._penalty += 1
        logger.log(
            level,
            "%s; reconnecting from since=%s in %.1fs",
            reason,
            self._last_id,
            delay,
        )
        await self._sleep(delay)

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
        self,
        base_url: str,
        topic: str,
        *,
        token: str = "",
        open_timeout: float = 30.0,
        read_timeout: float | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._topic = topic
        self._token = token
        self._open_timeout = open_timeout
        self._read_timeout = read_timeout

    async def subscribe(self, since: str | None) -> AsyncGenerator[dict, None]:
        import json

        import httpx

        params = {"since": since} if since else {}
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        url = f"{self._base_url}/{self._topic}/json"
        # A redundant floor under the intake budget, adopted deliberately: it
        # normalises through the `except Exception` below, needs no cancellation of
        # a live generator, and downgrades a broken hand-rolled watchdog from a
        # permanent hang to a bounded reconnect. It does MASK budget-arithmetic
        # defects at deploy time, so the unit tests stay primary. httpx's read
        # timeout resets on any received bytes, which is why it cannot replace the
        # per-frame budget: a peer dribbling newlines defeats it.
        #
        # `open_timeout` was assigned and never read -- the previous
        # `timeout=None` also meant no CONNECT timeout. Both live on one
        # httpx.Timeout object, so they are decided together: it becomes the
        # connect bound. Write and pool stay unbounded, as before.
        timeout = httpx.Timeout(
            None, connect=self._open_timeout, read=self._read_timeout
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
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
