"""Channel-neutral adapter contract shared by every messenger adapter.

Nothing here (or anywhere outside ``henk/channel/signal.py``) may reference
Signal-specific types, numbers, or the signal-cli-rest-api wire format — that
encapsulation is asserted by the channel-adapter contract tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Protocol, runtime_checkable

#: Conservative default per-message budget, in UTF-8 **bytes**. Signal accepts
#: more, but chunking well under the limit keeps replies readable and avoids
#: edge-case rejections. Bytes, not characters: bytes bound both the wire size
#: and any client-side character limit, whereas a character count bounds
#: neither — a chunk of emoji measured in characters encodes to 4x its
#: believed size.
DEFAULT_SAFE_LENGTH = 2000

#: The longest UTF-8 encoding of a single code point, and therefore the smallest
#: limit under which ``split_message`` can make progress at all: below it no code
#: point fits, so "never split a code point" and "reproduce the input exactly"
#: become jointly unsatisfiable and the window search would find a zero-length
#: cut. Enforced here AND at config load (``henk.config``).
MAX_CODE_POINT_BYTES = 4


class SendOutcome(str, Enum):
    """What a send operation observed about its own delivery.

    ``FAILED`` means **delivery was not confirmed**, never that nothing arrived:
    a transport fault can follow a message the bridge already accepted and sent,
    so a caller that retries on ``FAILED`` may duplicate a delivered message.
    By the same token ``DELIVERED`` is at-least-once, not exactly-once — the
    channel offers no idempotency key.
    """

    #: Every chunk was acknowledged.
    DELIVERED = "delivered"
    #: At least one chunk was acknowledged and the rest were abandoned. Never
    #: report this as success.
    PARTIAL = "partial"
    #: No chunk was acknowledged (including a send that produced no chunks).
    FAILED = "failed"


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

    async def send(self, text: str) -> SendOutcome:
        """Deliver a text reply to the owner, splitting long text as needed.

        Must not raise on transport errors: the outcome is the report. Callers
        that ignore the return value behave exactly as they did before it
        existed. On a non-delivered outcome the adapter emits its own standing
        failure notice — this is the reply path, so the adapter knows what was
        being sent.
        """
        ...

    async def send_proactive(
        self, text: str, *, failure_notice: str | None = None
    ) -> SendOutcome:
        """Deliver an agent-initiated message to the owner (not a reply).

        A separate operation rather than a flag on ``send``: the two differ in
        who authors the owner-facing failure notice. The adapter cannot know what
        a proactive send was carrying, so ``failure_notice`` is the caller's to
        supply — and when it supplies none, a failure is silent to the owner.

        Deliverable only to the configured owner identity; there is no recipient
        parameter, here or anywhere on this contract.
        """
        ...


def _byte_length(text: str) -> int:
    return len(text.encode("utf-8"))


def _chars_within(text: str, limit: int) -> int:
    """Longest character prefix of ``text`` whose UTF-8 encoding fits ``limit``.

    Never returns 0 for a non-empty ``text``, because ``limit`` is floored at
    ``MAX_CODE_POINT_BYTES`` — that floor is what guarantees the caller's loop
    advances.
    """
    total = 0
    for index, char in enumerate(text):
        width = len(char.encode("utf-8"))
        if total + width > limit:
            return index
        total += width
    return len(text)


def split_message(text: str, limit: int = DEFAULT_SAFE_LENGTH) -> list[str]:
    """Split ``text`` into ordered chunks each at most ``limit`` UTF-8 bytes.

    Splits at natural boundaries — paragraph, then line, then word — falling back
    to a hard cut only when a single token exceeds ``limit``. The concatenation of
    the returned chunks is exactly ``text``: content is never truncated or
    reordered, and no cut ever divides a code point.

    The measurement is bytes; the boundary search is characters. A window of
    characters that fits the byte budget is chosen first, so every cut inside it
    fits too. The guarantee is over code points, not grapheme clusters: a ZWJ or
    skin-tone emoji sequence can still be divided.
    """
    if limit < MAX_CODE_POINT_BYTES:
        raise ValueError(
            f"limit must be at least {MAX_CODE_POINT_BYTES} bytes (the longest "
            f"single code point's UTF-8 encoding); got {limit}"
        )
    if not text:
        return []
    if _byte_length(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while _byte_length(remaining) > limit:
        width = _chars_within(remaining, limit)
        window = remaining[:width]
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
                    cut = width  # no boundary in window: hard split
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks
