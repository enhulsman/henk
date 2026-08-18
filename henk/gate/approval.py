"""The authorization gate: two axes, fail closed, receipt always.

Every mutating tool invocation is decided here against the North Star's
permission model (design D4/D10):

**Axis 1 — the named action's authorization tier.** ``standing`` executes without
prompting; ``per-instance`` suspends the invocation, sends the owner a
resolve-then-confirm prompt, and resumes only on an explicit approval keyword.
The third tier, ``never``, is the absence of registration and is enforced
upstream by the closed-toolset boundary. Configuration can *demote* standing to
per-instance globally (the kill-switch) and can never widen anything.

**Axis 2 — turn scope, enforced with session taint.** A tool declares the turn
types it may run in (owner-only by default). The core frames every turn with a
:class:`TurnContext`; an invocation whose scope excludes event turns is denied —
silently, fail closed — during an event turn *and* during any turn of a session
an event turn has touched. That closes both halves of the injection path: the
event turn itself and the owner follow-up that triage mandates continues the same
session.

Every decision, permitted or not, is reported to the decision recorder so the
audit log carries a receipt at decision time: an agent that acts without asking
must be *more* accountable, not less. Reporting is best-effort by construction —
a broken audit path must never convert a permitted action into a denied one, nor
the reverse.

Read-only and notify-only invocations bypass all of this (no prompt, no receipt).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from henk.tools.base import AuthorizationTier, Tool, ToolClass, ToolResult, TurnType

logger = logging.getLogger("henk.gate")

APPROVE_KEYWORDS = frozenset({"yes", "approve"})
DENY_KEYWORDS = frozenset({"no", "deny"})

DEFAULT_TIMEOUT_SECONDS = 300.0

#: Explicit delimiters around every model-chosen argument value in an approval
#: prompt. The prompt states a RESOLVED ACTION, so no argument may be interpolated
#: raw: an undelimited value could pose as prompt text (design D7).
ARGUMENT_DELIMITERS = ("<<<", ">>>")

#: Bound on a rendered argument value. A prompt is an owner interruption; it must
#: stay readable on a phone and must not become a channel for a wall of text.
ARGUMENT_MAX_CHARS = 200


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


class _Sender(Protocol):
    async def send(self, text: str) -> None: ...


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
    (including error paths), so gate state never outlives its turn.
    """

    turn_type: TurnType = TurnType.OWNER
    #: False for a cap-suppressed incident: nothing may reach the channel.
    announceable: bool = True
    #: True once the session has processed an event turn — for its whole life.
    tainted: bool = False


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

    def exit_turn(self) -> None:
        """Drop the turn's context. Must run on every exit path, errors included."""
        self._turn_context = None

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
        if tool.tool_class in (ToolClass.READ_ONLY, ToolClass.NOTIFY_ONLY):
            # No prompt and no receipt: these bypass the gate by classification,
            # and their execution evidence lives in the session record's tool_calls.
            return GateDecision(ApprovalOutcome.APPROVED)

        context = self._turn_context or _UNFRAMED_CONTEXT
        reference = f"appr-{next(self._refs)}"
        declared_tier = tool.authorization

        scope_reason = self._scope_denial_reason(tool, context)
        if scope_reason is not None:
            return self._resolve(
                tool, declared_tier, ApprovalOutcome.OUT_OF_SCOPE, reference,
                context, scope_reason,
            )

        if self.effective_tier(tool) is AuthorizationTier.STANDING:
            return self._resolve(
                tool, declared_tier, ApprovalOutcome.AUTHORIZED, reference, context, ""
            )

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

        if self._pending is not None:
            return self._resolve(
                tool, declared_tier, ApprovalOutcome.REJECTED_BUSY, reference,
                context,
                "another approval is pending in this conversation; the action was "
                "not executed — resolve that one first and try again",
            )

        outcome = await self._prompt_and_wait(tool, arguments, reference)
        return self._resolve(
            tool, declared_tier, outcome, reference, context,
            _OUTCOME_REASONS.get(outcome, ""),
        )

    @staticmethod
    def _scope_denial_reason(tool: Tool, context: TurnContext) -> str | None:
        """Why this turn may not run this tool, or None if it may (design D10)."""
        scope = tuple(tool.turn_scope or ())
        if TurnType.EVENT not in scope:
            if context.turn_type is TurnType.EVENT:
                return (
                    "this is an event-triage turn driven by untrusted sensor data, "
                    "so writes are out of scope here; nothing was stored. Tell the "
                    "owner what should be remembered and let them use /remember or "
                    "/capture, or /new for a clean session."
                )
            if context.tainted:
                return (
                    "this session has already handled an incident, so it stays "
                    "tainted for its lifetime and writes are out of scope in it; "
                    "nothing was stored. The owner can use /remember or /capture "
                    "(which bypass this session entirely), or /new to start a "
                    "clean session where writes work again."
                )
        if context.turn_type not in scope:
            return (
                f"{tool.name} is not scoped for {context.turn_type.value} turns; "
                "the action was not executed"
            )
        return None

    async def _prompt_and_wait(
        self, tool: Tool, arguments: dict[str, Any], reference: str
    ) -> ApprovalOutcome:
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[ApprovalOutcome]" = loop.create_future()
        self._pending = _Pending(tool.name, dict(arguments), reference, future)
        logger.info("approval pending ref=%s tool=%s", reference, tool.name)
        try:
            await self._channel.send(self.format_prompt(tool.name, arguments))
            return await asyncio.wait_for(future, self._timeout)
        except asyncio.TimeoutError:
            logger.info("approval ref=%s timed out", reference)
            return ApprovalOutcome.TIMEOUT
        except Exception:
            # A channel failure must not leave the invocation in limbo: fail closed.
            logger.error("approval ref=%s could not be requested", reference,
                         exc_info=True)
            return ApprovalOutcome.CANCELLED
        finally:
            self._pending = None

    def _resolve(
        self,
        tool: Tool,
        tier: AuthorizationTier | None,
        outcome: ApprovalOutcome,
        reference: str,
        context: TurnContext,
        reason: str,
    ) -> GateDecision:
        self.report(
            tool=tool.name,
            tier=tier.value if tier is not None else None,
            outcome=outcome.value,
            reference=reference,
            turn_type=context.turn_type.value,
            # Stated rather than defaulted: everything the gate decides is a
            # model-initiated call. Owner commands report their own receipts.
            initiated_by="model",
        )
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
        future = self._pending.future
        if classification is Classification.APPROVE:
            if not future.done():
                future.set_result(ApprovalOutcome.APPROVED)
            return classification, False
        if classification is Classification.DENY:
            if not future.done():
                future.set_result(ApprovalOutcome.DENIED)
            return classification, False
        # Unrelated: fail closed, then let the message be handled as a new turn.
        if not future.done():
            future.set_result(ApprovalOutcome.CANCELLED)
        return classification, True

    # --- prompt rendering (resolve-then-confirm, design D7) ---------------

    @staticmethod
    def format_prompt(tool_name: str, arguments: dict[str, Any]) -> str:
        """Render the RESOLVED ACTION for confirmation.

        The tool name and each argument value get their own line, every value
        inside :data:`ARGUMENT_DELIMITERS`, whitespace-collapsed so a crafted
        multi-line value cannot forge extra prompt structure, delimiter sequences
        stripped so it cannot close its own quoting, and truncated to
        :data:`ARGUMENT_MAX_CHARS`. Authorization is never derived from content:
        the keyword match runs on the owner's own reply, not on anything here.
        """
        lines = [f"  tool: `{tool_name}`"]
        for key, value in arguments.items():
            lines.append(f"  {key}: {_render_argument(value)}")
        body = "\n".join(lines) if arguments else f"{lines[0]}\n  (no arguments)"
        return (
            "Approval needed. Resolved action:\n"
            f"{body}\n\n"
            "Reply `yes` to approve or `no` to deny. Anything else cancels it."
        )


def _render_argument(value: Any) -> str:
    open_delim, close_delim = ARGUMENT_DELIMITERS
    text = value if isinstance(value, str) else repr(value)
    # Collapse ALL whitespace (newlines included) to single spaces: a multi-line
    # value is the one shape that could otherwise impersonate prompt lines.
    flat = " ".join(str(text).split())
    flat = flat.replace(open_delim, "").replace(close_delim, "")
    if len(flat) > ARGUMENT_MAX_CHARS:
        omitted = len(flat) - ARGUMENT_MAX_CHARS
        flat = f"{flat[:ARGUMENT_MAX_CHARS]}… (truncated, {omitted} more chars)"
    return f"{open_delim}{flat}{close_delim}"


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

    This is the wrapper every tool call goes through. A non-executing decision
    becomes a failure result carrying the gate's own reason, so the model relays a
    stated constraint instead of improvising one.
    """
    decision = await gate.authorize(tool, arguments)
    if decision.permits:
        return await tool.run(**arguments)
    return ToolResult.failure(
        decision.reason or f"the action was not executed ({decision.outcome.value})"
    )
