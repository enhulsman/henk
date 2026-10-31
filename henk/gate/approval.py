"""The authorization gate: two axes, fail closed, receipt always.

Every mutating tool invocation is decided here against the North Star's
permission model (design D4/D10):

**Axis 1 — the named action's authorization tier.** ``standing`` executes without
prompting; ``per-instance`` suspends the invocation, sends the owner a
resolve-then-confirm prompt, and resumes only on an explicit approval: a typed
keyword, or the owner's 👍 on that very prompt (approval-reactions D4).
The third tier, ``never``, is the absence of registration and is enforced
upstream by the closed-toolset boundary. Configuration can *demote* standing to
per-instance globally (the kill-switch) and can never widen anything.

**Axis 2 — turn scope, enforced with session taint.** A tool declares the turn
types it may run in (owner-only by default). The core frames every turn with a
:class:`TurnContext`; an invocation whose scope excludes event turns is denied —
silently, fail closed — during an event turn *and* during any turn of a session
an event turn has touched. That closes both halves of the injection path: the
event turn itself and the owner follow-up that triage mandates continues the same
session. A permitted tool may also raise taint during a turn, before it runs.

Every decision, permitted or not, is reported to the decision recorder so the
audit log carries a receipt at decision time: an agent that acts without asking
must be *more* accountable, not less. Reporting is best-effort by construction —
a broken audit path must never convert a permitted action into a denied one, nor
the reverse.

Read-only and notify-only invocations bypass prompts and receipts, but can raise
taint when the registered tool declares itself a source.

The gate never renders a prompt (approval-reactions D1). It hands the channel a
structured :class:`ApprovalPrompt` (the tool's code-fixed label and name, and each
argument collapsed to one line and bounded) and the adapter renders that in its own
syntax. Once the approval is decided, the gate asks the adapter to edit the prompt
into a one-line record of the outcome (D2).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol

from henk.channel.base import (
    ApprovalPrompt,
    InboundReaction,
    PromptOutcome,
    PromptSent,
    SendOutcome,
)
from henk.tools.base import AuthorizationTier, Tool, ToolClass, ToolResult, TurnType

logger = logging.getLogger("henk.gate")

APPROVE_KEYWORDS = frozenset({"yes", "approve"})
DENY_KEYWORDS = frozenset({"no", "deny"})

DEFAULT_TIMEOUT_SECONDS = 300.0

#: Bound on an argument value handed to the adapter. A prompt is an owner
#: interruption; it must stay readable on a phone and must not become a channel
#: for a wall of text.
ARGUMENT_MAX_CHARS = 200

#: The reactions that answer a pending prompt, after normalisation (D4).
APPROVE_REACTION = "\U0001F44D"  # thumbs up
DENY_REACTION = "\U0001F44E"  # thumbs down
#: Dropped before matching, so every skin tone and the emoji-presentation form of
#: a thumb count: the Fitzpatrick modifiers U+1F3FB..U+1F3FF and variation
#: selector 16 (U+FE0F). Nothing else is dropped, so a doubled thumb or a thumb
#: next to another emoji never matches.
_REACTION_MODIFIERS = frozenset(
    [chr(code) for code in range(0x1F3FB, 0x1F400)] + ["\uFE0F"]
)


class ApprovalOutcome(str, Enum):
    """The v3 receipt vocabulary (design D5). Every value is a real, distinct event."""

    #: Standing tier: authorized without asking.
    AUTHORIZED = "authorized"
    #: Per-instance: the owner said yes.
    APPROVED = "approved"
    #: Per-instance: the owner said no.
    DENIED = "denied"
    #: Per-instance: an unrelated message arrived — fail closed, NOT an owner "no".
    CANCELLED = "cancelled"
    #: Per-instance: no reply inside the window.
    TIMEOUT = "timeout"
    #: Per-instance during a cap-suppressed event turn: the mutation is suppressed,
    #: not the prompt (design D6).
    SUPPRESSED = "suppressed"
    #: Turn-scope / session-taint denial (design D10).
    OUT_OF_SCOPE = "out-of-scope"
    #: A second per-instance request while one was already pending.
    REJECTED_BUSY = "rejected-busy"


#: The outcomes under which the invocation may actually run. Everything else is a
#: non-execution, and the tool is never called.
EXECUTING_OUTCOMES = frozenset({ApprovalOutcome.AUTHORIZED, ApprovalOutcome.APPROVED})


class Classification(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    UNRELATED = "unrelated"
    #: A reaction that answers nothing: it leaves the pending approval untouched
    #: and never becomes a turn (approval-reactions D4). Unlike a typed unrelated
    #: message it does not cancel, by owner decision.
    IGNORED = "ignored"


class _Sender(Protocol):
    """The slice of the channel adapter the gate uses."""

    async def send_prompt(self, prompt: ApprovalPrompt) -> PromptSent: ...

    async def edit_prompt(self, ref: str, outcome: PromptOutcome) -> bool: ...


class DecisionRecorder(Protocol):
    """Sink for authorization receipts. Implemented by the audit layer (D5)."""

    def record(
        self,
        *,
        tool: str,
        tier: str | None,
        outcome: str,
        reference: str | None = None,
        turn_type: str | None = None,
        initiated_by: str = "model",
        detail: str | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class TurnContext:
    """What the gate needs to know about the turn an invocation arrived in.

    Supplied by the agent core around every agent turn and cleared on the way out
    (including error paths), so gate state never outlives its turn. Incident
    taint has no source; tool-raised taint names the first tool that raised it.
    """

    turn_type: TurnType = TurnType.OWNER
    #: False for a cap-suppressed incident: nothing may reach the channel.
    announceable: bool = True
    #: True once the session has processed an event turn or a tool raised taint.
    tainted: bool = False
    taint_source: str | None = None


#: Used when no turn framed the invocation. An unframed call is structurally an
#: owner-initiated one (the core frames every event turn explicitly, and event
#: turns are the only untrusted path), so this is the honest default rather than a
#: hole: it grants owner scope, never event scope, and never suppresses a prompt.
_UNFRAMED_CONTEXT = TurnContext()


@dataclass(frozen=True)
class GateDecision:
    """One authorization decision: the outcome plus the honest model-facing reason."""

    outcome: ApprovalOutcome
    reference: str = ""
    reason: str = ""

    @property
    def permits(self) -> bool:
        return self.outcome in EXECUTING_OUTCOMES


@dataclass
class _Pending:
    tool_name: str
    arguments: dict[str, Any]
    reference: str
    future: "asyncio.Future[ApprovalOutcome]"
    #: The adapter's opaque reference to the sent prompt, stored when
    #: ``send_prompt`` returns. Until then (and for a prompt the channel gave no
    #: reference) every reaction is ignored (D4 race): compared for equality only.
    prompt_ref: str | None = None
    #: "reaction" or "keyword": set in the same step as ``future.set_result`` and
    #: only while the future is not done, so the first answer decides the outcome
    #: and the receipt detail alike (D4, D7).
    answered_by: str | None = None


class ApprovalGate:
    """Manages at most one pending approval per conversation."""

    def __init__(
        self,
        channel: _Sender,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        recorder: DecisionRecorder | None = None,
        demote_standing: bool = False,
    ) -> None:
        self._channel = channel
        self._timeout = timeout_seconds
        self._recorder = recorder
        self._demote_standing = demote_standing
        self._pending: _Pending | None = None
        self._refs = itertools.count(1)
        self._turn_context: TurnContext | None = None

    # --- per-turn context -------------------------------------------------

    def enter_turn(self, context: TurnContext) -> None:
        """Frame the turn an invocation will arrive in (called by the core)."""
        self._turn_context = context

    def exit_turn(self) -> TurnContext | None:
        """Return and clear the turn's context on every exit path."""
        context = self._turn_context
        self._turn_context = None
        return context

    @property
    def turn_context(self) -> TurnContext | None:
        return self._turn_context

    def has_pending(self) -> bool:
        return self._pending is not None

    def effective_tier(self, tool: Tool) -> AuthorizationTier | None:
        """The tier actually enforced: the tool's, narrowed by the kill-switch.

        Only ever narrows. There is deliberately no configuration path that turns
        a per-instance action into a standing one — authorization widens through
        code review alone (design D4).
        """
        tier = tool.authorization
        if self._demote_standing and tier is AuthorizationTier.STANDING:
            return AuthorizationTier.PER_INSTANCE
        return tier

    @staticmethod
    def classify(text: str) -> Classification:
        token = text.strip().lower()
        if token in APPROVE_KEYWORDS:
            return Classification.APPROVE
        if token in DENY_KEYWORDS:
            return Classification.DENY
        return Classification.UNRELATED

    # --- the decision -----------------------------------------------------

    async def authorize(self, tool: Tool, arguments: dict[str, Any]) -> GateDecision:
        """Decide one invocation of ``tool``. Never raises on a busy or scoped-out
        gate — every ambiguous case resolves as a recorded non-execution."""
        framed_at_start = self._turn_context is not None
        if tool.tool_class in (ToolClass.READ_ONLY, ToolClass.NOTIFY_ONLY):
            # No prompt and no receipt: these bypass the gate by classification,
            # and their execution evidence lives in the session record's tool_calls.
            decision = GateDecision(ApprovalOutcome.APPROVED)
            self._raise_taint(tool, framed_at_start)
            return decision

        context = self._turn_context or _UNFRAMED_CONTEXT
        reference = f"appr-{next(self._refs)}"
        declared_tier = tool.authorization

        scope_reason = self._scope_denial_reason(tool, context)
        if scope_reason is not None:
            detail = None
            if (context.turn_type is TurnType.OWNER and context.taint_source is not None
                    and TurnType.EVENT not in tool.turn_scope):
                detail = f"taint raised by {context.taint_source}"
            return self._resolve(
                tool, declared_tier, ApprovalOutcome.OUT_OF_SCOPE, reference,
                context, scope_reason, detail=detail,
            )

        if self.effective_tier(tool) is AuthorizationTier.STANDING:
            decision = self._resolve(
                tool, declared_tier, ApprovalOutcome.AUTHORIZED, reference, context, ""
            )
            self._raise_taint(tool, framed_at_start)
            return decision

        # Per-instance from here on (including a demoted standing action).
        if context.turn_type is TurnType.EVENT and not context.announceable:
            # D6: the owner must hear nothing during a cap-suppressed incident, and
            # a context-free prompt is exactly the interruption the cadence
            # contract forbids. Suppress the mutation, not the prompt.
            return self._resolve(
                tool, declared_tier, ApprovalOutcome.SUPPRESSED, reference, context,
                "this incident is suppressed under the alert cap, so no approval "
                "could be requested; the action was not executed",
            )

        undeclared = _undeclared_key(tool, arguments)
        if undeclared is not None:
            # D8: a key reaches the prompt as its own text, so only the tool's
            # declared parameter names may. Refused before anything is sent, with
            # its own reason: the unrelated-message one would have the model tell
            # the owner about a message they never sent.
            return self._resolve(
                tool, declared_tier, ApprovalOutcome.CANCELLED, reference, context,
                f'"{_bound_argument(undeclared)}" is not a parameter of {tool.name}; '
                "nothing was asked and the action was not executed",
                detail="undeclared argument",
            )

        if self._pending is not None:
            return self._resolve(
                tool, declared_tier, ApprovalOutcome.REJECTED_BUSY, reference,
                context,
                "another approval is pending in this conversation; the action was "
                "not executed — resolve that one first and try again",
            )

        outcome, reason, detail, edit = await self._prompt_and_wait(
            tool, arguments, reference
        )
        # The receipt first, at decision time: a turn cancelled while the edit is
        # in flight must not lose it. Then the edit, still before the decision
        # returns, so before the tool runs or the non-execution result flows back.
        decision = self._resolve(
            tool, declared_tier, outcome, reference, context, reason, detail=detail
        )
        if edit is not None:
            await self._record_outcome(*edit, reference)
        if decision.permits:
            self._raise_taint(tool, framed_at_start)
        return decision

    def _raise_taint(self, tool: Tool, framed_at_start: bool) -> None:
        """Taint the context held now, before a permitted source can run."""
        if not tool.raises_taint:
            return
        context = self._turn_context
        if context is None:
            if framed_at_start:
                return  # the authorizing turn ended while approval was pending
            context = _UNFRAMED_CONTEXT
        if not context.tainted:
            self._turn_context = replace(
                context, tainted=True, taint_source=tool.name
            )

    @staticmethod
    def _scope_denial_reason(tool: Tool, context: TurnContext) -> str | None:
        """Why this turn may not run this tool, or None if it may (design D10)."""
        scope = tuple(tool.turn_scope or ())
        if TurnType.EVENT not in scope:
            if context.turn_type is TurnType.EVENT:
                return (
                    "this is an event-triage turn driven by untrusted sensor data, "
                    "so writes are out of scope here; nothing was stored or "
                    "scheduled. Tell the owner what should be kept and let them use "
                    "/remember, /capture or /remind, or /new for a clean session."
                )
            if context.tainted:
                if context.taint_source is not None:
                    return (
                        f"a tool called earlier in this session ({context.taint_source}) "
                        "returned content from outside the owner's control, so this "
                        "session is now tainted for its lifetime and writes are out of "
                        "scope in it; nothing was stored or scheduled. The owner can "
                        "use /remember, /capture or /remind (which bypass this session "
                        "entirely), or /new to start a clean session where writes work "
                        "again."
                    )
                return (
                    "this session has already handled an incident, so it stays "
                    "tainted for its lifetime and writes are out of scope in it; "
                    "nothing was stored or scheduled. The owner can use /remember, "
                    "/capture or /remind (which bypass this session entirely), or "
                    "/new to start a clean session where writes work again."
                )
        if context.turn_type not in scope:
            return (
                f"{tool.name} is not scoped for {context.turn_type.value} turns; "
                "the action was not executed"
            )
        return None

    async def _prompt_and_wait(
        self, tool: Tool, arguments: dict[str, Any], reference: str
    ) -> tuple[
        ApprovalOutcome, str, str | None, tuple[str, PromptOutcome] | None
    ]:
        """Prompt and wait for the owner.

        Returns the outcome with its model-facing reason and receipt detail, and
        the outcome edit still to make (the prompt reference and the outcome line),
        or ``None`` when there is nothing to edit. The pending slot is released
        here, so BEFORE the caller writes the receipt and awaits the edit (D2): a
        message or reaction arriving during the edit is routed exactly as one
        arriving after the decision.
        """
        prompt = self.build_prompt(tool, arguments)
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[ApprovalOutcome]" = loop.create_future()
        pending = _Pending(tool.name, dict(arguments), reference, future)
        self._pending = pending
        logger.info("approval pending ref=%s tool=%s", reference, tool.name)
        sent: PromptSent | None = None
        try:
            try:
                sent = await self._channel.send_prompt(prompt)
            except Exception:
                # The contract says send_prompt never raises; a channel that does
                # must still not leave the invocation in limbo: fail closed.
                logger.error("approval ref=%s could not be requested", reference,
                             exc_info=True)
            if sent is None or sent.outcome is not SendOutcome.DELIVERED:
                outcome = ApprovalOutcome.CANCELLED
            else:
                pending.prompt_ref = sent.ref
                if sent.ref is None:
                    logger.warning(
                        "approval ref=%s: the prompt has no reference from the "
                        "channel; reactions to it are ignored and it will not be "
                        "edited", reference,
                    )
                try:
                    outcome = await asyncio.wait_for(future, self._timeout)
                except asyncio.TimeoutError:
                    logger.info("approval ref=%s timed out", reference)
                    outcome = ApprovalOutcome.TIMEOUT
        finally:
            self._pending = None

        if sent is None or sent.outcome is not SendOutcome.DELIVERED:
            # Nothing the owner saw, so nothing to edit.
            if sent is not None and sent.too_large:
                logger.warning(
                    "approval ref=%s: the prompt does not fit one message", reference
                )
                return outcome, _PROMPT_TOO_LARGE_REASON, "prompt too large", None
            if sent is not None:
                logger.error(
                    "approval ref=%s could not be requested (%s)",
                    reference, sent.outcome.value,
                )
            return outcome, _PROMPT_NOT_SENT_REASON, None, None

        detail = None
        if outcome in (ApprovalOutcome.APPROVED, ApprovalOutcome.DENIED):
            if pending.answered_by is not None:
                detail = f"answered by {pending.answered_by}"
        edit = None
        if sent.ref is not None:
            edit = (sent.ref, PromptOutcome(_OUTCOME_VERBS[outcome], prompt))
        return outcome, _OUTCOME_REASONS.get(outcome, ""), detail, edit

    async def _record_outcome(
        self, prompt_ref: str, outcome: PromptOutcome, reference: str
    ) -> None:
        """Edit the decided prompt into its outcome line: one best-effort attempt.

        Its failure is logged and changes nothing: not the decision, not the
        execution, not the receipt (D2). The prompt reference itself is never
        logged; the approval reference names the prompt well enough.
        """
        try:
            accepted = await self._channel.edit_prompt(prompt_ref, outcome)
        except Exception:
            logger.warning(
                "approval ref=%s: the prompt edit to %r failed",
                reference, outcome.verb, exc_info=True,
            )
            return
        if not accepted:
            logger.warning(
                "approval ref=%s: the prompt edit to %r was not accepted",
                reference, outcome.verb,
            )

    def _resolve(
        self,
        tool: Tool,
        tier: AuthorizationTier | None,
        outcome: ApprovalOutcome,
        reference: str,
        context: TurnContext,
        reason: str,
        *,
        detail: str | None = None,
    ) -> GateDecision:
        fields = dict(
            tool=tool.name,
            tier=tier.value if tier is not None else None,
            outcome=outcome.value,
            reference=reference,
            turn_type=context.turn_type.value,
            # Stated rather than defaulted: everything the gate decides is a
            # model-initiated call. Owner commands report their own receipts.
            initiated_by="model",
        )
        if detail is not None:
            fields["detail"] = detail
        self.report(**fields)
        return GateDecision(outcome=outcome, reference=reference, reason=reason)

    def report(self, **fields: Any) -> None:
        """Hand one receipt to the recorder. Never raises into the turn."""
        if self._recorder is None:
            return
        try:
            self._recorder.record(**fields)
        except Exception:  # pragma: no cover - defensive; audit is non-blocking
            logger.error("could not record an authorization receipt", exc_info=True)

    # --- routing the owner's reply ----------------------------------------

    def deliver(self, text: str) -> tuple[Classification, bool]:
        """Route an inbound message that arrived while an approval is pending.

        Returns ``(classification, requeue)``. ``requeue`` is True when the
        message was unrelated: the pending action fails closed (``cancelled``, a
        distinct event from an owner "no") and the message must then be processed
        as a normal new turn — it is not swallowed.
        """
        if self._pending is None:
            # Race: the approval resolved (e.g. timed out) between the caller's
            # has_pending() check and here. Per spec, a late reply is just a
            # normal message — treat it as unrelated and re-queue, never crash.
            return Classification.UNRELATED, True

        classification = self.classify(text)
        pending = self._pending
        if classification is Classification.APPROVE:
            self._answer(pending, ApprovalOutcome.APPROVED, "keyword")
            return classification, False
        if classification is Classification.DENY:
            self._answer(pending, ApprovalOutcome.DENIED, "keyword")
            return classification, False
        # Unrelated: fail closed, then let the message be handled as a new turn.
        self._answer(pending, ApprovalOutcome.CANCELLED, None)
        return classification, True

    def deliver_reaction(self, reaction: InboundReaction) -> Classification:
        """Route an owner reaction that passed the allowlist (approval-reactions D4).

        A reaction answers only the prompt that is pending right now, and only
        when it is the owner's 👍 or 👎 on that very message. Every other case is
        ``IGNORED``, in this order: nothing pending, or a pending prompt the gate
        holds no reference for (not yet returned by the channel, or none given);
        a removal, which never resolves or reverses anything; a reaction on any
        other message; a prompt already decided but whose slot is not yet
        released (a second 👍 answers nothing); any other emoji. ``IGNORED`` never touches the pending
        approval, and a reaction is never requeued as a turn, so unlike
        :meth:`deliver` there is no requeue flag to return.
        """
        pending = self._pending
        if pending is None or pending.prompt_ref is None:
            return Classification.IGNORED
        if reaction.removed:
            return Classification.IGNORED
        # Equality only: the reference is the adapter's, and neutral code never
        # parses it. `prompt_ref` is not None here, so a reaction carrying no
        # target can never match a prompt carrying none.
        if reaction.target_ref != pending.prompt_ref:
            return Classification.IGNORED
        if pending.future.done():
            return Classification.IGNORED
        classification = classify_reaction(reaction.emoji)
        if classification is Classification.APPROVE:
            self._answer(pending, ApprovalOutcome.APPROVED, "reaction")
        elif classification is Classification.DENY:
            self._answer(pending, ApprovalOutcome.DENIED, "reaction")
        return classification

    @staticmethod
    def _answer(
        pending: _Pending, outcome: ApprovalOutcome, answered_by: str | None
    ) -> None:
        """Resolve ``pending`` unless an earlier answer already did: first wins.

        How it was answered is recorded in the same step as the result and under
        the same condition, so a later answer can change neither (D4, D7).
        """
        if pending.future.done():
            return
        pending.answered_by = answered_by
        pending.future.set_result(outcome)

    # --- the structured prompt (resolve-then-confirm, approval-reactions D1) --

    @staticmethod
    def build_prompt(tool: Tool, arguments: dict[str, Any]) -> ApprovalPrompt:
        """The RESOLVED ACTION, handed to the adapter to render in its own syntax.

        The label and tool name come from the tool's code. Each argument keeps
        its key (already checked against the declared parameters, D8) and call
        order, and its value is collapsed to one line and bounded by
        :func:`_bound_argument`, so a crafted value cannot forge prompt lines in
        any adapter's rendering. Keeping that value visibly bounded (markup,
        escaping, delimiters) is the adapter's job. Authorization is never derived
        from content: matching runs on the owner's own reply or reaction, never
        on anything here.
        """
        return ApprovalPrompt(
            label=tool.label,
            tool_name=tool.name,
            fields=tuple(
                (str(key), _bound_argument(value)) for key, value in arguments.items()
            ),
        )


def classify_reaction(emoji: str) -> Classification:
    """👍 approves and 👎 denies in any skin tone; anything else is ``IGNORED``."""
    bare = "".join(char for char in emoji if char not in _REACTION_MODIFIERS)
    if bare == APPROVE_REACTION:
        return Classification.APPROVE
    if bare == DENY_REACTION:
        return Classification.DENY
    return Classification.IGNORED


def _bound_argument(value: Any) -> str:
    """One argument value (or an echoed key) as one bounded line of text.

    Collapses ALL whitespace, newlines included, to single spaces: a multi-line
    value is the one shape that could otherwise impersonate prompt lines in any
    rendering. A non-string value is shown as its ``repr``.
    """
    text = value if isinstance(value, str) else repr(value)
    flat = " ".join(str(text).split())
    if len(flat) > ARGUMENT_MAX_CHARS:
        omitted = len(flat) - ARGUMENT_MAX_CHARS
        flat = f"{flat[:ARGUMENT_MAX_CHARS]}… (truncated, {omitted} more chars)"
    return flat


def _undeclared_key(tool: Tool, arguments: dict[str, Any]) -> Any | None:
    """The first argument key that is not one of the tool's declared parameters.

    Exact match against ``parameters["properties"]``: a case variant is a
    different key. A tool that declares no properties accepts no key at all.
    """
    properties = (tool.parameters or {}).get("properties")
    declared = set(properties) if isinstance(properties, dict) else set()
    for key in arguments:
        if key not in declared:
            return key
    return None


#: The outcome line's opening word for each decided prompt: the owner's words.
_OUTCOME_VERBS = {
    ApprovalOutcome.APPROVED: "Approved",
    ApprovalOutcome.DENIED: "Denied",
    ApprovalOutcome.CANCELLED: "Cancelled",
    ApprovalOutcome.TIMEOUT: "Timed out",
}

#: The cancels the gate causes itself carry their own reason, never the
#: unrelated-message one below, which would have the model tell the owner about
#: a message they never sent (D8).
_PROMPT_TOO_LARGE_REASON = (
    "the action is too large to show in one approval prompt, so the owner was "
    "not asked; the action was not executed"
)
_PROMPT_NOT_SENT_REASON = (
    "delivery of the approval prompt could not be confirmed; the action was not "
    "executed"
)

#: Model-facing text for each non-executing outcome of the prompt flow. Honest
#: about what happened: a cancellation is not a denial, and neither executed.
_OUTCOME_REASONS = {
    ApprovalOutcome.DENIED: "denied by owner; the action was not executed",
    ApprovalOutcome.CANCELLED: (
        "the approval was cancelled because an unrelated message arrived; the "
        "action was not executed"
    ),
    ApprovalOutcome.TIMEOUT: "approval timed out; the action was not executed",
}


async def gated_invoke(
    gate: ApprovalGate, tool: Tool, arguments: dict[str, Any]
) -> ToolResult:
    """Invoke ``tool`` through ``gate``, executing at most once and only if allowed.

    This convenience wrapper is for callers outside the SDK path; the SDK calls
    ``authorize`` directly. Taint is raised there, before a permitted tool runs.
    A non-executing decision becomes a failure result carrying the gate's reason.
    """
    decision = await gate.authorize(tool, arguments)
    if decision.permits:
        return await tool.run(**arguments)
    return ToolResult.failure(
        decision.reason or f"the action was not executed ({decision.outcome.value})"
    )
