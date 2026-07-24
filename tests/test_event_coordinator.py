"""Event-coordinator dispatch tests (task 3.2/3.3 glue).

Exercises the pure batch-dispatch step: a triageable batch submits one event
turn; a cooldown re-fire submits nothing but writes a suppression audit record.
The async debounce timer in ``run`` is deploy-verified (5.3), not unit-tested.
"""

from __future__ import annotations

import json
from pathlib import Path

from henk.audit import AuditLog
from henk.events.coordinator import EventCoordinator
from henk.events.intake import EventIntake
from henk.events.pipeline import EventPipeline, PipelineConfig
from henk.events.types import Event

HOUR = 3600.0


class RecordingCore:
    def __init__(self) -> None:
        self.turns: list = []
        self.markers: list = []

    async def submit_event(self, turn) -> None:
        self.turns.append(turn)

    async def submit_marker(self, marker) -> None:
        self.markers.append(marker)


def _event(mid: str, title: str, message: str = "") -> Event:
    return Event(id=mid, title=title, message=message, arrival_time=0.0)


def _coordinator(tmp_path: Path) -> tuple[EventCoordinator, RecordingCore, Path]:
    core = RecordingCore()
    audit_path = tmp_path / "a.jsonl"
    pipeline = EventPipeline(
        PipelineConfig(cooldown_seconds=6 * HOUR, cap_per_24h=3, cooldown_overrides=())
    )
    coord = EventCoordinator(
        EventIntake.__new__(EventIntake),  # intake unused by dispatch_batch
        pipeline,
        core,
        audit=AuditLog(audit_path),
    )
    return coord, core, audit_path


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def test_triageable_batch_submits_one_event_turn(tmp_path):
    coord, core, audit_path = _coordinator(tmp_path)
    await coord.dispatch_batch([_event("e1", "Gatus: svc/api", "triggered")], now=0.0)
    assert len(core.turns) == 1
    assert len(core.turns[0].items) == 1
    assert _records(audit_path) == []  # nothing suppressed


async def test_cooldown_refire_submits_nothing_and_audits_suppression(tmp_path):
    coord, core, audit_path = _coordinator(tmp_path)
    await coord.dispatch_batch([_event("e1", "Gatus: svc/api")], now=0.0)
    core.turns.clear()
    await coord.dispatch_batch([_event("e2", "Gatus: svc/api")], now=1 * HOUR)
    assert core.turns == []  # inside cooldown → no conversation
    recs = _records(audit_path)
    assert len(recs) == 1
    assert recs[0]["record_type"] == "suppression"
    assert recs[0]["reason"] == "cooldown"
    assert recs[0]["identity_key"] == "gatus:svc/api"


# --- Checkpoint offset / marker (design D1: advance only when durable) -----


async def test_event_turn_carries_batch_last_seen_offset(tmp_path):
    coord, core, _ = _coordinator(tmp_path)
    batch = [_event("e1", "Gatus: svc/a"), _event("e2", "Gatus: svc/b")]
    await coord.dispatch_batch(batch, now=0.0)
    # The turn carries the batch's last-seen id so the core can checkpoint it
    # only after the triage record is durable.
    assert core.turns[0].offset == "e2"
    assert core.markers == []


async def test_suppression_only_batch_enqueues_checkpoint_marker(tmp_path):
    coord, core, audit_path = _coordinator(tmp_path)
    await coord.dispatch_batch([_event("e1", "Gatus: svc/api")], now=0.0)  # triaged
    core.turns.clear()
    # Re-fire inside cooldown → suppression only, no turn — but the checkpoint
    # must still advance, via a marker riding the same FIFO queue.
    await coord.dispatch_batch([_event("e9", "Gatus: svc/api")], now=1 * HOUR)
    assert core.turns == []
    assert len(core.markers) == 1
    assert core.markers[0].offset == "e9"


async def test_suppression_marker_withheld_when_audit_write_fails(tmp_path):
    # If the suppression record could not be persisted, the checkpoint must NOT
    # advance past it (gated on the audit write) — the event replays instead.
    core = RecordingCore()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    pipeline = EventPipeline(PipelineConfig(cooldown_seconds=6 * HOUR))
    coord = EventCoordinator(
        EventIntake.__new__(EventIntake), pipeline, core,
        audit=AuditLog(blocker / "a.jsonl"),  # unwritable → write() returns False
    )
    await coord.dispatch_batch([_event("e1", "Gatus: svc/api")], now=0.0)  # triaged
    core.turns.clear()
    await coord.dispatch_batch([_event("e9", "Gatus: svc/api")], now=1 * HOUR)
    assert core.markers == []  # suppression write failed → no advance


async def test_dispatch_defaults_now_to_wall_clock_not_monotonic(tmp_path):
    # D4: cadence decisions run on wall-clock. When no explicit now is given the
    # coordinator must read its wall clock (comparable to persisted `at`), never
    # the monotonic debounce clock.
    core = RecordingCore()
    pipeline = EventPipeline(PipelineConfig(cooldown_seconds=6 * HOUR))
    coord = EventCoordinator(
        EventIntake.__new__(EventIntake), pipeline, core,
        audit=AuditLog(tmp_path / "a.jsonl"),
        wall_clock=lambda: 1_700_000_000.0,
        mono_clock=lambda: 42.0,
    )
    await coord.dispatch_batch([_event("e1", "Gatus: svc/api")])  # no explicit now
    await coord.dispatch_batch([_event("e2", "Gatus: svc/api")])  # re-fire, suppressed
    rec = _records(tmp_path / "a.jsonl")[0]
    assert rec["at"] == 1_700_000_000.0  # stamped from the wall clock, not 42.0


def test_default_clocks_are_wall_and_monotonic():
    import time

    core = RecordingCore()
    coord = EventCoordinator(
        EventIntake.__new__(EventIntake),
        EventPipeline(PipelineConfig()),
        core,
    )
    assert coord._wall_clock is time.time
    assert coord._mono_clock is time.monotonic
