"""The reminders table and repository (group 2), from the reminders spec.

Real `sqlite3` files throughout (`tmp_path`), never a double: the transaction
contract this repository depends on is a property of the driver, and the durability
assertions use a real hard close and a real process kill.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from henk.store import Store, StoreError
from henk.store.db import REMINDER_COLUMNS
from henk.store.reminders import (
    CANCELLED,
    PENDING,
    STATUSES,
    ContentTooLongError,
    EmptyContentError,
    ReminderCapReachedError,
    ReminderStore,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A fixed instant well clear of any transition, plus a plausible zone key. These
#: tests are about storage, not resolution — group 4 owns the clock.
NOW = 1_787_000_000.0
DUE = NOW + 3600.0
TZ = "Europe/Amsterdam"


def _store(tmp_path: Path, **kwargs) -> Store:
    return Store(tmp_path / "store" / "henk.db", clock=lambda: NOW, **kwargs)


def _reminders(tmp_path: Path, **kwargs) -> ReminderStore:
    return ReminderStore(_store(tmp_path), **kwargs)


def _schedule(repo: ReminderStore, text: str = "call the plumber", **kwargs):
    params = {
        "due_at": DUE,
        "due_tz": TZ,
        "input_spec": "+1h",
        "source": "tool",
    }
    params.update(kwargs)
    return repo.schedule(text, **params)


# --- Schema (task 2.1) ----------------------------------------------------


def test_every_designed_column_exists_after_first_connect(tmp_path: Path):
    store = _store(tmp_path)
    conn = store.connection()
    live = [str(row[1]) for row in conn.execute("PRAGMA table_info(reminders)")]
    assert live == list(REMINDER_COLUMNS)
    # The five delivery-half columns ship here because there is no migration path.
    for column in (
        "next_attempt_at",
        "send_attempts",
        "delivered_at",
        "surfaced_at",
        "reported_at",
    ):
        assert column in live
    store.close()


def test_next_attempt_at_is_not_nullable(tmp_path: Path):
    # A null would make a pending row permanently unselectable by delivery's
    # query, since `NULL <= now` is not true: a reminder that exists, says
    # pending, and can never fire.
    store = _store(tmp_path)
    info = {
        str(row[1]): row for row in store.connection().execute(
            "PRAGMA table_info(reminders)"
        )
    }
    assert info["next_attempt_at"][3] == 1, "next_attempt_at must be NOT NULL"
    assert float(info["next_attempt_at"][4]) == 0.0, "and default to an eligible 0"
    store.close()


def test_the_status_due_index_exists(tmp_path: Path):
    store = _store(tmp_path)
    names = {
        str(row[0])
        for row in store.connection().execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert "idx_reminders_status_due" in names
    store.close()


def test_a_drifted_table_fails_loudly_naming_the_missing_column(tmp_path: Path):
    # CREATE TABLE IF NOT EXISTS is a no-op against a pre-existing table, so a
    # table written by an older build keeps its old columns forever — and the
    # failure would otherwise land on the deployed host only.
    path = tmp_path / "store" / "henk.db"
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(str(path))
    columns = ", ".join(
        f"{c} TEXT" for c in REMINDER_COLUMNS if c not in ("id", "due_tz")
    )
    raw.execute(f"CREATE TABLE reminders (id INTEGER PRIMARY KEY, {columns})")
    raw.commit()
    raw.close()

    with pytest.raises(StoreError) as exc:
        Store(path).connection()
    assert "due_tz" in str(exc.value)
    assert "migration" in str(exc.value).lower()


def test_an_unexpected_column_is_named_too(tmp_path: Path):
    path = tmp_path / "store" / "henk.db"
    path.parent.mkdir(parents=True)
    raw = sqlite3.connect(str(path))
    columns = ", ".join(f"{c} TEXT" for c in REMINDER_COLUMNS if c != "id")
    raw.execute(
        f"CREATE TABLE reminders (id INTEGER PRIMARY KEY, {columns}, terminal_at REAL)"
    )
    raw.commit()
    raw.close()

    with pytest.raises(StoreError) as exc:
        Store(path).connection()
    assert "terminal_at" in str(exc.value)


# --- Scheduling and listing (task 2.3) ------------------------------------


def test_schedule_stores_every_field_it_was_given(tmp_path: Path):
    repo = _reminders(tmp_path)
    stored = _schedule(repo, "buy bread", due_at=DUE, input_spec="2026-08-25 07:30")
    assert stored.id > 0
    assert stored.text == "buy bread"
    assert stored.due_at == DUE
    assert stored.due_tz == TZ
    assert stored.input_spec == "2026-08-25 07:30"
    assert stored.source == "tool"
    assert stored.status == PENDING
    assert stored.created_at == NOW
    # Read it back from the file, not from the returned object.
    assert repo.get(stored.id) == stored


def test_text_is_stored_trimmed_and_empty_text_is_refused(tmp_path: Path):
    repo = _reminders(tmp_path)
    assert _schedule(repo, "  padded  ").text == "padded"
    with pytest.raises(EmptyContentError):
        _schedule(repo, "   ")
    assert repo.count_pending() == 1


def test_over_limit_text_is_refused_naming_the_limit_and_stores_nothing(
    tmp_path: Path,
):
    repo = _reminders(tmp_path, text_length_limit=10)
    with pytest.raises(ContentTooLongError) as exc:
        _schedule(repo, "x" * 11)
    assert "10" in str(exc.value)
    assert repo.count_pending() == 0
    # No truncated variant, either.
    assert repo.list_pending().items == ()


def test_input_spec_is_truncated_silently_rather_than_refusing_the_schedule(
    tmp_path: Path,
):
    # A diagnostic column must never be the reason a valid schedule fails.
    repo = _reminders(tmp_path)
    stored = _schedule(repo, input_spec="+" + "9" * 500 + "d")
    assert len(stored.input_spec) == repo.input_spec_limit
    assert repo.get(stored.id).input_spec == stored.input_spec


def test_pending_is_listed_oldest_due_first(tmp_path: Path):
    repo = _reminders(tmp_path)
    late = _schedule(repo, "late", due_at=NOW + 9000)
    early = _schedule(repo, "early", due_at=NOW + 100)
    middle = _schedule(repo, "middle", due_at=NOW + 500)
    page = repo.list_pending()
    assert [r.id for r in page.items] == [early.id, middle.id, late.id]
    assert page.remainder == 0


def test_the_pending_listing_is_page_bounded_and_reports_the_remainder(
    tmp_path: Path,
):
    repo = _reminders(tmp_path, page_size=2)
    for n in range(5):
        _schedule(repo, f"item {n}", due_at=NOW + 100 * (n + 1))
    page = repo.list_pending()
    assert [r.text for r in page.items] == ["item 0", "item 1"]
    assert page.remainder == 3
    # An explicit limit overrides the configured page size.
    assert len(repo.list_pending(limit=4).items) == 4
    assert repo.list_pending(limit=None).remainder == 0


def test_terminal_rows_are_absent_from_the_listing_but_gettable_by_id(
    tmp_path: Path,
):
    repo = _reminders(tmp_path)
    stored = _schedule(repo, "cancel me")
    repo.cancel(stored.id)
    assert repo.list_pending().items == ()
    fetched = repo.get(stored.id)
    assert fetched is not None and fetched.status == CANCELLED


def test_get_on_an_unknown_id_is_none(tmp_path: Path):
    assert _reminders(tmp_path).get(9999) is None


# --- Cancel and reinstate -------------------------------------------------


def test_cancel_is_a_status_change_that_retains_the_row_text_and_due_instant(
    tmp_path: Path,
):
    repo = _reminders(tmp_path)
    stored = _schedule(repo, "the original text", due_at=DUE)
    cancelled = repo.cancel(stored.id)
    assert cancelled.status == CANCELLED
    assert cancelled.text == "the original text"
    assert cancelled.due_at == DUE
    assert repo.get(stored.id).status == CANCELLED


def test_cancel_on_an_unknown_or_non_pending_id_changes_nothing(tmp_path: Path):
    repo = _reminders(tmp_path)
    assert repo.cancel(9999) is None
    stored = _schedule(repo, "once")
    repo.cancel(stored.id)
    assert repo.cancel(stored.id) is None  # already cancelled
    assert repo.get(stored.id).status == CANCELLED


def test_reinstate_returns_a_cancelled_reminder_to_pending(tmp_path: Path):
    repo = _reminders(tmp_path)
    stored = _schedule(repo, "back please", due_at=DUE)
    repo.cancel(stored.id)
    back = repo.reinstate(stored.id)
    assert back.status == PENDING
    assert back.text == "back please"
    assert back.due_at == DUE
    assert repo.list_pending().items[0].id == stored.id


def test_reinstate_on_anything_but_a_cancelled_reminder_changes_nothing(
    tmp_path: Path,
):
    repo = _reminders(tmp_path)
    stored = _schedule(repo, "still pending")
    assert repo.reinstate(stored.id) is None
    assert repo.reinstate(9999) is None
    assert repo.get(stored.id).status == PENDING


# --- The pending cap (task 2.3) -------------------------------------------


def test_the_pending_cap_rejects_naming_the_cap_and_stores_nothing(tmp_path: Path):
    repo = _reminders(tmp_path, max_pending=2)
    _schedule(repo, "one")
    _schedule(repo, "two")
    with pytest.raises(ReminderCapReachedError) as exc:
        _schedule(repo, "three")
    assert "2" in str(exc.value)
    assert repo.count_pending() == 2
    assert [r.text for r in repo.list_pending().items] == ["one", "two"]


def test_a_cancelled_reminder_does_not_count_against_the_cap(tmp_path: Path):
    repo = _reminders(tmp_path, max_pending=1)
    first = _schedule(repo, "one")
    repo.cancel(first.id)
    _schedule(repo, "two")  # must not raise
    assert repo.count_pending() == 1


def test_reinstating_at_the_cap_is_refused_and_changes_nothing(tmp_path: Path):
    repo = _reminders(tmp_path, max_pending=1)
    first = _schedule(repo, "one")
    repo.cancel(first.id)
    _schedule(repo, "two")
    with pytest.raises(ReminderCapReachedError) as exc:
        repo.reinstate(first.id)
    assert "1" in str(exc.value)
    assert repo.get(first.id).status == CANCELLED


def test_the_cap_check_and_the_insert_are_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Without a shared transaction the count could be read, the insert fail, and
    # the two disagree. Forced by making the insert fail after the count was read.
    repo = _reminders(tmp_path, max_pending=5)
    _schedule(repo, "one")

    real_insert = ReminderStore._insert_locked

    def insert_then_fail(self, conn, **kwargs):
        real_insert(self, conn, **kwargs)
        raise RuntimeError("insert failed after the cap check read the count")

    monkeypatch.setattr(ReminderStore, "_insert_locked", insert_then_fail)
    with pytest.raises(RuntimeError):
        _schedule(repo, "two")
    monkeypatch.undo()
    assert repo.count_pending() == 1
    assert [r.text for r in repo.list_pending().items] == ["one"]


# --- next_attempt_at on every path into pending (task 2.4) ---------------


@pytest.mark.parametrize("source", ["tool", "command"])
def test_scheduling_by_either_source_writes_next_attempt_at(
    tmp_path: Path, source: str
):
    repo = _reminders(tmp_path)
    stored = _schedule(repo, "check the column", source=source)
    # Asserted on the STORED row, not on the call: the default exists as a second
    # line of defence, and a test that only checks the argument cannot tell the two
    # apart.
    row = _raw_row(repo, stored.id)
    assert row["next_attempt_at"] is not None
    assert float(row["next_attempt_at"]) == DUE
    assert row["source"] == source


def test_reinstating_writes_next_attempt_at(tmp_path: Path):
    repo = _reminders(tmp_path)
    stored = _schedule(repo, "reinstate me")
    repo.cancel(stored.id)
    # Zero it behind the repository's back, so a reinstate that forgot to write it
    # would leave the sentinel rather than accidentally passing on the old value.
    repo._store.connection().execute(
        "UPDATE reminders SET next_attempt_at = 0 WHERE id = ?", (stored.id,)
    )
    repo.reinstate(stored.id)
    row = _raw_row(repo, stored.id)
    assert float(row["next_attempt_at"]) == DUE
    assert row["status"] == PENDING


def _raw_row(repo: ReminderStore, reminder_id: int):
    return (
        repo._store.connection()
        .execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
        .fetchone()
    )


# --- Nothing is deleted, no text or due instant is rewritten (task 2.3) --


def _sql_literals(module) -> str:
    """Every string literal in ``module`` that is not a docstring, uppercased.

    Docstrings and comments are excluded deliberately: this file's own prose
    describes the statements it forbids, so a plain text search over the source
    matches the documentation rather than the code. Only literals that could reach
    the driver are inspected.
    """
    tree = ast.parse(inspect.getsource(module))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return "\n".join(
        node.value.upper()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    )


def test_no_repository_method_deletes_a_row_or_rewrites_text_or_due_at():
    # Asserted against the SQL the module can actually execute, so adding such a
    # statement later fails a test rather than a review.
    from henk.store import reminders as module

    sql = _sql_literals(module)
    assert "DELETE FROM REMINDERS" not in sql
    assert "DELETE" not in sql
    assert "DROP" not in sql
    for column in ("TEXT =", "DUE_AT =", "DUE_TZ =", "INPUT_SPEC =", "CREATED_AT ="):
        assert column not in sql, f"a write to {column} would rewrite history"
    # next_attempt_at IS written on a transition into pending — that is required,
    # not forbidden, so the guard above must not be read as covering it.
    assert "NEXT_ATTEMPT_AT = DUE_AT" in sql


def test_the_status_vocabulary_is_the_specced_one():
    assert set(STATUSES) == {
        "pending",
        "delivered",
        "delivered-late",
        "missed",
        "cancelled",
        "abandoned",
    }


def test_no_repository_write_commits_on_its_own():
    # Design D2's transaction-agnostic property, asserted on the call graph rather
    # than on the prose: a repository that commits cannot be composed.
    from henk.store import reminders as module

    tree = ast.parse(inspect.getsource(module))
    offenders = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback"}
    ]
    assert offenders == []
    sql = _sql_literals(module)
    for statement in ("COMMIT", "ROLLBACK", "BEGIN"):
        assert statement not in sql, f"{statement} belongs to Store.transaction()"


def test_a_repository_write_inside_a_callers_transaction_is_absent_when_it_raises(
    tmp_path: Path,
):
    store = _store(tmp_path)
    repo = ReminderStore(store)
    with pytest.raises(RuntimeError):
        with store.transaction():
            _schedule(repo, "abandoned by the caller")
            raise RuntimeError("the caller fails after the repository write")
    assert repo.count_pending() == 0
    store.close()


# --- Durability (task 2.5) ------------------------------------------------


def test_a_reminder_survives_a_hard_connection_close_and_reopen(tmp_path: Path):
    path = tmp_path / "store" / "henk.db"
    store = Store(path, clock=lambda: NOW)
    repo = ReminderStore(store)
    stored = _schedule(repo, "survive the reopen", due_at=DUE)
    # Hard close: no flush, no graceful shutdown hook, no cooperative teardown.
    store.connection().close()

    reopened = Store(path, clock=lambda: NOW)
    try:
        again = ReminderStore(reopened).get(stored.id)
        assert again is not None
        assert again.text == "survive the reopen"
        assert again.status == PENDING
        assert again.due_at == DUE
    finally:
        reopened.close()


def test_a_reminder_survives_a_sigkill(tmp_path: Path):
    db = tmp_path / "store" / "henk.db"
    script = (
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "from henk.store import Store\n"
        "from henk.store.reminders import ReminderStore\n"
        "store = Store(%r, clock=lambda: %r)\n"
        "ReminderStore(store).schedule('survives a kill', due_at=%r,"
        " due_tz=%r, input_spec='+1h', source='tool')\n"
        "os.kill(os.getpid(), 9)\n"
    ) % (str(REPO_ROOT), str(db), NOW, DUE, TZ)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True)
    assert proc.returncode != 0, proc.stderr

    reopened = Store(db, clock=lambda: NOW)
    try:
        page = ReminderStore(reopened).list_pending()
        assert [r.text for r in page.items] == ["survives a kill"]
        assert page.items[0].due_at == DUE
        assert page.items[0].status == PENDING
    finally:
        reopened.close()
