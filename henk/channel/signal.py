"""Signal adapter over signal-cli-rest-api (json-rpc mode).

This is the ONLY module that may know Signal specifics: the bridge wire format,
Henk's account number, and signal-cli-rest-api endpoints. Everything above it
speaks the channel-neutral contract in ``henk.channel.base``.

The transport is injected as a ``SignalBridge`` so the adapter's conversion,
backoff, and splitting logic are testable without a live bridge or a websocket
library. The default bridge (``SignalCliRestBridge``) is deployment wiring and
lazily imports its websocket dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Awaitable, Callable, Optional, Protocol, TypeVar

from henk.channel.base import (
    DEFAULT_SAFE_LENGTH,
    ApprovalPrompt,
    InboundMessage,
    InboundReaction,
    PromptOutcome,
    PromptSent,
    SendOutcome,
    split_message,
)

logger = logging.getLogger("henk.channel.signal")

_T = TypeVar("_T")

#: The adapter's standing owner-facing notice for a reply that was cut off. Only
#: correct for the reply path — a proactive send's notice is its caller's, since
#: the adapter cannot know what was being sent (design D2).
REPLY_FAILURE_NOTICE = "[⚠ part of this reply could not be delivered]"

#: How often the working indicator's start is re-asserted, in seconds, measured
#: from each start's ISSUE time (owner-acknowledgement design D4). A property of
#: Signal's clients, not a Henk policy knob: they expire a typing indicator about
#: 15 s after the last start. At 7 s, one lost refresh leaves at most 2 × 7 = 14 s
#: between the last successful start and the next one issued, still under the
#: expiry; at 8 s the same loss would leave 16 s and the indicator would drop.
#: The config loader caps `signal.acknowledge_timeout_seconds` at this value,
#: which is what keeps that arithmetic true when a refresh hangs.
TYPING_REFRESH_SECONDS = 7.0

# --- Approval prompt rendering (approval-reactions D5) -------------------------
#
# The bridge's styled text mode (`TextstyleParser`, bridge 0.101; see the change's
# notes/bridge-facts.md): `**bold**`, `*italic*`, `~strike~`, `||spoiler||` and
# `` `monospace` ``. A single backslash immediately before a styling character is
# swallowed and makes that character literal; any other backslash is literal; a
# backslash cannot itself be escaped. Everything below follows from those rules.

#: The approval prompt's footer, the owner's sentence verbatim.
PROMPT_FOOTER = (
    "React \U0001F44D to approve or \U0001F44E to deny, or reply yes / no. "
    "Any other message cancels it."
)
PROMPT_HEADER = "Approval needed"
#: Shown in place of the argument lines for an action that takes none.
NO_ARGUMENTS_LINE = "(no arguments)"
#: Every character the styled parser treats as a marker.
STYLE_CHARACTERS = "*`|~"
#: Placed before a styling character to make it literal. ONE backslash in the
#: decoded string: "two backslashes" in the bridge's doc is the JSON source form.
#: One constant, so the live crafted-value check (task 7.2) can correct it in one
#: place.
STYLE_ESCAPE = "\\"
#: The visible stand-in for every backslash in styled content. A content backslash
#: cannot be shown as itself: before a styling character the parser swallows it,
#: and a value ending in one would escape its own closing backtick.
BACKSLASH_STAND_IN = "\N{REVERSE SOLIDUS OPERATOR}"
#: Around every value in the unstyled form, and stripped from every value in both
#: forms so none can close its own quoting.
UNSTYLED_DELIMITERS = ("<<<", ">>>")


class SignalBridgeError(Exception):
    """Raised by a bridge when signal-cli-rest-api is unreachable or errors."""


class SignalBridge(Protocol):
    """Minimal transport contract the adapter depends on."""

    def receive(self) -> AsyncIterator[dict]:
        """Yield raw signal-cli-rest-api envelopes. May raise SignalBridgeError."""
        ...

    async def send(
        self,
        recipient: str,
        text: str,
        *,
        styled: bool = False,
        edit_timestamp: int | None = None,
    ) -> int | None:
        """Send one message. May raise SignalBridgeError.

        ``styled`` selects the bridge's styled text mode, ``edit_timestamp`` makes
        the send an edit of the message sent at that timestamp, and the return
        value is the response's sent timestamp as an int (``None`` when the bridge
        reported none). The adapter's minting still tolerates a decimal string.
        """
        ...

    async def send_receipt(self, recipient: str, timestamp: int) -> None:
        """Send a ``read`` receipt for the message sent at ``timestamp``.

        May raise SignalBridgeError. Single attempt, unbounded here: the bound is
        applied by cancellation outside this module (owner-acknowledgement D6).
        """
        ...

    async def start_typing(self, recipient: str) -> None:
        """Start (or re-assert) the typing indicator. May raise SignalBridgeError."""
        ...

    async def stop_typing(self, recipient: str) -> None:
        """Stop the typing indicator. May raise SignalBridgeError."""
        ...


class SignalAdapter:
    """Adapts a ``SignalBridge`` to the channel-neutral ``ChannelAdapter``."""

    def __init__(
        self,
        bridge: SignalBridge,
        *,
        account: str,
        owner: str,
        safe_length: int = DEFAULT_SAFE_LENGTH,
        backoff_base: float = 1.0,
        max_backoff: float = 30.0,
        max_send_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        styled_prompts: bool = False,
    ) -> None:
        self._bridge = bridge
        #: Prompts and their edits in styled text (approval-reactions D5). Fixed for
        #: the adapter's life, which is what makes an edit render in the mode its
        #: prompt was sent in: configuration loads at start, and a pending approval
        #: does not survive a restart.
        self._styled_prompts = styled_prompts
        self._account = account
        self._owner = owner
        self._safe_length = safe_length
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._max_send_attempts = max_send_attempts
        self._sleep = sleep
        #: Serializes outbound send sequences (reminder-delivery design D6). Chunks
        #: of concurrent senders could interleave before this existed — harmless
        #: while the only cross-task sender emitted a single-chunk notice, and a real
        #: defect the moment the reminder scheduler became a second multi-chunk
        #: sender.
        #:
        #: **Instance state, and that is load-bearing wiring.** A second
        #: `SignalAdapter` over the same bridge would satisfy every serialization
        #: test while serializing nothing, so the scheduler must be handed the SAME
        #: adapter the agent core holds (asserted where the runtime is wired).
        #:
        #: Deliberately unbounded: no hold timer and no chunk cap. Both were designed
        #: and rejected in channel-integrity D5 — a bounded hold's `failed` outcome
        #: is unreachable, and a chunk cap discards owner-requested content on the
        #: healthy path. The measured cost of waiting is ~1.1 s per chunk at the
        #: worst observed send over 29 days on the deployed host.
        #:
        #: Approval prompts and their edits take the same lock (approval-reactions
        #: D2): an edit is a send, and must not land inside another message's chunks.
        self._send_lock = asyncio.Lock()

    async def messages(self) -> AsyncIterator[InboundMessage | InboundReaction]:
        """Yield inbound messages and reactions, reconnecting with backoff on errors.

        A transport failure never propagates: it is logged, the adapter backs
        off, and the receive loop restarts. The agent process stays alive.
        """
        attempt = 0
        while True:
            try:
                async for envelope in self._bridge.receive():
                    message = self._convert(envelope)
                    if message is not None:
                        yield message
            except SignalBridgeError as exc:
                # Grow the backoff per consecutive error. We deliberately do NOT
                # reset on every yielded envelope: a bridge that flaps (one
                # message, then drops, repeatedly) would otherwise hot-reconnect
                # at the base delay forever instead of backing off.
                delay = min(
                    self._backoff_base * (2**attempt), self._max_backoff
                )
                attempt += 1
                logger.warning(
                    "signal bridge receive failed (%s); backing off %.1fs", exc, delay
                )
                await self._sleep(delay)
            else:
                # The receive stream ended cleanly; reset backoff, brief pause.
                attempt = 0
                await self._sleep(self._backoff_base)

    async def send(self, text: str) -> SendOutcome:
        """Reply path: the adapter's own standing notice describes a failure."""
        return await self._send_serialized(
            lambda: self._send_message(text, failure_notice=REPLY_FAILURE_NOTICE)
        )

    async def send_proactive(
        self, text: str, *, failure_notice: str | None = None
    ) -> SendOutcome:
        """Agent-initiated path: the caller owns the owner-facing notice."""
        return await self._send_serialized(
            lambda: self._send_message(text, failure_notice=failure_notice)
        )

    async def _send_serialized(self, sequence: Callable[[], Awaitable[_T]]) -> _T:
        """Run one whole send sequence under the send mutex (design D3).

        The mutex is taken **here**, around the whole sequence, and nowhere else.
        In the wrappers instead it would leave the notice outside the critical
        section — so a waiting sender's chunks could land between a truncated
        message and the banner explaining it — and in both places it would
        deadlock. Approval prompts and their edits are sequences too
        (approval-reactions D2), and come through here like replies.
        """
        async with self._send_lock:
            return await sequence()

    async def _send_message(
        self, text: str, *, failure_notice: str | None
    ) -> SendOutcome:
        """The one send sequence both message operations share (design D3).

        Shared rather than one wrapper calling the other: re-entering the other
        operation would deadlock outright once a send mutex exists, and the notice
        must be emitted from inside the same sequence as the chunks it describes.
        Called only through :meth:`_send_serialized`, which holds the mutex.
        """
        chunks = split_message(text, self._safe_length)
        if not chunks:
            # No chunk was attempted, so nothing was delivered — and no notice:
            # a banner claiming a delivery failure here would be false.
            return SendOutcome.FAILED
        for index, chunk in enumerate(chunks):
            if not await self._send_chunk(chunk):
                # A chunk failed permanently. Do NOT silently truncate: stop
                # (later chunks would arrive out of order) and tell the owner the
                # message was cut off, if this path has a notice to give.
                remaining = len(chunks) - index
                logger.error(
                    "send failed on chunk %d/%d; %d chunk(s) undelivered",
                    index + 1,
                    len(chunks),
                    remaining,
                )
                if failure_notice is not None:
                    # A single attempt, never the retry budget: a bridge that
                    # just refused three attempts will not take a fourth, and the
                    # notice's own failure is not worth more of the caller's
                    # latency. Its outcome is deliberately discarded — the notice
                    # never alters what this send reports. It is sent while the
                    # lock is still held, which is what "within the same
                    # serialized sequence" means.
                    await self._send_chunk(failure_notice, attempts=1)
                # The condition is "not delivered, having attempted a chunk" —
                # not "partial". A wholly-failed single-chunk send is the most
                # common failure shape, and a partial-only condition would drop
                # the notice exactly there.
                return SendOutcome.PARTIAL if index else SendOutcome.FAILED
        return SendOutcome.DELIVERED

    async def _send_chunk(self, chunk: str, *, attempts: int | None = None) -> bool:
        """Send one chunk with backoff. Returns True if delivered, False if given up.

        ``attempts`` overrides the configured retry budget (the failure notice
        gets exactly one try).
        """
        delivered, _ = await self._send_with_retry(chunk, attempts=attempts)
        return delivered

    async def _send_with_retry(
        self, text: str, *, attempts: int | None = None, styled: bool = False
    ) -> tuple[bool, int | None]:
        """Send one message with backoff: ``(delivered, the bridge's timestamp)``.

        Never styled unless asked: only approval prompts are (D5). The styled flag
        is passed only when set, so the request for every other send is exactly
        what it was before styling existed.
        """
        budget = self._max_send_attempts if attempts is None else attempts
        extra = {"styled": True} if styled else {}
        attempt = 0
        while True:
            try:
                returned = await self._bridge.send(self._owner, text, **extra)
                return True, returned
            except SignalBridgeError as exc:
                attempt += 1
                if attempt >= budget:
                    logger.error("giving up sending after %d attempts: %s", attempt, exc)
                    return False, None
                delay = min(
                    self._backoff_base * (2 ** (attempt - 1)), self._max_backoff
                )
                logger.warning("signal send failed (%s); retry in %.1fs", exc, delay)
                await self._sleep(delay)

    # --- Approval prompts (approval-reactions D2, D5) ---------------------------

    async def send_prompt(self, prompt: ApprovalPrompt) -> PromptSent:
        """Send one approval prompt to the owner, never split (D2).

        The size bound is measured on the text that would actually go out, after
        escaping, in UTF-8 bytes like every chunk: escaping can more than double a
        value, so measuring the unescaped form would admit a prompt that does not
        fit. An oversized prompt sends nothing and reports ``too_large``. The send
        itself is a reply-path send: under the lock, with the reply retry budget,
        and no failure notice (the gate reports a failed prompt to the model).
        """
        styled = self._styled_prompts
        text = render_prompt(prompt, styled=styled)
        if len(text.encode("utf-8")) > self._safe_length:
            logger.warning("approval prompt does not fit one message; not sent")
            return PromptSent(SendOutcome.FAILED, None, too_large=True)
        delivered, returned = await self._send_serialized(
            lambda: self._send_with_retry(text, styled=styled)
        )
        if not delivered:
            return PromptSent(SendOutcome.FAILED, None)
        # A delivered prompt without a usable timestamp still works by keyword;
        # only reactions to it and its edit are lost (D2).
        return PromptSent(SendOutcome.DELIVERED, _mint_sent_ref(returned))

    async def edit_prompt(self, ref: str, outcome: PromptOutcome) -> bool:
        """Edit the prompt ``ref`` names into its outcome line (D2).

        One attempt, under the send lock, in the mode prompts are sent in, to the
        owner only. A reference this module did not mint makes no request. A
        bridge error is reported as ``False``, never raised.
        """
        timestamp = _parse_channel_ref(ref)
        if timestamp is None:
            return False
        styled = self._styled_prompts
        text = render_outcome(outcome, styled=styled)
        extra = {"styled": True} if styled else {}

        async def edit() -> bool:
            try:
                await self._bridge.send(
                    self._owner, text, edit_timestamp=timestamp, **extra
                )
            except SignalBridgeError as exc:
                logger.warning("approval prompt edit failed: %s", exc)
                return False
            return True

        return await self._send_serialized(edit)

    # --- Owner acknowledgement (owner-acknowledgement D1, D2, D6, D7) -----------
    #
    # Single attempts, always addressed to `self._owner`: no operation takes a
    # recipient or a sender, so there is no input a receipt could be re-aimed
    # with. None of them takes `_send_lock` (D7): an acknowledgement carries no
    # content, and queueing a receipt behind a multi-chunk reply would stall the
    # receive loop whenever Henk is talking. None bounds itself either: the
    # whole-request bound is applied by cancellation in the channel-neutral
    # caller, outside this module, and so is the log line, since only the caller
    # knows the once-per-turn scope. A bridge error is reported as `False`.

    async def acknowledge(self, channel_ref: str | None) -> bool:
        """Send the owner a read receipt for the message ``channel_ref`` names."""
        timestamp = _parse_channel_ref(channel_ref)
        if timestamp is None:
            return False
        return await self._to_owner(self._bridge.send_receipt, timestamp)

    async def start_working(self) -> bool:
        """Start, or re-assert, the owner's typing indicator."""
        return await self._to_owner(self._bridge.start_typing)

    async def stop_working(self) -> bool:
        """Stop the owner's typing indicator."""
        return await self._to_owner(self._bridge.stop_typing)

    async def _to_owner(self, operation, *args) -> bool:
        try:
            await operation(self._owner, *args)
        except SignalBridgeError:
            return False
        return True

    def _convert(
        self, envelope: dict
    ) -> Optional[InboundMessage | InboundReaction]:
        """Convert a signal-cli-rest-api envelope to a channel-neutral item.

        Returns ``None`` for envelopes that are neither user text nor a reaction
        (receipts, typing indicators, empty data messages). A reaction envelope
        carries a null ``message`` and becomes an ``InboundReaction``, never an
        ``InboundMessage`` (approval-reactions D3).
        """
        env = envelope.get("envelope", envelope)
        data = env.get("dataMessage")
        if not isinstance(data, dict):
            return None
        reaction = data.get("reaction")
        if isinstance(reaction, dict):
            return self._convert_reaction(env, data, reaction)
        body = data.get("message")
        if not body:
            return None
        # DEPLOY-VERIFY (task 1.5/5.3): which identity Signal actually reports for
        # the owner (UUID vs E.164 number) must be confirmed against a real
        # envelope and matched to config `owner.id`, or the allowlist silently
        # drops every owner message. We prefer the stable UUID; do NOT loosen the
        # match to "either field" — that widens the allowlist. Set owner.id to
        # whatever this field emits, verified by the owner-accept smoke test.
        # With owner acknowledgement on, this is diagnosable from the phone: an
        # owner message that shows delivered but never READ means the allowlist
        # dropped it silently, or the receipt failed, which is logged
        # (owner-acknowledgement task 12.5).
        sender = env.get("sourceUuid") or env.get("source") or ""
        raw_ts = data.get("timestamp") or env.get("timestamp") or 0
        timestamp = float(raw_ts) / 1000.0 if raw_ts else 0.0
        is_group = "groupInfo" in data and data.get("groupInfo") is not None
        return InboundMessage(
            sender=sender,
            text=body,
            timestamp=timestamp,
            is_group=is_group,
            channel_ref=_mint_channel_ref(raw_ts),
        )

    def _convert_reaction(
        self, env: dict, data: dict, reaction: dict
    ) -> InboundReaction:
        """A reaction envelope as a neutral reaction (approval-reactions D3).

        The sender is read exactly as for a message, so the allowlist matches
        both the same way. The target reference is minted only when
        ``targetAuthorNumber`` is Henk's own configured account: no loosening to
        "either field", for the same reason as the sender match. Which field the
        bridge fills is a deploy-verify item (task 1.2, 7.1), and an unverified
        match must default to ignoring reactions, never to accepting them.
        """
        sender = env.get("sourceUuid") or env.get("source") or ""
        target_ref = None
        if reaction.get("targetAuthorNumber") == self._account:
            target_ref = _mint_sent_ref(reaction.get("targetSentTimestamp"))
        emoji = reaction.get("emoji")
        return InboundReaction(
            sender=sender,
            emoji=emoji if isinstance(emoji, str) else "",
            target_ref=target_ref,
            removed=reaction.get("isRemove") is True,
            is_group=data.get("groupInfo") is not None,
        )


def _mint_channel_ref(raw_ts: object) -> str | None:
    """The message's sender timestamp as a decimal string, which a receipt names.

    ``None`` for a timestamp-less (absent or zero) envelope rather than ``"0"``,
    which the receipt endpoint would reject on every such message (design D2).
    Only an integer is minted: the bridge reports milliseconds as a JSON integer,
    and anything else is not something a receipt could name exactly.
    """
    if isinstance(raw_ts, bool) or not isinstance(raw_ts, int) or raw_ts <= 0:
        return None
    return str(raw_ts)


def _mint_sent_ref(raw: object) -> str | None:
    """The reference to a message Henk sent: its sent timestamp, as a decimal string.

    The ONE function that mints both a prompt's reference (from the ``/v2/send``
    response) and a reaction's target reference (from ``targetSentTimestamp``), so
    the gate's equality check cannot fail on form (approval-reactions D2). Both
    are reduced to the same integer by :func:`_sent_timestamp` first.
    """
    value = _sent_timestamp(raw)
    return None if value is None else str(value)


def _sent_timestamp(raw: object) -> int | None:
    """A sent timestamp as a positive int, whatever JSON type carried it.

    The bridge reports a send response's timestamp as a JSON string and a
    reaction's target as an integer. Only a positive integer, or a string of ASCII
    digits naming one exactly, is accepted: anything else (absent, a bool, a
    float, a sign, padding) is ``None``.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str):
        return _parse_channel_ref(raw)
    return None


def _parse_channel_ref(channel_ref: object) -> int | None:
    """The timestamp a reference minted by ``_mint_channel_ref`` names, else ``None``.

    Stricter than ``int()`` on purpose: only the exact form this module mints is
    interpreted, so a reference that ``int()`` would merely tolerate (whitespace,
    a sign, underscores, non-ASCII digits, leading zeros) is refused without a
    bridge request rather than sent as a receipt for some other message.
    """
    if not isinstance(channel_ref, str) or not channel_ref.isascii():
        return None
    if not channel_ref.isdigit():
        return None
    value = int(channel_ref)
    if value <= 0 or str(value) != channel_ref:
        return None
    return value


def _strip_delimiters(value: str) -> str:
    """Remove every delimiter sequence, repeating until none is left.

    One pass is not enough: removing ``>>>`` from ``a<<>>><b`` assembles a fresh
    ``<<<``. Each pass shortens the text, so this terminates.
    """
    open_delim, close_delim = UNSTYLED_DELIMITERS
    while open_delim in value or close_delim in value:
        value = value.replace(open_delim, "").replace(close_delim, "")
    return value


def _escape_styled(text: str) -> str:
    """``text`` made literal for the styled parser.

    Every backslash first becomes the stand-in (it cannot be escaped, and left in
    place one could escape the marker after it), then every styling character
    gets one escape in front.
    """
    text = text.replace("\\", BACKSLASH_STAND_IN)
    for char in STYLE_CHARACTERS:
        text = text.replace(char, STYLE_ESCAPE + char)
    return text


def _value(value: str, *, styled: bool) -> str:
    """One argument value, visibly bounded: monospace when styled, else delimited."""
    value = _strip_delimiters(value)
    if styled:
        return f"`{_escape_styled(value)}`"
    open_delim, close_delim = UNSTYLED_DELIMITERS
    return f"{open_delim}{value}{close_delim}"


def _text(content: str, *, styled: bool) -> str:
    """Code-fixed content (a label, a key, a tool name, a verb) in either mode."""
    return _escape_styled(content) if styled else content


def render_prompt(prompt: ApprovalPrompt, *, styled: bool) -> str:
    """The approval prompt as the owner sees it (approval-reactions D5).

    A header line (bold when styled), the label with the tool name in brackets,
    one ``key: value`` line per argument in call order, and the footer. Every
    piece of content is escaped when styled, so nothing can change the styling;
    values arrive from the gate already on one line. An action without arguments
    says so on a line of its own.
    """
    header = f"**{PROMPT_HEADER}**" if styled else PROMPT_HEADER
    lines = [
        header,
        f"{_text(prompt.label, styled=styled)} ({_text(prompt.tool_name, styled=styled)})",
    ]
    lines += [
        f"{_text(key, styled=styled)}: {_value(value, styled=styled)}"
        for key, value in prompt.fields
    ]
    if not prompt.fields:
        # Explicit rather than an empty gap between the action and the footer.
        lines.append(NO_ARGUMENTS_LINE)
    lines.append(PROMPT_FOOTER)
    return "\n".join(lines)


def render_outcome(outcome: PromptOutcome, *, styled: bool) -> str:
    """A decided prompt as one line: the verb (bold when styled), the label, then
    each argument value in call order, comma-separated."""
    verb = _text(outcome.verb, styled=styled)
    head = f"**{verb}**" if styled else verb
    parts = [_text(outcome.prompt.label, styled=styled)]
    parts += [_value(value, styled=styled) for _, value in outcome.prompt.fields]
    return f"{head}: " + ", ".join(parts)


#: Why every phase gets the configured value in full, rather than a share of it.
#:
#: httpx applies a timeout PER PHASE, so any phase left unset falls back to
#: httpx's own 5s default — an unbounded segment in a guarantee that is supposed
#: to be explicit. That was the original defect: an untimed client gave connect,
#: write and read 5s *each*, and 5s is shorter than signal-cli's send latency
#: under load, so a message the bridge had already accepted and delivered came
#: back as a failure to retry.
#:
#: A *total* request budget was specified first and cannot be built this way.
#: httpcore applies the read and write timeouts per socket OPERATION, inside
#: `while True` loops (`_receive_response_headers` → `_receive_event` →
#: `network_stream.read`), so a response arriving in many small reads is never
#: bounded by any sum. The only mechanism that bounds a whole request is
#: cancelling it in flight, which manufactures the "may already have been
#: delivered" ambiguity ``SendOutcome`` exists to describe rather than to create.
#: So the guarantee is per-phase and stated as such.
#:
#: The value therefore lands on `read` in full, which is the phase that carries
#: signal-cli's own processing time and the one the motivating bug lives in.
#: A scalar populates all four phases, `pool` included.


class SignalCliRestBridge:
    """Concrete ``SignalBridge`` over signal-cli-rest-api in json-rpc mode.

    Deployment wiring: receive is a persistent websocket (low latency, no
    polling); send is a REST POST. Both translate transport failures into
    ``SignalBridgeError`` so the adapter's backoff loop handles them. The
    websocket dependency is imported lazily so importing this module never
    requires it (tests drive the adapter through ``FakeBridge`` instead).

    Both timeouts are required rather than defaulted: ``open_timeout`` used to
    carry a constructor default the wiring never supplied, which is how the
    receive path ended up with a number nobody chose.
    """

    def __init__(
        self,
        base_url: str,
        account: str,
        *,
        send_timeout: float,
        open_timeout: float,
    ):
        self._base_url = base_url.rstrip("/")
        self._account = account
        self._send_timeout = send_timeout
        self._open_timeout = open_timeout

    def _build_client(self):
        """The ONLY place an HTTP client is constructed, so no phase goes unbounded.

        Deliberately not ``asyncio.wait_for`` around the POST — see the note
        above on why a request total is not specified.
        """
        import httpx  # lazy

        return httpx.AsyncClient(timeout=httpx.Timeout(self._send_timeout))

    async def receive(self) -> AsyncIterator[dict]:  # pragma: no cover - deploy path
        import websockets  # lazy: only needed at runtime

        ws_url = self._base_url.replace("http", "ws", 1) + (
            f"/v1/receive/{self._account}"
        )
        try:
            async with websockets.connect(
                ws_url, open_timeout=self._open_timeout
            ) as socket:
                async for raw in socket:
                    yield json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - normalise every failure
            raise SignalBridgeError(f"receive websocket failed: {exc}") from exc

    async def send(
        self,
        recipient: str,
        text: str,
        *,
        styled: bool = False,
        edit_timestamp: int | None = None,
    ) -> int | None:
        """POST one message; return the response's sent timestamp as an int.

        ``text_mode`` and ``edit_timestamp`` are sent only when used, so every
        other send's request is exactly what it was. An error status is a
        ``SignalBridgeError`` (styled or not: there is no automatic fallback, D5).
        A response without a usable timestamp returns ``None`` and does NOT raise:
        the message was accepted, and raising would make the adapter retry and
        deliver it twice.
        """
        payload: dict = {
            "message": text,
            "number": self._account,
            "recipients": [recipient],
        }
        if styled:
            payload["text_mode"] = "styled"
        if edit_timestamp is not None:
            payload["edit_timestamp"] = int(edit_timestamp)
        try:
            async with self._build_client() as client:
                resp = await client.post(f"{self._base_url}/v2/send", json=payload)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise SignalBridgeError(f"send failed: {exc}") from exc
        return _response_timestamp(resp)

    # Acknowledgement requests (owner-acknowledgement design D6/D7). Each is one
    # attempt through `_build_client`, with no retry and no bound of its own: the
    # whole-request bound is applied by cancellation in the channel-neutral
    # caller, which is safe here because these carry no content and are
    # idempotent. They never touch the adapter's send lock.

    async def send_receipt(self, recipient: str, timestamp: int) -> None:
        payload = {
            "recipient": recipient,
            "receipt_type": "read",  # never "viewed": that has media semantics
            "timestamp": timestamp,
        }
        await self._acknowledgement(
            "POST", f"/v1/receipts/{self._account}", payload, "receipt"
        )

    async def start_typing(self, recipient: str) -> None:
        await self._acknowledgement(
            "PUT",
            f"/v1/typing-indicator/{self._account}",
            {"recipient": recipient},
            "typing start",
        )

    async def stop_typing(self, recipient: str) -> None:
        # `client.request`, not `client.delete()`: httpx's delete() takes no body,
        # and the bridge reads the recipient from it.
        await self._acknowledgement(
            "DELETE",
            f"/v1/typing-indicator/{self._account}",
            {"recipient": recipient},
            "typing stop",
        )

    async def _acknowledgement(
        self, method: str, path: str, payload: dict, what: str
    ) -> None:
        try:
            async with self._build_client() as client:
                resp = await client.request(
                    method, f"{self._base_url}{path}", json=payload
                )
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise SignalBridgeError(f"{what} failed: {exc}") from exc


def _response_timestamp(response) -> int | None:
    """The ``timestamp`` of a ``/v2/send`` response as an int, else ``None``.

    Bridge 0.101 answers ``[{"timestamp": "<decimal string>"}]``, a list with one
    object per send (seen live on rp5 2026-10-31); a bare object and an integer
    timestamp are accepted too. Henk sends to one recipient, so a list of any
    other length is ``None``, as is anything else (no body, not JSON, no key,
    not a number).
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, list):
        if len(body) != 1:
            return None
        body = body[0]
    if not isinstance(body, dict):
        return None
    return _sent_timestamp(body.get("timestamp"))
