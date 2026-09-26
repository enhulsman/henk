"""The replay boundary: stub tools, a refusing transport, a null channel (D14).

A replay re-runs one recorded triage against a real model with every tool
stubbed. The model is the only external party; everything else a live triage
could reach is replaced here by something that records the attempt and refuses:

- :class:`RefusingTransport` is the only transport the tool definitions are built
  over. Every request raises and is counted, so "no tool originated a request" is
  an observed zero, not an assumption about the stubs.
- :class:`NullChannel` is the approval gate's channel. The gate is framed as a
  tainted, non-announceable event turn, so it never prompts; if it did, the send
  raises and the gate fails closed (``cancelled``).
- :class:`RefusingStores` stands in for Henk's store. Every repository access
  raises, and the SQLite file is never constructed, let alone opened.
- :class:`ReplayReceipts` is the gate's decision recorder: receipts go to the run
  output, never to the audit log.

**The stubs carry the current definitions.** :func:`build_definition_registry`
builds the production registry (``build_production_registry``) over the refusing
transport and the refusing stores, which constructs every tool and runs none.
:func:`build_replay_registry` then registers one :class:`ReplayStub` per tool
with the same name, description, parameters, class, tier and turn scope, so the
model sees exactly what it sees live (and the tool-definitions hash agrees), and
the gate decides exactly as it does live. A stub's ``run`` never reaches its real
tool: it asks the :class:`ReplayServer`.

**What the server answers** (spec *Replay serves recorded results and says when
it cannot*):
- a mutating tool is never executed ("not executed in replay"), though the gate
  denies it first;
- ``publish_handoff`` and ``notify`` capture their document or message into the
  run output and return a result marked :data:`REPLAY_MARK`;
- a reconstructed case serves ``homelab_query`` from its capture
  (:mod:`henk.replay.case`) and answers every other tool "unavailable in
  reconstruction";
- otherwise a call is served its recorded result when its name and canonicalized
  arguments equal a recorded call's, repeated calls in recorded order, a recorded
  error as an error. Any other call is the explicit error
  ``not recorded in this replay: <tool>(<args>)``, counted.

This module imports no channel adapter, event intake, ntfy client, audit writer
or store connection (tested in a fresh interpreter).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from henk.agent.session import HANDOFF_TOOL_NAME
from henk.tools.base import Tool, ToolClass, ToolRegistry, ToolResult

logger = logging.getLogger("henk.replay.harness")

#: Every replay-authored result the model receives starts with this.
REPLAY_MARK = "[replay]"
NOT_RECORDED = "not recorded in this replay"
NOT_EXECUTED = "not executed in replay"
NOTIFY_TOOL_NAME = "notify"
#: The prefix `sdk_session._adapt_tool` gives a failed tool result.
_ADAPTER_ERROR_PREFIX = "ERROR: "

#: How a call that reached a stub was answered, as the run output tags it.
SERVED_RECORDED = "recorded"
SERVED_NOT_RECORDED = "not-recorded"
SERVED_CAPTURE = "capture"
SERVED_UNAVAILABLE = "unavailable"
SERVED_CAPTURED = "captured"
SERVED_NOT_EXECUTED = "not-executed"
#: Refused by the query's own validation, exactly as the live tool refuses it.
SERVED_REFUSED = "refused"


class ReplayIsolationError(RuntimeError):
    """Something in a replay reached for a live output or a store."""


# --- The refusing transport, channel, stores and receipts ---------------------


class RefusingTransport(httpx.AsyncBaseTransport):
    """An ``httpx`` transport that refuses every request and counts it.

    Only the method and host are kept: enough to say what was attempted, nothing
    of a query string or body.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts.append(f"{request.method} {request.url.host}")
        raise httpx.ConnectError(
            "refused: a replay originates no tool network request", request=request
        )


class NullChannel:
    """The gate's channel in a replay: every send is counted and refused."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    async def send(self, text: str) -> Any:
        self.attempts.append("send")
        raise ReplayIsolationError("a replay sends nothing over the channel")

    async def send_proactive(self, text: str, *, failure_notice: str | None = None) -> Any:
        self.attempts.append("send_proactive")
        raise ReplayIsolationError("a replay sends nothing over the channel")


class _RefusedRepository:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        raise ReplayIsolationError(
            f"a replay has no store: {self._name}.{attribute} was refused"
        )


class RefusingStores:
    """Stands in for ``HenkStores``: tools are constructed over it and never run.

    Truthy, so ``build_production_registry`` uses it rather than building the real
    store; every repository attribute refuses on first use.
    """

    def __init__(self) -> None:
        self.store = _RefusedRepository("store")
        self.memories = _RefusedRepository("memories")
        self.inbox = _RefusedRepository("inbox")
        self.reminders = _RefusedRepository("reminders")
        self.handoffs = _RefusedRepository("handoffs")


class ReplayReceipts:
    """The gate's decision recorder in a replay: receipts land in the run output."""

    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []

    def record(self, **fields: Any) -> None:
        self.decisions.append(
            {
                "tool": fields.get("tool"),
                "tier": fields.get("tier"),
                "outcome": fields.get("outcome"),
                "reference": fields.get("reference"),
                "turn_type": fields.get("turn_type"),
                "initiated_by": fields.get("initiated_by", "model"),
            }
        )


# --- Matching recorded calls --------------------------------------------------


def canonical_arguments(arguments: Any) -> str:
    """One spelling per argument set: sorted keys, no whitespace, ASCII."""
    return json.dumps(
        arguments if isinstance(arguments, Mapping) else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=repr,
    )


def _call_key(name: str, arguments: Any) -> tuple[str, str]:
    return (name, canonical_arguments(arguments))


def recorded_result(result: str, is_error: bool | None) -> ToolResult:
    """A recorded answer as the stub returns it, so the adapter reproduces it.

    ``_adapt_tool`` renders a failure as ``ERROR: <error>`` and sets no error
    flag, so a live tool failure was recorded as that text: served as a failure
    of the remainder, the model receives it byte for byte. A result the SDK
    flagged as an error is served as a failure too, and so is marked ``ERROR``.
    """
    if result.startswith(_ADAPTER_ERROR_PREFIX):
        return ToolResult.failure(result[len(_ADAPTER_ERROR_PREFIX):])
    if is_error is True:
        return ToolResult.failure(result)
    return ToolResult.success(result)


@dataclass
class ServedCall:
    name: str
    arguments: Any
    served: str
    detail: str | None = None


@dataclass
class ReplayServer:
    """Answers every stub call of one replay, and remembers how.

    ``queries`` is a reconstructed case's capture server
    (:class:`henk.replay.case.ReconstructedQueries`); None for a replay of
    recorded calls. A reconstructed recording's calls are never served: their
    arguments are ``unknown`` and their results were never recorded.
    """

    recording: Mapping[str, Any]
    queries: Any = None
    served: list[ServedCall] = field(default_factory=list)
    handoffs: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._reconstructed = bool(self.recording.get("reconstructed"))
        self._recorded: dict[tuple[str, str], deque] = defaultdict(deque)
        # A reconstructed case is never served from this index: `serve` answers
        # it from the capture first. And an entry whose arguments are not an
        # object (`unknown`) matches no call, even in a malformed live recording,
        # where it would otherwise canonicalize to `{}` and answer a no-argument
        # call with a result nobody asked for.
        for call in self.recording.get("transcript") or ():
            arguments = call.get("arguments")
            if not isinstance(arguments, Mapping):
                continue
            self._recorded[_call_key(call.get("name", ""), arguments)].append(call)

    @property
    def reconstructed(self) -> bool:
        return self._reconstructed

    def _log(self, name: str, arguments: Any, served: str, detail: str | None = None):
        self.served.append(ServedCall(name, dict(arguments), served, detail))

    def serve(self, name: str, tool_class: ToolClass | None,
              arguments: Mapping[str, Any]) -> ToolResult:
        arguments = dict(arguments or {})
        if tool_class is ToolClass.MUTATING:
            self._log(name, arguments, SERVED_NOT_EXECUTED)
            return ToolResult.failure(
                f"{NOT_EXECUTED}: {name} is a mutating tool, and a replay executes "
                "none. Nothing was stored, scheduled or sent."
            )
        if name == HANDOFF_TOOL_NAME:
            document = arguments.get("document")
            replay_id = f"replay-handoff-{len(self.handoffs) + 1}"
            self.handoffs.append(
                {"replay_id": replay_id,
                 "document": document if isinstance(document, str) else None}
            )
            self._log(name, arguments, SERVED_CAPTURED)
            return ToolResult.success(
                f"{REPLAY_MARK} handoff captured in the replay's run output, not "
                f"published (replay id: {replay_id})"
            )
        if name == NOTIFY_TOOL_NAME:
            message = arguments.get("message")
            self.notifications.append(
                {"message": message if isinstance(message, str) else None}
            )
            self._log(name, arguments, SERVED_CAPTURED)
            return ToolResult.success(
                f"{REPLAY_MARK} notification captured in the replay's run output, "
                "not sent"
            )
        if self._reconstructed:
            return self._serve_reconstructed(name, arguments)
        return self._serve_recorded(name, arguments)

    def _serve_reconstructed(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        from henk.replay.case import UNAVAILABLE

        if name == "homelab_query" and self.queries is not None:
            result = self.queries.serve(dict(arguments))
            if result.ok:
                served = SERVED_CAPTURE
            elif (result.error or "").startswith(UNAVAILABLE):
                served = SERVED_UNAVAILABLE
            else:
                served = SERVED_REFUSED
            self._log(name, arguments, served)
            return result
        self._log(name, arguments, SERVED_UNAVAILABLE)
        return ToolResult.failure(
            f"{UNAVAILABLE}: {name} has no captured data for this case. This is a "
            "limit of the reconstruction, not an answer."
        )

    def _serve_recorded(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        key = _call_key(name, arguments)
        queue = self._recorded.get(key)
        call = queue.popleft() if queue else None
        if call is not None and isinstance(call.get("result"), str):
            self._log(name, arguments, SERVED_RECORDED)
            return recorded_result(call["result"], call.get("is_error"))
        detail = (
            "the recording holds no answer for it" if call is not None else None
        )
        self._log(name, arguments, SERVED_NOT_RECORDED, detail)
        suffix = f" ({detail})" if detail else ""
        return ToolResult.failure(f"{NOT_RECORDED}: {name}({key[1]}){suffix}")

    @property
    def unrecorded(self) -> list[ServedCall]:
        return [c for c in self.served if c.served == SERVED_NOT_RECORDED]

    @property
    def unavailable(self) -> list[ServedCall]:
        return [c for c in self.served if c.served == SERVED_UNAVAILABLE]


# --- Stubs and registries -------------------------------------------------------


class ReplayStub(Tool):
    """One tool's current definition, answered by the replay server."""

    def __init__(self, definition: Tool, server: ReplayServer) -> None:
        self.name = definition.name
        self.description = definition.description
        self.parameters = definition.parameters
        self.tool_class = definition.tool_class
        self.authorization = definition.authorization
        self.turn_scope = tuple(definition.turn_scope)
        # Replay frames every turn with tainted REPLAY_TURN; source flags need not copy.
        self._server = server

    async def _run(self, **arguments: Any) -> ToolResult:  # type: ignore[override]
        return self._server.serve(self.name, self.tool_class, arguments)


def build_definition_registry(config: Any, client: httpx.AsyncClient) -> ToolRegistry:
    """The production registry for ``config``, constructed and never run.

    Built over ``client`` (the replay's refusing transport) and
    :class:`RefusingStores`, so no store is built and no tool can reach anything.
    Its tools are the source of the replay's definitions only.
    """
    from henk.events.incident_context import IncidentContextProvider
    from henk.tools import build_production_registry

    return build_production_registry(
        config,
        client,
        stores=RefusingStores(),
        resolver=None,
        reminder_receipts=None,
        incident_context=IncidentContextProvider(),
    )


def build_replay_registry(definitions: ToolRegistry, server: ReplayServer) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in definitions.tools():
        registry.register(ReplayStub(tool, server))
    return registry
