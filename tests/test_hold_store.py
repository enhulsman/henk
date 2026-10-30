"""maintenance-calendar task 6.2: the ``hold_windows`` and ``held_events`` tables.

Design D7: a window row is inserted (``INSERT OR IGNORE``) when its first event is
held, and its ``end`` is frozen there; a held row is keyed on the ntfy event id, so
a replay inserts nothing. Design D8: ``released_at`` is set once, guarded by
``released_at IS NULL``. Like ``reminders`` and ``handoffs``, there is no migration
mechanism, so a column drift check refuses a table this build does not recognize.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from henk.store import StoreError
from henk.store.db import HELD_EVENT_COLUMNS, HOLD_WINDOW_COLUMNS, Store
from henk.store.holds import HoldStore

W_START = 1_794_078_000.0  # Sat 7 Nov 2026 19:00Z
W_END = W_START + 7200.0


def _store(tmp_path: Path) -> HoldStore:
    return HoldStore(Store(tmp_path / "henk.db"))


def _hold(holds: HoldStore, event_id: str, *, window_id: str = "w1", end: float = W_END,
          published_at: float = W_START + 600, state: str = "firing",
          identity_key: str = "gatus:svc/api", title: str = "Homelab: patch round"):
    return holds.hold(
        window_id=window_id,
        window_title=title,
        window_start=W_START,
        window_end=end,
        event_id=event_id,
        identity_key=identity_key,
        state=state,
        title="Gatus: svc/api",
        message="triggered",
        raw_json='{"id": "%s"}' % event_id,
        published_at=published_at,
        arrival_time=published_at + 5,
        held_at=published_at + 6,
    )


def test_every_designed_column_exists_after_first_connect(tmp_path: Path):
    holds = _store(tmp_path)
    conn = holds.store.connection()
    windows = [str(r[1]) for r in conn.execute("PRAGMA table_info(hold_windows)")]
    events = [str(r[1]) for r in conn.execute("PRAGMA table_info(held_events)")]
    assert windows == list(HOLD_WINDOW_COLUMNS)
    assert set(HOLD_WINDOW_COLUMNS) == {"window_id", "title", "start", "end", "released_at"}
    assert events == list(HELD_EVENT_COLUMNS)
    assert set(HELD_EVENT_COLUMNS) == {
        "event_id", "window_id", "identity_key", "state", "title", "message",
        "raw_json", "published_at", "arrival_time", "held_at",
    }


@pytest.mark.parametrize("table,columns", [
    ("hold_windows", HOLD_WINDOW_COLUMNS),
    ("held_events", HELD_EVENT_COLUMNS),
])
def test_a_drifted_table_refuses_to_start_naming_the_column(tmp_path: Path, table, columns):
    path = tmp_path / "henk.db"
    conn = sqlite3.connect(path)
    kept = [c for c in columns if c != "title"]
    conn.execute(f"CREATE TABLE {table} ({', '.join(repr(c) + ' TEXT' for c in kept)})")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="title"):
        Store(path).connection()


def test_first_hold_inserts_the_window_with_its_end_frozen(tmp_path: Path):
    holds = _store(tmp_path)
    first = _hold(holds, "e1", end=W_END)
    assert first.status == "held"
    assert first.window_end == W_END
    # A later hold into the same window reports the FROZEN end, whatever the
    # caller now believes the end to be (D7: frozen at the first hold).
    second = _hold(holds, "e2", end=W_END + 3600)
    assert second.status == "held"
    assert second.window_end == W_END
    window = holds.windows(["w1"])["w1"]
    assert (window.title, window.start, window.end, window.released_at) == (
        "Homelab: patch round", W_START, W_END, None,
    )


def test_a_held_row_stores_every_field_the_release_rebuilds_from(tmp_path: Path):
    holds = _store(tmp_path)
    _hold(holds, "e1", published_at=W_START + 600)
    (row,) = holds.held_events("w1")
    assert row.event_id == "e1"
    assert row.window_id == "w1"
    assert row.identity_key == "gatus:svc/api"
    assert row.state == "firing"
    assert row.title == "Gatus: svc/api"
    assert row.message == "triggered"
    assert row.raw_json == '{"id": "e1"}'
    assert row.published_at == W_START + 600
    assert row.arrival_time == W_START + 605
    assert row.held_at == W_START + 606


def test_a_duplicate_event_id_inserts_nothing(tmp_path: Path):
    holds = _store(tmp_path)
    assert _hold(holds, "e1").status == "held"
    again = _hold(holds, "e1", window_id="w2")
    assert again.status == "duplicate"
    assert [r.event_id for r in holds.held_events("w1")] == ["e1"]
    # The duplicate did not create a window of its own with no held events.
    assert holds.windows(["w2"]) == {}
    assert holds.known_event_ids(["e1", "e9"]) == {"e1"}


def test_a_released_window_takes_no_new_hold(tmp_path: Path):
    holds = _store(tmp_path)
    _hold(holds, "e1")
    assert holds.mark_released("w1", at=W_END + 2000) is not None
    late = _hold(holds, "e2")
    assert late.status == "closed"
    assert [r.event_id for r in holds.held_events("w1")] == ["e1"]


def test_marking_released_is_guarded_and_returns_the_rows_once(tmp_path: Path):
    holds = _store(tmp_path)
    _hold(holds, "e1")
    _hold(holds, "e2", state="resolved", published_at=W_START + 900)
    rows = holds.mark_released("w1", at=W_END + 2000)
    assert [r.event_id for r in rows] == ["e1", "e2"]
    assert holds.windows(["w1"])["w1"].released_at == W_END + 2000
    # The guard: a second mark changes nothing and returns None.
    assert holds.mark_released("w1", at=W_END + 9000) is None
    assert holds.windows(["w1"])["w1"].released_at == W_END + 2000
    assert holds.mark_released("unknown", at=W_END) is None


def test_unreleased_windows_lists_only_windows_with_held_rows(tmp_path: Path):
    holds = _store(tmp_path)
    _hold(holds, "e1", window_id="w1")
    _hold(holds, "e2", window_id="w2")
    holds.mark_released("w2", at=W_END + 2000)
    assert [w.window_id for w in holds.unreleased_windows()] == ["w1"]


def test_held_events_are_ordered_by_publish_time_then_arrival_order(tmp_path: Path):
    holds = _store(tmp_path)
    _hold(holds, "late", published_at=W_START + 900)
    _hold(holds, "early", published_at=W_START + 100)
    _hold(holds, "same-second-second", published_at=W_START + 900)
    assert [r.event_id for r in holds.held_events("w1")] == [
        "early", "late", "same-second-second",
    ]


def test_extend_updates_the_frozen_end_of_an_unreleased_window_only(tmp_path: Path):
    holds = _store(tmp_path)
    _hold(holds, "e1")
    assert holds.extend("w1", W_END + 3600) is True
    assert holds.windows(["w1"])["w1"].end == W_END + 3600
    holds.mark_released("w1", at=W_END + 9000)
    assert holds.extend("w1", W_END + 7200) is False
    assert holds.windows(["w1"])["w1"].end == W_END + 3600


def test_a_backend_failure_is_a_store_error(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    holds = HoldStore(Store(blocker / "henk.db"))
    with pytest.raises(StoreError):
        _hold(holds, "e1")
    with pytest.raises(StoreError):
        holds.known_event_ids(["e1"])
