"""Composition layer: wires adapter → allowlist → gate-routing → agent core.

This is the run path the security controls actually live in. Without it, the
allowlist, the gate, and the core are unwired islands (scrutiny C3). The
``Dispatcher`` enforces the ordering the specs require:

1. every inbound message passes the owner allowlist first (strangers dropped);
2. while an approval is pending, the message is classified by the gate BEFORE
   normal queueing — an unrelated message fails the pending approval closed and
   is then re-queued as a normal turn (never swallowed);
3. otherwise it is queued for serial processing by the core.
"""

from __future__ import annotations

import asyncio
import logging

from henk.agent.core import AgentCore
from henk.channel.allowlist import AllowlistFilter
from henk.channel.base import ChannelAdapter, InboundMessage
from henk.gate.approval import ApprovalGate, Classification

logger = logging.getLogger("henk.app")


class Dispatcher:
    """Routes an allowed inbound message to either the gate or the core queue."""

    def __init__(
        self, allowlist: AllowlistFilter, gate: ApprovalGate, core: AgentCore
    ) -> None:
        self._allowlist = allowlist
        self._gate = gate
        self._core = core

    async def on_inbound(self, message: InboundMessage) -> None:
        if not self._allowlist.allows(message):
            return  # stranger / group: dropped silently, already logged
        if self._gate.has_pending():
            # Classify against the pending approval before normal queueing.
            classification, requeue = self._gate.deliver(message.text)
            logger.info("pending-approval message classified as %s", classification)
            if classification is Classification.UNRELATED and requeue:
                # Fail-closed already happened inside deliver(); the message is
                # not an approval, so process it as a normal new turn.
                await self._core.submit(message.text)
            return
        await self._core.submit(message.text)


class App:
    """Top-level runner: consumes the channel and pumps the core worker."""

    def __init__(
        self, adapter: ChannelAdapter, dispatcher: Dispatcher, core: AgentCore
    ) -> None:
        self._adapter = adapter
        self._dispatcher = dispatcher
        self._core = core

    async def run(self) -> None:
        worker = asyncio.create_task(self._core.run())
        try:
            async for message in self._adapter.messages():
                await self._dispatcher.on_inbound(message)
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
