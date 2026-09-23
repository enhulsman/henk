"""The handoff archive's table and repository (triage-quality task 6.1).

From `specs/triage-handoff` ("Published handoffs are retained locally, bounded by
count and age") and design D8. Real `sqlite3` files throughout (`tmp_path`), never a
double: the retention guarantee is "in the insert's transaction", which is a
property of the driver and of `Store.transaction`, not of a fake.

Placeholder data only (standing rule 1): `host-a.example`, `example-a.service`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from henk.store import Store, StoreError, build_stores
from henk.store import handoffs as handoffs_module
from henk.store.db import HANDOFF_COLUMNS
from henk.store.handoffs import (
    HANDOFF_DOCUMENT_MAX_BYTES,
    HANDOFF_MAX_AGE_SECONDS,
    HANDOFF_MAX_COUNT,
    HandoffStore,
    RetainedHandoff,
)

NOW = 1_790_000_000.0
DAY = 86_400.0

IDENTITY = ("grafana:HenkSwapPressure",)
RULE = ("grafana:HenkSwapPressure",)
NODES = ("vps",)


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _store(tmp_path: Path, clock: _Clock | None = None) -> Store:
    return Store(tmp_path / "store" / "henk.db", clock=clock or _Clock())


def _archive(tmp_path: Path, clock: _Clock | None = None) -> HandoffStore:
    return HandoffStore(_store(tmp_path, clock))


def _retain(archive: HandoffStore, document: str = "Trigger: swap on host-a.example",
            message_id: str | None = "hf-1", **kwargs) -> RetainedHandoff:
    params = {
        "message_id": message_id,
        "identity_keys": IDENTITY,
        "rule_keys": RULE,
        "nodes": NODES,
    }
    params.update(kwargs)
    return archive.retain(document, **params)


def _seed(store: Store, rows: list[tuple[float, str]]) -> None:
    """Insert rows directly (published_at, document), bypassing retention."""
    with store.transaction() as conn:
        conn.executemany(
            "INSERT INTO handoffs (message_id, published_at, document, truncated, "
            "identity_keys, rule_keys, nodes) VALUES (?, ?, ?, 0, '[]', '[]', '[]')",
            [(f"seed-{i}", at, doc) for i, (at, doc) in enumerate(rows)],
        )


def _all_rows(store: Store) -> list[sqlite3.Row]:
    return store.connection().execute(
        "SELECT * FROM handoffs ORDER BY id"
    ).fetchall()


# --- The bounds are the design's module constants -------------------------


def test_the_bounds_are_the_designed_constants():
    # D8 and the triage-handoff spec pin these; task 1.4 confirmed they fit the
    # measured triage rate (17 in the worst 90-day window), so they are not config.
    assert HANDOFF_MAX_COUNT == 500
    assert HANDOFF_MAX_AGE_SECONDS == 90 * 86_400
    assert HANDOFF_DOCUMENT_MAX_BYTES == 32 * 1024


# --- Round trip -----------------------------------------------------------


def test_a_retained_handoff_round_trips(tmp_path: Path):
    archive = _archive(tmp_path)
    written = _retain(
        archive,
        "Trigger: HenkSwapPressure on vps\nPickup: henk-pickup",
        message_id="hf-42",
        identity_keys=("grafana:HenkSwapPressure", "gatus:core/example-a"),
        rule_keys=("grafana:HenkSwapPressure", "gatus:core/example-a"),
        nodes=("vps", "rp5"),
    )
    assert isinstance(written.id, int) and written.id >= 1
    [read] = archive.eligible()
    assert read == written
    assert read.message_id == "hf-42"
    assert read.published_at == NOW
    assert read.document == "Trigger: HenkSwapPressure on vps\nPickup: henk-pickup"
    assert read.truncated is False
    assert read.identity_keys == ("grafana:HenkSwapPressure", "gatus:core/example-a")
    assert read.rule_keys == ("grafana:HenkSwapPressure", "gatus:core/example-a")
    assert read.nodes == ("vps", "rp5")


def test_keys_and_nodes_are_stored_as_json_arrays(tmp_path: Path):
    archive = _archive(tmp_path)
    _retain(archive, nodes=("vps", "rp2"))
    [row] = _all_rows(archive.store)
    assert json.loads(row["identity_keys"]) == list(IDENTITY)
    assert json.loads(row["rule_keys"]) == list(RULE)
    assert json.loads(row["nodes"]) == ["vps", "rp2"]


def test_the_document_is_stored_as_given(tmp_path: Path):
    # "document (as published)": no stripping, no reformatting.
    archive = _archive(tmp_path)
    text = "  Trigger: x\n\n  Evidence: example-a.service  \n"
    _retain(archive, text)
    [read] = archive.eligible()
    assert read.document == text


def test_eligible_is_newest_first(tmp_path: Path):
    clock = _Clock()
    archive = _archive(tmp_path, clock)
    first = _retain(archive, "first")
    clock.now += 60
    second = _retain(archive, "second")
    assert [h.id for h in archive.eligible()] == [second.id, first.id]


def test_a_handoff_without_incident_identity_is_refused(tmp_path: Path):
    # It could never relate to a later incident (spec: owner sessions are not
    # retained), so the repository refuses it too rather than store a dead row.
    archive = _archive(tmp_path)
    with pytest.raises(ValueError):
        _retain(archive, identity_keys=())
    assert archive.count() == 0


def test_a_backend_failure_is_a_store_error(tmp_path: Path):
    archive = _archive(tmp_path)
    archive.store.connection().execute("DROP TABLE handoffs")
    with pytest.raises(StoreError):
        _retain(archive)


# --- Message ids ----------------------------------------------------------


def test_an_empty_message_id_is_stored_as_null(tmp_path: Path):
    archive = _archive(tmp_path)
    _retain(archive, message_id="")
    _retain(archive, message_id="   ")
    _retain(archive, message_id=None)
    rows = _all_rows(archive.store)
    assert [row["message_id"] for row in rows] == [None, None, None]
    assert all(h.message_id is None for h in archive.eligible())


def test_lookup_accepts_the_bare_id_and_the_full_result_string(tmp_path: Path):
    # The pipeline's recurrence ref is the audit record's handoff_message_id,
    # which is the tool's whole result string (henk/events/pipeline.py:170-172).
    archive = _archive(tmp_path)
    written = _retain(archive, message_id="Ab3xYz9")
    assert archive.find_by_message_ref("Ab3xYz9") == written
    assert archive.find_by_message_ref("handoff published (id: Ab3xYz9)") == written
    assert archive.find_by_message_ref("handoff published (id: other)") is None
    assert archive.find_by_message_ref("") is None
    assert archive.find_by_message_ref("handoff published (id: )") is None
    assert archive.find_by_message_ref(None) is None


def test_lookup_never_matches_a_null_id(tmp_path: Path):
    archive = _archive(tmp_path)
    _retain(archive, message_id=None)
    assert archive.find_by_message_ref("") is None
    assert archive.find_by_message_ref("None") is None
    assert archive.find_by_message_ref("null") is None


# --- The 32 KB marker and flag --------------------------------------------


def test_a_document_at_the_byte_bound_is_not_truncated(tmp_path: Path):
    archive = _archive(tmp_path)
    text = "a" * HANDOFF_DOCUMENT_MAX_BYTES
    written = _retain(archive, text)
    assert written.truncated is False
    assert written.document == text


def test_a_longer_document_is_truncated_with_a_marker_and_flag(tmp_path: Path):
    archive = _archive(tmp_path)
    text = "Evidence line for example-a.service\n" * 2_000  # ~72 KB
    written = _retain(archive, text)
    [read] = archive.eligible()
    assert read == written
    assert read.truncated is True
    [row] = _all_rows(archive.store)
    assert row["truncated"] == 1
    stored = read.document
    assert len(stored.encode("utf-8")) <= HANDOFF_DOCUMENT_MAX_BYTES
    # Visible, and never a silent cut: it names the original size.
    assert "[handoff truncated" in stored
    assert str(len(text.encode("utf-8"))) in stored
    prefix = stored[: stored.index("\n[handoff truncated")]
    assert text.startswith(prefix)
    # Most of the bound is spent on the document, not the marker.
    assert len(prefix.encode("utf-8")) > HANDOFF_DOCUMENT_MAX_BYTES - 200


def test_one_byte_over_the_bound_is_truncated(tmp_path: Path):
    archive = _archive(tmp_path)
    written = _retain(archive, "b" * (HANDOFF_DOCUMENT_MAX_BYTES + 1))
    assert written.truncated is True
    assert len(written.document.encode("utf-8")) <= HANDOFF_DOCUMENT_MAX_BYTES


def test_the_bound_is_bytes_and_a_multibyte_character_is_never_split(tmp_path: Path):
    # 11,000 three-byte characters: 11,000 characters (under 32 K) but 33,000 bytes.
    archive = _archive(tmp_path)
    text = "€" * 11_000
    written = _retain(archive, text)
    assert written.truncated is True
    stored = written.document
    raw = stored.encode("utf-8")
    assert len(raw) <= HANDOFF_DOCUMENT_MAX_BYTES
    raw.decode("utf-8", errors="strict")  # valid UTF-8 end to end
    prefix = stored[: stored.index("\n[handoff truncated")]
    assert set(prefix) == {"€"}


# --- Retention holds its bounds -------------------------------------------


def test_retention_holds_the_count_bound_oldest_first(tmp_path: Path):
    clock = _Clock()
    store = _store(tmp_path, clock)
    archive = HandoffStore(store)
    # Exactly the maximum, all inside the age bound, oldest first by publish time.
    _seed(store, [(NOW - 1_000 + i, f"doc-{i}") for i in range(HANDOFF_MAX_COUNT)])
    assert archive.count() == HANDOFF_MAX_COUNT

    new = _retain(archive, "the newest")
    assert archive.count() == HANDOFF_MAX_COUNT
    documents = {row["document"] for row in _all_rows(store)}
    assert "doc-0" not in documents  # the oldest went
    assert "doc-1" in documents      # and only the oldest
    assert "the newest" in documents
    assert archive.eligible()[0] == new


def test_count_pruning_orders_by_publish_time_not_by_row_id(tmp_path: Path):
    # Row ids and publish times disagree here (a seeded row with a low id but a
    # late publish time). "Oldest first" is by publish time.
    store = _store(tmp_path)
    archive = HandoffStore(store)
    rows = [(NOW - 10, "late-low-id")]
    rows += [(NOW - 5_000 + i, f"doc-{i}") for i in range(HANDOFF_MAX_COUNT - 1)]
    _seed(store, rows)
    _retain(archive, "the newest")
    documents = {row["document"] for row in _all_rows(store)}
    assert "late-low-id" in documents
    assert "doc-0" not in documents
    assert archive.count() == HANDOFF_MAX_COUNT


def test_retention_holds_the_age_bound(tmp_path: Path):
    store = _store(tmp_path)
    archive = HandoffStore(store)
    cutoff = NOW - HANDOFF_MAX_AGE_SECONDS
    _seed(
        store,
        [
            (cutoff - 30 * DAY, "far too old"),
            (cutoff - 1, "just too old"),
            (cutoff, "exactly at the bound"),
            (NOW - DAY, "recent"),
        ],
    )
    _retain(archive, "new")
    documents = [row["document"] for row in _all_rows(store)]
    assert documents == ["exactly at the bound", "recent", "new"]
    assert all(
        h.published_at >= NOW - HANDOFF_MAX_AGE_SECONDS for h in archive.eligible()
    )


def test_both_bounds_together(tmp_path: Path):
    store = _store(tmp_path)
    archive = HandoffStore(store)
    old = [(NOW - HANDOFF_MAX_AGE_SECONDS - DAY - i, f"old-{i}") for i in range(10)]
    fresh = [(NOW - 1_000 + i, f"fresh-{i}") for i in range(HANDOFF_MAX_COUNT)]
    _seed(store, old + fresh)
    _retain(archive, "new")
    documents = [row["document"] for row in _all_rows(store)]
    assert len(documents) == HANDOFF_MAX_COUNT
    assert not any(d.startswith("old-") for d in documents)
    assert "fresh-0" not in documents
    assert documents[-1] == "new"


def test_the_new_handoff_is_never_pruned_by_its_own_insert(tmp_path: Path):
    # A clock that stepped back leaves the full table's rows "newer" than the
    # handoff being retained. The write must still land: a retain that reports
    # success and stored nothing is the silent no-op D8 rules out.
    store = _store(tmp_path)
    archive = HandoffStore(store)
    _seed(store, [(NOW + 1_000 + i, f"future-{i}") for i in range(HANDOFF_MAX_COUNT)])
    new = _retain(archive, "retained under a stepped-back clock")
    assert archive.count() == HANDOFF_MAX_COUNT
    ids = {row["id"] for row in _all_rows(store)}
    assert new.id in ids


def test_retention_runs_in_the_inserts_transaction(tmp_path: Path, monkeypatch):
    # If pruning fails, the insert must not have been committed on its own:
    # both happen together or neither does.
    store = _store(tmp_path)
    archive = HandoffStore(store)
    _seed(store, [(NOW - 10, "existing")])

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("prune failed")

    monkeypatch.setattr(handoffs_module, "_prune_locked", boom)
    with pytest.raises(StoreError):
        _retain(archive, "must roll back")
    documents = [row["document"] for row in _all_rows(store)]
    assert documents == ["existing"]


def test_retention_runs_after_the_insert(tmp_path: Path):
    # Pruning before the insert would leave MAX + 1 rows.
    store = _store(tmp_path)
    archive = HandoffStore(store)
    _seed(store, [(NOW - 1_000 + i, f"doc-{i}") for i in range(HANDOFF_MAX_COUNT)])
    for n in range(3):
        _retain(archive, f"new-{n}")
        assert archive.count() == HANDOFF_MAX_COUNT


def test_eligible_excludes_rows_past_the_age_bound_before_any_prune(tmp_path: Path):
    # Pruning runs only on insert; a quiet stretch must not make a 100-day-old
    # handoff eligible again. "Only handoffs within retention are eligible" (D9).
    clock = _Clock()
    archive = _archive(tmp_path, clock)
    old = _retain(archive, "old", message_id="hf-old")
    clock.now += HANDOFF_MAX_AGE_SECONDS + 1
    assert archive.eligible() == []
    assert archive.find_by_message_ref("hf-old") is None
    assert archive.count() == 1  # still on disk until the next insert prunes it
    clock.now -= 2  # back inside the bound
    assert archive.eligible() == [old]


def test_eligible_is_bounded_by_the_count(tmp_path: Path):
    store = _store(tmp_path)
    archive = HandoffStore(store)
    _seed(store, [(NOW - 5_000 + i, f"doc-{i}") for i in range(HANDOFF_MAX_COUNT + 5)])
    listed = archive.eligible()
    assert len(listed) == HANDOFF_MAX_COUNT
    assert listed[0].document == f"doc-{HANDOFF_MAX_COUNT + 4}"


# --- Row ids are never reused (audit v5's prior_handoff_ids) ---------------


def test_the_id_column_is_autoincrement(tmp_path: Path):
    store = _store(tmp_path)
    [sql] = store.connection().execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'handoffs'"
    ).fetchone()
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in " ".join(sql.split())


def test_a_row_id_is_never_reused_after_its_row_is_deleted(tmp_path: Path):
    # A bare rowid table hands out max(rowid) + 1, so deleting the newest row
    # (an owner-side purge, D7 residual 2) would reissue its id, and an audit
    # record's prior_handoff_ids would silently point at a different handoff.
    archive = _archive(tmp_path)
    first = _retain(archive, "one")
    second = _retain(archive, "two")
    archive.store.connection().execute("DELETE FROM handoffs")
    third = _retain(archive, "three")
    assert third.id > second.id > first.id


def test_a_row_id_is_never_reused_after_retention_empties_the_table(tmp_path: Path):
    clock = _Clock()
    archive = _archive(tmp_path, clock)
    first = _retain(archive, "one")
    archive.store.connection().execute("DELETE FROM handoffs WHERE id = ?", (first.id,))
    second = _retain(archive, "two")
    assert second.id != first.id


# --- _check_handoffs_columns ----------------------------------------------


def test_every_designed_column_exists_after_first_connect(tmp_path: Path):
    store = _store(tmp_path)
    live = [
        str(row[1])
        for row in store.connection().execute("PRAGMA table_info(handoffs)")
    ]
    assert live == list(HANDOFF_COLUMNS)
    assert set(HANDOFF_COLUMNS) == {
        "id",
        "message_id",
        "published_at",
        "document",
        "truncated",
        "identity_keys",
        "rule_keys",
        "nodes",
    }


def test_message_id_is_the_only_nullable_data_column(tmp_path: Path):
    store = _store(tmp_path)
    info = {
        str(row[1]): row
        for row in store.connection().execute("PRAGMA table_info(handoffs)")
    }
    assert info["message_id"][3] == 0, "message_id must be nullable"
    for column in ("published_at", "document", "truncated", "identity_keys",
                   "rule_keys", "nodes"):
        assert info[column][3] == 1, f"{column} must be NOT NULL"


def test_a_drifted_handoffs_table_fails_loudly_naming_the_missing_column(tmp_path: Path):
    path = tmp_path / "store" / "henk.db"
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(str(path))
    columns = ", ".join(
        f"{c} TEXT" for c in HANDOFF_COLUMNS if c not in ("id", "nodes")
    )
    raw.execute(f"CREATE TABLE handoffs (id INTEGER PRIMARY KEY AUTOINCREMENT, {columns})")
    raw.commit()
    raw.close()

    with pytest.raises(StoreError) as exc:
        Store(path).connection()
    message = str(exc.value)
    assert "handoffs" in message
    assert "nodes" in message
    assert "migration" in message.lower()


def test_an_unexpected_handoffs_column_is_named_too(tmp_path: Path):
    path = tmp_path / "store" / "henk.db"
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(str(path))
    columns = ", ".join(f"{c} TEXT" for c in HANDOFF_COLUMNS if c != "id")
    raw.execute(
        "CREATE TABLE handoffs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        f"{columns}, host_address TEXT)"
    )
    raw.commit()
    raw.close()

    with pytest.raises(StoreError) as exc:
        Store(path).connection()
    assert "host_address" in str(exc.value)


def test_a_handoffs_table_without_autoincrement_is_refused(tmp_path: Path):
    # Same columns, bare rowid: ids could be reissued under the audit log.
    path = tmp_path / "store" / "henk.db"
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(str(path))
    columns = ", ".join(f"{c} TEXT" for c in HANDOFF_COLUMNS if c != "id")
    raw.execute(f"CREATE TABLE handoffs (id INTEGER PRIMARY KEY, {columns})")
    raw.commit()
    raw.close()

    with pytest.raises(StoreError) as exc:
        Store(path).connection()
    assert "AUTOINCREMENT" in str(exc.value)


def test_the_reminders_check_still_names_its_own_table(tmp_path: Path):
    # The two guards share one helper; the reminders refusal keeps its wording.
    from henk.store.db import REMINDER_COLUMNS

    path = tmp_path / "store" / "henk.db"
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(str(path))
    columns = ", ".join(f"{c} TEXT" for c in REMINDER_COLUMNS if c not in ("id", "text"))
    raw.execute(f"CREATE TABLE reminders (id INTEGER PRIMARY KEY, {columns})")
    raw.commit()
    raw.close()
    with pytest.raises(StoreError) as exc:
        Store(path).connection()
    assert "reminders table" in str(exc.value)
    assert "handoffs" not in str(exc.value)


# --- One store file (secure-deployment delta) ------------------------------


def test_retained_handoffs_share_the_one_store_file(tmp_path: Path):
    from henk.config import StoreConfig

    stores = build_stores(StoreConfig(path=str(tmp_path / "henk-store.db")))
    assert stores.handoffs.store is stores.store
    assert stores.memories._store is stores.store  # noqa: SLF001 - identity check
    _retain(stores.handoffs)
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".db") == [
        "henk-store.db"
    ]
    stores.store.close()
