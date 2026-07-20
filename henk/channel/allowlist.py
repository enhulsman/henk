"""Owner-only allowlist: the security boundary at the channel edge.

Only messages from the configured owner identity, in a direct (non-group)
conversation, are passed on. Everything else is dropped with no reply, no read
receipt, no typing indicator — and logged with the sender identity for audit.
"""

from __future__ import annotations

import logging

from henk.channel.base import InboundMessage

logger = logging.getLogger("henk.channel.allowlist")


class AllowlistFilter:
    """Decides whether an inbound message may reach the agent core."""

    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id

    def allows(self, message: InboundMessage) -> bool:
        if message.is_group:
            logger.warning("dropped group message from sender=%s", message.sender)
            return False
        if message.sender != self.owner_id:
            logger.warning("dropped message from non-owner sender=%s", message.sender)
            return False
        return True
