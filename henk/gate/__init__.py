"""The authorization gate: tier + turn scope, fail closed, receipt always."""

from henk.gate.approval import (
    ARGUMENT_DELIMITERS,
    ARGUMENT_MAX_CHARS,
    EXECUTING_OUTCOMES,
    ApprovalGate,
    ApprovalOutcome,
    Classification,
    DecisionRecorder,
    GateDecision,
    TurnContext,
    gated_invoke,
)

__all__ = [
    "ARGUMENT_DELIMITERS",
    "ARGUMENT_MAX_CHARS",
    "ApprovalGate",
    "ApprovalOutcome",
    "Classification",
    "DecisionRecorder",
    "EXECUTING_OUTCOMES",
    "GateDecision",
    "TurnContext",
    "gated_invoke",
]
