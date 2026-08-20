"""Resolve a submitted time to an instant, and render an instant for a human.

This is the DST core, and the failure mode it is built against is a *silent* one:
a reminder an hour off, discovered a week later. Read
``openspec/changes/reminders-core/notes/dst-verified-facts.md`` before changing
anything here — every rule below exists because a probe was run, not because it
read well.

**Two input families, two arithmetic rules** (design D3):

- A **wall-clock** input (``YYYY-MM-DD HH:MM``, or a bare ``HH:MM`` on the command
  path) names a reading on the owner's clock. It is resolved *through* the owner's
  zone with the **target date's own** offset, so it is subject to that zone's
  transitions: nonexistent and ambiguous readings are both possible.
- A **duration** (``+90m``, ``+2h``, ``+3d``) names an elapsed interval and is added
  to the **instant**, never to the wall clock. ``aware + timedelta(days=3)`` is
  wall-clock arithmetic and elapses 71 hours across the spring transition and 73
  across the autumn one; adding to the instant is 72 both ways, which is what the
  owner asked for. The DST evaluation is *skipped* on this path — a check there can
  only produce a false rejection.

An ISO value carrying an offset or a ``Z`` suffix is **rejected**. It does denote an
instant, and that is the problem: the offset is a conversion *the model performed*,
from a reading it inferred to a zone it guessed. ``2026-08-25T07:30:00Z`` becomes
09:30 CEST. A cross-check against the owner's zone cannot separate "correctly means
Tokyo 9am" from "wrongly Z-suffixed a local reading" — both disagree with the owner's
zone — so rejection is the only check that works.

**The grammar is an explicit whitelist, matched before parsing.** ``fromisoformat``
accepts ``20260825``, ``2026-W35`` and ``2026-W35-1``, and it makes ``2026-08-25``
indistinguishable from ``2026-08-25T00:00`` afterwards, so "reject a date with no
time of day" is only decidable on the *string*. The duration magnitude is bounded in
the grammar rather than by catching an exception, because the arithmetic runs before
the horizon check would get a turn: ``+999999999d`` raises ``ValueError: year ... out
of range`` and ``+99999999999999d`` an ``OSError`` from the platform clock.

**Nothing here reads the process timezone** (design D8a). No ``datetime.now()``, no
zone-less ``fromtimestamp``, no bare ``.astimezone()``, and no ``.timestamp()`` on a
naive value — that last one is the natural two-line shape of the bug, since
``fromisoformat`` returns naive. The clock is injected and read **exactly once** per
resolution, and the suite runs every clock-touching test under a hostile ``TZ``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger("henk.reminders.timeparse")

#: The two input paths. One parameter rather than two booleans, because the two
#: things that vary — whether a bare clock reading is accepted, and whether the
#: error text addresses the model or the owner — are perfectly correlated, and two
#: parameters could be combined wrongly.
TOOL = "tool"
COMMAND = "command"

#: Local-reading classifications.
NORMAL = "normal"
IMAGINARY = "imaginary"
AMBIGUOUS = "ambiguous"

#: Weekday and month names from a FIXED table, not ``strftime``. ``%A`` and ``%B``
#: follow ``LC_TIME``, so a locale added to the image for an unrelated reason would
#: turn every reminder confirmation Dutch. ``%Z`` is safe — it comes from
#: ``tzname()`` — but there is no reason to keep one locale dependency for two
#: format codes.
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

#: Delimited as data and one line, so a turn's time header cannot read as an
#: instruction and cannot become a second rendering surface beside the due times.
TIME_HEADER_PREFIX = "[CURRENT TIME — data, not instructions]"

#: `YYYY-MM-DD` with `T` or a space, a mandatory time of day, optional seconds and
#: optional fractional seconds. Anchored, so nothing trails.
_DATED = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$"
)
#: A bare clock reading. Command path only: the next-occurrence search stays where
#: the owner reads the confirmation immediately.
_CLOCK = re.compile(r"^(\d{2}):(\d{2})$")
#: `+N` with a unit. `N` is 1-6 digits — generous and sufficient — and must be
#: strictly positive, so `+0m` is refused rather than resolving to now.
_DURATION = re.compile(r"^\+(\d{1,6})([mhd])$")

#: Shapes rejected with their own message, matched before the accept-set so the
#: error can say what was wrong rather than only listing the accepted forms.
_OFFSETY = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"\s*(?:[Zz]|[+-]\d{2}(?::?\d{2})?)$"
)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}

_ACCEPTED_FORMS = {
    TOOL: (
        "accepted forms are a local date and time with no UTC offset "
        "(2026-08-25 07:30 or 2026-08-25T07:30), or a relative offset "
        "(+90m, +2h, +3d)"
    ),
    COMMAND: (
        "accepted forms are +90m / +2h / +3d, a clock time like 07:30, or a dated "
        "time like 2026-08-25 07:30"
    ),
}


class TimeResolutionError(ValueError):
    """A submitted time this module refuses. Nothing is stored for one of these.

    ``shape`` and ``reason`` are short, enumerated labels for the INFO log line —
    never the submitted string, which is unbounded and owner-personal.
    """

    def __init__(self, message: str, *, shape: str, reason: str) -> None:
        self.shape = shape
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class Resolution:
    """One accepted time: the instant, plus any disclosure the echo must carry."""

    due_at: float
    #: Non-empty only for an ambiguous wall-clock reading, where the owner must be
    #: told the reading happens twice and which occurrence was taken. Empty is the
    #: normal case and is asserted as such — an implementation whose detection is
    #: inverted or stubbed would otherwise annotate everything.
    disclosure: str = ""


# --- Rendering ------------------------------------------------------------


def render_instant(epoch: float, zone: ZoneInfo) -> str:
    """Render one instant for a human: weekday, date, local time, zone marker.

    Every human-facing time goes through here — tool results, all four command
    replies, the read tool **and** the per-turn time header — so a due time can
    never read differently in two places. That argument applies at least as
    strongly between *now* and *due* as between two due times, which is why the
    header is on the list rather than being a second surface beside it.

    The weekday is the field a human actually checks when the model resolved "next
    Tuesday", and the zone marker makes a DST boundary visible for a few
    characters. Not every zone has an alphabetic abbreviation: ``tzname()`` yields
    ``+0545`` for Kathmandu, ``+1030`` for Lord Howe and ``-03`` for São Paulo, so
    the wording must never promise letters.

    ``zone`` is the **currently configured** owner zone, never a row's stored
    ``due_tz``: the instant is fixed, its wall clock is not, and the owner reads
    today's zone.
    """
    try:
        stamp = datetime.fromtimestamp(float(epoch), zone)
    except (OverflowError, OSError, ValueError):
        # The renderer sits on the reply path. A stored value the platform clock
        # cannot represent must not take the reply down with it.
        return "an unreadable time"
    marker = stamp.tzname() or _numeric_offset(stamp)
    return (
        f"{WEEKDAYS[stamp.weekday()]} {stamp.day} {MONTHS[stamp.month - 1]} "
        f"at {stamp:%H:%M} {marker}"
    )


def _numeric_offset(stamp: datetime) -> str:
    """Fallback zone marker when ``tzname()`` gives nothing. Never locale-driven."""
    offset = stamp.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{sign}{total // 3600:02d}{(total % 3600) // 60:02d}"


def current_time_header(epoch: float, zone: ZoneInfo) -> str:
    """The one-line, data-delimited current-time header for an owner turn."""
    return f"{TIME_HEADER_PREFIX} {render_instant(epoch, zone)}"


# --- Classification (design D4's two steps) -------------------------------


def classify_local(naive: datetime, zone: ZoneInfo) -> str:
    """Classify a naive local reading as normal, imaginary or ambiguous.

    Two steps, and **both are needed** — this is the part not to touch:

    1. **Two offsets?** ``dt.utcoffset() != dt.replace(fold=1).utcoffset()`` is true
       for both imaginary and ambiguous readings and false otherwise. It finds both
       kinds and cannot tell them apart.
    2. **Which kind?** Round-trip through UTC and back into the zone. An imaginary
       reading comes back with a *different* wall clock (PEP 495 gives it the
       pre-transition offset, so ``02:30 CET`` normalises to ``01:30 UTC``, which
       renders as ``03:30 CEST`` — the silent one-hour error). An ambiguous reading
       comes back identical.

    The next reader will assume the round-trip alone is sufficient. It is not: **both
    folds of an ambiguous reading round-trip cleanly**, which is why step 1 exists.

    Two further traps, verified rather than reasoned:

    - ``fold=0`` is the earlier instant for an **ambiguous** reading only. For an
      imaginary one it is the *later* (epoch 1774747800 against 1774744200). Do not
      generalise it.
    - Two aware date-times in one zone compare by **wall clock**, so ``fold=0`` and
      ``fold=1`` of one reading compare *equal* while denoting instants an hour
      apart. Never order candidates as aware values; order them as instants.

    Verified against a ground-truth oracle over every UTC minute of a year in twelve
    zones — ~6.2M classifications, zero mismatches. The oracle is committed as
    ``tests/test_reminders_oracle.py``.
    """
    fold0 = naive.replace(tzinfo=zone, fold=0)
    fold1 = naive.replace(tzinfo=zone, fold=1)
    if fold0.utcoffset() == fold1.utcoffset():
        return NORMAL
    # Explicit `timezone.utc` on both hops: a bare `.astimezone()` would read the
    # process zone, which is the leak D8a exists to close.
    back = fold0.astimezone(timezone.utc).astimezone(zone)
    return AMBIGUOUS if back.replace(tzinfo=None) == naive else IMAGINARY


def gap_neighbours(naive: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """The last valid reading before an imaginary reading's gap, and the first after.

    Derived from the **transition boundaries**, never computed as ±1 hour: not every
    gap is an hour wide, and a named neighbour that is itself invalid makes the
    error useless. ``Australia/Lord_Howe``'s gap is 30 minutes;
    ``Antarctica/Troll``'s is two hours, so its +1h neighbour is *also* imaginary.

    For an imaginary reading, ``fold=1``'s instant precedes the transition and
    ``fold=0``'s is at or after it (because ``fold=0`` carries the smaller,
    pre-transition offset), so the transition instant is found by bisecting between
    them at one-second resolution.
    """
    fold0 = naive.replace(tzinfo=zone, fold=0)
    fold1 = naive.replace(tzinfo=zone, fold=1)
    after_offset = fold1.utcoffset()
    low = int(fold1.timestamp())
    high = int(fold0.timestamp())
    while low + 1 < high:
        mid = (low + high) // 2
        if datetime.fromtimestamp(mid, zone).utcoffset() == after_offset:
            high = mid
        else:
            low = mid
    transition = high
    # One minute before the jump, and the jump itself: both are readings that exist.
    return (
        datetime.fromtimestamp(transition - 60, zone),
        datetime.fromtimestamp(transition, zone),
    )


# --- The resolver ---------------------------------------------------------


class TimeResolver:
    """Resolves a submitted time string to an instant in the owner's zone."""

    def __init__(
        self,
        zone: ZoneInfo,
        *,
        clock: Callable[[], float] = time.time,
        horizon_days: int = 365,
        clock_skew_tolerance_seconds: float = 120.0,
    ) -> None:
        self._zone = zone
        self._clock = clock
        self._horizon_days = horizon_days
        self._skew = float(clock_skew_tolerance_seconds)

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    @property
    def zone_key(self) -> str:
        """The configured zone key, stored on every row as ``due_tz``."""
        return str(self._zone.key)

    def current_instant(self) -> float:
        """One read of the injected clock, as epoch seconds.

        Deliberately NOT called ``now()``: the suite's process-timezone guard flags
        every zero-argument ``now()`` call, and a method by that name here is both a
        false positive to silence every time and — more to the point — the same word
        as the thing it must never be, ``datetime.now()``. A caller that needs both
        the current instant and a resolution should pass this value into
        :meth:`resolve` so they share one clock read.
        """
        return self._clock()

    def render(self, epoch: float) -> str:
        return render_instant(epoch, self._zone)

    def time_header(self, epoch: float) -> str:
        return current_time_header(epoch, self._zone)

    def resolve(self, when: str, *, path: str = TOOL, now: float | None = None) -> Resolution:
        """Resolve ``when`` for ``path``, or raise :class:`TimeResolutionError`.

        The clock is read **exactly once** and every comparison below uses that
        value. Two reads would reopen a sub-second window in which a candidate
        selected as future is past when the ladder re-checks it, producing a
        spurious "that time is in the past" on a valid schedule — and one read is
        also one place for a process-zone leak to hide.
        """
        captured = self._clock() if now is None else float(now)
        raw = (when or "").strip()

        # --- Shape (the whitelist, before any parse) -----------------------
        duration = _DURATION.match(raw)
        if duration:
            return self._resolve_duration(duration, captured, path)

        dated = _DATED.match(raw)
        if dated:
            return self._resolve_dated(dated, captured, path)

        clock_reading = _CLOCK.match(raw)
        if clock_reading and path == COMMAND:
            return self._resolve_next_occurrence(clock_reading, captured, path)

        raise self._reject_shape(raw, path)

    # --- Family C: durations ------------------------------------------------

    def _resolve_duration(self, match: re.Match, now: float, path: str) -> Resolution:
        magnitude = int(match.group(1))
        if magnitude <= 0:
            # A zero magnitude resolves to the current instant rather than a future
            # one, so it is refused by the grammar rather than by the past check.
            raise self._rejection(
                f"a relative offset must be more than zero — {_ACCEPTED_FORMS[path]}.",
                shape="duration-zero",
                reason="magnitude is not strictly positive",
                path=path,
            )
        due = now + magnitude * _UNIT_SECONDS[match.group(2)]
        # No imaginary/ambiguous evaluation on this path: a duration is never a wall
        # clock, so a check here could only ever produce a false rejection. And no
        # past check: a strictly positive magnitude added to `now` is always future.
        self._check_horizon(due, now, path)
        return Resolution(due_at=due)

    # --- Family A: a fully specified wall clock -----------------------------

    def _resolve_dated(self, match: re.Match, now: float, path: str) -> Resolution:
        year, month, day, hour, minute = (int(match.group(i)) for i in range(1, 6))
        second = int(match.group(6) or 0)
        micro = int((match.group(7) or "0").ljust(6, "0"))
        try:
            naive = datetime(year, month, day, hour, minute, second, micro)
        except ValueError as exc:
            # Shape-valid but calendar-invalid (2026-02-30, month 13, hour 24).
            raise self._rejection(
                f"that is not a real date and time — {_ACCEPTED_FORMS[path]}.",
                shape="invalid-calendar-value",
                reason=str(exc),
                path=path,
            ) from exc
        due, disclosure = self._evaluate(naive, path)
        self._check_past(due, now, path)
        self._check_horizon(due, now, path)
        return Resolution(due_at=due, disclosure=disclosure)

    # --- Family B: the bare clock reading (design D10) ----------------------

    def _resolve_next_occurrence(
        self, match: re.Match, now: float, path: str
    ) -> Resolution:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise self._rejection(
                f"{hour:02d}:{minute:02d} is not a clock time — "
                f"{_ACCEPTED_FORMS[path]}.",
                shape="invalid-clock-reading",
                reason="hour or minute out of range",
                path=path,
            )
        # Zone-explicit: the LOCAL date, not the process's idea of today.
        today = datetime.fromtimestamp(now, self._zone).date()

        # SELECTION RUNS FIRST, and that ordering is load-bearing. Evaluated the
        # other way round, `/remind 02:30` sent at 20:00 on the spring-forward day
        # is refused with "02:30 does not exist on 2026-03-29" — while the reading
        # the owner actually meant, 02:30 the following night, exists and is
        # perfectly schedulable. The same command at 00:30 that night IS correctly
        # refused, because there the owner is asking for tonight.
        selected = self._select_candidate(today, hour, minute, now)
        if selected is None:
            # At most one date forward. A reminder silently scheduled 24 hours late
            # is a broken promise, so "next occurrence" is not licence to keep
            # walking until a valid reading turns up. (A zone with a fully SKIPPED
            # calendar date — Pacific/Apia skipped 2011-12-30 — is out of scope,
            # named rather than silently mishandled.)
            selected = self._select_candidate(
                today + timedelta(days=1), hour, minute, now
            )
        if selected is None:  # pragma: no cover - every next-date instant is future
            raise self._rejection(
                f"{hour:02d}:{minute:02d} has no next occurrence in the next day.",
                shape="clock-reading-unreachable",
                reason="no candidate instant after now",
                path=path,
            )
        naive, due = selected
        # D4 and D5 in full on the SELECTED candidate — not rejection alone: the
        # advanced candidate can be ambiguous rather than imaginary, and that case
        # must still schedule, with its disclosure.
        evaluated_due, disclosure = self._evaluate(naive, path, chosen=due)
        # The past check is unreachable here by construction: selection only ever
        # returns an instant strictly after the single captured `now`. Stated rather
        # than relied on.
        self._check_horizon(evaluated_due, now, path)
        return Resolution(due_at=evaluated_due, disclosure=disclosure)

    def _select_candidate(
        self, day: date_type, hour: int, minute: int, now: float
    ) -> tuple[datetime, float] | None:
        """The earliest distinct INSTANT of this reading on ``day`` after ``now``.

        Both folds are considered, because where the reading repeats the earlier
        occurrence may already have passed: at 02:45 during the first pass of the
        repeated hour, a fold=0-only rule skips a valid occurrence 45 minutes away
        and lands 24.75 hours out.

        Ordering is on the epoch value, never on the aware date-times — the two
        folds of one reading compare *equal* as aware values while denoting instants
        an hour apart.
        """
        naive = datetime.combine(day, datetime.min.time()).replace(
            hour=hour, minute=minute
        )
        instants = sorted(
            {naive.replace(tzinfo=self._zone, fold=fold).timestamp() for fold in (0, 1)}
        )
        for instant in instants:
            if instant > now:
                return naive, instant
        return None

    # --- The shared imaginary / ambiguous evaluation ------------------------

    def _evaluate(
        self, naive: datetime, path: str, *, chosen: float | None = None
    ) -> tuple[float, str]:
        """Evaluate one wall-clock reading. Returns ``(instant, disclosure)``.

        ``chosen`` is the instant selection already picked (the ``HH:MM`` path); for
        a fully specified reading there is nothing to choose and the rule is
        ``fold=0`` — by definition the first of the two occurrences, the first time
        the owner's clock reads that value, and the choice that is never *late*.
        """
        kind = classify_local(naive, self._zone)
        if kind == IMAGINARY:
            raise self._imaginary_error(naive, path)
        fold0 = naive.replace(tzinfo=self._zone, fold=0).timestamp()
        due = fold0 if chosen is None else chosen
        if kind == NORMAL:
            return due, ""
        # Ambiguous: the reading exists twice. There IS a defensible answer here,
        # unlike the imaginary case, so it resolves and the echo says so.
        which = "first" if due == fold0 else "second"
        return due, (
            f"that reading occurs twice that night — the clocks go back — and this "
            f"is the {which} of the two"
        )

    def _imaginary_error(self, naive: datetime, path: str) -> TimeResolutionError:
        before, after = gap_neighbours(naive, self._zone)
        gap_start = (before + timedelta(minutes=1)).strftime("%H:%M")
        message = (
            f"{naive:%H:%M} does not exist on {naive.day} {MONTHS[naive.month - 1]} "
            f"{naive.year} in {self.zone_key} — the clocks go forward from "
            f"{gap_start} to {after:%H:%M} that night. The nearest valid readings "
            f"are {before:%H:%M} and {after:%H:%M}."
        )
        if path == TOOL:
            # Aimed at the model: without this a rejection does not AVOID inventing
            # intent, it delegates the invention to the least accountable actor in
            # the loop, with no echo of its reasoning.
            message += (
                " Ask the owner which of those two they meant; do not pick one "
                "yourself and do not retry with a substituted time."
            )
        return self._rejection(
            message,
            shape="imaginary-wall-clock",
            reason="reading falls inside a spring-forward gap",
            path=path,
        )

    # --- The remaining ladder steps -----------------------------------------

    def _check_past(self, due: float, now: float, path: str) -> None:
        # On absolute instants, never on aware date-times: ordering inside a
        # repeated hour is a wall-clock comparison there, not an instant one.
        if due >= now - self._skew:
            return
        raise self._rejection(
            f"that time is already in the past — it is currently "
            f"{render_instant(now, self._zone)}.",
            shape="past",
            reason="instant precedes now beyond the skew tolerance",
            path=path,
        )

    def _check_horizon(self, due: float, now: float, path: str) -> None:
        if due <= now + self._horizon_days * 86400:
            return
        raise self._rejection(
            f"that is further ahead than the {self._horizon_days}-day limit on "
            "reminders; nothing was scheduled.",
            shape="beyond-horizon",
            reason=f"instant is beyond {self._horizon_days} days",
            path=path,
        )

    def _reject_shape(self, raw: str, path: str) -> TimeResolutionError:
        """Pick the most specific rejection for an unwhitelisted string."""
        if _OFFSETY.match(raw):
            return self._rejection(
                "a reminder time must be the owner's local time with no UTC offset "
                "and no Z suffix — an offset is a conversion I cannot check, and it "
                f"is how a local reading becomes silently hours wrong. "
                f"{_ACCEPTED_FORMS[path].capitalize()}.",
                shape="offset-carrying",
                reason="value carries a UTC offset or Z suffix",
                path=path,
            )
        if _DATE_ONLY.match(raw):
            return self._rejection(
                "that is a date with no time of day; a reminder needs both. "
                f"{_ACCEPTED_FORMS[path].capitalize()}.",
                shape="date-only",
                reason="no time of day",
                path=path,
            )
        if _CLOCK.match(raw):  # path == TOOL
            return self._rejection(
                "a bare clock time is not accepted here — give the date as well, so "
                "there is no guessing which day is meant. "
                f"{_ACCEPTED_FORMS[path].capitalize()}.",
                shape="bare-clock-on-tool-path",
                reason="clock reading submitted on the tool path",
                path=path,
            )
        return self._rejection(
            f"I could not read that as a time. {_ACCEPTED_FORMS[path].capitalize()}.",
            shape="unrecognized",
            reason="matched no accepted shape",
            path=path,
        )

    @staticmethod
    def _rejection(
        message: str, *, shape: str, reason: str, path: str
    ) -> TimeResolutionError:
        """Log one rejection and return the error for the caller to raise.

        The INFO line is the ONLY trace a rejection leaves: it writes no audit record
        by design — receipts record state changes, and none occurred — nothing is
        stored, and tool-result text is no longer logged. Without it a model
        repeatedly submitting a bad form is invisible except in the token bill. The
        line carries the enumerated shape and reason, never the submitted string,
        which is unbounded and owner-personal.

        Returns rather than raises so every call site reads `raise ...` and the
        control flow is visible where it happens.
        """
        logger.info(
            "rejected a reminder time on the %s path: shape=%s reason=%s",
            path,
            shape,
            reason,
        )
        return TimeResolutionError(message, shape=shape, reason=reason)
