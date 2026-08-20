"""Runtime wiring for the reminder scheduler (reminder-delivery group 8).

From the agent-core delta's "The reminder scheduler runs alongside the core worker".
Two halves, and the second is the one that would be easy to get quietly wrong:

- **Lifecycle.** The task starts with the app and is cancelled with it, nothing is left
  pending, and no task exists at all when the capability is disabled.
- **Isolation, both ways.** A scheduler failure must not stop replies or triage, and a
  failure in either of those must not stop the scheduler.

Plus the one wiring assertion with real consequences: the scheduler must be handed the
**same adapter instance** the core holds. The send lock is instance state, so a second
adapter over the same bridge would satisfy every serialization test in the suite while
serializing nothing at all — the failure mode would be interleaved chunks in production
and a green suite.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from henk.app import App, Dispatcher
from henk.config import Config
from henk.reminders.scheduler import ReminderScheduler
from henk.runtime import build_runtime
from tests.conftest import FakeChannel, FakeSessionFactory, inbound
from tests.test_config import SAMPLE


def _enabled_config(tmp_path: Path) -> Config:
    base = Config.load(SAMPLE, env={})
    config = dataclasses.replace(
        base,
        owner=dataclasses.replace(base.owner, timezone="Europe/Amsterdam"),
        reminders=dataclasses.replace(base.reminders, enabled=True),
        store=dataclasses.replace(base.store, path=str(tmp_path / "henk.db")),
    )
    return config


def _disabled_config(tmp_path: Path) -> Config:
    base = Config.load(SAMPLE, env={})
    assert base.reminders.enabled is False
    return dataclasses.replace(
        base, store=dataclasses.replace(base.store, path=str(tmp_path / "henk.db"))
    )


# --- Wiring --------------------------------------------------------------


async def test_enabled_wires_a_scheduler_onto_the_app(tmp_path: Path):
    app, client = build_runtime(_enabled_config(tmp_path))
    try:
        assert isinstance(app._scheduler, ReminderScheduler)
        # And the owner-turn note provider, which is the scheduler's other half.
        assert app._core._deliveries is not None
    finally:
        await client.aclose()


async def test_disabled_wires_no_scheduler_and_no_note(tmp_path: Path):
    app, client = build_runtime(_disabled_config(tmp_path))
    try:
        assert app._scheduler is None
        assert app._core._deliveries is None
        # Belt and braces on the inertness claim: no time header either, which is the
        # tell the reminders-core deploy record uses.
        assert app._core._time_header is None
    finally:
        await client.aclose()


async def test_the_scheduler_gets_the_same_adapter_instance_as_the_core(
    tmp_path: Path,
):
    """The send lock is instance state, so this is not a tidiness assertion.

    A second `SignalAdapter` over the same bridge has its own lock. Every serialization
    test in the suite would still pass — they build one adapter and drive it — while
    production interleaved the scheduler's chunks with the core's. Identity is the only
    thing worth asserting here; equality would not catch it.
    """
    app, client = build_runtime(_enabled_config(tmp_path))
    try:
        assert app._scheduler._channel is app._core._channel
        assert app._scheduler._channel is app._adapter
    finally:
        await client.aclose()


async def test_the_scheduler_shares_the_stores_repository_and_resolver(
    tmp_path: Path,
):
    """One store and one resolver, or the owner adjudicates two versions of a time."""
    app, client = build_runtime(_enabled_config(tmp_path))
    try:
        commands = app._core._commands
        assert app._scheduler._reminders is commands.reminders
        assert app._scheduler._resolver is commands._resolver
        assert app._core._deliveries._reminders is app._scheduler._reminders
        assert app._core._deliveries._resolver is app._scheduler._resolver
    finally:
        await client.aclose()


async def test_the_scheduler_takes_its_bounds_from_config(tmp_path: Path):
    base = _enabled_config(tmp_path)
    config = dataclasses.replace(
        base,
        reminders=dataclasses.replace(
            base.reminders,
            poll_interval_seconds=7,
            tick_delivery_limit=3,
            note_window_seconds=600,
            note_max_items=2,
        ),
    )
    app, client = build_runtime(config)
    try:
        assert app._scheduler._config.poll_interval_seconds == 7
        assert app._scheduler._config.tick_delivery_limit == 3
        assert app._core._deliveries._window == 600
        assert app._core._deliveries._max_items == 2
    finally:
        await client.aclose()


async def test_the_scheduler_writes_receipts_naming_the_scheduler(tmp_path: Path):
    app, client = build_runtime(_enabled_config(tmp_path))
    try:
        assert app._scheduler._receipts is not None
    finally:
        await client.aclose()


# --- Lifecycle -----------------------------------------------------------


class _Recording:
    """A runnable double that records how long it ran and how it stopped."""

    def __init__(self, *, fail: bool = False) -> None:
        self.ticks = 0
        self.cancelled = False
        self.fail = fail

    async def run(self) -> None:
        try:
            while True:
                self.ticks += 1
                if self.fail:
                    raise RuntimeError("the scheduler blew up")
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _OneMessage:
    """A channel that yields one inbound message then ends the stream."""

    def __init__(self, text: str = "hello") -> None:
        self.channel = FakeChannel()
        self._text = text

    async def messages(self):
        # Yields around the message on purpose. A real adapter is I/O bound and hands
        # control back constantly; nothing in the fake reply path awaits anything, so
        # without these the background tasks would never get a slice before the stream
        # ends and the app cancels them — and every isolation assertion below would be
        # vacuously true.
        for _ in range(10):
            await asyncio.sleep(0)
        yield inbound(self._text)
        for _ in range(10):
            await asyncio.sleep(0)

    async def send(self, text):
        return await self.channel.send(text)

    async def send_proactive(self, text, *, failure_notice=None):
        return await self.channel.send_proactive(text, failure_notice=failure_notice)


def _app(scheduler=None, coordinator=None, *, adapter=None):
    from henk.agent.core import AgentCore
    from henk.channel.allowlist import AllowlistFilter
    from henk.gate.approval import ApprovalGate

    adapter = adapter or _OneMessage()
    factory = FakeSessionFactory()
    gate = ApprovalGate(adapter, timeout_seconds=1.0)
    core = AgentCore(factory, adapter, gate=gate)
    dispatcher = Dispatcher(AllowlistFilter("+31600000000"), gate, core)
    return App(
        adapter, dispatcher, core, coordinator=coordinator, scheduler=scheduler
    ), core


async def test_the_scheduler_runs_for_the_apps_lifetime_and_is_cancelled_cleanly():
    scheduler = _Recording()
    app, _core = _app(scheduler=scheduler)
    await app.run()  # the message stream ends, which shuts the app down
    assert scheduler.ticks > 0, "the scheduler task never started"
    assert scheduler.cancelled, "the scheduler task was not cancelled on shutdown"
    assert not asyncio.all_tasks() - {asyncio.current_task()}, "a task was left running"


async def test_no_scheduler_task_is_created_when_none_is_wired():
    app, _core = _app(scheduler=None)
    await app.run()  # must not raise on a None scheduler
    assert not asyncio.all_tasks() - {asyncio.current_task()}


async def test_the_scheduler_and_the_coordinator_both_run_and_both_stop():
    scheduler, coordinator = _Recording(), _Recording()
    app, _core = _app(scheduler=scheduler, coordinator=coordinator)
    await app.run()
    assert scheduler.ticks > 0 and coordinator.ticks > 0
    assert scheduler.cancelled and coordinator.cancelled


async def test_a_scheduler_failure_leaves_replies_working():
    """Its task dying must not take message handling down with it.

    The scheduler's own `run()` swallows per-tick errors, so reaching this state means
    something escaped that loop entirely — and even then the owner must still get
    replies.
    """
    adapter = _OneMessage("are you there?")
    app, core = _app(scheduler=_Recording(fail=True), adapter=adapter)
    await app.run()
    assert adapter.channel.sent, "the owner got no reply"
    assert "are you there?" in adapter.channel.sent[0]


async def test_a_coordinator_failure_leaves_the_scheduler_ticking():
    scheduler = _Recording()
    app, _core = _app(scheduler=scheduler, coordinator=_Recording(fail=True))
    await app.run()
    assert scheduler.ticks > 0
    assert scheduler.cancelled


async def test_a_scheduler_failure_leaves_the_coordinator_running():
    coordinator = _Recording()
    app, _core = _app(scheduler=_Recording(fail=True), coordinator=coordinator)
    await app.run()
    assert coordinator.ticks > 0
    assert coordinator.cancelled


# --- The real scheduler's own isolation (9.4's test-level half) ----------


def test_the_scheduler_module_opens_no_socket_and_registers_no_handler():
    """secure-deployment delta: the scheduler introduces no inbound surface.

    Asserted against the module's own imports and calls, so a listener added later
    fails here rather than at a deploy inspection. The runtime half of this scenario
    lands at deploy time (task 11.2), where the container's listening sockets are
    compared before and after.
    """
    import ast
    import inspect

    from henk.reminders import scheduler as module

    tree = ast.parse(inspect.getsource(module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("socket", "http", "httpx", "websockets", "signal", "selectors"):
        assert forbidden not in imported, f"the scheduler imports {forbidden}"

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for forbidden in (
        "bind",
        "listen",
        "connect",
        "add_signal_handler",
        "signal",
        "create_server",
        "open_connection",
        "add_reader",
    ):
        assert forbidden not in called, f"the scheduler calls {forbidden}"


async def test_a_tick_can_only_be_caused_by_the_clock(tmp_path: Path):
    """No endpoint, socket, signal handler or message can force a tick.

    The public surface is `run()` and `tick()`; nothing subscribes the scheduler to
    anything. Asserted by driving the whole inbound path with a message and checking
    the scheduler never ran.
    """
    from henk.reminders.scheduler import ReminderScheduler as _S

    public = {
        name
        for name in dir(_S)
        if not name.startswith("_") and callable(getattr(_S, name))
    }
    assert public == {"run", "tick"}, public

    scheduler = _Recording()
    adapter = _OneMessage("/memories")
    app, _core = _app(adapter=adapter)  # deliberately NO scheduler wired
    await app.run()
    assert scheduler.ticks == 0, "an inbound message reached the scheduler"
