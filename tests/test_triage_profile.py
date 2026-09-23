"""The triage profile: a second session factory for event sessions (group 9).

From `specs/agent-core` (*Event sessions run on the triage profile*: *Defaults change
nothing*, *A configured triage profile applies to event sessions only*, *Follow-ups
stay on the triage profile*, *Reset returns to the chat profile*, *Profiles share the
boundary*, *The record names the profile*, *A follow-up record names the factory's
profile, not its trigger*, *A plain owner session records the chat profile*),
`specs/audit-log` (v5 `profile`/`effort`) and design D11. Tasks 9.1 and the 2.1
re-assertion group 2 deferred: through the REAL event factory `runtime.py` builds.

Every "event follows chat" case is built from a `Config.from_dict` dict, never with
`dataclasses.replace` on a built config, which does not re-inherit (the comment on
`AgentConfig.__post_init__`).

Placeholders only (standing rule 1). No model call (standing rule 5): the real
factories' `create()` is replaced by one returning a fake session.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from henk.agent.core import AgentCore
from henk.agent.sdk_session import SdkSessionFactory, reasoning_options
from henk.agent.session import CHAT_PROFILE, EVENT_PROFILE
from henk.agent.turns import EventTurn, EventTurnItem
from henk.audit import AUDIT_SCHEMA_PATH, AuditLog
from henk.config import Config
from henk.events.identity import derive_identity
from henk.events.types import Event
from henk.runtime import build_runtime
from henk.tools.base import ToolRegistry
from tests.conftest import (
    TRIAGE_REPLY,
    EventSession,
    EventSessionFactory,
    FakeChannel,
    handoff_stats,
    make_clock,
)
from tests.test_config import SAMPLE

SCHEMA = json.loads(AUDIT_SCHEMA_PATH.read_text())

TITLE = "[FIRING:1] HenkDiskFull henk (host-a.example)"
MESSAGE = "alertname = HenkDiskFull\nhost = host-a.example\nnode = vps"


def _turn(eid: str = "e1", *, announceable: bool = True) -> EventTurn:
    event = Event(id=eid, title=TITLE, message=MESSAGE, arrival_time=0.0)
    item = EventTurnItem(event=event, identity=derive_identity(event))
    return EventTurn(items=(item,), announceable=announceable)


def _records(path: Path) -> list[dict]:
    return [
        r
        for r in (json.loads(line) for line in path.read_text().splitlines() if line.strip())
        if r.get("record_type") == "session"
    ]


class ProfiledFactory(EventSessionFactory):
    """A fake factory carrying the profile, effort and model a real one does."""

    def __init__(self, profile: str, effort: str | None, model: str, **kw) -> None:
        super().__init__(**kw)
        self.profile = profile
        self.effort = effort
        self.model = model


def _chat(**kw) -> ProfiledFactory:
    return ProfiledFactory(CHAT_PROFILE, "high", "claude-sonnet-5", **kw)


def _event(**kw) -> ProfiledFactory:
    return ProfiledFactory(EVENT_PROFILE, "max", "claude-opus-5-5", **kw)


def _core(chat, event, *, audit=None, clock=None, channel=None, **kw) -> AgentCore:
    return AgentCore(
        chat,
        channel or FakeChannel(),
        clock=clock or make_clock([0]),
        audit=audit,
        event_factory=event,
        **kw,
    )


# --- The runtime's real factories -------------------------------------------


def _sample_raw(tmp_path: Path | None = None, agent: dict | None = None) -> dict:
    raw = yaml.safe_load(SAMPLE.read_text())
    if agent is not None:
        raw["agent"] = agent
    if tmp_path is not None:
        audit = str(tmp_path / "henk-audit.jsonl")
        raw["audit"]["path"] = audit
        raw["events"]["audit_path"] = audit
        raw["store"]["path"] = str(tmp_path / "henk-store.db")
    return raw


async def _runtime(raw: dict):
    app, client = build_runtime(Config.from_dict(raw, env={}))
    return app, client


@pytest.mark.parametrize(
    "agent",
    [
        None,  # the sample as shipped: event keys commented out
        {},  # an empty agent section
        {"model": "claude-opus-5-5", "effort": "xhigh", "thinking": "disabled"},
        {"model": "claude-haiku-5", "effort": "low"},
        {"effort": None, "thinking": None},
    ],
)
async def test_defaults_change_nothing_through_the_runtime_event_factory(agent):
    # agent-core *Defaults change nothing*, re-asserted (task 2.1 carry-forward)
    # through the event factory runtime.py builds, not a test-local helper.
    raw = _sample_raw(agent=agent)
    assert not {"event_model", "event_effort", "event_thinking"} & set(raw["agent"] or {})
    app, client = await _runtime(raw)
    try:
        chat = app._core._factory
        event = app._core._event_factory
        assert isinstance(event, SdkSessionFactory)
        assert event is not chat
        assert event.config == chat.config
        assert (event.config.model, event.config.effort, event.config.thinking) == (
            chat.config.model,
            chat.config.effort,
            chat.config.thinking,
        )
        assert reasoning_options(event.config) == reasoning_options(chat.config)
        if agent and "model" in agent:
            assert event.config.model == agent["model"]
        if agent and "effort" in agent:
            assert event.config.effort == agent["effort"]
    finally:
        await client.aclose()


async def test_the_runtime_builds_the_configured_triage_profile():
    raw = _sample_raw(
        agent={
            "model": "claude-sonnet-5",
            "effort": "high",
            "thinking": "adaptive",
            "event_model": "claude-opus-5-5",
            "event_effort": "max",
            "event_thinking": "disabled",
        }
    )
    app, client = await _runtime(raw)
    try:
        chat, event = app._core._factory, app._core._event_factory
        assert (chat.config.model, chat.config.effort, chat.config.thinking) == (
            "claude-sonnet-5", "high", "adaptive",
        )
        assert (event.config.model, event.config.effort, event.config.thinking) == (
            "claude-opus-5-5", "max", "disabled",
        )
        # Effort is passed to the SDK explicitly on both (D11 Opus 5.5 notes).
        assert reasoning_options(event.config)["effort"] == "max"
        assert reasoning_options(chat.config)["effort"] == "high"
        assert chat.profile == CHAT_PROFILE and event.profile == EVENT_PROFILE
        assert chat.effort == "high" and event.effort == "max"
    finally:
        await client.aclose()


async def test_profiles_share_the_boundary():
    # agent-core *Profiles share the boundary*: the same registry and gate
    # OBJECTS, and configurations differing at most in model, effort, thinking.
    raw = _sample_raw(
        agent={"event_model": "claude-opus-5-5", "event_effort": "max",
               "event_thinking": "disabled"}
    )
    app, client = await _runtime(raw)
    try:
        chat, event = app._core._factory, app._core._event_factory
        assert event.registry is chat.registry
        assert event.gate is chat.gate
        # ... and it is the gate the core frames turns for, not a second one.
        assert chat.gate is app._core._gate
        assert event.config != chat.config
        assert dataclasses.replace(
            event.config,
            model=chat.config.model,
            effort=chat.config.effort,
            thinking=chat.config.thinking,
        ) == chat.config
    finally:
        await client.aclose()


def _patch_creates(app) -> dict[str, list]:
    """Replace both real factories' create() with one returning a fake session
    tagged by the factory's own config, so no SDK is needed."""
    made: dict[str, list] = {"chat": [], "event": []}
    for name, factory in (("chat", app._core._factory), ("event", app._core._event_factory)):
        def create(factory=factory, name=name):
            session = EventSession(TRIAGE_REPLY, handoff_stats("hf-1", model=factory.config.model))
            made[name].append(session)
            return session

        factory.create = create
    return made


async def test_a_configured_triage_profile_applies_to_event_sessions_only(tmp_path):
    # agent-core *A configured triage profile applies to event sessions only*,
    # through the runtime's real factories and audit log.
    raw = _sample_raw(
        tmp_path,
        agent={"model": "claude-sonnet-5", "effort": "high",
               "event_model": "claude-opus-5-5", "event_effort": "max"},
    )
    app, client = await _runtime(raw)
    try:
        core = app._core
        core._channel = FakeChannel()
        made = _patch_creates(app)
        await core.process(_turn())
        assert (len(made["event"]), len(made["chat"])) == (1, 0)
        await core.process("/new")
        await core.process("a fresh owner question")
        assert (len(made["event"]), len(made["chat"])) == (1, 1)
        await core.aclose()
        triage, owner = _records(tmp_path / "henk-audit.jsonl")
        assert (triage["trigger"], triage["profile"], triage["effort"]) == (
            "event", EVENT_PROFILE, "max",
        )
        assert triage["model"] == "claude-opus-5-5"
        assert (owner["trigger"], owner["profile"], owner["effort"]) == (
            "owner-message", CHAT_PROFILE, "high",
        )
        assert owner["model"] == "claude-sonnet-5"
        for record in (triage, owner):
            jsonschema.validate(record, SCHEMA)
    finally:
        await client.aclose()


# --- Routing through the core ----------------------------------------------


async def test_event_turns_use_the_event_factory_and_owner_turns_the_chat_one():
    chat, event = _chat(), _event()
    core = _core(chat, event, clock=make_clock([0, 0, 1, 1]))
    await core.process("owner first")
    await core.process(_turn())
    assert (chat.create_count, event.create_count) == (1, 1)


async def test_follow_ups_stay_on_the_triage_profile(tmp_path):
    # agent-core *Follow-ups stay on the triage profile*: the owner's reply inside
    # the idle window continues the triage session, which the event factory made.
    chat, event = _chat(), _event()
    audit = AuditLog(tmp_path / "a.jsonl")
    core = _core(chat, event, audit=audit, clock=make_clock([0, 0, 1, 1]))
    await core.process(_turn())
    await core.process("what does the log say?")
    assert (chat.create_count, event.create_count) == (0, 1)
    assert len(event.created[0].contents) == 2  # the same session, both turns
    await core.aclose()
    triage, follow_up = _records(tmp_path / "a.jsonl")
    # agent-core *A follow-up record names the factory's profile, not its trigger*.
    assert follow_up["trigger"] == "owner-message"
    assert (follow_up["profile"], follow_up["effort"]) == (EVENT_PROFILE, "max")
    # With no stream model, the follow-up's record falls back to the model of
    # the factory that made the session, not the chat default.
    assert follow_up["model"] == "claude-opus-5-5"
    assert (triage["profile"], triage["effort"]) == (EVENT_PROFILE, "max")
    # audit-log *Non-triage records carry null evidence links* (continuation).
    assert follow_up["prior_handoff_ids"] is None
    for record in (triage, follow_up):
        jsonschema.validate(record, SCHEMA)


async def test_reset_returns_to_the_chat_profile(tmp_path):
    # agent-core *Reset returns to the chat profile*.
    chat, event = _chat(), _event()
    audit = AuditLog(tmp_path / "a.jsonl")
    core = _core(chat, event, audit=audit, clock=make_clock([0, 0, 1, 1, 2, 2]))
    await core.process(_turn())
    await core.process("/new")
    await core.process("hello again")
    assert (chat.create_count, event.create_count) == (1, 1)
    await core.aclose()
    records = _records(tmp_path / "a.jsonl")
    assert [(r["trigger"], r["profile"], r["effort"]) for r in records] == [
        ("event", EVENT_PROFILE, "max"),
        ("owner-message", CHAT_PROFILE, "high"),
    ]


async def test_idle_expiry_also_returns_to_the_chat_profile(tmp_path):
    chat, event = _chat(), _event()
    audit = AuditLog(tmp_path / "a.jsonl")
    # The triage at t=0, the owner message after the idle window.
    core = _core(chat, event, audit=audit, clock=make_clock([0, 0, 7200, 7200]),
                 idle_timeout_seconds=3600)
    await core.process(_turn())
    await core.process("later question")
    assert (chat.create_count, event.create_count) == (1, 1)
    await core.aclose()
    records = _records(tmp_path / "a.jsonl")
    assert [(r["trigger"], r["profile"]) for r in records] == [
        ("event", EVENT_PROFILE),
        ("owner-message", CHAT_PROFILE),
    ]


async def test_a_new_incident_after_an_owner_session_uses_the_event_factory(tmp_path):
    chat, event = _chat(), _event()
    audit = AuditLog(tmp_path / "a.jsonl")
    core = _core(chat, event, audit=audit, clock=make_clock([0, 0, 1, 1, 2, 2]))
    await core.process("owner first")
    await core.process(_turn("a"))
    await core.process(_turn("b"))
    assert (chat.create_count, event.create_count) == (1, 2)
    await core.aclose()
    assert [r["profile"] for r in _records(tmp_path / "a.jsonl")] == [
        CHAT_PROFILE, EVENT_PROFILE, EVENT_PROFILE,
    ]


# --- The record names the profile -----------------------------------------


async def test_the_record_names_the_profile(tmp_path):
    # agent-core *The record names the profile*.
    chat, event = _chat(), _event(stats=handoff_stats("hf-1", model="claude-opus-5-5"))
    audit = AuditLog(tmp_path / "a.jsonl")
    core = _core(chat, event, audit=audit)
    await core.process(_turn())
    (record,) = _records(tmp_path / "a.jsonl")
    assert (record["profile"], record["effort"]) == (EVENT_PROFILE, "max")
    jsonschema.validate(record, SCHEMA)


async def test_a_plain_owner_session_records_the_chat_profile(tmp_path):
    # agent-core *A plain owner session records the chat profile*.
    chat, event = _chat(), _event()
    audit = AuditLog(tmp_path / "a.jsonl")
    core = _core(chat, event, audit=audit, clock=make_clock([0, 0]))
    await core.process("just a question")
    await core.aclose()
    (record,) = _records(tmp_path / "a.jsonl")
    assert (record["trigger"], record["profile"], record["effort"]) == (
        "owner-message", CHAT_PROFILE, "high",
    )
    assert record["prior_handoff_ids"] is None and record["recording_id"] is None
    jsonschema.validate(record, SCHEMA)


async def test_a_null_effort_is_recorded_as_null_not_a_default(tmp_path):
    # An explicit null effort defers to the CLI; the record must not invent one.
    chat = ProfiledFactory(CHAT_PROFILE, "high", "claude-sonnet-5")
    event = ProfiledFactory(EVENT_PROFILE, None, "claude-opus-5-5")
    audit = AuditLog(tmp_path / "a.jsonl")
    core = _core(chat, event, audit=audit)
    await core.process(_turn())
    (record,) = _records(tmp_path / "a.jsonl")
    assert (record["profile"], record["effort"]) == (EVENT_PROFILE, None)


async def test_the_event_factory_model_is_the_record_fallback(tmp_path):
    # A triage that raised before any assistant message has no stats model; the
    # record must name the model the EVENT factory runs, not the chat model.
    chat, event = _chat(), _event()

    async def boom(text):
        raise RuntimeError("placeholder failure")

    original = event.create

    def create():
        session = original()
        session.run_turn = boom
        return session

    event.create = create
    audit = AuditLog(tmp_path / "a.jsonl")
    core = _core(chat, event, audit=audit, model="claude-sonnet-5")
    await core.process(_turn(announceable=False))
    (record,) = _records(tmp_path / "a.jsonl")
    assert record["model"] == "claude-opus-5-5"


# --- Fakes without a profile, and no event factory --------------------------


async def test_a_factory_without_a_profile_records_chat_and_null_effort(tmp_path):
    # D11: "Fakes that do not carry `profile` default to `chat`."
    factory = EventSessionFactory()
    audit = AuditLog(tmp_path / "a.jsonl")
    core = AgentCore(factory, FakeChannel(), clock=make_clock([0]), audit=audit)
    await core.process(_turn())
    (record,) = _records(tmp_path / "a.jsonl")
    assert (record["profile"], record["effort"]) == (CHAT_PROFILE, None)


async def test_without_an_event_factory_event_sessions_use_the_chat_factory(tmp_path):
    # The event factory is optional; its absence keeps the single-factory core
    # exactly as before, and the record names the factory that made the session.
    chat = _chat()
    audit = AuditLog(tmp_path / "a.jsonl")
    core = AgentCore(chat, FakeChannel(), clock=make_clock([0]), audit=audit)
    await core.process(_turn())
    assert chat.create_count == 1
    (record,) = _records(tmp_path / "a.jsonl")
    assert (record["profile"], record["effort"]) == (CHAT_PROFILE, "high")


def test_the_sdk_factory_defaults_to_the_chat_profile():
    factory = SdkSessionFactory(ToolRegistry(), None, model="claude-sonnet-5",
                                system_prompt="p", effort="high")
    assert factory.profile == CHAT_PROFILE
    assert factory.effort == "high"
    assert factory.model == "claude-sonnet-5"


def test_the_sdk_factory_refuses_an_unknown_profile():
    with pytest.raises(ValueError):
        SdkSessionFactory(ToolRegistry(), None, model="m", system_prompt="p",
                          profile="triage")
