"""The handoff archive: published triage handoffs, retained locally and bounded.

Why it exists (triage-quality design D8): handoffs used to live only in ntfy, which
keeps them 72 h, so a later triage of a related incident had no history to read.
``publish_handoff`` now retains what it published here, in the one SQLite file the
other repositories share, and the related-handoff digest (design D9) reads it.

What is load-bearing:

- **Bounds are module constants, not config** (D8; task 1.4 measured 17 event
  triages in the worst 90-day window against the 500-row bound). Age is the real
  bound, the count is the storage backstop (500 x 32 KB ≤ 16 MB).
- **Retention runs in the insert's transaction**, oldest first by publish time,
  and never prunes the row it is inserting.
- **Row ids are never reused** (``AUTOINCREMENT``, :mod:`henk.store.db`), because
  audit v5's ``prior_handoff_ids`` stores them.
- **A document past 32 KB is stored truncated with a visible marker and a flag**,
  never silently cut.
- **This repository is not a tool and no tool reads it** (triage-handoff spec,
  "Retained handoffs stay inside the triage path"). Retained handoffs are model
  output from tainted sessions; they reach the model only through an event turn's
  digest.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from henk.store.db import Store
from henk.store.errors import StoreError

#: At most this many handoffs are retained (D8).
HANDOFF_MAX_COUNT = 500
#: No handoff older than this is retained or eligible (D8): 90 days.
HANDOFF_MAX_AGE_SECONDS = 90 * 86_400
#: A stored document, truncation marker included, is at most this many UTF-8 bytes.
HANDOFF_DOCUMENT_MAX_BYTES = 32 * 1024

#: The tool's result string. The audit record's ``handoff_message_id`` and the
#: pipeline's recurrence ref carry it whole (henk/events/pipeline.py:170-172), so
#: lookup accepts it as well as the bare id.
_RESULT_PREFIX = "handoff published (id: "
_RESULT_FORM = re.compile(r"^handoff published \(id:(?P<id>.*)\)$", re.DOTALL)

_TRUNCATION_MARKER = (
    "\n[handoff truncated here: the published document was {size} bytes; "
    "the archive keeps at most {bound} bytes]"
)


def format_handoff_result(message_id: str) -> str:
    """The ``publish_handoff`` result text for ``message_id`` (may be empty)."""
    return f"{_RESULT_PREFIX}{message_id})"


def parse_handoff_message_id(ref: str | None) -> str | None:
    """The bare ntfy message id in ``ref``, or None when there is none.

    Accepts both forms: the bare id, and the tool's whole result string
    ``handoff published (id: X)``. An empty id, in either form, is None — which is
    what the archive stores as ``NULL`` and never matches on lookup.
    """
    if ref is None:
        return None
    text = str(ref).strip()
    match = _RESULT_FORM.match(text)
    if match is not None:
        text = match.group("id").strip()
    return text or None


@dataclass(frozen=True)
class RetainedHandoff:
    """One archived handoff, as stored."""

    id: int
    message_id: str | None
    published_at: float
    document: str
    truncated: bool
    identity_keys: tuple[str, ...]
    rule_keys: tuple[str, ...]
    nodes: tuple[str, ...]


def _bounded_document(document: str) -> tuple[str, bool]:
    """``document`` within the byte bound, marker and flag included when cut.

    Cut on a UTF-8 character boundary, so the stored text is always valid; the
    marker names the original size, so the cut is never silent.
    """
    raw = document.encode("utf-8")
    if len(raw) <= HANDOFF_DOCUMENT_MAX_BYTES:
        return document, False
    marker = _TRUNCATION_MARKER.format(size=len(raw), bound=HANDOFF_DOCUMENT_MAX_BYTES)
    budget = HANDOFF_DOCUMENT_MAX_BYTES - len(marker.encode("utf-8"))
    # errors="ignore" drops only the partial character the byte cut split: the
    # input is a valid str, so nothing before the cut can be invalid.
    kept = raw[:budget].decode("utf-8", errors="ignore")
    return kept + marker, True


def _strings(values: Iterable[str]) -> list[str]:
    return [str(v) for v in values]


def _decode(text: str) -> tuple[str, ...]:
    value = json.loads(text or "[]")
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


def _row_to_handoff(row: sqlite3.Row) -> RetainedHandoff:
    return RetainedHandoff(
        id=int(row["id"]),
        message_id=row["message_id"],
        published_at=float(row["published_at"]),
        document=str(row["document"]),
        truncated=bool(row["truncated"]),
        identity_keys=_decode(row["identity_keys"]),
        rule_keys=_decode(row["rule_keys"]),
        nodes=_decode(row["nodes"]),
    )


def _prune_locked(conn: sqlite3.Connection, now: float, *, protect_id: int) -> int:
    """Apply both bounds, oldest first. Returns how many rows were removed.

    Runs inside the caller's ``transaction()`` and issues no commit of its own, so
    the prune and the insert it accompanies are atomic together. ``protect_id``
    keeps the row being inserted from being pruned by its own insert, which a
    stepped-back clock could otherwise cause: a retain that reported success and
    stored nothing is the silent no-op D8 rules out.
    """
    cutoff = now - HANDOFF_MAX_AGE_SECONDS
    removed = conn.execute(
        "DELETE FROM handoffs WHERE published_at < ? AND id != ?",
        (cutoff, protect_id),
    ).rowcount
    # Keep the newest (MAX - 1) others plus the protected row.
    removed += conn.execute(
        "DELETE FROM handoffs WHERE id IN ("
        "  SELECT id FROM handoffs WHERE id != ?"
        "  ORDER BY published_at DESC, id DESC LIMIT -1 OFFSET ?"
        ")",
        (protect_id, HANDOFF_MAX_COUNT - 1),
    ).rowcount
    return removed


_SELECT = (
    "SELECT id, message_id, published_at, document, truncated, identity_keys, "
    "rule_keys, nodes FROM handoffs"
)


class HandoffStore:
    """Repository over the ``handoffs`` table. Borrows the shared :class:`Store`."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @property
    def store(self) -> Store:
        return self._store

    # --- write --------------------------------------------------------------

    def retain(
        self,
        document: str,
        *,
        message_id: str | None,
        identity_keys: Iterable[str],
        rule_keys: Iterable[str],
        nodes: Iterable[str],
    ) -> RetainedHandoff:
        """Archive one published handoff and apply retention, in one transaction.

        ``message_id`` may be the bare id or the tool's result string; an empty id
        is stored as ``NULL``. Raises ``ValueError`` for a handoff with no incident
        identity (it could never relate to a later incident, so it is never
        stored) and :class:`StoreError` for a backend failure.
        """
        identities = _strings(identity_keys)
        if not identities:
            raise ValueError(
                "a handoff with no incident identity is never retained; "
                "nothing was stored"
            )
        rules = _strings(rule_keys)
        node_names = _strings(nodes)
        stored_id = parse_handoff_message_id(message_id)
        text, truncated = _bounded_document(str(document))
        now = float(self._store.clock())
        try:
            with self._store.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO handoffs (message_id, published_at, document, "
                    "truncated, identity_keys, rule_keys, nodes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        stored_id,
                        now,
                        text,
                        1 if truncated else 0,
                        json.dumps(identities),
                        json.dumps(rules),
                        json.dumps(node_names),
                    ),
                )
                new_id = int(cursor.lastrowid or 0)
                _prune_locked(conn, now, protect_id=new_id)
        except sqlite3.Error as exc:
            raise StoreError(f"could not retain the handoff: {exc}") from exc
        return RetainedHandoff(
            id=new_id,
            message_id=stored_id,
            published_at=now,
            document=text,
            truncated=truncated,
            identity_keys=tuple(identities),
            rule_keys=tuple(rules),
            nodes=tuple(node_names),
        )

    # --- reads (the digest's, never a tool's) ---------------------------------

    def eligible(self, now: float | None = None) -> list[RetainedHandoff]:
        """Every handoff within retention, newest first.

        Filtered by age at read time as well as pruned at insert: pruning runs only
        when a handoff is inserted, so after a quiet stretch a row past the age
        bound can still be on disk, and it must not become eligible again (D9).
        """
        at = float(self._store.clock() if now is None else now)
        return self._select(
            f"{_SELECT} WHERE published_at >= ? "
            "ORDER BY published_at DESC, id DESC LIMIT ?",
            (at - HANDOFF_MAX_AGE_SECONDS, HANDOFF_MAX_COUNT),
        )

    def find_by_message_ref(
        self, ref: str | None, now: float | None = None
    ) -> RetainedHandoff | None:
        """The newest eligible handoff published under ``ref``'s message id.

        ``ref`` is the bare id or the full ``handoff published (id: X)`` string.
        An empty id never matches, so a ``NULL`` row is never returned.
        """
        message_id = parse_handoff_message_id(ref)
        if message_id is None:
            return None
        at = float(self._store.clock() if now is None else now)
        rows = self._select(
            f"{_SELECT} WHERE message_id = ? AND published_at >= ? "
            "ORDER BY published_at DESC, id DESC LIMIT 1",
            (message_id, at - HANDOFF_MAX_AGE_SECONDS),
        )
        return rows[0] if rows else None

    def count(self) -> int:
        """Rows on disk, eligible or not (pruning happens at the next insert)."""
        try:
            row = self._store.connection().execute(
                "SELECT COUNT(*) FROM handoffs"
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"could not count handoffs: {exc}") from exc
        return int(row[0])

    def _select(self, sql: str, params: tuple) -> list[RetainedHandoff]:
        try:
            rows = self._store.connection().execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"could not read handoffs: {exc}") from exc
        return [_row_to_handoff(row) for row in rows]
