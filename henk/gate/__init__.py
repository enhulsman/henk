"""Approval gate: the mutation-approval scaffold (tested in v1, unused in prod)."""

from henk.gate.approval import (
    ApprovalGate,
    ApprovalOutcome,
    Classification,
    GateBusyError,
    gated_invoke,
)

__all__ = [
    "ApprovalGate",
    "ApprovalOutcome",
    "Classification",
    "GateBusyError",
    "gated_invoke",
]
