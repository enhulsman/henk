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

    async def submit_event(self, turn) -> None:
        self.turns.append(turn)


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
