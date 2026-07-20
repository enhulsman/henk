"""Channel-neutral adapter contract shared by every messenger adapter.

Nothing here (or anywhere outside ``henk/channel/signal.py``) may reference
Signal-specific types, numbers, or the signal-cli-rest-api wire format — that
encapsulation is asserted by the channel-adapter contract tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable

#: Conservative default per-message length. Signal accepts more, but chunking
#: well under the limit keeps replies readable and avoids edge-case rejections.
DEFAULT_SAFE_LENGTH = 2000


@dataclass(frozen=True)
class InboundMessage:
    """A message received from a channel, normalised to channel-neutral fields."""

    sender: str
    text: str
    timestamp: float
    is_group: bool = False


@runtime_checkable
class ChannelAdapter(Protocol):
    """The only surface the agent core sees. Any messenger implements this."""

    async def messages(self) -> AsyncIterator[InboundMessage]:
        """Yield inbound messages as they arrive. Must not raise on transport errors."""
        ...

    async def send(self, text: str) -> None:
        """Deliver a text reply to the owner, splitting long text as needed."""
        ...


def split_message(text: str, limit: int = DEFAULT_SAFE_LENGTH) -> list[str]:
    """Split ``text`` into ordered chunks each at most ``limit`` chars.

    Splits at natural boundaries — paragraph, then line, then word — falling back
    to a hard character cut only when a single token exceeds ``limit``. The
    concatenation of the returned chunks is exactly ``text``: content is never
    truncated or reordered.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut != -1:
            cut += 2
        else:
            cut = window.rfind("\n")
            if cut != -1:
                cut += 1
            else:
                cut = window.rfind(" ")
                if cut != -1:
                    cut += 1
                else:
                    cut = limit  # no boundary in window: hard split
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks
