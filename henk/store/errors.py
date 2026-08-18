"""Store error taxonomy.

Two kinds, deliberately distinct: a *backend* failure (:class:`StoreError` — the
file is gone, the disk is full, SQLite refuses) is an operational problem the
caller must surface honestly rather than paper over, while a *content* rejection
(:class:`InvalidContentError` and its subclasses) is the caller's input failing a
documented rule. The specs demand different handling for each: a read failure
must never be presented as an empty store, and an over-limit fact must be
rejected naming the limit rather than truncated.
"""

from __future__ import annotations


class StoreError(RuntimeError):
    """The store backend could not be opened, read, or written."""


class InvalidContentError(ValueError):
    """Content the store refuses to accept (never silently repaired)."""


class EmptyContentError(InvalidContentError):
    """Empty or whitespace-only content."""


class ContentTooLongError(InvalidContentError):
    """Content longer than the configured per-fact limit."""

    def __init__(self, limit: int, length: int | None = None) -> None:
        self.limit = limit
        self.length = length
        detail = f" (got {length})" if length is not None else ""
        super().__init__(
            f"the text is longer than the {limit}-character limit{detail}; "
            "nothing was stored — shorten it and try again"
        )
