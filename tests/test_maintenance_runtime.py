"""maintenance-calendar task 7.3: runtime wiring of the calendar, holds and release.

What ``build_runtime`` must assemble (notes/apply-group3.md, -group5.md, -group6.md
"Handed on"):

- **one** :class:`CalendarClient`, from ``build_calendar_client``, shared by the two
  tools and the window provider (one token cache);
- the provider's snapshot ``load()``-ed before intake starts, and its ``run()`` a
  background task;
- ``HoldLedger`` → ``AgentCore(release_sink=ledger.mark_released)`` →
  ``EventCoordinator(holds=ledger, windows=provider_or_None)`` → ``HoldReleaser``,
  one ledger throughout, the releaser a background task;
- ``ApprovalReferences(receipts)`` as the gate's recorder, and ``CalendarReceipts``
  plus the references handed to the registry, when the calendar is enabled;
- the owner-turn maintenance note and the owner zone for the release line.

And the spec's *The calendar capability is disabled by default and inert when off*
(*Registry unchanged when disabled*), with what "unchanged" covers stated in
``test_disabled_with_no_held_rows_changes_nothing_the_owner_or_google_sees``.
Release still runs with the calendar disabled (event-intake delta, design D11).

Secure-deployment *The calendar key is the only new secret*: the variable is gone
from ``os.environ`` before anything that can start the SDK's CLI subprocess exists.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

from henk.agent.maintenance import MaintenanceNote
from henk.app import App
from henk.audit import CalendarReceipts, MutationReceipts
from henk.calendar.client import CalendarClient
from henk.calendar.windows import MaintenanceWindows, snapshot_path
from henk.config import Config
from henk.events.holds import HoldLedger
from henk.events.release import HoldReleaser
from henk.agent.turns import EventTurn
from henk.runtime import build_runtime
from henk.store.db import Store
from henk.store.holds import HoldStore
from henk.tools import build_production_registry
from henk.tools.calendar import CALENDAR_TOOL_NAMES, ApprovalReferences
from tests.calendar_fixtures import CALENDAR_ID, ENV_VAR, key_b64
from tests.replay_fakes import sample_raw
from tests.test_reminders_runtime import _app as _plain_app
from tests.test_reminders_runtime import _OneMessage, _Recording


#: Not the default 30, so a wiring that hard-codes the default cannot pass.
GRACE_MINUTES = 45


def _raw(tmp_path: Path, *, calendar: bool = True, events: bool = True) -> dict:
    raw = sample_raw(tmp_path)
    raw["owner"]["timezone"] = "Europe/Amsterdam"
    raw["events"]["enabled"] = events
    raw["calendar"]["enabled"] = calendar
    raw["calendar"]["grace_minutes"] = GRACE_MINUTES
    if calendar:
        raw["calendar"]["calendar_id"] = CALENDAR_ID
    return raw


def _config(tmp_path: Path, **kw) -> Config:
    return Config.from_dict(_raw(tmp_path, **kw), env={ENV_VAR: key_b64()})


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        return httpx.Response(503)


@pytest.fixture
def transport(monkeypatch) -> RecordingTransport:
    """Every request the runtime's shared client makes, recorded and refused."""
    import henk.runtime as runtime

    recording = RecordingTransport()
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.setdefault("transport", recording)
        return real(*args, **kwargs)

    monkeypatch.setattr(runtime.httpx, "AsyncClient", client)
    return recording


def _hold_row(config: Config, *, window_id: str = "evt-old", end: float,
              state: str = "firing", event_id: str = "m1") -> None:
    """A held row from an earlier, enabled run, written straight to the store."""
    store = Store(config.store.path)
    try:
        HoldStore(store).hold(
            window_id=window_id,
            window_title="Homelab: patch round",
            window_start=end - 7200,
            window_end=end,
            event_id=event_id,
            identity_key="gatus:svc/api",
            state=state,
            title="Gatus: svc/api",
            message="triggered" if state == "firing" else "resolved",
            raw_json=json.dumps({"id": event_id, "title": "Gatus: svc/api"}),
            published_at=end - 3600,
            arrival_time=end - 3590,
            held_at=end - 3589,
        )
    finally:
        store.close()


# --- Enabled: one client, one ledger, the provider loaded first ---------------


async def test_one_calendar_client_serves_both_tools_and_the_provider(tmp_path, transport):
    app, client = build_runtime(_config(tmp_path))
    try:
        registry = app._core._factory.registry
        upcoming = registry.get("calendar_upcoming")
        create = registry.get("calendar_create_event")
        assert isinstance(app._windows, MaintenanceWindows)
        assert isinstance(upcoming._client, CalendarClient)
        assert upcoming._client is create._client is app._windows._source
        # Built over the runtime's one shared HTTP client.
        assert upcoming._client._http is client
        # Construction contacts nobody.
        assert transport.hosts == []
    finally:
        await client.aclose()


async def test_the_snapshot_is_loaded_before_intake_starts(tmp_path):
    config = _config(tmp_path)
    now = time.time()
    path = snapshot_path(config.audit.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "fetched_at": now - 60,
        "windows": [{"id": "evt-now", "title": "Homelab: patch round",
                     "start": now - 600, "end": now + 600}],
    }))
    app, client = build_runtime(config)
    try:
        # No task has run: the coordinator (and with it intake) has not started,
        # yet the provider already knows the running window from the snapshot.
        assert [w.id for w in app._windows.active(time.time())] == ["evt-now"]
        assert app._coordinator._windows is app._windows
    finally:
        await client.aclose()


async def test_the_hold_chain_runs_through_one_ledger(tmp_path):
    config = _config(tmp_path)
    app, client = build_runtime(config)
    try:
        ledger = app._coordinator.holds
        assert isinstance(ledger, HoldLedger)
        sink = app._core._release_sink
        assert sink.__self__ is ledger and sink.__func__ is HoldLedger.mark_released
        assert isinstance(app._releaser, HoldReleaser)
        assert app._releaser._ledger is ledger
        assert app._releaser._coordinator is app._coordinator
        assert app._releaser._windows is app._windows
        assert app._releaser._grace == GRACE_MINUTES * 60
        # Hold receipts land in the runtime's one audit log.
        assert ledger._receipts._audit is app._core._audit
    finally:
        await client.aclose()


async def test_the_gate_records_through_the_approval_reference_tap(tmp_path):
    app, client = build_runtime(_config(tmp_path))
    try:
        gate = app._dispatcher._gate
        tap = gate._recorder
        assert isinstance(tap, ApprovalReferences)
        # The tap forwards to the SAME receipts the core fans in from.
        assert isinstance(tap._inner, MutationReceipts)
        assert tap._inner is app._core._receipts
        assert tap._inner.sink == app._core._note_receipt
        create = app._core._factory.registry.get("calendar_create_event")
        assert create._references is tap
        assert isinstance(create._receipts, CalendarReceipts)
        assert create._receipts._audit is app._core._audit
    finally:
        await client.aclose()


async def test_the_core_gets_the_maintenance_note_and_the_owner_zone(tmp_path):
    config = _config(tmp_path)
    app, client = build_runtime(config)
    try:
        note = app._core._maintenance
        assert isinstance(note, MaintenanceNote)
        assert note._windows is app._windows
        assert note._holds is app._coordinator.holds.store
        assert note._zone == config.owner.zone
        assert note._grace == GRACE_MINUTES * 60
        assert app._core._owner_zone == config.owner.zone
    finally:
        await client.aclose()


async def test_the_registry_and_prompt_carry_the_calendar_when_enabled(tmp_path):
    config = _config(tmp_path)
    app, client = build_runtime(config)
    try:
        names = app._core._factory.registry.names()
        assert names[-2:] == list(CALENDAR_TOOL_NAMES)
        assert app._core._factory.registry is app._core._event_factory.registry
        assert app._core._tool_names == frozenset(names)
        assert "calendar" in app._core._factory.config.system_prompt.lower()
    finally:
        await client.aclose()


# --- Disabled: unchanged, except that release still runs -------------------------


async def test_disabled_with_no_held_rows_changes_nothing_the_owner_or_google_sees(
    tmp_path, transport
):
    """What "unchanged" covers, with ``calendar.enabled`` false and no held rows:

    - the registry's tool names equal those built from the same config with no
      ``calendar`` section at all (the pre-change shape), and none is a calendar tool;
    - the system prompt equals that config's, and never says "calendar";
    - the gate records into the core's own receipts (no reference tap);
    - no calendar client and no window provider exist; the snapshot file is never
      created; the coordinator classifies with no window source;
    - the owner-turn content is byte-identical (the note composes nothing);
    - no request reaches any host.

    What is NOT unchanged: with events enabled, the release task exists and ticks
    every 60 s (one read of ``hold_windows``); with no rows it submits nothing,
    writes nothing and marks nothing (group 6, decision 12).
    """
    config = _config(tmp_path, calendar=False)
    pre_raw = _raw(tmp_path, calendar=False)
    del pre_raw["calendar"]
    pre = Config.from_dict(pre_raw, env={})
    app, client = build_runtime(config)
    try:
        registry = app._core._factory.registry
        pre_names = build_production_registry(pre, httpx.AsyncClient()).names()
        assert registry.names() == pre_names
        assert not set(registry.names()) & set(CALENDAR_TOOL_NAMES)
        prompt = app._core._factory.config.system_prompt
        assert prompt == pre.agent.system_prompt
        assert "calendar" not in prompt.lower()
        assert app._dispatcher._gate._recorder is app._core._receipts
        assert isinstance(app._core._receipts, MutationReceipts)
        assert app._windows is None
        assert app._coordinator._windows is None
        assert app._releaser._windows is None
        assert not snapshot_path(config.audit.path).exists()
        assert app._core._with_maintenance("hello") == "hello"
        audit = Path(config.audit.path)
        before = audit.read_text() if audit.exists() else ""

        await app._releaser.tick()

        assert app._core._queue.empty()
        assert (audit.read_text() if audit.exists() else "") == before
        assert transport.hosts == []
    finally:
        await client.aclose()


async def test_disabled_with_held_rows_the_releaser_still_releases(tmp_path):
    # Rows held while the calendar was on, then rolled back (design Migration Plan
    # step 6): the release task runs whatever `calendar.enabled` says.
    config = _config(tmp_path, calendar=False)
    end = time.time() - 3 * 3600
    _hold_row(config, end=end)
    app, client = build_runtime(config)
    try:
        assert isinstance(app._releaser, HoldReleaser)
        assert app._coordinator._windows is None
        # The configured grace applies with the calendar off too (group 6, 14).
        assert app._releaser._grace == GRACE_MINUTES * 60
        app._coordinator.note_subscribe()
        app._coordinator.note_keepalive()

        await app._releaser.tick()

        turn = app._core._queue.get_nowait()
        assert isinstance(turn, EventTurn)
        assert turn.held_release.window_id == "evt-old"
        assert turn.offset is None
        assert "evt-old" in app._coordinator.holds.in_flight
    finally:
        await client.aclose()


async def test_disabled_an_all_recovered_window_is_marked_released_by_the_releaser(
    tmp_path,
):
    config = _config(tmp_path, calendar=False)
    end = time.time() - 3 * 3600
    _hold_row(config, end=end, state="resolved", event_id="r1")
    app, client = build_runtime(config)
    try:
        app._coordinator.note_subscribe()
        app._coordinator.note_keepalive()
        await app._releaser.tick()
        assert app._core._queue.empty()
        records = [json.loads(l) for l in Path(config.audit.path).read_text().splitlines()]
        [release] = [r for r in records if r.get("record_type") == "hold-release"]
        assert release["window_id"] == "evt-old"
        assert (release["held"], release["recovered"], release["forwarded"]) == (1, 1, 0)
    finally:
        await client.aclose()


async def test_disabled_the_note_still_reports_a_recent_release(tmp_path):
    # Decision (notes/apply-group7.md): released windows are shown with the
    # calendar off; active windows need the provider, which is not built.
    config = _config(tmp_path, calendar=False)
    end = time.time() - 3 * 3600
    _hold_row(config, end=end, state="resolved", event_id="r1")
    app, client = build_runtime(config)
    try:
        app._coordinator.note_subscribe()
        app._coordinator.note_keepalive()
        await app._releaser.tick()
        block = app._core._maintenance.block()
        assert "ended; 1 held, 1 recovered, 0 forwarded for triage." in block
        assert app._core._maintenance._grace == GRACE_MINUTES * 60
    finally:
        await client.aclose()


async def test_without_an_owner_zone_no_note_is_composed(tmp_path):
    raw = _raw(tmp_path, calendar=False)
    raw["owner"].pop("timezone")
    app, client = build_runtime(Config.from_dict(raw, env={}))
    try:
        assert app._core._maintenance is None
        assert app._core._owner_zone is None
        assert isinstance(app._releaser, HoldReleaser)
    finally:
        await client.aclose()


async def test_calendar_on_events_off_builds_no_provider_but_keeps_the_tools(
    tmp_path, transport
):
    # Orchestrator decision (notes/apply-group7.md, decision 13): with events off,
    # `holds()` is false for every event, so a provider would poll Google every
    # 5 minutes for an always-empty snapshot. `calendar_upcoming` reads live and
    # does not need it.
    config = _config(tmp_path, events=False)
    assert config.calendar.enabled and not config.events.enabled
    app, client = build_runtime(config)
    try:
        # No provider, and so no background task for it; no hold chain either.
        assert app._windows is None
        assert app._coordinator is None
        assert app._releaser is None
        assert app._core._release_sink is None
        assert app._core._maintenance._windows is None
        assert not snapshot_path(config.audit.path).exists()
        # The tools are still registered, over the one shared client.
        registry = app._core._factory.registry
        assert registry.names()[-2:] == list(CALENDAR_TOOL_NAMES)
        assert isinstance(registry.get("calendar_upcoming")._client, CalendarClient)
        # Nothing was requested at startup.
        assert transport.hosts == []
    finally:
        await client.aclose()


# --- The key never reaches the SDK subprocess ----------------------------------


class _FakeSdk(types.ModuleType):
    """Stands in for ``claude_agent_sdk``: records whether the key is in the
    environment when a client is constructed and when it connects (the moment the
    real one spawns the CLI subprocess)."""

    def __init__(self) -> None:
        super().__init__("claude_agent_sdk")
        self.seen: list[tuple[str, bool]] = []
        sdk = self

        class ClaudeSDKClient:
            def __init__(self, options=None) -> None:
                sdk.seen.append(("construct", ENV_VAR in os.environ))

            async def connect(self) -> None:
                sdk.seen.append(("connect", ENV_VAR in os.environ))

            async def query(self, text) -> None:
                return None

            async def receive_response(self):
                if False:  # pragma: no cover - an empty stream
                    yield None

        self.ClaudeSDKClient = ClaudeSDKClient
        self.ClaudeAgentOptions = lambda **kw: kw
        self.HookMatcher = lambda **kw: kw
        self.PermissionResultAllow = object
        self.PermissionResultDeny = object
        self.tool = lambda *a, **kw: (lambda fn: fn)
        self.create_sdk_mcp_server = lambda **kw: kw


async def test_the_key_is_gone_before_any_sdk_client_exists(tmp_path, monkeypatch):
    fake = _FakeSdk()
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    monkeypatch.setenv(ENV_VAR, key_b64())
    config = Config.from_dict(_raw(tmp_path), env=os.environ)
    assert config.secrets.google_calendar_key is not None
    app, client = build_runtime(config)
    try:
        for factory in (app._core._factory, app._core._event_factory):
            session = factory.create()
            await session.run_turn("hello")
        assert fake.seen == [("construct", False), ("connect", False)] * 2
    finally:
        await client.aclose()


async def test_the_key_is_gone_before_the_session_factories_are_built_when_enabled(
    tmp_path, monkeypatch
):
    import henk.runtime as runtime

    seen: list[bool] = []
    real = runtime.SdkSessionFactory

    def spy(*args, **kwargs):
        seen.append(ENV_VAR in os.environ)
        return real(*args, **kwargs)

    monkeypatch.setattr(runtime, "SdkSessionFactory", spy)
    monkeypatch.setenv(ENV_VAR, key_b64())
    app, client = runtime.build_runtime(Config.from_dict(_raw(tmp_path), env=os.environ))
    try:
        assert seen == [False, False]
    finally:
        await client.aclose()


# --- App: the provider and the releaser run as background tasks ---------------


async def test_the_provider_and_the_releaser_run_for_the_apps_lifetime():
    windows, releaser = _Recording(), _Recording()
    app, _core = _plain_app()
    app = App(app._adapter, app._dispatcher, app._core, windows=windows,
              releaser=releaser)
    await app.run()
    assert windows.ticks > 0 and releaser.ticks > 0
    assert windows.cancelled and releaser.cancelled
    assert not asyncio.all_tasks() - {asyncio.current_task()}


async def test_no_provider_or_releaser_task_when_none_is_wired():
    app, _core = _plain_app()
    await app.run()
    assert app._windows is None and app._releaser is None
    assert not asyncio.all_tasks() - {asyncio.current_task()}


async def test_a_failing_releaser_leaves_replies_and_the_provider_running():
    adapter = _OneMessage("are you there?")
    windows = _Recording()
    base, _core = _plain_app(adapter=adapter)
    app = App(adapter, base._dispatcher, base._core, windows=windows,
              releaser=_Recording(fail=True))
    await app.run()
    assert adapter.channel.sent and "are you there?" in adapter.channel.sent[0]
    assert windows.ticks > 0 and windows.cancelled


async def test_a_failing_provider_leaves_the_releaser_running():
    releaser = _Recording()
    base, _core = _plain_app()
    app = App(base._adapter, base._dispatcher, base._core,
              windows=_Recording(fail=True), releaser=releaser)
    await app.run()
    assert releaser.ticks > 0 and releaser.cancelled
