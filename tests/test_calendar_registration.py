"""maintenance-calendar group 3: registration behind the flag, and the prompt rule.

From the maintenance-calendar spec: *The calendar capability is disabled by default
and inert when off* (registry unchanged, no Google request), *Only owner-reviewed
Homelab events are admitted* (the empty-allowlist startup WARNING), *Henk does not
propose calendar events on his own initiative* (the system-prompt rule, and no
event or scheduler path that names the create tool). Tasks 3.4 (registration) and
3.5.

No live Google call: every request goes to an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import pytest

import henk
from henk.calendar.client import CalendarClient
from henk.config import (
    BASE_TOOL_SUMMARIES,
    CALENDAR_TOOL_SUMMARIES,
    COUNT_WORDS,
    DOCS_TOOL_SUMMARIES,
    QUERY_TOOL_SUMMARIES,
    REMINDER_TOOL_SUMMARIES,
    SESSIONS_TOOL_SUMMARIES,
    Config,
    build_system_prompt,
)
from henk.tools import build_calendar_client, build_production_registry
from henk.tools.base import AuthorizationTier, ToolClass, TurnType
from henk.tools.calendar import (
    CALENDAR_TOOL_NAMES,
    SHORT_WEEKDAYS,
    ApprovalReferences,
    CalendarCreateEventTool,
    CalendarUpcomingTool,
)
from tests.calendar_fixtures import (
    CALENDAR_ID,
    ENV_VAR,
    OWNER_EMAIL,
    enabled_raw,
    key_b64,
    minimal_raw,
)
from tests.test_calendar_client import FakeGoogle

#: The registry a minimal config produced before this change (the default
#: config: homelab_query on, everything else optional off).
PRE_CHANGE_NAMES = [
    "homelab_health",
    "todo_read",
    "notify",
    "publish_handoff",
    "store_memory",
    "capture",
    "inbox_read",
    "homelab_query",
]


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(599)


def _enabled_config(**personal_data) -> Config:
    raw = enabled_raw()
    if personal_data:
        raw["personal_data"] = personal_data
    return Config.from_dict(raw, env={ENV_VAR: key_b64()})


# --- Inert when off -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [minimal_raw(), minimal_raw(enabled=False), minimal_raw(enabled=False, calendar_id="x")],
    ids=["absent", "disabled", "disabled-with-an-id"],
)
async def test_registry_unchanged_when_disabled(raw):
    transport = RecordingTransport()
    async with httpx.AsyncClient(transport=transport) as client:
        config = Config.from_dict(raw, env={ENV_VAR: key_b64()})
        names = build_production_registry(config, client).names()
    assert names == PRE_CHANGE_NAMES
    assert not any(name.startswith("calendar") for name in names)
    assert transport.requests == []


def test_no_client_is_built_when_disabled():
    config = Config.from_dict(minimal_raw(), env={})
    assert build_calendar_client(config, httpx.AsyncClient()) is None


def test_disabled_calendar_logs_no_allowlist_warning(caplog):
    caplog.set_level(logging.WARNING)
    config = Config.from_dict(minimal_raw(), env={})
    build_production_registry(config, httpx.AsyncClient())
    assert "calendar_creator_allowlist" not in caplog.text


# --- Registered when on -------------------------------------------------------


async def test_enabled_registers_both_tools_after_every_other():
    transport = RecordingTransport()
    async with httpx.AsyncClient(transport=transport) as client:
        registry = build_production_registry(_enabled_config(), client)
    names = registry.names()
    assert names == PRE_CHANGE_NAMES + list(CALENDAR_TOOL_NAMES)
    upcoming = registry.get("calendar_upcoming")
    create = registry.get("calendar_create_event")
    assert isinstance(upcoming, CalendarUpcomingTool)
    assert upcoming.tool_class is ToolClass.READ_ONLY
    assert isinstance(create, CalendarCreateEventTool)
    assert create.tool_class is ToolClass.MUTATING
    assert create.authorization is AuthorizationTier.PER_INSTANCE
    assert tuple(create.turn_scope) == (TurnType.OWNER,)
    # Construction contacts nobody: no sign-in, no list.
    assert transport.requests == []


def test_build_calendar_client_builds_the_one_client_from_config():
    config = _enabled_config()
    client = build_calendar_client(config, httpx.AsyncClient())
    assert isinstance(client, CalendarClient)
    assert "placeholder" not in repr(client)


def test_a_passed_client_is_the_one_both_tools_use():
    config = _enabled_config()
    http = httpx.AsyncClient()
    shared = build_calendar_client(config, http)
    registry = build_production_registry(config, http, calendar_client=shared)
    assert registry.get("calendar_upcoming")._client is shared
    assert registry.get("calendar_create_event")._client is shared


def test_both_tools_share_one_client_when_none_is_passed():
    registry = build_production_registry(_enabled_config(), httpx.AsyncClient())
    assert (
        registry.get("calendar_upcoming")._client
        is registry.get("calendar_create_event")._client
    )


def test_the_runtime_receipts_and_references_reach_the_create_tool():
    from henk.audit import CalendarReceipts

    receipts = CalendarReceipts(None)
    references = ApprovalReferences(None)
    registry = build_production_registry(
        _enabled_config(),
        httpx.AsyncClient(),
        calendar_receipts=receipts,
        calendar_references=references,
    )
    create = registry.get("calendar_create_event")
    assert create._receipts is receipts
    assert create._references is references


def test_an_empty_creator_allowlist_warns_at_startup(caplog):
    caplog.set_level(logging.WARNING)
    build_production_registry(_enabled_config(), httpx.AsyncClient())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    calendar_warnings = [
        r for r in warnings if "calendar_creator_allowlist" in r.getMessage()
    ]
    assert len(calendar_warnings) == 1
    assert "only events Henk created" in calendar_warnings[0].getMessage()


def test_a_configured_allowlist_does_not_warn(caplog):
    caplog.set_level(logging.WARNING)
    config = _enabled_config(calendar_creator_allowlist=[OWNER_EMAIL])
    build_production_registry(config, httpx.AsyncClient())
    assert "calendar_creator_allowlist" not in caplog.text


def _future_slot(hours_ahead: int, length_hours: int) -> tuple[str, str]:
    zone = ZoneInfo("Europe/Amsterdam")
    start = (datetime.now(zone) + timedelta(days=7)).replace(
        hour=12, minute=0, second=0, microsecond=0, tzinfo=None
    ) + timedelta(hours=hours_ahead)
    end = start + timedelta(hours=length_hours)

    def wall(stamp: datetime) -> str:
        return (
            f"{SHORT_WEEKDAYS[stamp.weekday()]} {stamp.year:04d}-{stamp.month:02d}-"
            f"{stamp.day:02d} {stamp.hour:02d}:{stamp.minute:02d}"
        )

    return wall(start), wall(end)


async def test_the_registry_built_client_reads_and_writes_the_configured_calendar():
    # The wiring end to end: the configured calendar id, the creator allowlist,
    # and the owner's zone both for rendering and for the times sent to Google.
    config = _enabled_config(calendar_creator_allowlist=[OWNER_EMAIL])
    google = FakeGoogle()
    google.pages = [
        {
            "items": [
                {
                    "id": "evt-a",
                    "status": "confirmed",
                    "summary": "Homelab: patch round",
                    "start": {"dateTime": "2026-11-07T19:00:00Z"},
                    "end": {"dateTime": "2026-11-07T21:00:00Z"},
                    "creator": {"email": OWNER_EMAIL},
                }
            ]
        }
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(google.handler)) as http:
        registry = build_production_registry(config, http)
        listed = await registry.get("calendar_upcoming").run()
        start, end = _future_slot(0, 2)
        created = await registry.get("calendar_create_event").run(
            title="Homelab: x", start=start, end=end
        )
    assert listed.ok, listed.error
    assert (
        "Saturday 7 November at 20:00 CET – Saturday 7 November at 22:00 CET "
        "<<<Homelab: patch round>>>"
    ) in listed.content
    assert created.ok, created.error
    events_path = f"/calendars/{quote(CALENDAR_ID, safe='')}/events"
    for request in google.api_requests:
        assert request.url.raw_path.decode().split("?")[0].endswith(events_path)
    (insert,) = [r for r in google.api_requests if r.method == "POST"]
    body = json.loads(insert.content)
    zone = ZoneInfo("Europe/Amsterdam")
    expected = datetime.strptime(start[4:], "%Y-%m-%d %H:%M").replace(tzinfo=zone)
    assert body["start"] == {
        "dateTime": expected.isoformat(timespec="seconds"),
        "timeZone": "Europe/Amsterdam",
    }


@pytest.mark.parametrize("events_enabled", [False, True])
async def test_the_registry_hands_the_tools_the_events_flag(events_enabled):
    # The hold predicate needs events.enabled; a registry that passed a constant
    # would make the create result claim a hold the provider will not make.
    raw = enabled_raw()
    raw["events"] = {"enabled": events_enabled}
    config = Config.from_dict(raw, env={ENV_VAR: key_b64()})
    google = FakeGoogle()
    async with httpx.AsyncClient(transport=httpx.MockTransport(google.handler)) as http:
        registry = build_production_registry(config, http)
        start, end = _future_slot(0, 2)
        result = await registry.get("calendar_create_event").run(
            title="Homelab: x", start=start, end=end
        )
    assert result.ok, result.error
    assert ("Alerts to Henk are held" in result.content) is events_enabled


# --- The system-prompt rule (3.5) ---------------------------------------------


def test_the_system_prompt_carries_the_rule_when_enabled():
    prompt = build_system_prompt(calendar_enabled=True)
    assert "calendar events are created only at the owner's request" in prompt.lower()
    for name, summary in CALENDAR_TOOL_SUMMARIES:
        assert prompt.count(f"- {name} — {summary}\n") == 1


def test_the_rule_forbids_unprompted_suggestions_and_triage_writes():
    prompt = build_system_prompt(calendar_enabled=True).lower()
    assert "never send a message just to suggest" in prompt
    assert "never while triaging" in prompt


def test_the_disabled_prompt_names_no_calendar():
    for prompt in (build_system_prompt(), build_system_prompt(calendar_enabled=False)):
        assert "calendar" not in prompt.lower()


def test_the_flag_off_prompt_is_byte_identical_for_every_other_combination():
    for flags in _flag_combinations(include_calendar=False):
        assert build_system_prompt(**flags) == build_system_prompt(
            **flags, calendar_enabled=False
        )


def test_the_loader_wires_the_flag_from_the_calendar_section():
    enabled = _enabled_config()
    assert "only at the owner's request" in enabled.agent.system_prompt.lower()
    disabled = Config.from_dict(minimal_raw(enabled=False), env={})
    assert "calendar" not in disabled.agent.system_prompt.lower()


def test_the_calendar_drops_no_scheduling_from_the_exclusions():
    # Putting an event on a calendar is scheduling; "no scheduling" beside it is
    # the kind of small untruth the model then has to reconcile.
    assert "no scheduling" in build_system_prompt()
    assert "no scheduling" not in build_system_prompt(calendar_enabled=True)


@pytest.mark.parametrize("reminders_on", [False, True])
def test_the_prompt_matches_the_registry_with_the_calendar_on(reminders_on):
    raw = enabled_raw()
    if reminders_on:
        raw["reminders"] = {"enabled": True}
    config = Config.from_dict(raw, env={ENV_VAR: key_b64()})
    registry = build_production_registry(config, httpx.AsyncClient())
    prompt = config.agent.system_prompt
    enumerated = [
        line[2:].split(" — ", 1)[0] for line in prompt.splitlines() if line.startswith("- ")
    ]
    assert enumerated == registry.names()
    assert f"exactly these {COUNT_WORDS[len(registry.names())]}" in prompt


def test_the_full_capability_set_with_the_calendar_counts_fifteen():
    prompt = build_system_prompt(
        reminders_enabled=True,
        homelab_query_enabled=True,
        homelab_docs_enabled=True,
        sessions_enabled=True,
        calendar_enabled=True,
    )
    summaries = (
        BASE_TOOL_SUMMARIES
        + REMINDER_TOOL_SUMMARIES
        + QUERY_TOOL_SUMMARIES
        + DOCS_TOOL_SUMMARIES
        + SESSIONS_TOOL_SUMMARIES
        + CALENDAR_TOOL_SUMMARIES
    )
    assert len(summaries) == 15
    assert COUNT_WORDS[15] == "fifteen"
    assert "exactly these fifteen" in prompt


def _flag_combinations(*, include_calendar: bool):
    names = ["reminders_enabled", "homelab_query_enabled", "homelab_docs_enabled",
             "sessions_enabled"] + (["calendar_enabled"] if include_calendar else [])
    for mask in range(2 ** len(names)):
        yield {name: bool(mask & (1 << i)) for i, name in enumerate(names)}


def test_the_count_table_covers_every_combination_with_the_calendar():
    for flags in _flag_combinations(include_calendar=True):
        assert "exactly these " in build_system_prompt(**flags)


# --- No event, scheduler or release path names the create tool ----------------


def test_no_event_or_scheduler_module_names_the_create_tool():
    root = Path(henk.__file__).parent
    for package in ("events", "reminders"):
        for path in sorted((root / package).rglob("*.py")):
            text = path.read_text()
            assert "calendar_create_event" not in text, path
            assert "CalendarCreateEventTool" not in text, path
            assert not re.search(r"\bCREATE_TOOL_NAME\b", text), path
