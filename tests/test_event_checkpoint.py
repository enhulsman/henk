"""Intake-offset checkpoint store tests (task 1.2), from specs/event-intake.

The checkpoint is a tiny durable last-seen-id file on the audit volume. It must
round-trip the id across a simulated container recreation (new instance, same
path), be last-write-wins, return ``None`` when no file exists yet, and — like
the audit writer — never raise on a write failure (loud ERROR, non-blocking).
"""

from __future__ import annotations

import logging
from pathlib import Path

from henk.events.checkpoint import OffsetCheckpoint


def test_missing_file_reads_as_none(tmp_path: Path):
    cp = OffsetCheckpoint(tmp_path / "intake-offset")
    assert cp.read() is None


def test_write_then_read_round_trips(tmp_path: Path):
    cp = OffsetCheckpoint(tmp_path / "intake-offset")
    assert cp.write("abc123") is True
    assert cp.read() == "abc123"


def test_survives_recreation_new_instance_same_path(tmp_path: Path):
    path = tmp_path / "intake-offset"
    OffsetCheckpoint(path).write("id-1")
    # A fresh process/instance pointed at the same path must see the id.
    assert OffsetCheckpoint(path).read() == "id-1"


def test_last_write_wins(tmp_path: Path):
    cp = OffsetCheckpoint(tmp_path / "intake-offset")
    cp.write("first")
    cp.write("second")
    assert cp.read() == "second"
    # Only one line/value is retained — it is a cursor, not a log.
    assert (tmp_path / "intake-offset").read_text().strip() == "second"


def test_write_creates_parent_directory(tmp_path: Path):
    cp = OffsetCheckpoint(tmp_path / "nested" / "dir" / "intake-offset")
    assert cp.write("x") is True
    assert cp.read() == "x"


def test_write_failure_is_logged_not_raised(tmp_path: Path, caplog):
    # Parent path is a FILE, so mkdir/open must fail — mirror AuditLog discipline.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    cp = OffsetCheckpoint(blocker / "intake-offset")
    with caplog.at_level(logging.ERROR, logger="henk.events.checkpoint"):
        ok = cp.write("id-1")
    assert ok is False  # non-blocking: returns False rather than raising
    assert any("checkpoint write failed" in r.message for r in caplog.records)


def test_read_blank_file_is_none(tmp_path: Path):
    path = tmp_path / "intake-offset"
    path.write_text("   \n")
    assert OffsetCheckpoint(path).read() is None
