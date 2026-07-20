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
        if not owner_id or not owner_id.strip():
            # An empty owner id would make "sender != owner" false for a
            # senderless envelope — a fail-open hole. Refuse to construct.
            raise ValueError("owner_id must be a non-empty identity")
        self.owner_id = owner_id

    def allows(self, message: InboundMessage) -> bool:
        if message.is_group:
            logger.warning("dropped group message from sender=%s", message.sender)
            return False
        # Empty/unknown sender is never the owner — guard explicitly so a
        # senderless envelope can never slip through the equality check.
        if not message.sender or message.sender != self.owner_id:
            logger.warning("dropped message from non-owner sender=%s", message.sender)
            return False
        return True
