"""Shared rig for the maintenance-calendar hold and release tests (group 6).

One SQLite file, one audit log, one window source and one wall clock, wired the way
task 7.3 will wire them: a :class:`HoldLedger` over the shared store, an
:class:`EventCoordinator` that classifies through it, and a :class:`HoldReleaser`
that releases through the coordinator. :func:`restart` builds fresh components over
the same files, which is how a process restart is simulated.

No network and no real time: the clock is a settable value, the window source a fake
with :class:`MaintenanceWindows`' query semantics, and :class:`ScriptedIntake` stands
in for ntfy so the coordinator's ``run()`` can be driven step by step.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from henk.audit import AuditLog
from henk.audit.logger import HoldReceipts
from henk.calendar.windows import MaintenanceWindow
from henk.events.coordinator import EventCoordinator
from henk.events.holds import HoldLedger
from henk.events.pipeline import EventPipeline, PipelineConfig
from henk.events.release import HoldReleaser
from henk.events.types import Event
from henk.store.db import Store
from henk.store.holds import HoldStore

MIN = 60.0
HOUR = 3600.0
#: Saturday 7 November 2026, 20:00 CET (19:00Z) to 22:00 CET; grace 30 minutes.
W_START = datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc).timestamp()
W_END = W_START + 2 * HOUR
GRACE = 30 * MIN
DUE = W_END + GRACE  # 22:30 CET
TITLE = "Homelab: patch round"
PATCH = MaintenanceWindow(id="evt-patch", title=TITLE, start=W_START, end=W_END)


def at(hhmm: str) -> float:
    """Epoch seconds for ``HH:MM`` CET on the window's evening (7 November 2026)."""
    hours, minutes = (int(x) for x in hhmm.split(":"))
    return W_START + (hours - 20) * HOUR + minutes * MIN


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeWindows:
    """A window source with :class:`MaintenanceWindows`' query semantics."""

    def __init__(self, *windows: MaintenanceWindow, grace: float = GRACE) -> None:
        self.windows = list(windows)
        self.grace = grace

    def active(self, t: float) -> tuple[MaintenanceWindow, ...]:
        return tuple(
            w
            for w in sorted(self.windows, key=lambda w: w.start)
            if w.start <= t < w.end + self.grace
        )

    def active_window(self, window_id: str, t: float) -> MaintenanceWindow | None:
        for w in self.active(t):
            if w.id == window_id:
                return w
        return None


class RecordingCore:
    """The core as the coordinator sees it: records turns and markers."""

    def __init__(self) -> None:
        self.turns: list = []
        self.markers: list = []

    async def submit_event(self, turn) -> None:
        self.turns.append(turn)

    async def submit_marker(self, marker) -> None:
        self.markers.append(marker)


class GatedCore(RecordingCore):
    """A core whose ``submit_event`` suspends until the test opens the gate."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()

    async def submit_event(self, turn) -> None:
        self.waiting.set()
        await self.gate.wait()
        self.turns.append(turn)


class ScriptedIntake:
    """Stands in for :class:`EventIntake` under the coordinator's pump.

    The test pushes ``"subscribe"``, ``"keepalive"``, ``"unsubscribe"`` or an
    :class:`Event`; the
    generator replays them in order, invoking the callbacks the coordinator passed,
    exactly where the real intake invokes them, and otherwise waits.
    """

    def __init__(self) -> None:
        self.script: asyncio.Queue = asyncio.Queue()

    def push(self, *items: Any) -> None:
        for item in items:
            self.script.put_nowait(item)

    async def events(self, *, on_subscribe=None, on_keepalive=None, on_unsubscribe=None):
        while True:
            item = await self.script.get()
            if item == "subscribe":
                if on_subscribe is not None:
                    on_subscribe()
            elif item == "keepalive":
                if on_keepalive is not None:
                    on_keepalive()
            elif item == "unsubscribe":
                if on_unsubscribe is not None:
                    on_unsubscribe()
            else:
                yield item


def alert(
    mid: str,
    name: str = "svc/api",
    *,
    state: str = "firing",
    published: float | None = None,
    arrival: float | None = None,
    message: str | None = None,
) -> Event:
    """A Gatus event as intake builds it: publish time from the frame's ``time``."""
    title = f"Gatus: {name}"
    text = message if message is not None else (
        "triggered" if state == "firing" else "resolved"
    )
    raw: dict[str, Any] = {
        "id": mid,
        "event": "message",
        "topic": "henk-events",
        "title": title,
        "message": text,
    }
    if published is not None:
        raw["time"] = int(published)
    return Event(
        id=mid,
        title=title,
        message=text,
        arrival_time=arrival if arrival is not None else (published or 0.0),
        raw=raw,
        published_at=published,
    )


@dataclass
class Rig:
    tmp_path: Path
    clock: Clock
    windows: FakeWindows | None
    store: Store
    holds: HoldStore
    audit: Any
    ledger: HoldLedger
    pipeline: EventPipeline
    core: Any
    intake: ScriptedIntake
    coord: EventCoordinator
    releaser: HoldReleaser
    options: dict = field(default_factory=dict)

    @property
    def audit_path(self) -> Path:
        return self.tmp_path / "henk-audit.jsonl"

    def records(self, record_type: str | None = None) -> list[dict]:
        if not self.audit_path.exists():
            return []
        out = [
            json.loads(line)
            for line in self.audit_path.read_text().splitlines()
            if line.strip()
        ]
        if record_type is None:
            return out
        return [r for r in out if r.get("record_type") == record_type]

    def caught_up(self) -> None:
        """Intake subscribed and saw a keepalive, with nothing in flight."""
        self.coord.note_subscribe()
        self.coord.note_keepalive()

    def window_row(self, window_id: str = PATCH.id):
        return self.holds.windows([window_id]).get(window_id)


def build(
    tmp_path: Path,
    *,
    now: float = W_START,
    windows: FakeWindows | None | str = "default",
    core: Any | None = None,
    audit: Any | None | str = "default",
    cooldown: float = 6 * HOUR,
    cap: int = 3,
    grace: float = GRACE,
    debounce: float = 0.0,
    clock: Clock | None = None,
    **releaser_options: Any,
) -> Rig:
    clock = clock or Clock(now)
    if windows == "default":
        windows = FakeWindows(PATCH, grace=grace)
    store = Store(tmp_path / "henk.db")
    holds = HoldStore(store)
    if audit == "default":
        audit = AuditLog(tmp_path / "henk-audit.jsonl", clock=clock)
    ledger = HoldLedger(holds, HoldReceipts(audit), clock=clock)
    pipeline = EventPipeline(
        PipelineConfig(cooldown_seconds=cooldown, cap_per_24h=cap, cooldown_overrides=())
    )
    core = core if core is not None else RecordingCore()
    intake = ScriptedIntake()
    coord = EventCoordinator(
        intake,
        pipeline,
        core,
        debounce_seconds=debounce,
        audit=audit,
        holds=ledger,
        windows=windows,
        wall_clock=clock,
        mono_clock=lambda: 0.0,
    )
    releaser = HoldReleaser(
        ledger,
        coord,
        grace_seconds=grace,
        windows=windows,
        clock=clock,
        **releaser_options,
    )
    return Rig(
        tmp_path=tmp_path,
        clock=clock,
        windows=windows,
        store=store,
        holds=holds,
        audit=audit,
        ledger=ledger,
        pipeline=pipeline,
        core=core,
        intake=intake,
        coord=coord,
        releaser=releaser,
        options=dict(
            cooldown=cooldown, cap=cap, grace=grace, debounce=debounce, **releaser_options
        ),
    )


def restart(rig: Rig, *, windows: FakeWindows | None | str = "same", core: Any | None = None,
            **overrides: Any) -> Rig:
    """Fresh components over the same SQLite file, audit log and clock."""
    rig.store.close()
    options = dict(rig.options)
    options.update(overrides)
    return build(
        rig.tmp_path,
        windows=rig.windows if windows == "same" else windows,
        core=core,
        clock=rig.clock,
        **options,
    )


async def settle(rounds: int = 50) -> None:
    """Let every ready task run to its next real wait (no wall time passes)."""
    for _ in range(rounds):
        await asyncio.sleep(0)
