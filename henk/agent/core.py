"""Agent core: turns inbound owner messages into Claude Agent SDK turns.

Responsibilities:
- one session per conversation, reused across follow-ups for context continuity;
- serial processing — messages are queued and run one at a time, in order;
- reset on ``/new`` and after an idle window;
- honest, short error replies when a turn fails, without crashing the process.

Only the agent's final text reply is sent; intermediate tool activity is not.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from henk.agent.session import AgentSession, SessionFactory

logger = logging.getLogger("henk.agent")

RESET_COMMAND = "/new"
RESET_CONFIRMATION = "Session reset."
DEFAULT_ERROR_REPLY = (
    "Sorry — I hit an error handling that and couldn't complete it. "
    "Try again in a moment."
)


class _Sender:
    async def send(self, text: str) -> None: ...


class AgentCore:
    def __init__(
        self,
        factory: SessionFactory,
        channel: _Sender,
        *,
        idle_timeout_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
        error_reply: str = DEFAULT_ERROR_REPLY,
    ) -> None:
        self._factory = factory
        self._channel = channel
        self._idle_timeout = idle_timeout_seconds
        self._clock = clock
        self._error_reply = error_reply
        self._session: AgentSession | None = None
        self._last_activity: float | None = None
        self._queue: "asyncio.Queue[str]" = asyncio.Queue()

    async def submit(self, text: str) -> None:
        """Enqueue an inbound message for serial processing."""
        await self._queue.put(text)

    async def run(self) -> None:
        """Process the queue forever, one message at a time, in arrival order."""
        while True:
            text = await self._queue.get()
            try:
                await self.process(text)
            except Exception:  # pragma: no cover - defensive; process handles its own
                logger.exception("unexpected error processing message")
            finally:
                self._queue.task_done()

    async def process(self, text: str) -> None:
        """Handle a single message end to end (used directly by tests)."""
        if text.strip() == RESET_COMMAND:
            await self._close_session()
            self._last_activity = None
            await self._channel.send(RESET_CONFIRMATION)
            return

        await self._ensure_session()
        try:
            reply = await self._session.run_turn(text)  # type: ignore[union-attr]
        except Exception:
            logger.exception("agent turn failed")
            await self._channel.send(self._error_reply)
            self._last_activity = self._clock()
            return
        self._last_activity = self._clock()
        if reply:
            await self._channel.send(reply)

    async def _ensure_session(self) -> None:
        now = self._clock()
        expired = (
            self._last_activity is not None
            and (now - self._last_activity) > self._idle_timeout
        )
        if self._session is None or expired:
            await self._close_session()
            self._session = self._factory.create()
            self._last_activity = now

    async def _close_session(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover - best effort
                logger.warning("error closing session", exc_info=True)
            self._session = None
