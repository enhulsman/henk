"""The store's explicit transaction boundary and the autocommit port (group 1).

From the reminders spec's "The store exposes an explicit transaction boundary"
scenarios. Every test here runs against a **real** `sqlite3` file under `tmp_path`:
the defect being fixed — pysqlite's implicit `BEGIN`, which a repository's own
`commit()` then closes on its caller's behalf — is invisible to any double that has
no connection and no implicit transaction. A cooperative fake would pass against
the broken code.
"""

from __future__ import annotations

import subprocess
import sqlite3
import sys
from pathlib import Path

import pytest

from henk.store import MemoryStore, SqliteInboxStore, Store, StoreError

REPO_ROOT = Path(__file__).resolve().parent.parent


def _store(tmp_path: Path, **kwargs) -> Store:
    return Store(tmp_path / "store" / "henk.db", **kwargs)


# --- Autocommit: the assertion that pins the actual fix (task 1.3) ---------


def test_connection_is_in_autocommit_mode(tmp_path: Path):
    # isolation_level=None is the fix. With pysqlite's default "" the driver opens
    # an implicit transaction before the first write, and a repository's own
    # commit() then commits whatever its caller had open.
    store = _store(tmp_path)
    assert store.connection().isolation_level is None
    store.close()


def test_no_transaction_is_open_outside_a_transaction_scope(tmp_path: Path):
    store = _store(tmp_path)
    memories = MemoryStore(store)
    conn = store.connection()
    assert conn.in_transaction is False
    memories.add("a fact")
    # The repository's write opened and closed its own transaction; nothing lingers
    # for the next caller to have committed out from under them.
    assert conn.in_transaction is False
    store.close()


def test_a_transaction_scope_actually_opens_one(tmp_path: Path):
    store = _store(tmp_path)
    conn = store.connection()
    with store.transaction():
        assert conn.in_transaction is True
    assert conn.in_transaction is False
    store.close()


# --- Rollback semantics (task 1.2) ----------------------------------------


def test_two_writes_in_one_transaction_roll_back_together(tmp_path: Path):
    store = _store(tmp_path)
    memories = MemoryStore(store)
    with pytest.raises(RuntimeError):
        with store.transaction():
            memories.add("first")
            memories.add("second")
            raise RuntimeError("boom after both writes")
    assert memories.list_all() == []
    store.close()


def test_second_write_raising_leaves_neither(tmp_path: Path):
    store = _store(tmp_path)
    conn = store.connection()
    with pytest.raises(sqlite3.Error):
        with store.transaction():
            conn.execute(
                "INSERT INTO memories (content, memory_type, created_at) "
                "VALUES ('one', 'pinned', 1.0)"
            )
            # A real driver error, not a synthesized one: NOT NULL on content.
            conn.execute(
                "INSERT INTO memories (content, memory_type, created_at) "
                "VALUES (NULL, 'pinned', 2.0)"
            )
    rows = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
    assert rows[0] == 0
    store.close()


def test_a_repository_call_cannot_commit_its_callers_transaction(tmp_path: Path):
    # The headline defect. Under the old code MemoryStore.add()'s own commit()
    # closed the enclosing transaction, so the caller's later failure could not
    # roll it back.
    store = _store(tmp_path)
    memories = MemoryStore(store)
    inbox = SqliteInboxStore(store)
    with pytest.raises(RuntimeError):
        with store.transaction():
            memories.add("a fact the caller will abandon")
            inbox.append("a thought the caller will abandon")
            raise RuntimeError("the caller fails after the repository writes")
    assert memories.list_all() == []
    assert inbox.list_open().items == ()
    store.close()


def test_a_swallowed_inner_failure_still_rolls_the_outer_back(tmp_path: Path):
    # The poisoning rule: "the caller must re-raise" is a convention no test can
    # enforce, so a nested scope that raised marks the transaction unsalvageable.
    store = _store(tmp_path)
    memories = MemoryStore(store)
    with store.transaction():
        memories.add("written before the inner failure")
        try:
            with store.transaction():
                memories.add("written inside the failing inner scope")
                raise RuntimeError("inner failure, about to be swallowed")
        except RuntimeError:
            pass
        # The outer scope exits normally, and must STILL roll back.
    assert memories.list_all() == []
    store.close()


def test_a_standalone_repository_write_is_committed(tmp_path: Path):
    store = _store(tmp_path)
    memories = MemoryStore(store)
    memories.add("standalone")
    # Visible from a *second* connection, which only committed data can be.
    other = sqlite3.connect(str(store.path))
    try:
        count = other.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        other.close()
    assert count == 1
    store.close()


def test_nested_entry_issues_no_second_begin(tmp_path: Path):
    # Asserted from the statements the driver actually executed, not from the
    # manager's own depth counter — a counter that lies is exactly the bug.
    store = _store(tmp_path)
    conn = store.connection()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        with store.transaction():
            with store.transaction():
                conn.execute(
                    "INSERT INTO memories (content, memory_type, created_at) "
                    "VALUES ('x', 'pinned', 1.0)"
                )
    finally:
        conn.set_trace_callback(None)
    begins = [s for s in statements if s.strip().upper().startswith("BEGIN")]
    commits = [s for s in statements if s.strip().upper().startswith("COMMIT")]
    assert len(begins) == 1, statements
    assert begins[0].strip().upper() == "BEGIN IMMEDIATE"
    assert len(commits) == 1, statements
    store.close()


def test_the_transaction_rolls_back_with_an_explicit_rollback(tmp_path: Path):
    store = _store(tmp_path)
    conn = store.connection()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        with pytest.raises(RuntimeError):
            with store.transaction():
                pass_through = "INSERT INTO memories (content, memory_type, "
                conn.execute(pass_through + "created_at) VALUES ('y', 'pinned', 1.0)")
                raise RuntimeError("boom")
    finally:
        conn.set_trace_callback(None)
    assert any(s.strip().upper().startswith("ROLLBACK") for s in statements), statements
    assert not any(s.strip().upper().startswith("COMMIT") for s in statements)
    store.close()


def test_a_later_transaction_is_not_poisoned_by_an_earlier_failure(tmp_path: Path):
    store = _store(tmp_path)
    memories = MemoryStore(store)
    with pytest.raises(RuntimeError):
        with store.transaction():
            memories.add("doomed")
            raise RuntimeError("boom")
    memories.add("fine")
    assert [m.content for m in memories.list_all()] == ["fine"]
    store.close()


def test_transaction_on_an_unopenable_store_raises_store_error(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    store = Store(blocker / "henk.db")
    with pytest.raises(StoreError):
        with store.transaction():
            pass


# --- The single-connection assumption (task 1.5) --------------------------


def test_no_store_call_is_dispatched_to_a_thread():
    """One connection is only safe while nothing dispatches a store call off-loop.

    `Store.transaction()` counts depth on the store object and shares one
    connection, so two concurrently-open transactions would silently interleave.
    Nothing does that today; this is the guard against a future change that does,
    and it belongs in the suite rather than in a docstring.
    """
    result = subprocess.run(
        ["grep", "-rn", "-E", r"to_thread|run_in_executor", "henk/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.stdout == "", (
        "a store call may now be reachable off the event loop; "
        "Store.transaction() shares one connection and counts depth per store, "
        "so two interleaved transactions would corrupt each other:\n"
        + result.stdout
    )


# --- Atomicity the implicit BEGIN used to provide for free (task 1.7) -----


def test_a_failure_after_cap_eviction_leaves_the_evicted_memory_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # MemoryStore.add is insert-then-trim: several statements that used to ride
    # pysqlite's implicit BEGIN as one transaction. Unported under autocommit they
    # would be independent commits, and a failure between them would leave a
    # memory evicted with nothing written in its place.
    store = _store(tmp_path)
    memories = MemoryStore(store, caps={"pinned": 1, "agent": 1})
    memories.add("the original fact")

    real_trim = MemoryStore._trim_locked

    def trim_then_fail(self, conn, memory_type, *, protect_id):
        evicted = real_trim(self, conn, memory_type, protect_id=protect_id)
        assert evicted, "the cap should have evicted the original fact"
        raise RuntimeError("failure after the eviction's DELETE, before the commit")

    monkeypatch.setattr(MemoryStore, "_trim_locked", trim_then_fail)
    with pytest.raises(RuntimeError):
        memories.add("the replacement fact")

    monkeypatch.undo()
    surviving = [m.content for m in memories.list_all()]
    assert surviving == ["the original fact"]
    store.close()


def test_a_failure_during_mark_done_leaves_the_item_open(tmp_path: Path):
    # The inbox's other multi-statement path: UPDATE then read-back.
    store = _store(tmp_path)
    inbox = SqliteInboxStore(store)
    item = inbox.append("a thought")
    with pytest.raises(RuntimeError):
        with store.transaction():
            inbox.mark_done(item.id)
            raise RuntimeError("the caller fails after the mark-done")
    assert inbox.get(item.id).status == "open"
    store.close()


# --- Durability across a hard close (no graceful shutdown) ----------------


def test_a_committed_write_survives_a_hard_process_kill(tmp_path: Path):
    """SIGKILL, not `store.close()`: durability, not tidy shutdown.

    Run in a child process so the kill is real. `synchronous=FULL` plus a
    committed WAL frame is what makes this hold.
    """
    db = tmp_path / "store" / "henk.db"
    script = (
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "from henk.store import MemoryStore, Store\n"
        "store = Store(%r)\n"
        "MemoryStore(store).add('survives a kill')\n"
        "os.kill(os.getpid(), 9)\n"
    ) % (str(REPO_ROOT), str(db))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True)
    assert proc.returncode != 0  # killed, never exited cleanly
    reopened = Store(db)
    try:
        assert [m.content for m in MemoryStore(reopened).list_all()] == [
            "survives a kill"
        ]
    finally:
        reopened.close()
