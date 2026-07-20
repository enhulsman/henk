"""Inline, fail-closed approval gate for mutating tool invocations.

Read-only and notify-only tools bypass the gate. A mutating tool suspends its
invocation, prompts the owner over the channel with the tool name and exact
arguments, and resumes only on an explicit approval keyword. Denial, an
unrelated message, or timeout all resolve as *not executed* (fail closed).

Each approval is bound to one invocation via a one-time internal reference and
is single-use: the owner never types the reference, and an approved-and-executed
invocation cannot authorise a second call.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from henk.tools.base import Tool, ToolClass, ToolResult

logger = logging.getLogger("henk.gate")

APPROVE_KEYWORDS = frozenset({"yes", "approve"})
DENY_KEYWORDS = frozenset({"no", "deny"})

DEFAULT_TIMEOUT_SECONDS = 300.0


class ApprovalOutcome(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"


class Classification(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    UNRELATED = "unrelated"


class GateBusyError(RuntimeError):
    """Raised if a second approval is requested while one is already pending.

    Serial per-conversation processing guarantees this never happens in practice;
    it exists as a hard invariant check.
    """


class _Sender(Protocol):
    async def send(self, text: str) -> None: ...


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
    ) -> None:
        self._channel = channel
        self._timeout = timeout_seconds
        self._pending: _Pending | None = None
        self._refs = itertools.count(1)

    def has_pending(self) -> bool:
        return self._pending is not None

    @staticmethod
    def classify(text: str) -> Classification:
        token = text.strip().lower()
        if token in APPROVE_KEYWORDS:
            return Classification.APPROVE
        if token in DENY_KEYWORDS:
            return Classification.DENY
        return Classification.UNRELATED

    async def authorize(
        self, tool: Tool, arguments: dict[str, Any]
    ) -> ApprovalOutcome:
        """Return the approval outcome for one invocation of ``tool``.

        Read-only and notify-only tools are auto-approved without a prompt.
        """
        if tool.tool_class in (ToolClass.READ_ONLY, ToolClass.NOTIFY_ONLY):
            return ApprovalOutcome.APPROVED
        if self._pending is not None:
            raise GateBusyError(
                "an approval is already pending in this conversation"
            )

        reference = f"appr-{next(self._refs)}"
        loop = asyncio.get_event_loop()
        future: "asyncio.Future[ApprovalOutcome]" = loop.create_future()
        self._pending = _Pending(tool.name, dict(arguments), reference, future)
        logger.info(
            "approval pending ref=%s tool=%s", reference, tool.name
        )
        await self._channel.send(self._format_prompt(tool.name, arguments))
        try:
            return await asyncio.wait_for(future, self._timeout)
        except asyncio.TimeoutError:
            logger.info("approval ref=%s timed out", reference)
            return ApprovalOutcome.TIMEOUT
        finally:
            self._pending = None

    def deliver(self, text: str) -> tuple[Classification, bool]:
        """Route an inbound message that arrived while an approval is pending.

        Returns ``(classification, requeue)``. ``requeue`` is True when the
        message was unrelated: the pending action fails closed (denied) and the
        message must then be processed as a normal new turn — it is not swallowed.
        """
        if self._pending is None:
            raise GateBusyError("deliver() called with no pending approval")

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
            future.set_result(ApprovalOutcome.DENIED)
        return classification, True

    @staticmethod
    def _format_prompt(tool_name: str, arguments: dict[str, Any]) -> str:
        arg_lines = "\n".join(f"  {k}: {v!r}" for k, v in arguments.items())
        body = arg_lines if arg_lines else "  (no arguments)"
        return (
            f"Approval needed to run `{tool_name}` with:\n{body}\n\n"
            "Reply `yes` to approve or `no` to deny."
        )


async def gated_invoke(
    gate: ApprovalGate, tool: Tool, arguments: dict[str, Any]
) -> ToolResult:
    """Invoke ``tool`` through ``gate``, executing at most once and only if approved.

    This is the wrapper every tool call goes through. In v1 no production tool is
    mutating, so the gate only ever fires in tests — but the path is identical to
    the one a future write tool will take.
    """
    outcome = await gate.authorize(tool, arguments)
    if outcome is ApprovalOutcome.APPROVED:
        return await tool.run(**arguments)
    if outcome is ApprovalOutcome.DENIED:
        return ToolResult.failure("denied by owner; the action was not executed")
    return ToolResult.failure("approval timed out; the action was not executed")
