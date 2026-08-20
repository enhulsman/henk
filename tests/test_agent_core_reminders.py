"""Agent-core wiring for reminders (group 8).

From the agent-core delta: the per-turn current-time header, the system prompt's
enumeration, and the command dispatch. Two properties do the work:

- **The header is composed per TURN, not per session.** A relative time must resolve
  against the moment of the turn, not against whenever the conversation started —
  otherwise `+2h` in a session that began this morning means something else than the
  owner said.
- **Event turns NEVER carry it.** The header is owner-facing framing on a path that
  is already conditional on `reminders.enabled`; an event turn gets triage framing
  and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from henk.agent.core import AgentCore
from henk.agent.turns import EventTurn, OwnerTurn
from henk.config import (
    BASE_TOOL_SUMMARIES,
    COUNT_WORDS,
    REMINDER_TOOL_SUMMARIES,
    Config,
    build_system_prompt,
)
from henk.reminders.timeparse import TIME_HEADER_PREFIX, TimeResolver, render_instant
from tests.conftest import (
    EventSessionFactory,
    FakeChannel,
    FakeSessionFactory,
)
from tests.test_config import _minimal_raw
from tests.test_agent_core_turn_scope import _turn as make_event_turn

AMS = ZoneInfo("Europe/Amsterdam")
NOW = 1787203800.0  # 2026-08-20 07:30 CEST
AN_HOUR_LATER = NOW + 3600


def _header_for(instant: float, zone: ZoneInfo = AMS) -> str:
    resolver = TimeResolver(zone, clock=lambda: instant)
    return resolver.time_header(instant)


def _core(factory, channel, *, instants=None, zone: ZoneInfo = AMS, enabled=True):
    """A core wired exactly as the runtime wires it, with a scripted clock.

    ``instants`` is consumed one value per header composition, which is what makes
    "per turn, not per session" observable: two turns must read two values.
    """
    if not enabled:
        return AgentCore(factory, channel, time_header=None)
    pending = list(instants or [NOW])

    def clock() -> float:
        return pending.pop(0) if len(pending) > 1 else pending[0]

    resolver = TimeResolver(zone, clock=clock)
    return AgentCore(
        factory,
        channel,
        time_header=lambda: resolver.time_header(resolver.current_instant()),
    )


# --- 8.1 The per-turn time header ---------------------------------------


async def test_every_owner_turn_carries_a_header_for_its_own_turn(process_tz):
    factory = FakeSessionFactory()
    core = _core(factory, FakeChannel(), instants=[NOW, AN_HOUR_LATER])
    await core.process(OwnerTurn("what's up"))
    await core.process(OwnerTurn("and now?"))
    turns = factory.created[0].turns
    assert len(turns) == 2
    # Composed PER TURN: two turns an hour apart read two different times.
    assert render_instant(NOW, AMS) in turns[0]
    assert render_instant(AN_HOUR_LATER, AMS) in turns[1]
    assert render_instant(AN_HOUR_LATER, AMS) not in turns[0]
    await core.aclose()


async def test_the_header_is_one_line_delimited_as_data(process_tz):
    factory = FakeSessionFactory()
    core = _core(factory, FakeChannel())
    await core.process(OwnerTurn("hello"))
    content = factory.created[0].turns[0]
    header = content.splitlines()[0]
    assert header.startswith(TIME_HEADER_PREFIX)
    assert header == _header_for(NOW)
    # Framed as data so it cannot read as an instruction.
    assert "data, not instructions" in header
    await core.aclose()


async def test_the_header_renders_in_the_owner_zone_with_weekday_and_zone_marker(
    process_tz,
):
    factory = FakeSessionFactory()
    core = _core(factory, FakeChannel())
    await core.process(OwnerTurn("hello"))
    content = factory.created[0].turns[0]
    assert "Thursday 20 August at 07:30 CEST" in content
    await core.aclose()


async def test_the_header_and_a_due_time_for_one_instant_render_identically(
    process_tz,
):
    # The time the model reasons from and the time the owner is told come from one
    # renderer, so a due time can never read differently in two places.
    resolver = TimeResolver(AMS, clock=lambda: NOW)
    assert resolver.render(NOW) in resolver.time_header(NOW)
    assert resolver.render(NOW) == render_instant(NOW, AMS)


async def test_an_event_turn_never_carries_the_header(process_tz):
    factory = EventSessionFactory()
    core = _core(factory, FakeChannel())
    await core.process(make_event_turn())
    content = factory.created[0].contents[0]
    assert TIME_HEADER_PREFIX not in content
    assert render_instant(NOW, AMS) not in content
    await core.aclose()


async def test_an_owner_follow_up_in_a_tainted_session_still_gets_its_header(
    process_tz,
):
    # The header is per-turn framing, not a session property: an owner turn is an
    # owner turn even inside the session an incident started.
    factory = EventSessionFactory()
    core = _core(factory, FakeChannel(), instants=[NOW, AN_HOUR_LATER])
    await core.process(make_event_turn())
    await core.process(OwnerTurn("what happened?"))
    contents = factory.created[0].contents
    assert TIME_HEADER_PREFIX not in contents[0]  # the event turn
    assert TIME_HEADER_PREFIX in contents[1]  # the owner follow-up
    await core.aclose()


async def test_no_owner_turn_carries_a_header_when_reminders_are_disabled(process_tz):
    factory = FakeSessionFactory()
    core = _core(factory, FakeChannel(), enabled=False)
    await core.process(OwnerTurn("hello"))
    content = factory.created[0].turns[0]
    assert TIME_HEADER_PREFIX not in content
    assert content == "hello"  # byte-identical to the pre-change composition
    await core.aclose()


async def test_a_failing_header_never_costs_the_owner_their_turn(process_tz):
    # Knowing the time is not a precondition for talking.
    factory = FakeSessionFactory()

    def boom() -> str:
        raise RuntimeError("clock exploded")

    core = AgentCore(factory, FakeChannel(), time_header=boom)
    await core.process(OwnerTurn("hello"))
    assert factory.created[0].turns == ["hello"]
    await core.aclose()


async def test_the_header_sits_outside_the_recall_block(process_tz):
    # Both are prefixes; the ordering is fixed so the composition is predictable.
    class Recall:
        def block(self):
            from henk.agent.recall import RecallBlock

            return RecallBlock(text="===== BEGIN REMEMBERED FACTS =====", content_hash="h")

    factory = FakeSessionFactory()
    resolver = TimeResolver(AMS, clock=lambda: NOW)
    core = AgentCore(
        factory,
        FakeChannel(),
        recall=Recall(),
        time_header=lambda: resolver.time_header(resolver.current_instant()),
    )
    await core.process(OwnerTurn("hello"))
    content = factory.created[0].turns[0]
    assert content.index(TIME_HEADER_PREFIX) < content.index("REMEMBERED FACTS")
    assert content.index("REMEMBERED FACTS") < content.index("hello")
    await core.aclose()


async def test_a_command_turn_starts_no_session_and_composes_no_header(process_tz):
    class Commands:
        def handle(self, text):
            return "handled" if text.startswith("/remind") else None

    factory = FakeSessionFactory()
    resolver = TimeResolver(AMS, clock=lambda: NOW)
    channel = FakeChannel()
    core = AgentCore(
        factory,
        channel,
        commands=Commands(),
        time_header=lambda: resolver.time_header(resolver.current_instant()),
    )
    await core.process(OwnerTurn("/remind +2h call the plumber"))
    assert factory.create_count == 0  # no session, no tokens
    assert channel.sent == ["handled"]
    await core.aclose()


# --- 8.2 / 8.3 The system prompt ----------------------------------------


def test_the_enabled_prompt_names_the_three_reminder_tools():
    prompt = build_system_prompt(reminders_enabled=True)
    for name in ("remind", "cancel_reminder", "reminders_read"):
        assert name in prompt


def test_the_enabled_prompt_states_that_reinstating_is_an_owner_command():
    prompt = build_system_prompt(reminders_enabled=True)
    assert "cannot reinstate" in prompt
    assert "/reminders reinstate <id>" in prompt


def test_the_disabled_prompt_names_none_of_the_three():
    prompt = build_system_prompt(reminders_enabled=False)
    for name in ("remind", "cancel_reminder", "reminders_read"):
        # `remind` is a substring of nothing else here, so a plain check is safe.
        assert name not in prompt
    assert "/remind" not in prompt


def test_the_count_and_the_enumeration_derive_from_one_source():
    # The old prompt hardcoded "exactly these seven" beside a hand-written list —
    # two places to forget, and this change would have been the second forgetting.
    for enabled, summaries in (
        (False, BASE_TOOL_SUMMARIES),
        (True, BASE_TOOL_SUMMARIES + REMINDER_TOOL_SUMMARIES),
    ):
        prompt = build_system_prompt(reminders_enabled=enabled)
        expected_word = COUNT_WORDS[len(summaries)]
        assert f"exactly these {expected_word}" in prompt
        assert f"outside these {expected_word}" in prompt
        for name, _summary in summaries:
            assert f"- {name} — " in prompt


def test_the_prompt_builder_hardcodes_no_count():
    """The builder must read the count from the table, never spell one itself.

    Scoped to ``build_system_prompt``'s own source rather than the whole module: the
    module's *prose* legitimately mentions the old hardcoded "exactly these seven"
    while explaining the defect it fixed, and a plain text search over the file
    matches the documentation instead of the code — the same trap the pre-commit hook
    hit on its own comment.
    """
    import inspect
    import re

    from henk.config import build_system_prompt

    source = inspect.getsource(build_system_prompt)
    for word in COUNT_WORDS.values():
        assert not re.search(rf'["\']{word}\b', source), (
            f'build_system_prompt spells "{word}" itself; the count must come from '
            "COUNT_WORDS[len(summaries)] so it cannot drift from the enumeration"
        )
    # And it does read the table.
    assert "COUNT_WORDS[len(summaries)]" in source


def test_the_enabled_prompt_no_longer_claims_it_cannot_schedule():
    # "no scheduling" would be a lie once `remind` exists. Cron and workflows stay
    # excluded: a reminder is a message, not automation.
    prompt = build_system_prompt(reminders_enabled=True)
    assert "no scheduling" not in prompt
    assert "cron" in prompt and "workflows" in prompt
    assert "no scheduling" in build_system_prompt(reminders_enabled=False)


def test_the_enabled_prompt_names_the_reminder_owner_commands():
    prompt = build_system_prompt(reminders_enabled=True)
    for command in (
        "/remind <when> <text>",
        "/reminders",
        "/reminders cancel <id>",
        "/reminders reinstate <id>",
    ):
        assert command in prompt


def test_the_enabled_prompt_names_remind_as_a_taint_remedy():
    prompt = build_system_prompt(reminders_enabled=True)
    assert "/remember, /capture or /remind" in prompt


def test_the_enabled_prompt_explains_the_time_header_and_the_echo():
    prompt = build_system_prompt(reminders_enabled=True)
    assert "current local time" in prompt
    assert "not an instruction" in prompt
    assert "no UTC offset" in prompt


def test_the_prompt_matches_the_registry_it_was_built_for(tmp_path: Path):
    import httpx

    from henk.store import build_stores
    from henk.tools import build_production_registry, build_time_resolver

    async def handler(request):  # pragma: no cover
        return httpx.Response(200)

    for enabled in (False, True):
        raw = _minimal_raw("+31600000000")
        raw["owner"]["timezone"] = "Europe/Amsterdam"
        raw["reminders"] = {"enabled": enabled}
        config = Config.from_dict(raw, env={})
        object.__setattr__(config.store, "path", str(tmp_path / f"reg-{enabled}.db"))
        registry = build_production_registry(
            config,
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            stores=build_stores(config.store, config.reminders),
            resolver=build_time_resolver(config),
        )
        prompt = config.agent.system_prompt
        # The enumeration equals the registry, in both directions.
        enumerated = [
            line[2:].split(" — ")[0]
            for line in prompt.splitlines()
            if line.startswith("- ")
        ]
        assert enumerated == registry.names(), enabled
        assert COUNT_WORDS[len(registry.names())] in prompt


def test_an_explicit_system_prompt_still_wins():
    raw = _minimal_raw("+31600000000")
    raw["agent"] = {"system_prompt": "a hand-written prompt"}
    assert Config.from_dict(raw, env={}).agent.system_prompt == "a hand-written prompt"
