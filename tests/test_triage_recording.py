"""Triage recording: the transcript, the recording, and its audit link (group 10).

From `specs/triage-replay` (*Every triage is recorded to the audit volume*: *A triage
leaves a recording*, *Denied calls are recorded too*, *An errored triage is still
recorded*, *Recording does not widen audit capture*, *A recording failure does not
disturb the triage*, *Recording can be turned off*) and `specs/audit-log` (*Session
records name their profile and event-triage records link their evidence*: *A triage
record links its recording and history*, *A failed recording leaves a null link, not a
false one*, *Non-triage records carry null evidence links*, *References carry no
content*), design D13. Tasks 10.1 and 10.2. The bounds, cases and retention are in
`test_recording_retention.py`.

The SDK stream is faked with the block shapes `claude_agent_sdk` 0.2.157 exposes
(`ToolUseBlock.id/.name/.input`, `ToolResultBlock.tool_use_id/.content/.is_error`,
`UserMessage.content`), pinned against the installed SDK by an `importorskip` test
that runs in the container.

Placeholders only (standing rule 1). No model call (standing rule 5).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from henk.agent.core import AgentCore
from henk.agent.ending import COMPLETED, ERROR
from henk.agent.permission import decide_tool_permission
from henk.agent.sdk_session import (
    RESULT_CAPTURING_TOOLS,
    SdkSessionFactory,
    _SdkAgentSession,
)
from henk.agent.session import CHAT_PROFILE, EVENT_PROFILE, TranscriptCall
from henk.agent.turns import EventTurn, EventTurnItem
from henk.audit import AuditLog
from henk.config import Config
from henk.events.checkpoint import OffsetCheckpoint
from henk.events.identity import derive_identity
from henk.events.types import Event
from henk.gate.approval import ApprovalGate, TurnContext
from henk.replay.recorder import (
    RECORDING_SCHEMA_PATH,
    TriageRecorder,
    factory_fingerprint,
    is_recording_id,
    list_recordings,
    load_recording,
)
from henk.runtime import build_runtime
from henk.store import Store
from henk.store.handoffs import HandoffStore, format_handoff_result
from henk.tools.base import (
    AuthorizationTier,
    Tool,
    ToolClass,
    ToolRegistry,
    ToolResult,
    TurnType,
)
from tests.conftest import (
    TRIAGE_REPLY,
    EventSession,
    EventSessionFactory,
    FakeChannel,
    FakeSessionFactory,
    make_clock,
)
from tests.test_config import SAMPLE
from tests.test_triage_ending import (
    AssistantMessage,
    _FakeClient,
    _assistant,
    _result,
)

SCHEMA = json.loads(RECORDING_SCHEMA_PATH.read_text())

MODEL = "claude-opus-5-5"
SYSTEM_PROMPT = "You are Henk, a placeholder system prompt for tests."
T0 = 1_790_144_043.0  # 2026-09-23T06:14:03Z

TITLE = "[FIRING:1] HenkDiskFull henk (host-a.example)"
MESSAGE = (
    "Value: A=91.2\nLabels:\n - alertname = HenkDiskFull\n"
    " - identity_scope = host\n - host = host-a.example\n - node = vps\n"
)

#: Canaries: each must reach the recording and never the audit record.
RESULT_1 = "canary-result-one: example-a.service 612 MiB at 192.0.2.10"
RESULT_2 = "canary-result-two: swap 98.39% on host-a.example"
HANDOFF_DOC = "canary-handoff-document: example-a.service page-cache burst"
HANDOFF_RESULT = format_handoff_result("msg-0001")


def _validate(record: dict) -> None:
    jsonschema.validate(record, SCHEMA)


# --- Fake SDK stream blocks (the 0.2.157 field sets we read) -----------------


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: bool | None = None


@dataclass
class UserMessage:
    content: Any
    uuid: str | None = None
    parent_tool_use_id: str | None = None
    tool_use_result: Any = None


def test_the_fake_blocks_match_the_installed_sdk():
    sdk = pytest.importorskip("claude_agent_sdk")
    for fake, real in ((ToolUseBlock, sdk.ToolUseBlock),
                       (ToolResultBlock, sdk.ToolResultBlock)):
        assert [f.name for f in dataclasses.fields(fake)] == [
            f.name for f in dataclasses.fields(real)
        ]
    real_user = {f.name for f in dataclasses.fields(sdk.UserMessage)}
    assert "content" in real_user


def _use(n: int, name: str, args: dict, *, prefix: str = "mcp__henk__"):
    return AssistantMessage(
        content=[ToolUseBlock(f"tu-{n}", f"{prefix}{name}", args)], model=MODEL
    )


def _answer(n: int, text: str, *, is_error: bool | None = None) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(
        f"tu-{n}", [{"type": "text", "text": text}], is_error)])


def _call(n: int, name: str, args: dict, text: str, *, is_error=None, **kw) -> list:
    return [_use(n, name, args, **kw), _answer(n, text, is_error=is_error)]


QUERY_1 = {"query": "memory_movers", "host": "vps", "window": "6h"}
QUERY_2 = {"query": "host_service_state", "host": "vps"}


def _three_call_turn() -> list:
    return [
        *_call(1, "homelab_query", QUERY_1, RESULT_1),
        *_call(2, "homelab_query", QUERY_2, RESULT_2),
        *_call(3, "publish_handoff", {"document": HANDOFF_DOC}, HANDOFF_RESULT),
        _assistant(TRIAGE_REPLY, model=MODEL),
        _result(),
    ]


# --- Tools and factories --------------------------------------------------------


class QueryTool(Tool):
    name = "homelab_query"
    description = "placeholder read-only query tool"
    tool_class = ToolClass.READ_ONLY
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}}

    async def _run(self, **kwargs) -> ToolResult:  # pragma: no cover - never run
        return ToolResult.success("unused")


class HandoffTool(Tool):
    name = "publish_handoff"
    description = "placeholder handoff tool"
    tool_class = ToolClass.NOTIFY_ONLY
    parameters = {"type": "object", "properties": {"document": {"type": "string"}}}

    async def _run(self, **kwargs) -> ToolResult:  # pragma: no cover - never run
        return ToolResult.success("unused")


class MutatingTool(Tool):
    name = "example_mutate"
    description = "placeholder per-instance mutating tool"
    tool_class = ToolClass.MUTATING
    authorization = AuthorizationTier.PER_INSTANCE
    turn_scope = (TurnType.OWNER,)
    parameters = {"type": "object", "properties": {"x": {"type": "integer"}}}

    async def _run(self, **kwargs) -> ToolResult:  # pragma: no cover - never run
        return ToolResult.success("mutated")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (QueryTool(), HandoffTool(), MutatingTool()):
        registry.register(tool)
    return registry


class ScriptedSdkFactory(SdkSessionFactory):
    """The real factory, with `create()` returning the real session class over a
    scripted fake client: the real fingerprint, transcript and ending paths."""

    def __init__(self, turns_per_session: list[list], *, profile=EVENT_PROFILE,
                 effort="high", registry=None, gate=None) -> None:
        registry = registry or _registry()
        super().__init__(registry, gate or ApprovalGate(FakeChannel()), model=MODEL,
                         system_prompt=SYSTEM_PROMPT, effort=effort, thinking=None,
                         profile=profile)
        self._turns = list(turns_per_session)
        self.created: list[_SdkAgentSession] = []

    def create(self):
        client = _FakeClient(list(self._turns))
        session = _SdkAgentSession(client, tool_classes={
            t.name: t.tool_class.value for t in self.registry.tools()})
        self.created.append(session)
        return session


def _turn(eid: str = "e1", *, announceable: bool = True, offset: str | None = "off-1",
          message: str = MESSAGE) -> EventTurn:
    event = Event(id=eid, title=TITLE, message=message, arrival_time=T0,
                  raw={"event": "message", "time": int(T0) - 60})
    item = EventTurnItem(event=event, identity=derive_identity(event))
    return EventTurn(items=(item,), announceable=announceable, offset=offset)


def _core(factory, tmp_path: Path, *, recorder: Any = "default", channel=None,
          checkpoint=None, **kw) -> AgentCore:
    if recorder == "default":
        recorder = TriageRecorder(tmp_path / "triage-recordings",
                                  replays_dir=tmp_path / "triage-replays")
    return AgentCore(
        factory,
        channel or FakeChannel(),
        clock=make_clock([0]),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        model=MODEL,
        checkpoint=checkpoint,
        recorder=recorder,
        **kw,
    )


def _sessions(tmp_path: Path) -> list[dict]:
    path = tmp_path / "audit.jsonl"
    return [
        r for r in (json.loads(line) for line in path.read_text().splitlines() if line)
        if r.get("record_type") == "session"
    ]


def _audit_text(tmp_path: Path) -> str:
    return (tmp_path / "audit.jsonl").read_text()


def _only_recording(tmp_path: Path) -> dict:
    directory = tmp_path / "triage-recordings"
    [recording_id] = list_recordings(directory)
    return load_recording(directory, recording_id)


# --- The per-turn transcript on the session (10.4 seam) -------------------------


async def _run_session(turns: list) -> tuple[_SdkAgentSession, list]:
    session = _SdkAgentSession(_FakeClient(turns))
    outcomes = []
    for _ in turns:
        try:
            outcomes.append(await session.run_turn("x"))
        except Exception as exc:  # noqa: BLE001 - recording what the turn did
            outcomes.append(exc)
    return session, outcomes


async def test_the_transcript_holds_every_call_in_order_with_its_answer():
    session, _ = await _run_session([_three_call_turn()])
    assert session.transcript() == (
        TranscriptCall("homelab_query", QUERY_1, RESULT_1, None, "tu-1"),
        TranscriptCall("homelab_query", QUERY_2, RESULT_2, None, "tu-2"),
        TranscriptCall("publish_handoff", {"document": HANDOFF_DOC}, HANDOFF_RESULT,
                       None, "tu-3"),
    )


async def test_the_transcript_is_the_last_turn_only():
    session, _ = await _run_session([
        _call(1, "homelab_query", QUERY_1, RESULT_1) + [_assistant(), _result()],
        _call(2, "homelab_query", QUERY_2, RESULT_2) + [_assistant(), _result()],
    ])
    assert [c.tool_use_id for c in session.transcript()] == ["tu-2"]
    # The audit stats stay cumulative: only the transcript is per turn.
    assert [c.name for c in session.stats().tool_calls] == ["homelab_query"] * 2


async def test_the_transcript_is_reset_even_when_the_next_turn_fails_first():
    session, outcomes = await _run_session([
        _call(1, "homelab_query", QUERY_1, RESULT_1) + [_assistant(), _result()],
        RuntimeError("placeholder query failure"),
    ])
    assert isinstance(outcomes[1], RuntimeError)
    assert session.transcript() == ()


async def test_the_transcript_keeps_an_error_flag_a_missing_answer_and_a_builtin():
    session, outcomes = await _run_session([[
        *_call(1, "Bash", {"command": "ls"}, "Bash is not a Henk tool; blocked",
               is_error=True, prefix=""),
        _use(2, "homelab_query", QUERY_1),
        RuntimeError("placeholder stream failure"),
    ]])
    assert isinstance(outcomes[0], RuntimeError)
    assert session.transcript() == (
        TranscriptCall("Bash", {"command": "ls"}, "Bash is not a Henk tool; blocked",
                       True, "tu-1"),
        TranscriptCall("homelab_query", QUERY_1, None, None, "tu-2"),
    )


async def test_the_transcript_copies_the_arguments():
    args = {"query": "memory_movers"}
    session, _ = await _run_session([_call(1, "homelab_query", args, RESULT_1)
                                     + [_assistant(), _result()]])
    args["query"] = "mutated after the fact"
    assert session.transcript()[0].arguments == {"query": "memory_movers"}


async def test_string_user_content_is_not_read_as_blocks():
    session, _ = await _run_session([[UserMessage(content="plain text"),
                                      _assistant(), _result()]])
    assert session.transcript() == ()


# --- 10.1 A triage leaves a recording -------------------------------------------


async def test_a_triage_leaves_a_recording(tmp_path: Path, monkeypatch):
    """*A triage leaves a recording*: three calls, byte-exact content, profile, reply,
    ending, recorded live, schema-valid."""
    factory = ScriptedSdkFactory([_three_call_turn()])
    # A distinct chat factory: the recording must name the factory that made the
    # triage session, never the chat one.
    chat = ScriptedSdkFactory([], profile=CHAT_PROFILE, effort="low")
    core = _core(chat, tmp_path, event_factory=factory)
    captured: list[str] = []
    original = _FakeClient.query

    async def spy(self, text):
        captured.append(text)
        return await original(self, text)

    monkeypatch.setattr(_FakeClient, "query", spy)
    await core.process(_turn())

    recording = _only_recording(tmp_path)
    _validate(recording)
    assert recording["reconstructed"] is False
    assert recording["content"] == captured[0]  # byte-for-byte as sent
    assert recording["profile"] == {"name": EVENT_PROFILE, "model": MODEL,
                                    "effort": "high", "thinking": None}
    assert [(c["name"], c["arguments"], c["result"]) for c in recording["transcript"]] == [
        ("homelab_query", QUERY_1, RESULT_1),
        ("homelab_query", QUERY_2, RESULT_2),
        ("publish_handoff", {"document": HANDOFF_DOC}, HANDOFF_RESULT),
    ]
    assert recording["reply"] == TRIAGE_REPLY
    assert recording["ending"] == {"outcome": COMPLETED, "error_class": None,
                                   "http_status": None}
    assert recording["complete"] is True and recording["incomplete_reasons"] == []
    assert recording["hashes"] == {
        "system_prompt": factory_fingerprint(factory)["system_prompt_sha256"],
        "tool_definitions": factory_fingerprint(factory)["tool_definitions_sha256"],
    }
    [incident] = recording["incidents"]
    assert incident["title"] == TITLE and incident["message"] == MESSAGE
    assert incident["notified"] == int(T0) - 60 and incident["received"] == T0
    assert incident["recurrence"] is False and incident["prior_handoff_ref"] is None
    assert recording["usage"] == {"input_tokens": 1, "output_tokens": 1,
                                  "cache_read_input_tokens": None}


async def test_the_recording_names_the_turn_it_records(tmp_path: Path):
    factory = ScriptedSdkFactory([_three_call_turn()])
    core = _core(factory, tmp_path, event_factory=factory)
    await core.process(_turn(announceable=False))
    recording = _only_recording(tmp_path)
    assert recording["announceable"] is False
    assert recording["suppressed_count"] == 0
    assert is_recording_id(recording["recording_id"])


def test_the_fingerprint_changes_with_the_prompt_and_the_tools():
    base = factory_fingerprint(ScriptedSdkFactory([]))
    other_prompt = ScriptedSdkFactory([])
    other_prompt._config = dataclasses.replace(other_prompt.config,
                                               system_prompt=SYSTEM_PROMPT + " ")
    assert factory_fingerprint(other_prompt)["system_prompt_sha256"] != (
        base["system_prompt_sha256"])
    assert factory_fingerprint(other_prompt)["tool_definitions_sha256"] == (
        base["tool_definitions_sha256"])

    class RenamedQuery(QueryTool):
        description = "placeholder read-only query tool, reworded"

    registry = ToolRegistry()
    for tool in (RenamedQuery(), HandoffTool(), MutatingTool()):
        registry.register(tool)
    changed = factory_fingerprint(ScriptedSdkFactory([], registry=registry))
    assert changed["tool_definitions_sha256"] != base["tool_definitions_sha256"]
    assert changed["system_prompt_sha256"] == base["system_prompt_sha256"]
    # The registry's order does not change the hash; the definitions do.
    reordered = ToolRegistry()
    for tool in (MutatingTool(), HandoffTool(), QueryTool()):
        reordered.register(tool)
    assert factory_fingerprint(ScriptedSdkFactory([], registry=reordered))[
        "tool_definitions_sha256"] == base["tool_definitions_sha256"]


def test_a_fake_factory_fingerprints_as_unknown_not_invented():
    fp = factory_fingerprint(FakeSessionFactory())
    assert fp == {"profile": CHAT_PROFILE, "model": None, "effort": None,
                  "thinking": None, "system_prompt_sha256": None,
                  "tool_definitions_sha256": None}


# --- 10.1 Denied calls are recorded too -----------------------------------------


async def test_denied_calls_are_recorded_too(tmp_path: Path):
    """*Denied calls are recorded too*: the gate's own denial text, as the model got it."""
    registry = _registry()
    gate = ApprovalGate(FakeChannel())
    # The denial text the model receives is the real gate's, framed exactly as the
    # core frames a triage turn: event, tainted.
    gate.enter_turn(TurnContext(turn_type=TurnType.EVENT, announceable=True,
                                tainted=True))
    decision = await decide_tool_permission(
        registry, gate, "mcp__henk__example_mutate", {"x": 7})
    gate.exit_turn()
    assert decision.allow is False and decision.reason
    factory = ScriptedSdkFactory([[
        *_call(1, "example_mutate", {"x": 7}, decision.reason, is_error=True),
        *_call(2, "Bash", {"command": "id"},
               "Bash is not a Henk tool; blocked by the closed-toolset hook",
               is_error=True, prefix=""),
        _assistant(TRIAGE_REPLY, model=MODEL),
        _result(),
    ]], registry=registry, gate=gate)
    core = _core(factory, tmp_path, event_factory=factory, gate=gate)
    await core.process(_turn())
    recording = _only_recording(tmp_path)
    _validate(recording)
    denied, blocked = recording["transcript"]
    assert (denied["name"], denied["arguments"]) == ("example_mutate", {"x": 7})
    assert denied["result"] == decision.reason and denied["is_error"] is True
    assert (blocked["name"], blocked["is_error"]) == ("Bash", True)
    assert "closed-toolset hook" in blocked["result"]


# --- 10.1 An errored triage is still recorded -----------------------------------


async def test_an_errored_triage_is_still_recorded(tmp_path: Path):
    """*An errored triage is still recorded*: the two calls before the raise, and
    ending `error`."""
    factory = ScriptedSdkFactory([[
        *_call(1, "homelab_query", QUERY_1, RESULT_1),
        *_call(2, "homelab_query", QUERY_2, RESULT_2),
        RuntimeError("placeholder stream failure"),
    ]])
    channel = FakeChannel()
    core = _core(factory, tmp_path, event_factory=factory, channel=channel)
    await core.process(_turn())
    recording = _only_recording(tmp_path)
    _validate(recording)
    assert [c["result"] for c in recording["transcript"]] == [RESULT_1, RESULT_2]
    assert recording["ending"]["outcome"] == ERROR
    assert recording["reply"] is None
    [record] = _sessions(tmp_path)
    assert record["outcome"] == ERROR
    assert record["recording_id"] == recording["recording_id"]
    # The notice still went out: recording changed nothing about the ending path.
    assert channel.sent and channel.sent[0].startswith("[AI] Triage incomplete")


async def test_an_api_error_triage_records_its_ending_class(tmp_path: Path):
    factory = ScriptedSdkFactory([[
        _assistant("placeholder refusal", model=MODEL, error="rate_limit"),
        _result(is_error=True, api_error_status=529),
    ]])
    core = _core(factory, tmp_path, event_factory=factory)
    await core.process(_turn())
    recording = _only_recording(tmp_path)
    _validate(recording)
    assert recording["ending"] == {"outcome": ERROR, "error_class": "rate_limit",
                                   "http_status": 529}
    assert recording["reply"] == "placeholder refusal"


async def test_a_session_without_a_transcript_still_records_and_says_so(tmp_path: Path):
    # The group-9 pattern: an optional method missing on a fake is "no signal".
    factory = EventSessionFactory()
    channel = FakeChannel()
    core = _core(factory, tmp_path, channel=channel)
    await core.process(_turn())
    recording = _only_recording(tmp_path)
    _validate(recording)
    assert recording["transcript"] == []
    assert recording["complete"] is False
    assert recording["incomplete_reasons"] == ["transcript-unavailable"]
    assert channel.sent == [TRIAGE_REPLY]


async def test_a_raising_transcript_is_treated_as_unavailable(tmp_path: Path):
    class Raising(EventSession):
        def transcript(self):
            raise RuntimeError("placeholder")

    class Factory(EventSessionFactory):
        def create(self):
            session = Raising(self.reply, self.stats)
            self.created.append(session)
            return session

    core = _core(Factory(), tmp_path)
    await core.process(_turn())
    recording = _only_recording(tmp_path)
    assert recording["incomplete_reasons"] == ["transcript-unavailable"]
    assert _sessions(tmp_path)[0]["recording_id"] == recording["recording_id"]


async def test_the_recording_is_written_before_the_record_that_links_it(tmp_path: Path):
    factory = ScriptedSdkFactory([_three_call_turn()])
    seen: list[bool] = []

    class Audit(AuditLog):
        def write(self, record):
            if record.get("record_type") == "session":
                seen.append(bool(list_recordings(tmp_path / "triage-recordings")))
            return super().write(record)

    core = AgentCore(factory, FakeChannel(), clock=make_clock([0]),
                     audit=Audit(tmp_path / "audit.jsonl"), event_factory=factory,
                     recorder=TriageRecorder(tmp_path / "triage-recordings"))
    await core.process(_turn())
    assert seen == [True]


# --- 10.2 Recording does not widen audit capture --------------------------------


def test_result_capturing_tools_are_unchanged():
    assert RESULT_CAPTURING_TOOLS == frozenset({"publish_handoff"})


async def test_recording_does_not_widen_audit_capture(tmp_path: Path):
    """*Recording does not widen audit capture*: result text only for the handoff."""
    factory = ScriptedSdkFactory([_three_call_turn()])
    core = _core(factory, tmp_path, event_factory=factory)
    await core.process(_turn())
    [record] = _sessions(tmp_path)
    assert [(c["name"], c["result_id"]) for c in record["tool_calls"]] == [
        ("homelab_query", None),
        ("homelab_query", None),
        ("publish_handoff", HANDOFF_RESULT),
    ]
    text = _audit_text(tmp_path)
    for canary in (RESULT_1, RESULT_2, HANDOFF_DOC, "192.0.2.10"):
        assert canary not in text
    # ...while the recording holds all of it.
    recorded = json.dumps(_only_recording(tmp_path))
    for canary in (RESULT_1, RESULT_2, HANDOFF_DOC):
        assert canary in recorded


# --- 10.2 A recording failure does not disturb the triage -----------------------


class _RaisingRecorder:
    def __init__(self) -> None:
        self.calls = 0

    def record(self, **kwargs):
        self.calls += 1
        raise RuntimeError(f"placeholder failure carrying {RESULT_1}")


def _unwritable_recorder(tmp_path: Path) -> TriageRecorder:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    return TriageRecorder(blocker / "triage-recordings")


@pytest.mark.parametrize("failing", ["unwritable", "raising"])
async def test_a_recording_failure_does_not_disturb_the_triage(
    tmp_path: Path, caplog, failing
):
    """*A recording failure does not disturb the triage* and *A failed recording
    leaves a null link, not a false one*."""
    recorder = (_unwritable_recorder(tmp_path) if failing == "unwritable"
                else _RaisingRecorder())
    checkpoint = OffsetCheckpoint(tmp_path / "intake-offset")
    channel = FakeChannel()
    factory = ScriptedSdkFactory([_three_call_turn()])
    core = _core(factory, tmp_path, recorder=recorder, channel=channel,
                 checkpoint=checkpoint, event_factory=factory)
    with caplog.at_level(logging.DEBUG):
        await core.process(_turn())

    assert channel.sent == [TRIAGE_REPLY]
    [record] = _sessions(tmp_path)
    assert record["recording_id"] is None
    assert record["outcome"] == COMPLETED
    assert record["handoff_message_id"] == HANDOFF_RESULT
    assert checkpoint.read() == "off-1"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a recording failure must be logged at error level"
    logged = "\n".join(
        r.getMessage() + (logging.Formatter().formatException(r.exc_info)
                          if r.exc_info else "")
        for r in caplog.records
    )
    for canary in (RESULT_1, RESULT_2, HANDOFF_DOC, TITLE, "192.0.2.10"):
        assert canary not in logged
    assert not list((tmp_path).glob("**/*.tmp"))


async def test_a_recorder_answering_something_other_than_an_id_links_nothing(
    tmp_path: Path,
):
    class Odd:
        def record(self, **kwargs):
            return 123

    factory = ScriptedSdkFactory([_three_call_turn()])
    core = _core(factory, tmp_path, recorder=Odd(), event_factory=factory)
    await core.process(_turn())
    assert _sessions(tmp_path)[0]["recording_id"] is None


async def test_a_retention_failure_keeps_the_written_recording_linked(
    tmp_path: Path, monkeypatch, caplog
):
    def failing_prune(self, *, now=None):
        raise RuntimeError("placeholder retention failure")

    monkeypatch.setattr(TriageRecorder, "prune", failing_prune)
    factory = ScriptedSdkFactory([_three_call_turn()])
    core = _core(factory, tmp_path, event_factory=factory)
    with caplog.at_level(logging.ERROR):
        await core.process(_turn())
    [recording_id] = list_recordings(tmp_path / "triage-recordings")
    assert _sessions(tmp_path)[0]["recording_id"] == recording_id
    assert any("retention" in r.getMessage() for r in caplog.records)


async def test_a_failed_recording_links_nothing_even_when_a_file_was_attempted(
    tmp_path: Path, monkeypatch
):
    # The write reaches the rename and fails there: no file, and no id on the record.
    import henk.replay.recorder as recorder_module

    def fail(src, dst):
        raise OSError("placeholder rename failure")

    monkeypatch.setattr(recorder_module.os, "replace", fail)
    factory = ScriptedSdkFactory([_three_call_turn()])
    core = _core(factory, tmp_path, event_factory=factory)
    await core.process(_turn())
    assert _sessions(tmp_path)[0]["recording_id"] is None
    assert list_recordings(tmp_path / "triage-recordings") == []


# --- 10.2 Recording can be turned off -------------------------------------------


def _sample_raw(tmp_path: Path) -> dict:
    raw = yaml.safe_load(SAMPLE.read_text())
    audit = str(tmp_path / "henk-audit.jsonl")
    raw["audit"]["path"] = audit
    raw["events"]["audit_path"] = audit
    raw["store"]["path"] = str(tmp_path / "henk-store.db")
    return raw


async def test_recording_is_on_with_the_key_absent(tmp_path: Path):
    # Standing rule 4: rp5's config.yaml has no `triage_recording` section.
    raw = _sample_raw(tmp_path)
    del raw["triage_recording"]
    config = Config.from_dict(raw, env={})
    app, client = build_runtime(config)
    try:
        recorder = app._core._recorder
        assert isinstance(recorder, TriageRecorder)
        assert recorder.directory == config.audit.triage_recordings_dir
        assert recorder.directory == tmp_path / "triage-recordings"
        assert recorder.replays_dir == tmp_path / "triage-replays"
    finally:
        await client.aclose()


async def test_recording_can_be_turned_off_in_the_runtime(tmp_path: Path):
    raw = _sample_raw(tmp_path)
    raw["triage_recording"] = {"enabled": False}
    app, client = build_runtime(Config.from_dict(raw, env={}))
    try:
        assert app._core._recorder is None
    finally:
        await client.aclose()


async def test_recording_can_be_turned_off(tmp_path: Path):
    """*Recording can be turned off*: nothing written, the triage otherwise unchanged."""
    outcomes = {}
    for label, recorder in (("on", "default"), ("off", None)):
        base = tmp_path / label
        base.mkdir()
        channel = FakeChannel()
        factory = ScriptedSdkFactory([_three_call_turn()])
        core = _core(factory, base, recorder=recorder, channel=channel,
                     event_factory=factory)
        await core.process(_turn())
        [record] = _sessions(base)
        outcomes[label] = (channel.calls, record)
    on_calls, on_record = outcomes["on"]
    off_calls, off_record = outcomes["off"]
    assert not (tmp_path / "off" / "triage-recordings").exists()
    assert list_recordings(tmp_path / "on" / "triage-recordings")
    assert off_calls == on_calls
    assert off_record["recording_id"] is None
    assert on_record["recording_id"] is not None
    for key in ("recording_id", "at"):
        on_record.pop(key)
        off_record.pop(key)
    assert off_record == on_record


# --- 10.2 A triage record links its recording and history ------------------------


def _archive(tmp_path: Path) -> tuple[HandoffStore, list[int]]:
    store = Store(tmp_path / "store" / "henk.db", clock=lambda: T0 - 3600)
    archive = HandoffStore(store)
    identity = derive_identity(_turn().items[0].event).key
    ids = []
    for n in (1, 2):
        handoff = archive.retain(
            f"canary-prior-handoff-{n}: example-a.service was restarted",
            message_id=f"msg-prior-{n}", identity_keys=(identity,),
            rule_keys=("grafana:HenkDiskFull",), nodes=("vps",),
        )
        ids.append(handoff.id)
    return archive, ids


async def test_a_triage_record_links_its_recording_and_history(tmp_path: Path):
    """*A triage record links its recording and history*."""
    archive, ids = _archive(tmp_path)
    factory = ScriptedSdkFactory([_three_call_turn()], effort="max")
    core = _core(factory, tmp_path, event_factory=factory, handoff_archive=archive)
    await core.process(_turn())
    [record] = _sessions(tmp_path)
    recording = _only_recording(tmp_path)
    assert record["profile"] == EVENT_PROFILE
    assert record["effort"] == "max"
    assert record["recording_id"] == recording["recording_id"]
    assert sorted(record["prior_handoff_ids"]) == sorted(ids)
    assert sorted(recording["prior_handoff_ids"]) == sorted(ids)
    # The id is a real file's name, not a guess.
    assert (tmp_path / "triage-recordings" / f"{record['recording_id']}.json").is_file()


# --- 10.2 Non-triage records carry null evidence links ---------------------------


async def test_non_triage_records_carry_null_evidence_links(tmp_path: Path):
    """*Non-triage records carry null evidence links*: a plain owner session and an
    owner continuation inside a triage session."""
    chat = ScriptedSdkFactory([[_assistant("owner reply", model=MODEL), _result()]],
                              profile=CHAT_PROFILE)
    event = ScriptedSdkFactory([
        _three_call_turn(),
        [_assistant("follow-up reply", model=MODEL), _result()],
    ])
    clock = make_clock([0, 1, 2, 3, 4, 5])
    core = AgentCore(chat, FakeChannel(), clock=clock,
                     audit=AuditLog(tmp_path / "audit.jsonl"), event_factory=event,
                     recorder=TriageRecorder(tmp_path / "triage-recordings"))
    await core.process("hello")  # plain owner session
    await core.process(_turn())  # displaces it; the triage record flushes
    await core.process("and the follow-up?")  # continuation in the triage session
    await core.aclose()
    owner, triage, continuation = _sessions(tmp_path)
    assert (owner["trigger"], owner["profile"]) == ("owner-message", CHAT_PROFILE)
    assert (continuation["trigger"], continuation["profile"]) == (
        "owner-message", EVENT_PROFILE)
    for record in (owner, continuation):
        assert record["recording_id"] is None
        assert record["prior_handoff_ids"] is None
    assert triage["recording_id"] is not None
    # One recording, for the one triage: owner turns are never recorded.
    assert len(list_recordings(tmp_path / "triage-recordings")) == 1


# --- 10.2 References carry no content --------------------------------------------


async def test_references_carry_no_content(tmp_path: Path):
    """*References carry no content*: no substring of the composed content, of a
    retained handoff, or of any tool result but the handoff id."""
    archive, _ = _archive(tmp_path)
    message = MESSAGE + " - canary_payload = canary-payload-7c1e\n"
    factory = ScriptedSdkFactory([_three_call_turn()])
    core = _core(factory, tmp_path, event_factory=factory, handoff_archive=archive)
    await core.process(_turn(message=message))
    [record] = _sessions(tmp_path)
    recording = _only_recording(tmp_path)
    assert "canary-prior-handoff-1" in recording["content"]  # the digest was shown
    line = json.dumps(record)
    assert "canary-payload-7c1e" not in line
    assert "canary-prior-handoff" not in line
    for call in recording["transcript"]:
        if call["result"] != HANDOFF_RESULT:
            assert call["result"] not in line
    # No 40-character window of the composed content appears in the record.
    content = recording["content"]
    windows = {content[i:i + 40] for i in range(0, max(1, len(content) - 40), 7)}
    assert not any(w in line for w in windows if w.strip())
    # And the link itself is only an id.
    assert is_recording_id(record["recording_id"])
