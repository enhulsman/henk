"""Owner-only purge contract, against the real store schema and synthetic rows."""

from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest

from henk.store.db import Store

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy' / 'henk-purge-handoffs.py'


def run(*args):
    # Isolated Python and a timeout also catch accidental package imports or a
    # backup attempted on the connection holding the write transaction.
    return subprocess.run(
        [sys.executable, '-I', str(SCRIPT), *map(str, args)],
        capture_output=True, text=True, timeout=5,
    )


def rows(path, table='handoffs'):
    with sqlite3.connect(path) as conn:
        return conn.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'synthetic ?#% store.db'
    store = Store(path)
    with store.transaction() as conn:
        conn.executemany(
            'INSERT INTO handoffs (message_id, published_at, document) VALUES (?, ?, ?)',
            [('example-a', 0, 'A' * 90 + '\nsecond line'),
             ('example-b', 60, 'second document'),
             ('example-a', 120, '')],
        )
        conn.execute("INSERT INTO memories (content, memory_type, created_at) VALUES ('synthetic memory', 'fact', 1)")
        conn.execute("INSERT INTO inbox (text, created_at) VALUES ('synthetic inbox', 2)")
        conn.execute("INSERT INTO reminders (text, due_at, due_tz, created_at, source, status) VALUES ('synthetic reminder', 3, 'UTC', 2, 'owner', 'pending')")
    store.close()
    return path


def copies(db):
    return list(db.parent.glob(db.name + '.prepurge-*'))


def test_dry_run_all_prints_utc_and_truncated_first_line(db):
    before = db.read_bytes()
    result = run(db, '--all')
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 4
    assert lines[0].split('\t') == ['1', '1970-01-01T00:00:00Z', 'example-a', 'A' * 80]
    assert lines[1].split('\t') == ['2', '1970-01-01T00:01:00Z', 'example-b', 'second document']
    assert lines[2].split('\t') == ['3', '1970-01-01T00:02:00Z', 'example-a', '']
    assert lines[-1] == 'dry run: 3 row(s); re-run with --yes to delete'
    assert db.read_bytes() == before
    assert not copies(db)


def test_dry_run_by_message_id(db):
    result = run(db, '--message-id', 'example-b')
    assert result.returncode == 0
    assert result.stdout.splitlines()[0].startswith('2\t')
    assert 'dry run: 1 row(s)' in result.stdout
    assert len(rows(db)) == 3
    assert not copies(db)


@pytest.mark.parametrize('selection,remaining', [
    (['--all'], []),
    (['--message-id', 'example-a'], [2]),
    (['--message-id', 'example-a', '--message-id', 'example-b'], []),
    (['--message-id', 'example-a', '--message-id', 'example-a'], [2]),
    (['--message-id', 'missing', '--message-id', 'example-b'], [1, 3]),
])
def test_confirmed_purge_backs_up_and_only_deletes_selected_rows(db, selection, remaining):
    original = {table: rows(db, table) for table in ('handoffs', 'memories', 'inbox', 'reminders')}
    result = run(db, *selection, '--yes')
    assert result.returncode == 0, result.stderr
    [backup] = copies(db)
    assert re.fullmatch(re.escape(db.name) + r'\.prepurge-\d{8}T\d{6}Z', backup.name)
    assert str(backup) in result.stdout
    assert result.stdout.index(str(backup)) < result.stdout.index('deleted ')
    assert result.stdout.splitlines()[-1] == f'deleted {3 - len(remaining)} row(s)'
    assert [row[0] for row in rows(db)] == remaining
    for table, before in original.items():
        assert rows(backup, table) == before
        if table != 'handoffs':
            assert rows(db, table) == before


@pytest.mark.parametrize('yes', [[], ['--yes']])
def test_no_match_makes_no_copy_or_change(db, yes):
    before = db.read_bytes()
    result = run(db, '--message-id', "missing' OR 1=1 --", *yes)
    assert result.returncode == 1
    assert db.read_bytes() == before
    assert not copies(db)


def test_empty_table_returns_one(db):
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM handoffs')
    result = run(db, '--all', '--yes')
    assert result.returncode == 1
    assert not copies(db)


@pytest.mark.parametrize('args', [[], ['--all'], ['example.db'],
    ['example.db', '--all', '--message-id', 'example-a'],
    ['example.db', '--message-id']])
def test_usage_errors(args):
    result = run(*args)
    assert result.returncode == 2
    assert 'usage:' in result.stderr


@pytest.mark.parametrize('yes', [[], ['--yes']])
def test_write_lock_fails_without_copy_or_change(db, yes):
    with sqlite3.connect(db) as locker:
        locker.execute('BEGIN IMMEDIATE')
        before = db.read_bytes()
        result = run(db, '--all', *yes)
        assert result.returncode == 3, result.stderr
        assert db.read_bytes() == before
        assert not copies(db)
        assert len(rows(db)) == 3


def test_missing_table_fails_without_copy_or_change(db):
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TABLE handoffs')
    before = db.read_bytes()
    result = run(db, '--all', '--yes')
    assert result.returncode == 3
    assert db.read_bytes() == before
    assert not copies(db)


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'absent.db'
    result = run(path, '--all', '--yes')
    assert result.returncode == 3
    assert not path.exists()
    assert not copies(path)


def test_existing_backup_is_never_overwritten(db):
    # Cover the second-resolution filename collision without racing the clock.
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    backups = []
    for offset in range(-2, 10):
        stamp = (now + timedelta(seconds=offset)).strftime('%Y%m%dT%H%M%SZ')
        backup = db.with_name(db.name + '.prepurge-' + stamp)
        backup.write_bytes(b'existing synthetic backup')
        backups.append(backup)
    before = db.read_bytes()
    result = run(db, '--all', '--yes')
    assert result.returncode == 3
    assert db.read_bytes() == before
    assert all(path.read_bytes() == b'existing synthetic backup' for path in backups)


def test_backup_preserves_private_database_permissions(db):
    db.chmod(0o600)
    result = run(db, '--all', '--yes')
    assert result.returncode == 0, result.stderr
    [backup] = copies(db)
    assert backup.stat().st_mode & 0o777 == 0o600
