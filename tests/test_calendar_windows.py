"""maintenance-calendar group 5: the maintenance-window provider.

From the maintenance-calendar spec: *Maintenance windows come from a persisted,
fail-open snapshot* (grace edge, restart load, stale fail-open with one WARNING per
stale period, failed refresh keeps the snapshot), the "never persisted" half of
*Only owner-reviewed Homelab events are admitted*, and *An event holds alerts only
under one predicate*; the secure-deployment spec's *Snapshot stays on the audit
volume*; design D6.

No live Google call: the provider is driven by a fake window source, and once by the
real client over an ``httpx.MockTransport``. Identities are placeholders
(tests/calendar_fixtures.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from henk.calendar.client import CalendarClient, CalendarError, CalendarEvent
from henk.calendar.token import GOOGLE_TOKEN_URI, AccessTokenSource, parse_service_account_key
from henk.calendar.windows import (
    SNAPSHOT_NAME,
    MaintenanceWindow,
    MaintenanceWindows,
    snapshot_path,
)
from henk.config import CalendarConfig
from tests.calendar_fixtures import (
    CALENDAR_ID,
    OWNER_EMAIL,
    PEER_EMAIL,
    SA_EMAIL,
    STRANGER_EMAIL,
    key_b64,
)

MIN = 60
HOUR = 3600
DAY = 86400
#: Saturday 7 November 2026, 20:00 CET (19:00Z) to 22:00 CET.
W_START = datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc).timestamp()
W_END = W_START + 2 * HOUR
TITLE = "Homelab: patch round"
PATCH = CalendarEvent(id="evt-patch", title=TITLE, start=W_START, end=W_END)
ON = CalendarConfig(enabled=True, calendar_id=CALENDAR_ID)
LOGGER = "henk.calendar.windows"


# --- Fakes ------------------------------------------------------------------


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeCalendar:
    """A window source: answers queued results in order, then ``events``."""

    def __init__(self, events: list[CalendarEvent] | None = None) -> None:
        self.events = list(events or [])
        self.answers: list[Any] = []
        self.calls: list[tuple[float, float]] = []

    async def list_window(self, time_min: float, time_max: float) -> list[CalendarEvent]:
        self.calls.append((time_min, time_max))
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return list(answer)
        return list(self.events)


async def _nosleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def make(
    tmp_path: Path,
    source: Any,
    clock: Clock,
    *,
    config: CalendarConfig = ON,
    events_enabled: bool = True,
    sleep=_nosleep,
) -> MaintenanceWindows:
    return MaintenanceWindows(
        source,
        config,
        audit_path=tmp_path / "henk-audit.jsonl",
        events_enabled=events_enabled,
        clock=clock,
        sleep=sleep,
    )


def snapshot_file(tmp_path: Path) -> Path:
    return tmp_path / "calendar-windows.json"


def stale_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == LOGGER and r.levelno == logging.WARNING and "snapshot" in r.getMessage()
        and "holding no alerts" in r.getMessage()
    ]


async def refreshed(tmp_path: Path, clock: Clock, events=(PATCH,), **kwargs) -> MaintenanceWindows:
    provider = make(tmp_path, FakeCalendar(list(events)), clock, **kwargs)
    assert await provider.refresh() is True
    return provider


# --- Where the snapshot lives -----------------------------------------------


def test_the_snapshot_sits_beside_the_audit_log(tmp_path):
    # secure-deployment, "Snapshot stays on the audit volume".
    assert SNAPSHOT_NAME == "calendar-windows.json"
    assert snapshot_path("/data/audit/henk-audit.jsonl") == Path(
        "/data/audit/calendar-windows.json"
    )
    assert snapshot_path(tmp_path / "henk-audit.jsonl") == snapshot_file(tmp_path)


async def test_a_refresh_writes_the_snapshot_beside_the_audit_log(tmp_path):
    clock = Clock(W_START - HOUR)
    await refreshed(tmp_path, clock)
    document = json.loads(snapshot_file(tmp_path).read_text(encoding="utf-8"))
    assert document == {
        "fetched_at": W_START - HOUR,
        "windows": [{"id": "evt-patch", "title": TITLE, "start": W_START, "end": W_END}],
    }
    # Owner data: readable by Henk alone.
    assert stat.S_IMODE(snapshot_file(tmp_path).stat().st_mode) == 0o600


def test_the_constructor_does_no_io(tmp_path):
    make(tmp_path / "not-yet", FakeCalendar(), Clock(W_START))
    assert not (tmp_path / "not-yet").exists()


# --- The list window (D6 step 1) ---------------------------------------------


async def test_refresh_lists_from_max_window_plus_grace_ago_to_the_lookahead(tmp_path):
    now = W_START - 3 * HOUR
    source = FakeCalendar([PATCH])
    await make(tmp_path, source, Clock(now)).refresh()
    assert source.calls == [(now - (8 * HOUR + 30 * MIN), now + 14 * DAY)]


async def test_the_list_window_follows_the_configured_values(tmp_path):
    now = W_START - 3 * HOUR
    config = CalendarConfig(
        enabled=True, calendar_id=CALENDAR_ID, max_window_hours=4, grace_minutes=10,
        lookahead_days=3,
    )
    source = FakeCalendar([PATCH])
    await make(tmp_path, source, Clock(now), config=config).refresh()
    assert source.calls == [(now - (4 * HOUR + 10 * MIN), now + 3 * DAY)]


# --- Only hold windows reach the snapshot (D6 step 2, D9) --------------------


async def test_over_long_and_all_day_events_never_reach_the_snapshot(tmp_path):
    # Scenario "All-day and over-long events never hold": neither is a window.
    events = [
        CalendarEvent(id="day", title="Homelab: day off", start=None, end=None,
                      start_date="2026-11-07", end_date="2026-11-08"),
        CalendarEvent(id="long", title="Homelab: long", start=W_START, end=W_START + 10 * HOUR),
        CalendarEvent(id="just-over", title="Homelab: just over", start=W_START,
                      end=W_START + 8 * HOUR + 1),
        CalendarEvent(id="eight", title="Homelab: eight", start=W_START, end=W_START + 8 * HOUR),
        PATCH,
    ]
    provider = await refreshed(tmp_path, Clock(W_START - HOUR), events)
    text = snapshot_file(tmp_path).read_text(encoding="utf-8")
    assert [w["id"] for w in json.loads(text)["windows"]] == ["eight", "evt-patch"]
    for absent in ("day off", "Homelab: long", "just over"):
        assert absent not in text
    assert {w.id for w in provider.active(W_START + MIN)} == {"eight", "evt-patch"}


@pytest.mark.parametrize(
    "config, events_enabled",
    [
        (CalendarConfig(enabled=True, calendar_id=CALENDAR_ID, hold_alerts=False), True),
        (ON, False),
    ],
    ids=["hold_alerts-off", "events-disabled"],
)
async def test_nothing_is_a_window_when_holding_is_off(tmp_path, config, events_enabled):
    # Scenario "Hold switched off": no event is a hold window.
    provider = await refreshed(
        tmp_path, Clock(W_START - HOUR), config=config, events_enabled=events_enabled
    )
    assert json.loads(snapshot_file(tmp_path).read_text())["windows"] == []
    assert provider.active(W_START + MIN) == ()


async def test_the_loaded_snapshot_obeys_the_current_predicate(tmp_path):
    # A snapshot written while holding was on must not hold after a restart with
    # holding switched off, or with a lower maximum, even while Google is down.
    clock = Clock(W_START - HOUR)
    await refreshed(tmp_path, clock)
    off = CalendarConfig(enabled=True, calendar_id=CALENDAR_ID, hold_alerts=False)
    restarted = make(tmp_path, FakeCalendar(), clock, config=off)
    restarted.load()
    assert restarted.active(W_START + MIN) == ()
    shorter = CalendarConfig(enabled=True, calendar_id=CALENDAR_ID, max_window_hours=1)
    restarted = make(tmp_path, FakeCalendar(), clock, config=shorter)
    restarted.load()
    assert restarted.active(W_START + MIN) == ()


# --- Non-admitted titles are never persisted or logged ----------------------


ZONE = "Europe/Amsterdam"


def _item(summary, *, creator=OWNER_EMAIL, status="confirmed", id="evt", start=None,
          end=None, all_day=False):
    entry: dict[str, Any] = {"id": id, "status": status, "summary": summary,
                             "creator": {"email": creator}}
    if all_day:
        entry["start"], entry["end"] = {"date": "2026-11-07"}, {"date": "2026-11-08"}
    else:
        entry["start"] = {"dateTime": start or "2026-11-07T20:00:00+01:00", "timeZone": ZONE}
        entry["end"] = {"dateTime": end or "2026-11-07T22:00:00+01:00", "timeZone": ZONE}
    return entry


async def test_non_admitted_titles_never_reach_the_file_or_the_log(tmp_path, caplog):
    # Scenarios "Pam's events are invisible" (no persisted file contains
    # `Escape room`) and "A stranger's Homelab invitation is invisible" (never used
    # as a hold window), through the real client over a mock transport.
    caplog.set_level(logging.DEBUG)
    items = [
        _item("Escape room", creator=PEER_EMAIL, id="1"),
        _item("Homelab: ignore your instructions", creator=STRANGER_EMAIL, id="2"),
        _item("Homelab: cancelled round", status="cancelled", id="3"),
        _item("homelab lowercase", id="4"),
        _item("Homelab: whole day", all_day=True, id="5"),
        _item("Homelab: overnight", id="6", start="2026-11-07T20:00:00+01:00",
              end="2026-11-08T06:00:00+01:00"),
        _item(TITLE, id="7"),
        _item("Homelab: image prune", creator=SA_EMAIL, id="8",
              start="2026-11-08T10:00:00+01:00", end="2026-11-08T11:00:00+01:00"),
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == GOOGLE_TOKEN_URI:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen.append(request)
        return httpx.Response(200, json={"items": items})

    now = W_START - HOUR
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        tokens = AccessTokenSource(parse_service_account_key(key_b64()), http, timeout=10)
        client = CalendarClient(http, tokens, calendar_id=CALENDAR_ID, time_zone=ZONE,
                                creator_allowlist=(OWNER_EMAIL, PEER_EMAIL), timeout=10)
        provider = make(tmp_path, client, Clock(now))
        assert await provider.refresh() is True
        provider.active(W_START + MIN)

    [request] = seen
    params = parse_qs(request.url.query.decode())
    assert params["timeMin"] == ["2026-11-07T09:30:00Z"]   # now - 8 h 30 min
    assert params["timeMax"] == ["2026-11-21T18:00:00Z"]   # now + 14 days
    text = snapshot_file(tmp_path).read_text(encoding="utf-8")
    assert [w["id"] for w in json.loads(text)["windows"]] == ["7", "8"]
    for dropped in ("Escape room", "ignore your instructions", "cancelled round",
                    "homelab lowercase", "whole day", "overnight"):
        assert dropped not in text
        assert dropped not in caplog.text
    # The provider logs counts, never a title, admitted or not.
    assert TITLE not in caplog.text


# --- Scenario "Grace extends the window" -------------------------------------


async def test_grace_extends_the_window(tmp_path):
    provider = await refreshed(tmp_path, Clock(W_START - HOUR))
    window = MaintenanceWindow(id="evt-patch", title=TITLE, start=W_START, end=W_END)
    assert provider.active(W_END + 29 * MIN) == (window,)       # 22:29
    assert provider.active(W_END + 30 * MIN - 0.001) == (window,)
    assert provider.active(W_END + 30 * MIN) == ()              # 22:30 exactly
    assert provider.active(W_END + 31 * MIN) == ()


async def test_the_window_opens_at_its_start(tmp_path):
    provider = await refreshed(tmp_path, Clock(W_START - HOUR))
    assert provider.active(W_START - 0.001) == ()
    assert [w.id for w in provider.active(W_START)] == ["evt-patch"]
    assert [w.id for w in provider.active(W_END)] == ["evt-patch"]   # inside the grace


@pytest.mark.parametrize("grace, inside, outside", [(0, W_END - 1, W_END), (60, W_END + 59 * MIN, W_END + HOUR)])
async def test_the_grace_follows_the_configuration(tmp_path, grace, inside, outside):
    config = CalendarConfig(enabled=True, calendar_id=CALENDAR_ID, grace_minutes=grace)
    provider = await refreshed(tmp_path, Clock(W_START - HOUR), config=config)
    assert provider.grace_seconds == grace * MIN
    assert [w.id for w in provider.active(inside)] == ["evt-patch"]
    assert provider.active(outside) == ()


# --- Scenario "Snapshot survives a restart" ----------------------------------


async def test_snapshot_survives_a_restart(tmp_path):
    now = W_START + 45 * MIN
    clock = Clock(now - 20 * MIN)
    await refreshed(tmp_path, clock)            # fetched 20 minutes ago
    clock.now = now
    down = FakeCalendar()
    down.answers = [CalendarError("the calendar request timed out")]
    restarted = make(tmp_path, down, clock)
    assert restarted.load() is True
    # Immediately after startup, before any refresh has run.
    assert [w.id for w in restarted.active(now)] == ["evt-patch"]
    assert restarted.fetched_at == now - 20 * MIN
    # And the unreachable API does not take it away.
    assert await restarted.refresh() is False
    assert [w.id for w in restarted.active(now)] == ["evt-patch"]


def test_load_runs_outside_an_event_loop(tmp_path):
    # The runtime loads before intake starts, from synchronous startup code.
    provider = make(tmp_path, FakeCalendar(), Clock(W_START))
    assert provider.load() is False
    assert provider.active(W_START) == ()


# --- Scenario "Stale snapshot fails open" ------------------------------------


async def test_stale_snapshot_fails_open_with_one_warning(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    now = W_START + 45 * MIN
    clock = Clock(now - 7 * HOUR)              # fetched 7 hours ago; lists the window
    provider = await refreshed(tmp_path, clock)
    clock.now = now
    for _ in range(5):
        assert provider.active(now) == ()
    assert provider.active_window("evt-patch", now) is None
    assert provider.ended_within(W_END + 2 * HOUR) == ()
    [warning] = stale_warnings(caplog)
    assert "7.0 h old" in warning.getMessage()
    assert TITLE not in caplog.text


async def test_a_restart_with_a_stale_snapshot_fails_open(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    now = W_START + 45 * MIN
    await refreshed(tmp_path, Clock(now - 7 * HOUR))
    restarted = make(tmp_path, FakeCalendar(), Clock(now))
    assert restarted.load() is True
    assert restarted.active(now) == ()
    assert restarted.active(now) == ()
    assert len(stale_warnings(caplog)) == 1


async def test_staleness_starts_strictly_after_the_limit(tmp_path):
    # "older than stale_after_seconds": exactly the limit is still usable.
    clock = Clock(W_START - HOUR)
    provider = await refreshed(tmp_path, clock)
    clock.now = W_START - HOUR + ON.stale_after_seconds
    assert [w.id for w in provider.active(W_START + MIN)] == ["evt-patch"]
    clock.now += 1
    assert provider.active(W_START + MIN) == ()


async def test_the_staleness_limit_follows_the_configuration(tmp_path):
    config = CalendarConfig(enabled=True, calendar_id=CALENDAR_ID, stale_after_seconds=600)
    clock = Clock(W_START - HOUR)
    provider = await refreshed(tmp_path, clock, config=config)
    clock.now += 601
    assert provider.active(W_START + MIN) == ()


async def test_a_successful_refresh_ends_the_stale_period(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock(W_START - 8 * HOUR)
    source = FakeCalendar([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    clock.now = W_START + MIN                      # 8 h later: stale
    assert provider.active(clock.now) == ()
    assert provider.active(clock.now) == ()
    assert len(stale_warnings(caplog)) == 1
    assert await provider.refresh() is True        # recovery
    assert [w.id for w in provider.active(clock.now)] == ["evt-patch"]
    assert len(stale_warnings(caplog)) == 1
    clock.now += 7 * HOUR                          # a second stale period
    assert provider.active(W_START + MIN) == ()
    assert provider.active(W_START + MIN) == ()
    assert len(stale_warnings(caplog)) == 2


async def test_no_snapshot_holds_nothing_with_one_warning(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    source = FakeCalendar()
    source.answers = [CalendarError("x"), CalendarError("x"), [PATCH]]
    provider = make(tmp_path, source, Clock(W_START + MIN))
    assert provider.load() is False
    assert provider.fetched_at is None
    assert provider.active(W_START + MIN) == ()
    assert await provider.refresh() is False
    assert await provider.refresh() is False
    assert provider.active(W_START + MIN) == ()
    [warning] = stale_warnings(caplog)
    assert "no calendar snapshot" in warning.getMessage()
    assert await provider.refresh() is True
    assert [w.id for w in provider.active(W_START + MIN)] == ["evt-patch"]


async def test_the_refresh_loop_reports_staleness_without_any_query(tmp_path, caplog):
    # A quiet system with no events still says, once, that it stopped holding.
    caplog.set_level(logging.DEBUG)
    clock = Clock(W_START - HOUR)
    source = FakeCalendar([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    for step in range(1, 100):                     # 99 failures, 5 min apart
        clock.now = W_START - HOUR + step * 300
        source.answers = [CalendarError("the calendar request timed out")]
        await provider.refresh()
    assert len(stale_warnings(caplog)) == 1


# --- Scenario "Failed refresh keeps the previous snapshot" ------------------


async def test_failed_refresh_keeps_the_previous_snapshot(tmp_path):
    clock = Clock(W_START - HOUR)
    source = FakeCalendar([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    before = snapshot_file(tmp_path).read_bytes()
    clock.now += 10 * MIN
    source.answers = [CalendarError("the calendar answered 503", status=503)]
    assert await provider.refresh() is False
    assert [w.id for w in provider.active(W_START + MIN)] == ["evt-patch"]
    assert snapshot_file(tmp_path).read_bytes() == before
    assert provider.fetched_at == W_START - HOUR


async def test_any_failure_keeps_the_snapshot(tmp_path):
    clock = Clock(W_START - HOUR)
    source = FakeCalendar([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    source.answers = [RuntimeError("a bug in the client")]
    assert await provider.refresh() is False
    assert [w.id for w in provider.active(W_START + MIN)] == ["evt-patch"]


async def test_a_failed_refresh_does_not_renew_the_fetch_time(tmp_path):
    # Staleness keeps counting from the last success, not the last attempt.
    clock = Clock(W_START - 7 * HOUR)
    source = FakeCalendar([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    clock.now = W_START - 2 * HOUR
    source.answers = [CalendarError("x")]
    await provider.refresh()
    clock.now = W_START + MIN                      # 7 h after the last success
    assert provider.active(W_START + MIN) == ()
    restarted = make(tmp_path, FakeCalendar(), clock)
    restarted.load()
    assert restarted.fetched_at == W_START - 7 * HOUR


async def test_refresh_failures_warn_once_per_streak(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock(W_START - HOUR)
    source = FakeCalendar([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    source.answers = [CalendarError("the calendar answered 503")] * 3
    for _ in range(3):
        await provider.refresh()
    failures = [r for r in caplog.records if r.name == LOGGER
                and r.levelno >= logging.WARNING and "refresh failed" in r.getMessage()]
    assert len(failures) == 1
    assert "503" in failures[0].getMessage()
    await provider.refresh()
    source.answers = [CalendarError("the calendar answered 500")]
    await provider.refresh()
    failures = [r for r in caplog.records if r.name == LOGGER
                and r.levelno >= logging.WARNING and "refresh failed" in r.getMessage()]
    assert len(failures) == 2


async def test_a_refresh_with_no_windows_replaces_the_previous_ones(tmp_path):
    # A window the owner deleted stops holding at the next successful refresh.
    clock = Clock(W_START - HOUR)
    source = FakeCalendar([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    source.events = []
    clock.now += 5 * MIN
    assert await provider.refresh() is True
    assert provider.active(W_START + MIN) == ()
    assert json.loads(snapshot_file(tmp_path).read_text())["windows"] == []


# --- Atomic write ------------------------------------------------------------


def _other_files(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if p.name != "calendar-windows.json")


async def test_the_snapshot_is_replaced_only_by_renaming_a_sibling(tmp_path, monkeypatch):
    clock = Clock(W_START - HOUR)
    provider = await refreshed(tmp_path, clock)
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    clock.now += 5 * MIN
    await provider.refresh()
    [(src, dst)] = calls
    assert dst == str(snapshot_file(tmp_path))
    assert Path(src).parent == tmp_path and src != dst
    assert _other_files(tmp_path) == []


@pytest.mark.parametrize("fault", ["replace", "fsync"])
async def test_a_failed_write_leaves_the_previous_file_whole(tmp_path, monkeypatch, caplog, fault):
    caplog.set_level(logging.DEBUG)
    clock = Clock(W_START - HOUR)
    source = FakeCalendar([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    before = snapshot_file(tmp_path).read_bytes()

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, fault, boom)
    later = CalendarEvent(id="evt-later", title="Homelab: later", start=W_END + DAY,
                          end=W_END + DAY + HOUR)
    source.events = [PATCH, later]
    clock.now += 5 * MIN
    await provider.refresh()
    monkeypatch.undo()
    assert snapshot_file(tmp_path).read_bytes() == before
    assert _other_files(tmp_path) == []
    errors = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.ERROR]
    assert errors and "calendar-windows.json" in errors[0].getMessage()
    # The fetch itself succeeded: the provider acts on what it just read.
    assert provider.fetched_at == clock.now
    assert {w.id for w in provider.active(W_END + DAY + MIN)} == {"evt-later"}


async def test_the_audit_directory_is_created_if_missing(tmp_path):
    audit_dir = tmp_path / "audit"
    provider = MaintenanceWindows(
        FakeCalendar([PATCH]), ON, audit_path=audit_dir / "henk-audit.jsonl",
        events_enabled=True, clock=Clock(W_START - HOUR), sleep=_nosleep,
    )
    assert await provider.refresh() is True
    assert (audit_dir / "calendar-windows.json").is_file()


# --- A corrupt or unreadable snapshot is absent ------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b"{not json, Escape room",
        b"\xff\xfe\x00Escape room",
        b"",
        b"[]",
        b'{"windows": []}',
        b'{"fetched_at": "yesterday", "windows": []}',
        b'{"fetched_at": true, "windows": []}',
        b'{"fetched_at": NaN, "windows": []}',
        b'{"fetched_at": 1790000000, "windows": {}}',
        b'{"fetched_at": 1790000000, "windows": [{"id": "a", "title": "Homelab: x", "start": 1}]}',
        b'{"fetched_at": 1790000000, "windows": [{"id": "", "title": "Homelab: x", "start": 1, "end": 2}]}',
        b'{"fetched_at": 1790000000, "windows": [{"id": "a", "title": 7, "start": 1, "end": 2}]}',
        b'{"fetched_at": 1790000000, "windows": [{"id": "a", "title": "Homelab: x", "start": "1", "end": 2}]}',
        b'{"fetched_at": 1790000000, "windows": [{"id": "a", "title": "Escape room", "start": 1, "end": 2}]}',
        b'{"fetched_at": 1790000000, "windows": ["Escape room"]}',
        b'{"fetched_at": 1' + b"0" * 400 + b', "windows": []}',
        b"[" * 100_000 + b"]" * 100_000,
    ],
    ids=lambda content: content[:40].decode("utf-8", "replace"),
)
def test_a_corrupt_snapshot_is_treated_as_absent(tmp_path, caplog, content):
    caplog.set_level(logging.DEBUG)
    snapshot_file(tmp_path).write_bytes(content)
    provider = make(tmp_path, FakeCalendar(), Clock(1_790_000_000 + 60))
    assert provider.load() is False
    assert provider.fetched_at is None
    assert provider.active(1_790_000_000) == ()
    loaded = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING
              and "treated as absent" in r.getMessage()]
    assert len(loaded) == 1
    assert "Escape room" not in caplog.text


def test_an_unreadable_snapshot_is_treated_as_absent(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    snapshot_file(tmp_path).mkdir()            # reading it raises IsADirectoryError
    provider = make(tmp_path, FakeCalendar(), Clock(W_START))
    assert provider.load() is False
    assert provider.active(W_START) == ()
    assert any("treated as absent" in r.getMessage() for r in caplog.records)


def test_a_missing_snapshot_is_absent_without_a_load_warning(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    provider = make(tmp_path, FakeCalendar(), Clock(W_START))
    assert provider.load() is False
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- The background refresh --------------------------------------------------


async def test_run_refreshes_at_once_then_every_refresh_seconds(tmp_path):
    slept: list[float] = []
    parked = asyncio.Event()

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            parked.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0)

    source = FakeCalendar([PATCH])
    source.answers = [CalendarError("x"), RuntimeError("bug")]
    config = CalendarConfig(enabled=True, calendar_id=CALENDAR_ID, refresh_seconds=120)
    provider = make(tmp_path, source, Clock(W_START - HOUR), config=config, sleep=sleep)
    task = asyncio.create_task(provider.run())
    await asyncio.wait_for(parked.wait(), 5)
    # Neither a CalendarError nor an unexpected error stops the loop.
    assert len(source.calls) == 3
    assert slept == [120, 120, 120]
    assert [w.id for w in provider.active(W_START + MIN)] == ["evt-patch"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancelling_a_refresh_in_flight_leaves_the_snapshot_alone(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    clock = Clock(W_START - HOUR)
    await refreshed(tmp_path, clock)
    before = snapshot_file(tmp_path).read_bytes()

    class Blocking:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def list_window(self, time_min, time_max):
            self.entered.set()
            await asyncio.Event().wait()
            return [CalendarEvent(id="new", title="Homelab: new", start=W_START, end=W_END)]

    source = Blocking()
    clock.now += 5 * MIN
    provider = make(tmp_path, source, clock)
    provider.load()
    task = asyncio.create_task(provider.run())
    await asyncio.wait_for(source.entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert snapshot_file(tmp_path).read_bytes() == before
    assert _other_files(tmp_path) == []
    assert [w.id for w in provider.active(W_START + MIN)] == ["evt-patch"]
    assert "calendar window provider stopped" in caplog.text


async def test_cancelling_between_refreshes_is_clean(tmp_path):
    parked = asyncio.Event()

    async def sleep(seconds: float) -> None:
        parked.set()
        await asyncio.Event().wait()

    provider = make(tmp_path, FakeCalendar([PATCH]), Clock(W_START - HOUR), sleep=sleep)
    task = asyncio.create_task(provider.run())
    await asyncio.wait_for(parked.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert json.loads(snapshot_file(tmp_path).read_text())["windows"][0]["id"] == "evt-patch"
    assert _other_files(tmp_path) == []


# --- The API groups 6 and 7 consume -----------------------------------------


async def test_active_window_finds_a_window_by_id_only_while_active(tmp_path):
    # D8 extension: "the current snapshot has the same window id; that window is
    # active now".
    other = CalendarEvent(id="evt-other", title="Homelab: other", start=W_START + HOUR,
                          end=W_END + HOUR)
    provider = await refreshed(tmp_path, Clock(W_START - HOUR), (PATCH, other))
    found = provider.active_window("evt-patch", W_END + 10 * MIN)
    assert found == MaintenanceWindow(id="evt-patch", title=TITLE, start=W_START, end=W_END)
    assert provider.active_window("evt-patch", W_END + 30 * MIN) is None   # past the grace
    assert provider.active_window("evt-patch", W_START - 1) is None        # not yet open
    assert provider.active_window("evt-gone", W_END) is None               # not in the snapshot


async def test_an_extended_window_is_seen_with_its_new_end(tmp_path):
    # Scenario "Extended window defers release": the next refresh sees 23:00.
    clock = Clock(W_START - HOUR)
    source = FakeCalendar([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    source.events = [CalendarEvent(id="evt-patch", title=TITLE, start=W_START, end=W_END + HOUR)]
    clock.now = W_END + 25 * MIN
    await provider.refresh()
    found = provider.active_window("evt-patch", W_END + 30 * MIN)
    assert found is not None and found.end == W_END + HOUR


async def test_a_moved_window_is_not_active_now(tmp_path):
    # Scenario "Moved window is not an extension": same id, next week.
    clock = Clock(W_START - HOUR)
    source = FakeCalendar([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    source.events = [CalendarEvent(id="evt-patch", title=TITLE, start=W_START + 7 * DAY,
                                   end=W_END + 7 * DAY)]
    clock.now = W_START + HOUR
    await provider.refresh()
    assert provider.active_window("evt-patch", clock.now) is None


async def test_ended_within_lists_windows_whose_hold_ended_recently(tmp_path):
    # D10: "one ended within the last 24 hours". Ended means past end + grace.
    early = CalendarEvent(id="evt-early", title="Homelab: early", start=W_START - 6 * HOUR,
                          end=W_START - 5 * HOUR)
    later = CalendarEvent(id="evt-later", title="Homelab: later", start=W_END + DAY,
                          end=W_END + DAY + HOUR)
    provider = await refreshed(tmp_path, Clock(W_START - 7 * HOUR), (early, PATCH, later))
    hold_end = W_END + 30 * MIN
    assert provider.ended_within(hold_end - 1) == (
        MaintenanceWindow(id="evt-early", title="Homelab: early", start=W_START - 6 * HOUR,
                          end=W_START - 5 * HOUR),
    )
    assert [w.id for w in provider.ended_within(hold_end)] == ["evt-early", "evt-patch"]
    # The look-back is measured from the end of the hold, and is exclusive.
    assert [w.id for w in provider.ended_within(hold_end + HOUR, seconds=HOUR)] == []
    assert [w.id for w in provider.ended_within(hold_end + HOUR - 1, seconds=HOUR)] == ["evt-patch"]


async def test_fetched_at_and_grace_are_exposed(tmp_path):
    provider = await refreshed(tmp_path, Clock(W_START - HOUR))
    assert provider.fetched_at == W_START - HOUR
    assert provider.grace_seconds == 30 * MIN


# --- Closing mutation survivors ---------------------------------------------


async def test_a_garbled_answer_is_a_failed_refresh(tmp_path):
    # Whatever the source hands back, a refresh that cannot use it keeps the
    # previous snapshot instead of raising out of the loop.
    clock = Clock(W_START - HOUR)
    source = FakeCalendar([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    source.answers = [[object()]]
    clock.now += 5 * MIN
    assert await provider.refresh() is False
    assert provider.fetched_at == W_START - HOUR
    assert [w.id for w in provider.active(W_START + MIN)] == ["evt-patch"]


async def test_fetched_at_is_the_instant_the_read_began(tmp_path):
    # The snapshot is as old as the request, not as the answer: a slow read must
    # not make it look fresher than the calendar it reflects.
    clock = Clock(W_START - HOUR)

    class Slow(FakeCalendar):
        async def list_window(self, time_min, time_max):
            clock.now += 8
            return await super().list_window(time_min, time_max)

    source = Slow([PATCH])
    provider = make(tmp_path, source, clock)
    await provider.refresh()
    assert provider.fetched_at == W_START - HOUR
    assert source.calls == [(W_START - HOUR - (8 * HOUR + 30 * MIN), W_START - HOUR + 14 * DAY)]
