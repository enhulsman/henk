#!/usr/bin/env python3
"""Owner-run recovery for retained handoffs. Stop Henk before using this tool."""

import argparse
from datetime import datetime, timezone
import sqlite3
import shutil


def database_uri(path, mode):
    # URI metacharacters are legal in filenames; do not let them change mode.
    escaped = path.replace('%', '%25').replace('?', '%3F').replace('#', '%23')
    return 'file:' + escaped + '?mode=' + mode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('db', metavar='DB', help='existing Henk SQLite database')
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--all', action='store_true', help='select every handoff')
    selection.add_argument('--message-id', action='append', help='select an exact message id (repeatable)')
    parser.add_argument('--yes', action='store_true', help='back up, then delete the selected rows')
    args = parser.parse_args()
    predicate = '1' if args.all else 'message_id IN (' + ','.join('?' for _ in args.message_id) + ')'
    parameters = [] if args.all else args.message_id
    conn = None
    source = None
    try:
        # Check through a read-only connection first: even opening a writable
        # WAL database can checkpoint it on close when its table is missing.
        source = sqlite3.connect(database_uri(args.db, 'ro'), uri=True, timeout=0)
        if source.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'handoffs'"
        ).fetchone() is None:
            parser.exit(3, 'purge failed: no handoffs table\n')
        conn = sqlite3.connect(database_uri(args.db, 'rw'), uri=True, timeout=0)
        # Hold the write reservation across selection, backup and deletion. A
        # running Henk must fail here, before a backup file is even created.
        conn.execute('BEGIN IMMEDIATE')
        selected = conn.execute(
            'SELECT id, published_at, message_id, document FROM handoffs WHERE '
            + predicate + ' ORDER BY id', parameters,
        ).fetchall()
        if not selected:
            print('no row matched')
            return 1
        for row_id, published_at, message_id, document in selected:
            published = datetime.fromtimestamp(published_at, timezone.utc).isoformat().replace('+00:00', 'Z')
            first_line = document.splitlines()[0][:80] if document else ''
            print(f'{row_id}\t{published}\t{message_id}\t{first_line}')
        if not args.yes:
            print(f'dry run: {len(selected)} row(s); re-run with --yes to delete')
            return 0

        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup_path = args.db + '.prepurge-' + stamp
        # Exclusive creation protects an earlier backup in the same second.
        with open(backup_path, 'xb'):
            pass
        # Apply the store's permissions before copying any private content.
        shutil.copymode(args.db, backup_path)
        # SQLite backup on the connection holding a write transaction would
        # wait on itself. A separate reader sees the committed pre-purge state;
        # our write reservation prevents anyone changing it during the copy.
        try:
            destination = sqlite3.connect(backup_path, timeout=0)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
            source = None
        print(backup_path, flush=True)
        deleted = conn.execute('DELETE FROM handoffs WHERE ' + predicate, parameters).rowcount
        conn.commit()
        print(f'deleted {deleted} row(s)')
        return 0
    except (sqlite3.Error, OSError, ValueError, OverflowError) as exc:
        parser.exit(3, f'purge failed: {exc}\n')
    finally:
        if conn is not None:
            # Closing rolls back an uncommitted transaction, including dry runs.
            conn.close()
        if source is not None:
            source.close()


if __name__ == '__main__':
    raise SystemExit(main())
