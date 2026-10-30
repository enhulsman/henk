"""Owner-only allowlist: the security boundary at the channel edge.

Only messages and reactions from the configured owner identity, in a direct
(non-group) conversation, are passed on. Everything else is dropped with no reply,
no read receipt, no typing indicator — and logged with the sender identity for
audit. A reaction gets the same check as a message (approval-reactions D3): it can
answer an approval, so a stranger's must never reach the gate.
"""

from __future__ import annotations

import logging

from henk.channel.base import InboundMessage, InboundReaction

logger = logging.getLogger("henk.channel.allowlist")


class AllowlistFilter:
    """Decides whether an inbound message or reaction may be routed at all."""

    def __init__(self, owner_id: str) -> None:
        if not owner_id or not owner_id.strip():
            # An empty owner id would make "sender != owner" false for a
            # senderless envelope — a fail-open hole. Refuse to construct.
            raise ValueError("owner_id must be a non-empty identity")
        self.owner_id = owner_id

    def allows(self, message: InboundMessage | InboundReaction) -> bool:
        kind = "reaction" if isinstance(message, InboundReaction) else "message"
        if message.is_group:
            logger.warning("dropped group %s from sender=%s", kind, message.sender)
            return False
        # Empty/unknown sender is never the owner — guard explicitly so a
        # senderless envelope can never slip through the equality check.
        if not message.sender or message.sender != self.owner_id:
            logger.warning("dropped %s from non-owner sender=%s", kind, message.sender)
            return False
        return True
