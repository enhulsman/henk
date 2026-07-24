"""Durable intake-offset checkpoint on the audit volume (design D1).

A one-value cursor — the last-seen ntfy message id whose *outcome is durable* —
stored as a tiny file beside the audit log. It is NOT the audit record and NOT a
log: only the latest id is kept (last-write-wins), written atomically (temp file
+ ``os.replace``) so a crash mid-write can never leave a torn cursor.

On startup ``runtime.py`` reads it and seeds ``EventIntake`` so the first
``subscribe`` resumes with ``since=<offset>``. The *advance* is driven by the
agent core after each triage's audit record is durable (design D1), never by the
intake on yield — so the cursor never moves past an event whose outcome is not
yet on disk. Writes mirror :class:`~henk.audit.logger.AuditLog`'s discipline: a
failure is logged at ERROR and swallowed, never blocking intake or triage.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("henk.events.checkpoint")


class OffsetCheckpoint:
    """Reads/writes the last-seen event id to a small file. Never raises on write."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def read(self) -> str | None:
        """Return the persisted last-seen id, or ``None`` if absent/blank."""
        try:
            value = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def write(self, offset: str) -> bool:
        """Atomically persist ``offset`` (last-write-wins). False (logged) on failure."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(offset, encoding="utf-8")
            os.replace(tmp, self._path)  # atomic on POSIX: no torn cursor on crash
            return True
        except OSError:
            logger.error("checkpoint write failed for %s", self._path, exc_info=True)
            return False
