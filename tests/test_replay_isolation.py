"""Replay isolation: a replay that tries everything reaches nothing (group 11,
task 11.2, and 11.3's import-graph check).

From `specs/triage-replay` *Replay keeps the boundary and cannot reach live outputs
or state*: *A replay that tries everything reaches nothing*, *The hook still blocks
built-ins*; and design D14 "The boundary": the same PreToolUse hook, the same empty
`allowed_tools`, the same `can_use_tool` -> gate path; a tainted, non-announceable
event framing over a refusing channel; no adapter, intake, ntfy client, `AuditLog`
or `Store`; tool definitions built over a transport that raises on every request.

Every guarantee is checked by observation, not by reading the code: the refusing
transport counts attempts, `Store` and `AuditLog` fail the test if constructed,
`sqlite3.connect` fails the test if called, the audit file's bytes are compared,
the null channel counts sends, and the import graph is read in a fresh interpreter.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from henk.agent.permission import pretooluse_block_decision
from henk.agent.sdk_session import BUILTIN_HOST_TOOLS, SdkSessionFactory
from henk.agent.session import EVENT_PROFILE
from henk.gate.approval import ApprovalOutcome, TurnType
from henk.replay import harness
from henk.replay import run as run_mod
from henk.replay.recorder import tool_definitions_hash
from henk.tools.base import ToolClass
from tests.replay_fakes import (
    OTHER_MODEL,
    T,
    SessionMaker,
    live_recording,
    make_config,
    write_recording,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Plausible arguments for every tool the production builder can register.
ARGUMENTS = {
    "homelab_health": {},
    "todo_read": {},
    "notify": {"message": "tried to notify"},
    "publish_handoff": {"document": "tried to publish"},
    "store_memory": {"content": "tried to remember"},
    "capture": {"text": "tried to capture"},
    "inbox_read": {},
    "remind": {"when": "tomorrow 09:00", "text": "tried to remind"},
    "cancel_reminder": {"reminder": "1"},
    "reminders_read": {},
    "homelab_query": {"query_name": "scrape_targets"},
    "homelab_docs": {"action": "search", "query": "swap"},
    "sessions_read": {},
}
BUILTINS = ("Bash", "WebFetch", "ToolSearch", "Write")


class _Forbidden:
    """Replaces a constructor or function: any call fails the test."""

    def __init__(self, what: str) -> None:
        self.what = what

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"the replay reached {self.what}")


@pytest.fixture
def sealed(monkeypatch):
    """Every live output and store, patched to fail the test on first touch."""
    import henk.audit
    import henk.audit.logger
    import henk.store
    import henk.store.db
    import henk.store.factory

    for module in (henk.store, henk.store.db, henk.store.factory):
        monkeypatch.setattr(module, "Store", _Forbidden("the store"))
    monkeypatch.setattr(henk.store.factory, "build_stores", _Forbidden("build_stores"))
    monkeypatch.setattr(sqlite3, "connect", _Forbidden("sqlite3.connect"))
    real_audit_log = henk.audit.logger.AuditLog
    monkeypatch.setattr(real_audit_log, "write", _Forbidden("an audit write"))
    for module in (henk.audit, henk.audit.logger):
        monkeypatch.setattr(module, "AuditLog", _Forbidden("the audit writer"))
    try:
        import henk.channel.signal as signal
        monkeypatch.setattr(signal, "SignalAdapter", _Forbidden("the Signal adapter"))
        monkeypatch.setattr(signal, "SignalCliRestBridge", _Forbidden("the bridge"))
        import henk.events.intake as intake
        monkeypatch.setattr(intake, "NtfyEventStream", _Forbidden("the ntfy stream"))
        monkeypatch.setattr(intake, "EventIntake", _Forbidden("event intake"))
    except ImportError:  # pragma: no cover
        pass
    return monkeypatch


def _everything(registry_names) -> list[tuple[str, dict]]:
    script = [(name, dict(ARGUMENTS[name])) for name in registry_names]
    script += [(name, {"command": "id"}) for name in BUILTINS]
    return script


async def _try_everything(tmp_path, *, recorded=()):
    config = make_config(tmp_path, everything=True)
    rid = write_recording(config, live_recording(list(recorded)))
    audit_before = Path(config.audit.path).read_bytes()
    source = run_mod.resolve_source(config, rid)
    # First learn the registry, then script a model that calls all of it.
    names = _registry_names(config)
    maker = SessionMaker(_everything(names))
    outcome = await run_mod.run_replay(
        config, source, model=OTHER_MODEL, effort="high", thinking=None,
        create_session=maker, clock=lambda: float(T + 3600),
    )
    return config, outcome, maker, names, audit_before


def _registry_names(config) -> list[str]:
    client = httpx.AsyncClient(transport=harness.RefusingTransport())
    return harness.build_definition_registry(config, client).names()


def test_the_test_registry_holds_every_tool_the_builder_can_register(tmp_path):
    config = make_config(tmp_path, everything=True)
    assert set(_registry_names(config)) == set(ARGUMENTS)


async def test_a_replay_that_tries_everything_reaches_nothing(tmp_path, sealed):
    config, outcome, maker, names, audit_before = await _try_everything(tmp_path)
    record = outcome.record
    [client] = maker.clients
    attempted = [name for name, _, _ in client.results]
    assert len(attempted) == len(names) + len(BUILTINS)

    # No tool originated an HTTP request.
    assert outcome.transport.attempts == []
    assert record["isolation"]["refused_tool_requests"] == 0
    # No channel send (the gate's prompt path included).
    assert outcome.channel.attempts == []
    assert record["isolation"]["channel_attempts"] == 0
    # No audit line: the file is byte-identical.
    assert Path(config.audit.path).read_bytes() == audit_before
    # No store file was opened (Store and sqlite3.connect are sealed above).
    assert not Path(config.store.path).exists()

    # Every mutating attempt was denied out of scope, recorded in the run output.
    mutating = [n for n in names
                if outcome.factory.registry.get(n).tool_class is ToolClass.MUTATING]
    assert set(mutating) == {"store_memory", "capture", "remind", "cancel_reminder"}
    decisions = record["authorization_decisions"]
    assert sorted(d["tool"] for d in decisions) == sorted(mutating)
    assert {d["outcome"] for d in decisions} == {ApprovalOutcome.OUT_OF_SCOPE.value}
    assert {d["turn_type"] for d in decisions} == {TurnType.EVENT.value}
    by_name = {c["name"]: c for c in record["tool_calls"]}
    for name in mutating:
        assert by_name[name]["served"] == "denied"
        assert by_name[name]["is_error"] is True
    # The handoff and the notification are in the run output, and nowhere else.
    assert [h["document"] for h in record["captured_handoffs"]] == ["tried to publish"]
    assert [n["message"] for n in record["captured_notifications"]] == ["tried to notify"]
    # Every built-in was blocked by the hook.
    for name in BUILTINS:
        assert by_name[name]["served"] == "blocked"
        assert "closed-toolset hook" in by_name[name]["result"]


async def test_a_stub_that_reached_its_real_tool_would_be_seen(tmp_path):
    """The transport check can fail: a real tool over the replay's client raises
    and is counted, so a zero count is evidence."""
    config = make_config(tmp_path)
    transport = harness.RefusingTransport()
    client = httpx.AsyncClient(transport=transport)
    registry = harness.build_definition_registry(config, client)
    result = await registry.get("notify").run(message="x")
    assert not result.ok
    assert len(transport.attempts) == 1
    await client.aclose()


async def test_the_gate_is_framed_as_a_tainted_non_announceable_event_turn(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    seen = []

    class Peek(SessionMaker):
        def __call__(self, factory):
            session = super().__call__(factory)
            original = session.run_turn

            async def run_turn(text):
                seen.append(factory.gate.turn_context)
                return await original(text)

            session.run_turn = run_turn
            return session

    outcome = await run_mod.run_replay(
        config, run_mod.resolve_source(config, rid), model=OTHER_MODEL,
        effort="high", thinking=None, create_session=Peek(), clock=lambda: float(T))
    [context] = seen
    assert context.turn_type is TurnType.EVENT
    assert context.announceable is False
    assert context.tainted is True
    # Cleared on the way out.
    assert outcome.factory.gate.turn_context is None


async def test_the_hook_still_blocks_built_ins(tmp_path):
    config = make_config(tmp_path)
    rid = write_recording(config, live_recording([]))
    maker = SessionMaker([("Bash", {"command": "id"})])
    outcome = await run_mod.run_replay(
        config, run_mod.resolve_source(config, rid), model=OTHER_MODEL,
        effort="high", thinking=None, create_session=maker, clock=lambda: float(T))
    factory = outcome.factory
    # The production factory and its production hook, not a replay copy.
    assert type(factory) is SdkSessionFactory
    assert factory.profile == EVENT_PROFILE
    assert factory.config.allowed_tools == ()
    assert factory.config.disallowed_tools == tuple(BUILTIN_HOST_TOOLS)
    assert factory.config.permission_mode == "default"
    hook = factory._build_pretooluse_hook()
    for name in ("Bash", "ToolSearch", "TaskCreate", "mcp__other__x"):
        assert await hook({"tool_name": name}, "tu-x", None) == \
            pretooluse_block_decision(name)
        assert pretooluse_block_decision(name) is not None
    assert await hook({"tool_name": "mcp__henk__homelab_health"}, "tu-y", None) == {}
    [client] = maker.clients
    [(_, text, is_error)] = client.results
    assert is_error is True and "blocked by the closed-toolset hook" in text
    assert outcome.record["tool_calls"][0]["served"] == "blocked"


def test_the_replay_registry_carries_the_current_definitions(tmp_path):
    config = make_config(tmp_path, everything=True)
    client = httpx.AsyncClient(transport=harness.RefusingTransport())
    definitions = harness.build_definition_registry(config, client)
    server = harness.ReplayServer(live_recording([]))
    replay = harness.build_replay_registry(definitions, server)
    assert replay.names() == definitions.names()
    assert tool_definitions_hash(replay.tools()) == tool_definitions_hash(definitions.tools())
    for tool in replay.tools():
        real = definitions.get(tool.name)
        assert isinstance(tool, harness.ReplayStub)
        assert (tool.tool_class, tool.authorization, tuple(tool.turn_scope)) == (
            real.tool_class, real.authorization, tuple(real.turn_scope))


async def test_a_mutating_stub_never_executes_even_past_the_gate(tmp_path):
    server = harness.ReplayServer(live_recording([
        ("store_memory", {"content": "x"}, "Remembered #1")]))
    result = server.serve("store_memory", ToolClass.MUTATING, {"content": "x"})
    assert not result.ok and "not executed in replay" in result.error


async def test_the_null_channel_refuses_and_counts():
    channel = harness.NullChannel()
    with pytest.raises(harness.ReplayIsolationError):
        await channel.send("approve?")
    with pytest.raises(harness.ReplayIsolationError):
        await channel.send_proactive("hello")
    assert len(channel.attempts) == 2


def test_the_refusing_stores_refuse_every_use():
    stores = harness.RefusingStores()
    for name in ("store", "memories", "inbox", "reminders", "handoffs"):
        with pytest.raises(harness.ReplayIsolationError):
            getattr(getattr(stores, name), "add")


# --- The import graph (11.3) -----------------------------------------------------

#: The modules a replay must never load: the channel adapter and its transport,
#: event intake with its ntfy stream, the coordinator, the pipeline, the
#: checkpoint, the audit writer, the scheduler, and the live runtime wiring.
FORBIDDEN = (
    "henk.channel.signal",
    "henk.events.intake",
    "henk.events.coordinator",
    "henk.events.pipeline",
    "henk.events.checkpoint",
    "henk.audit",
    "henk.reminders.scheduler",
    "henk.runtime",
    "henk.app",
    "henk.__main__",
)
#: The only channel modules the core's types pull in: protocol and allowlist types,
#: no adapter.
ALLOWED_CHANNEL = {"henk.channel", "henk.channel.base", "henk.channel.allowlist"}


def test_the_replay_imports_no_channel_intake_or_ntfy_module():
    code = (
        "import json, sys\n"
        "import henk.replay.__main__, henk.replay.run, henk.replay.harness, "
        "henk.replay.case\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('henk'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=REPO_ROOT, capture_output=True,
        text=True, check=True,
    )
    loaded = set(json.loads(result.stdout.strip().splitlines()[-1]))
    assert "henk.replay.run" in loaded
    for module in FORBIDDEN:
        assert not any(m == module or m.startswith(module + ".") for m in loaded), module
    assert {m for m in loaded if m.startswith("henk.channel")} <= ALLOWED_CHANNEL
