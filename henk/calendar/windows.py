"""The maintenance-window provider: poll, snapshot, fail open (design D6).

:class:`MaintenanceWindows` answers one question for the event pipeline: which
planned maintenance windows cover an instant. It never asks Google at that moment.
It works from a snapshot that a background loop refreshes every
``calendar.refresh_seconds`` and persists beside the audit log, so a restart during
a patch round, with Google or the uplink down, still knows the round is running.

Each refresh:

1. lists ``[now - (max_window_hours + grace_minutes), now + lookahead_days]``
   through :meth:`CalendarClient.list_window`, which returns admitted events only;
2. keeps the events for which :func:`henk.calendar.client.holds` is true (the one
   predicate: timed, at most ``max_window_hours``, holding switched on);
3. writes ``<audit dir>/calendar-windows.json`` atomically: ``fetched_at`` and the
   ``{id, title, start, end}`` of each window, as epoch seconds.

A window is **active** at instant ``t`` when ``start <= t < end + grace``. When no
snapshot exists, or its ``fetched_at`` is older than ``stale_after_seconds``, every
query answers "nothing" and one WARNING is logged per stale period; only a successful
refresh ends the period. A failed refresh keeps the previous snapshot, including its
``fetched_at``, so staleness keeps counting from the last success.

The API for the pipeline (group 6) and the owner-turn note (group 7):

- :meth:`MaintenanceWindows.load` once at startup, before intake starts;
- :meth:`MaintenanceWindows.run` as a background task (cancel to stop);
- :meth:`MaintenanceWindows.active` for the hold decision at publish time;
- :meth:`MaintenanceWindows.active_window` for D8's extension check;
- :meth:`MaintenanceWindows.ended_within` for D10's "ended recently" windows;
- :attr:`MaintenanceWindows.grace_seconds` and :attr:`MaintenanceWindows.fetched_at`.

Nothing here logs a title. Non-admitted events never reach this module at all (the
client drops them where it reads them), so they can reach neither the file nor the
log; admitted titles go to the file, which is mode 600 on the audit volume, and
log lines carry counts only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, Sequence

from henk.calendar.client import TITLE_PREFIX, CalendarError, CalendarEvent, holds
from henk.config import CalendarConfig

logger = logging.getLogger("henk.calendar.windows")

#: The snapshot's file name. It lives in the directory holding the audit log, on
#: the audit volume (secure-deployment spec); no config key can place it elsewhere.
SNAPSHOT_NAME = "calendar-windows.json"

_TEMP_PREFIX = "." + SNAPSHOT_NAME + "."
_TEMP_SUFFIX = ".tmp"


def snapshot_path(audit_path: str | Path) -> Path:
    """Where the snapshot lives, derived from the audit log's path."""
    return Path(audit_path).parent / SNAPSHOT_NAME


@dataclass(frozen=True)
class MaintenanceWindow:
    """One hold window as the snapshot knows it. ``start``/``end`` are epoch seconds.

    ``end`` is the calendar event's end, without the grace; the hold lasts until
    ``end + MaintenanceWindows.grace_seconds``. ``id`` is Google's event id.
    """

    id: str
    title: str
    start: float
    end: float


@dataclass(frozen=True)
class _Snapshot:
    fetched_at: float
    windows: tuple[MaintenanceWindow, ...]


class _WindowSource(Protocol):
    async def list_window(
        self, time_min: float, time_max: float
    ) -> Sequence[CalendarEvent]: ...


class MaintenanceWindows:
    """The window provider. One instance per runtime, only when the calendar is on.

    Constructing it does no I/O. Queries are synchronous and read one immutable
    snapshot object, which a refresh replaces in a single assignment, so a query
    never sees half a refresh.
    """

    def __init__(
        self,
        source: _WindowSource,
        config: CalendarConfig,
        *,
        audit_path: str | Path,
        events_enabled: bool,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._source = source
        self._config = config
        self._events_enabled = events_enabled
        self._path = snapshot_path(audit_path)
        self._clock = clock
        self._sleep = sleep
        self._snapshot: _Snapshot | None = None
        # True once this stale period's WARNING is out; a successful refresh
        # clears it, which is what ends the period.
        self._stale_warned = False
        self._failures = 0

    # --- the API ------------------------------------------------------------

    @property
    def grace_seconds(self) -> float:
        """How long after a window's end alerts are still held."""
        return self._config.grace_minutes * 60

    @property
    def fetched_at(self) -> float | None:
        """When the snapshot in use was fetched (epoch seconds); None without one.

        Reported even when the snapshot is stale: it is the age the WARNING names.
        """
        return None if self._snapshot is None else self._snapshot.fetched_at

    def active(self, t: float) -> tuple[MaintenanceWindow, ...]:
        """The windows active at ``t``: ``start <= t < end + grace``, in start order.

        Empty when the snapshot is absent or stale (fail open). Staleness is judged
        against the clock now, not against ``t``: an event published an hour ago is
        tested against the windows as currently known.
        """
        windows = self._usable()
        grace = self.grace_seconds
        return tuple(w for w in windows if w.start <= t < w.end + grace)

    def active_window(self, window_id: str, t: float) -> MaintenanceWindow | None:
        """The window with this id if the current snapshot has it and it is active
        at ``t``; else None (also when the snapshot is absent or stale).

        Design D8's extension check: "the current snapshot has the same window id;
        that window is active now". The returned ``end`` is the calendar's current
        end, so an extended window comes back with its new end and a moved one
        comes back as None.
        """
        for window in self.active(t):
            if window.id == window_id:
                return window
        return None

    def ended_within(self, t: float, seconds: float = 86400) -> tuple[MaintenanceWindow, ...]:
        """Windows whose hold ended in ``(t - seconds, t]``, in start order.

        "Ended" means past ``end + grace``, the instant :meth:`active` stops
        reporting it. Empty when the snapshot is absent or stale.

        Limit: the snapshot only reaches back ``max_window_hours + grace_minutes``
        before its fetch time (that is the list window D6 specifies; Google's
        ``timeMin`` filters on an event's end). With the defaults, windows that
        ended more than about 8.5 h ago are no longer in it, so a full 24-hour
        look-back needs the held-window rows, not this snapshot.
        """
        windows = self._usable()
        grace = self.grace_seconds
        return tuple(w for w in windows if t - seconds < w.end + grace <= t)

    # --- startup ------------------------------------------------------------

    def load(self) -> bool:
        """Read the persisted snapshot. Call once, before event intake starts.

        Returns True when a snapshot was read (it may still be stale). A missing
        file is normal on a first start; an unreadable or malformed one is logged
        and treated as absent. Never raises for a bad file, so a corrupt snapshot
        cannot stop Henk from starting. Windows that no longer hold under the
        current configuration (holding switched off, a lower maximum) are dropped.
        """
        try:
            data = self._path.read_bytes()
        except FileNotFoundError:
            logger.info("no calendar snapshot at %s yet", self._path)
            return False
        except OSError as exc:
            logger.warning(
                "calendar snapshot at %s could not be read (%s); treated as absent",
                self._path,
                type(exc).__name__,
            )
            return False
        try:
            snapshot = _decode(data)
        except Exception:
            # Belt and braces: _decode returns None for every shape it knows.
            snapshot = None
        if snapshot is None:
            # Never the content: a damaged file may hold anything.
            logger.warning(
                "calendar snapshot at %s is malformed; treated as absent", self._path
            )
            return False
        kept = self._holding(snapshot.windows)
        if len(kept) != len(snapshot.windows):
            logger.info(
                "%d window(s) in the calendar snapshot no longer hold under the "
                "current configuration",
                len(snapshot.windows) - len(kept),
            )
        self._snapshot = _Snapshot(fetched_at=snapshot.fetched_at, windows=kept)
        logger.info(
            "loaded the calendar snapshot: %d window(s), fetched %.0f s ago",
            len(kept),
            self._clock() - snapshot.fetched_at,
        )
        return True

    # --- refreshing ---------------------------------------------------------

    async def run(self) -> None:
        """Refresh now, then every ``refresh_seconds``. Only cancellation stops it.

        A failure inside one refresh is logged and the loop carries on: a provider
        that died on one bad answer would turn a Google blip into hours of holding
        nothing after the snapshot goes stale.
        """
        logger.info(
            "calendar window provider started; refreshing every %.0fs",
            self._config.refresh_seconds,
        )
        try:
            while True:
                try:
                    await self.refresh()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error(
                        "a calendar window refresh failed unexpectedly; the next one "
                        "will retry",
                        exc_info=True,
                    )
                await self._sleep(self._config.refresh_seconds)
        except asyncio.CancelledError:
            logger.info("calendar window provider stopped")
            raise

    async def refresh(self) -> bool:
        """One refresh. True when the calendar was read; never raises but on cancel.

        The clock is read once: the list window and ``fetched_at`` share it. A read
        failure keeps the previous snapshot and its ``fetched_at``. A read that
        succeeded replaces the snapshot in memory even when writing the file fails
        (logged at ERROR): the provider acts on what it just read, and the file
        keeps its previous, whole content.
        """
        now = self._clock()
        config = self._config
        time_min = now - (config.max_window_hours * 3600 + config.grace_minutes * 60)
        time_max = now + config.lookahead_days * 86400
        try:
            events = await self._source.list_window(time_min, time_max)
            # holds() admits timed events only, so start and end are instants here.
            # Inside the try: an answer that cannot be used is a failed refresh too.
            windows = tuple(
                MaintenanceWindow(id=e.id, title=e.title, start=e.start, end=e.end)
                for e in events
                if holds(e, config, events_enabled=self._events_enabled)
            )
        except CalendarError as exc:
            # A CalendarError's text never quotes Google's body (client.py).
            self._refresh_failed(str(exc))
            return False
        except Exception as exc:
            logger.error("calendar window refresh raised", exc_info=True)
            self._refresh_failed(type(exc).__name__)
            return False

        self._snapshot = _Snapshot(fetched_at=now, windows=windows)
        self._stale_warned = False
        if self._failures:
            logger.info(
                "calendar refresh succeeded again after %d failure(s)", self._failures
            )
            self._failures = 0
        try:
            _write_atomically(self._path, _encode(self._snapshot))
        except Exception as exc:
            logger.error(
                "could not write the calendar snapshot %s (%s); the previous file is "
                "kept and this refresh is used from memory",
                self._path,
                type(exc).__name__,
            )
        logger.debug("calendar refresh: %d window(s)", len(windows))
        return True

    # --- internals ----------------------------------------------------------

    def _refresh_failed(self, reason: str) -> None:
        """Log a failed refresh: WARNING for the first of a streak, INFO after."""
        self._failures += 1
        if self._failures == 1:
            logger.warning(
                "calendar refresh failed (%s); keeping the previous snapshot", reason
            )
        else:
            logger.info(
                "calendar refresh failed again (%s); %d in a row", reason, self._failures
            )
        # A quiet system with no events still learns, once, that holding stopped.
        self._usable()

    def _usable(self) -> tuple[MaintenanceWindow, ...]:
        """The snapshot's windows, or nothing when it is absent or stale."""
        snapshot = self._snapshot
        if snapshot is None:
            self._warn_stale("no calendar snapshot")
            return ()
        age = self._clock() - snapshot.fetched_at
        if age > self._config.stale_after_seconds:
            self._warn_stale(
                f"the calendar snapshot is {age / 3600:.1f} h old "
                f"(limit {self._config.stale_after_seconds / 3600:.1f} h)"
            )
            return ()
        return snapshot.windows

    def _warn_stale(self, what: str) -> None:
        if self._stale_warned:
            return
        self._stale_warned = True
        logger.warning("%s: holding no alerts until a refresh succeeds", what)

    def _holding(self, windows: Any) -> tuple[MaintenanceWindow, ...]:
        """The windows that hold under the current configuration (the one predicate)."""
        return tuple(
            w
            for w in windows
            if holds(
                CalendarEvent(id=w.id, title=w.title, start=w.start, end=w.end),
                self._config,
                events_enabled=self._events_enabled,
            )
        )


# --- the file ---------------------------------------------------------------


def _encode(snapshot: _Snapshot) -> bytes:
    document = {
        "fetched_at": snapshot.fetched_at,
        "windows": [
            {"id": w.id, "title": w.title, "start": w.start, "end": w.end}
            for w in snapshot.windows
        ],
    }
    return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer literal too large for a float
        return None
    return number if math.isfinite(number) else None


def _decode(data: bytes) -> _Snapshot | None:
    """Parse a snapshot file; None for anything that is not exactly what we write.

    All or nothing: one malformed window makes the whole file absent, because a file
    that is wrong in one place cannot be trusted in the others. A title that
    admission would never have produced (no ``Homelab`` prefix) counts as malformed.
    """
    try:
        document = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None
    if not isinstance(document, dict):
        return None
    fetched_at = _number(document.get("fetched_at"))
    entries = document.get("windows")
    if fetched_at is None or not isinstance(entries, list):
        return None
    windows: list[MaintenanceWindow] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        window_id, title = entry.get("id"), entry.get("title")
        start, end = _number(entry.get("start")), _number(entry.get("end"))
        if not isinstance(window_id, str) or not window_id:
            return None
        if not isinstance(title, str) or not title.startswith(TITLE_PREFIX):
            return None
        if start is None or end is None:
            return None
        windows.append(MaintenanceWindow(id=window_id, title=title, start=start, end=end))
    return _Snapshot(fetched_at=fetched_at, windows=tuple(windows))


def _write_atomically(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a sibling temp file and a rename.

    A reader, or a crash, sees the old file or the new one, never a partial one.
    The temp file is removed on any failure. Mode 600 (``mkstemp``'s default).
    Mirrors ``henk.replay.recorder.write_atomically``, which is not imported: that
    module pulls in the agent package, and the calendar package stays import-light.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
