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
    #: Opaque, adapter-minted reference whose ONLY use is to be handed back to
    #: that adapter's ``acknowledge`` (owner-acknowledgement design D2, the one
    #: bounded encapsulation exception). Neutral code carries it and never parses
    #: it, persists it, audits it or puts it in an agent turn; the core receives
    #: an owner message as its text alone. ``None`` when the channel has nothing
    #: to acknowledge by. Deliberately not ``repr=False``: the same value already
    #: crosses the boundary as ``timestamp`` and is not sensitive, so hiding it
    #: would protect nothing (the rule is about scope, not secrecy).
    channel_ref: str | None = None


# --- Approval prompts and reactions (approval-reactions D1-D3) ---------------
#
# The gate hands the adapter a STRUCTURED prompt and the adapter renders it in its
# own syntax (D1): markup, escaping and any delimiters are the adapter's, because a
# second channel's rules would differ. The gate keeps the trust work that is not
# syntax: every value whitespace-collapsed to one line and bounded in length.


@dataclass(frozen=True)
class ApprovalPrompt:
    """A structured approval prompt; the adapter renders it in its own syntax.

    ``label`` and ``tool_name`` are fixed in code (the tool's own declaration);
    only the field values are model-chosen, and they arrive already collapsed to
    one line and bounded by the gate.
    """

    label: str
    tool_name: str
    #: (schema key, bounded value), in call order.
    fields: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PromptOutcome:
    """A decided prompt: ``verb`` is Approved | Denied | Cancelled | Timed out."""

    verb: str
    prompt: ApprovalPrompt


@dataclass(frozen=True)
class PromptSent:
    """What ``send_prompt`` observed: the delivery outcome and an opaque reference.

    ``ref`` names the sent prompt so a reaction can be matched to it and the
    prompt edited later (D2), the second bounded encapsulation exception beside
    ``InboundMessage.channel_ref``: neutral code compares it for equality and hands
    it back to ``edit_prompt``, and never parses, persists, audits it or puts it
    in a turn. ``None`` when the channel reported no reference; the prompt still
    works by keyword, and reactions to it are ignored.
    """

    outcome: SendOutcome
    ref: str | None = None
    #: True when nothing was sent because the prompt, in the form it would have
    #: been sent, does not fit one message. ``outcome`` is then ``FAILED``.
    too_large: bool = False


@dataclass(frozen=True)
class InboundReaction:
    """A reaction received from a channel, normalised to channel-neutral fields.

    A type of its own rather than an ``InboundMessage`` with empty text (D3): an
    empty-text message that slipped past a check would reach the core as a turn,
    while a reaction reaching code that expects ``.text`` fails loudly. It carries
    no acknowledgement reference, because a reaction is never acknowledged.
    """

    sender: str
    emoji: str
    #: Adapter-minted, in the same form ``send_prompt`` returns its reference;
    #: ``None`` unless the target is Henk's own message.
    target_ref: str | None
    #: True when the owner took the reaction back.
    removed: bool
    is_group: bool = False


@runtime_checkable
class ChannelAdapter(Protocol):
    """The only surface the agent core sees. Any messenger implements this."""

    async def messages(self) -> AsyncIterator[InboundMessage | InboundReaction]:
        """Yield inbound messages and reactions as they arrive.

        Must not raise on transport errors. A reaction is never yielded as a
        message (approval-reactions D3).
        """
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

    async def send_prompt(self, prompt: ApprovalPrompt) -> PromptSent:
        """Send one approval prompt to the owner, rendered in the adapter's syntax.

        Never split (approval-reactions D2): a prompt whose form as sent exceeds
        one message is not sent at all and reports ``FAILED`` with ``too_large``,
        since a reaction lands on one message and an edit rewrites one. Serialized
        with every other send and retried like a reply; at-least-once like every
        send. Returns the outcome with an opaque reference to the sent message, or
        none when the channel reported none. Must not raise on transport errors.
        """
        ...

    async def edit_prompt(self, ref: str, outcome: PromptOutcome) -> bool:
        """Edit the prompt ``ref`` names into its one-line outcome.

        Rendered in the mode the prompt was sent in; serialized with every other
        send; a single attempt, because a lost edit costs a cosmetic record and a
        retried one costs the owner latency on every approval (D2). Reports
        whether the channel accepted it. A reference this adapter cannot interpret
        makes no request and returns ``False``. Must not raise on transport errors.
        """
        ...

    # Acknowledgement operations (owner-acknowledgement). Content-free signals to
    # the owner, not messages. None takes a recipient, a sender or any identity:
    # each is directed to the configured owner identity the adapter was built
    # with, which is what keeps a stranger unreachable even if a caller were to
    # misplace the call. Each reports whether the channel accepted it, makes a
    # single attempt, and never raises on a transport error. None bounds itself or
    # logs: the caller applies the bound and owns the log line.

    async def acknowledge(self, channel_ref: str | None) -> bool:
        """Tell the owner that the inbound message ``channel_ref`` was read.

        Takes only the reference this adapter minted on that message, never the
        message itself, which carries a ``sender``. Only for a message that has
        passed the owner-only allowlist. ``None``, or a reference this adapter
        cannot interpret, makes no request and returns ``False``. Must not raise
        on transport errors.
        """
        ...

    async def start_working(self) -> bool:
        """Start, or re-assert, the owner-facing working indicator.

        Owner only; no parameters. Must not raise on transport errors.
        """
        ...

    async def stop_working(self) -> bool:
        """Stop the owner-facing working indicator.

        Owner only; no parameters. Must not raise on transport errors.
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
