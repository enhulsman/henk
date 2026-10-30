"""The two calendar tools: `calendar_upcoming` and `calendar_create_event`.

maintenance-calendar design D3, D4, D5 and D9.

`calendar_upcoming` is **read-only** and takes no arguments. It reads the next
``calendar.lookahead_days`` live on every call and renders the admitted events,
titles and times only. An unreadable calendar is a failure, never "nothing planned":
the model would tell the owner his patch round is not on the calendar.

`calendar_create_event` is **mutating**, tier **per-instance**, **owner-turn-only**.
It changes a shared calendar another agent also reads, so every write is shown to
the owner first. The gate renders each raw argument inside ``<<< >>>`` after
collapsing whitespace and stripping the delimiter sequences, and then the tool runs
with the model's raw arguments. So the arguments are constrained until that
normalisation is the identity on every accepted value: what the owner approves is
byte for byte what is written, weekday included. The weekday is the cue that catches
a model resolving "Saturday" to the wrong date, and it must be inside the prompt
because the owner sees nothing else the model said before answering.

**Every hold claim comes from** :func:`henk.calendar.client.holds`: the
``(holds alerts)`` marker here and the create result's second sentence. Neither
reads the flags itself, so neither can claim a hold the window provider will not
make.

**Weekday and month names come from the fixed tables in**
:mod:`henk.reminders.timeparse`, never from the C library: a locale added to the
image for an unrelated reason must not turn ``Sat`` into ``za``.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from henk.calendar.client import (
    TITLE_PREFIX,
    CalendarClient,
    CalendarError,
    CalendarEvent,
    InsertedEvent,
    NonexistentLocalTime,
    holds,
)
from henk.config import CalendarConfig
from henk.reminders.timeparse import (
    IMAGINARY,
    MONTHS,
    WEEKDAYS,
    classify_local,
    render_instant,
)
from henk.tools.base import AuthorizationTier, Tool, ToolClass, ToolResult, TurnType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from henk.audit import CalendarReceipts

logger = logging.getLogger("henk.tools.calendar")

#: The approval prompt's argument delimiters and the gate's "approved" outcome,
#: restated rather than imported: ``henk.gate`` imports ``henk.tools``, so importing
#: it back here is circular, and the delimiters belong to the channel adapter's
#: unstyled prompt form (approval-reactions D1), which a tool must not import. A
#: test pins both to their owners' values.
ARGUMENT_DELIMITERS = ("<<<", ">>>")
APPROVED_OUTCOME = "approved"

UPCOMING_TOOL_NAME = "calendar_upcoming"
CREATE_TOOL_NAME = "calendar_create_event"
#: The names this module registers, in registration order. The registry, the
#: system prompt's enumeration and the "no event path names the create tool" test
#: read this one tuple.
CALENDAR_TOOL_NAMES = (UPCOMING_TOOL_NAME, CREATE_TOOL_NAME)

#: ``Mon``..``Sun``, derived from the shared fixed table rather than typed twice.
#: Read at call time by the weekday check; never derived from the C library.
SHORT_WEEKDAYS: tuple[str, ...] = tuple(name[:3] for name in WEEKDAYS)

#: The argument format of ``start`` and ``end``, as the parameter schema states it.
WALL_TIME_SCHEMA_PATTERN = (
    "^(" + "|".join(SHORT_WEEKDAYS) + r") \d{4}-\d{2}-\d{2} \d{2}:\d{2}$"
)
#: The execution-time check: ASCII digits only (``\d`` also matches other scripts'
#: digits) and ``fullmatch``, because ``$`` also matches before a trailing newline.
_WALL_TIME = re.compile(
    "(" + "|".join(SHORT_WEEKDAYS) + r") ([0-9]{4})-([0-9]{2})-([0-9]{2}) "
    r"([0-9]{2}):([0-9]{2})"
)
WALL_TIME_EXAMPLE = "Sat 2026-11-07 20:00"

TITLE_MAX_CHARS = 100
#: The title rules the schema can express: the prefix, then characters each
#: optionally preceded by ONE space (so no run, no trailing space), none of them
#: whitespace, ``<``, ``>`` or a C0/C1 control character. No lookaround, so any JSON
#: Schema regex engine reads it the same way. Format characters (bidi overrides,
#: zero-width) are refused at execution only, so on those the schema is the more
#: permissive of the two; execution decides.
TITLE_SCHEMA_PATTERN = "^" + TITLE_PREFIX + r"(?: ?[^\s<>\x00-\x1f\x7f-\x9f])*$"
#: Refused anywhere in a title. Wider than the prompt's ``<<<``/``>>>``: a lone
#: ``<`` or ``>`` cannot sit next to the prompt's delimiters, and the adapter's
#: delimiter strip can never reassemble one from a title that holds neither.
_ANGLE_BRACKETS = frozenset("<>")
#: Unicode categories refused anywhere in a title: control (Cc), format (Cf: bidi
#: overrides and zero-width characters, which would make the prompt read
#: differently from what it says) and lone surrogates (Cs).
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})

#: Titles are rendered inside the prompt's own delimiters, capped at this length.
RENDERED_TITLE_MAX_CHARS = 120
UPCOMING_MAX_EVENTS = 50

#: A start this far in the past is still accepted: the owner may approve a few
#: minutes after asking for "now".
PAST_TOLERANCE_SECONDS = 15 * 60
MAX_DAYS_AHEAD = 90

_NOTHING_CREATED = "nothing was created"


# --- Rendering ----------------------------------------------------------------


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def render_title(title: str) -> str:
    """A calendar title as one delimited, capped line fragment.

    Delimiter sequences are stripped until none is left (removing ``>>>`` from
    ``<<>>><`` would otherwise assemble a fresh ``<<<``), then whitespace is
    collapsed, so no title can close its own quoting or forge a second line.
    """
    open_delim, close_delim = ARGUMENT_DELIMITERS
    flat = title
    while open_delim in flat or close_delim in flat:
        flat = flat.replace(open_delim, "").replace(close_delim, "")
    flat = " ".join(flat.split())
    if len(flat) > RENDERED_TITLE_MAX_CHARS:
        flat = flat[: RENDERED_TITLE_MAX_CHARS - 1] + "…"
    return f"{open_delim}{flat}{close_delim}"


def _render_date(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"


def _all_day_span(event: CalendarEvent) -> str:
    """``Sunday 8 November (all day)``, or a span; Google's end date is exclusive."""
    try:
        first = date.fromisoformat(event.start_date or "")
        last = date.fromisoformat(event.end_date or "") - timedelta(days=1)
    except ValueError:
        return "an unreadable date (all day)"
    if last <= first:
        return f"{_render_date(first)} (all day)"
    return f"{_render_date(first)} – {_render_date(last)} (all day)"


def _sort_key(event: CalendarEvent, zone: ZoneInfo) -> float:
    if event.start is not None:
        return event.start
    try:
        day = date.fromisoformat(event.start_date or "")
    except ValueError:
        return float("inf")
    return datetime(day.year, day.month, day.day, tzinfo=zone).timestamp()


# --- Argument rules -------------------------------------------------------------


_WHITESPACE_AND_DELIMITER_RULE = (
    "the title may contain single spaces only (no leading, trailing, repeated or "
    "other whitespace) and no < or > at all (the approval prompt quotes it inside "
    "<<< >>>), so the prompt shows it exactly as it will be written"
)


def title_problem(title: Any) -> str | None:
    """The rule a title breaks, or None when it is accepted.

    Every accepted title is left unchanged by the gate's prompt rendering:
    whitespace-collapsing is the identity on a string whose only whitespace is
    single interior spaces, and stripping delimiters is the identity on a string
    with no ``<`` or ``>`` in it.
    """
    if not isinstance(title, str):
        return "the title must be text"
    if not title.startswith(TITLE_PREFIX):
        return (
            f"the title must start with `{TITLE_PREFIX}` (the prefix rule: Henk "
            f"only writes and reads {TITLE_PREFIX} events)"
        )
    if len(title) > TITLE_MAX_CHARS:
        return (
            f"the title is {len(title)} characters long; the limit is "
            f"{TITLE_MAX_CHARS}"
        )
    if any(unicodedata.category(char) in _REFUSED_CATEGORIES for char in title):
        return "the title contains a control or invisible formatting character"
    if (
        title != title.strip()
        or "  " in title
        or any(char.isspace() and char != " " for char in title)
        or any(char in _ANGLE_BRACKETS for char in title)
    ):
        return _WHITESPACE_AND_DELIMITER_RULE
    return None


def _wall_time(name: str, value: Any) -> tuple[datetime | None, str | None]:
    """Parse ``Ddd YYYY-MM-DD HH:MM`` into a naive wall-clock reading.

    Returns the reading or the rule it broke. The weekday is checked against
    :data:`SHORT_WEEKDAYS`, a fixed table, read here at call time.
    """
    shape = (
        f"{name} must be Ddd YYYY-MM-DD HH:MM in the owner's local time, for "
        f"example {WALL_TIME_EXAMPLE}"
    )
    if not isinstance(value, str):
        return None, shape
    match = _WALL_TIME.fullmatch(value)
    if match is None:
        return None, shape
    weekday, year, month, day, hour, minute = match.groups()
    try:
        wall = datetime(int(year), int(month), int(day), int(hour), int(minute))
    except ValueError:
        return None, f"{name} {value} names no real date and time"
    if SHORT_WEEKDAYS[wall.weekday()] != weekday:
        return None, (
            f"{name} says {weekday}, but {year}-{month}-{day} is a "
            f"{WEEKDAYS[wall.weekday()]}"
        )
    return wall, None


# --- calendar_upcoming --------------------------------------------------------


class CalendarUpcomingTool(Tool):
    name = UPCOMING_TOOL_NAME
    description = (
        "List the Homelab maintenance planned on the owner's calendar for the next "
        "two weeks or so (the period is configured and stated in the result): "
        "titles and times only, soonest first. An event marked (holds alerts) is a "
        "maintenance window: alerts that reach Henk while it runs, and for a short "
        "grace after it, are held instead of triaged. Read live on every call; a "
        "failure is a failure, never an empty calendar."
    )
    tool_class = ToolClass.READ_ONLY
    #: No arguments: the period is configured, so the model cannot widen it and
    #: then believe a shorter read covered it.
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    #: Admitted titles were written by the owner, by Pam behind the owner's prompt,
    #: or by Henk behind the owner's prompt (design D2): not a taint source.
    raises_taint = False

    def __init__(
        self,
        client: CalendarClient,
        *,
        calendar: CalendarConfig,
        zone: ZoneInfo,
        events_enabled: bool,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._calendar = calendar
        self._zone = zone
        self._events_enabled = events_enabled
        self._clock = clock

    async def _run(self, **arguments: Any) -> ToolResult:
        if arguments:
            return ToolResult.failure(
                f"{UPCOMING_TOOL_NAME} takes no arguments (got "
                f"{', '.join(sorted(arguments))}); its period is configured"
            )
        days = self._calendar.lookahead_days
        now = self._clock()
        try:
            events = await self._client.list_window(now, now + days * 86400)
        except CalendarError as exc:
            logger.warning("calendar_upcoming could not read the calendar: %s", exc)
            return ToolResult.failure(
                f"the calendar could not be read ({exc}); this is a failure, not "
                "an empty calendar"
            )
        period = f"the next {_plural(days, 'day')}"
        if not events:
            return ToolResult.success(f"No Homelab events are planned in {period}.")
        events = sorted(events, key=lambda event: _sort_key(event, self._zone))
        shown = events[:UPCOMING_MAX_EVENTS]
        lines = [f"Homelab events planned in {period}, soonest first:"]
        lines.extend(f"- {self._line(event)}" for event in shown)
        if len(events) > len(shown):
            lines.append(
                f"...and {_plural(len(events) - len(shown), 'later event')} not shown."
            )
        return ToolResult.success("\n".join(lines))

    def _line(self, event: CalendarEvent) -> str:
        if event.all_day or event.start is None or event.end is None:
            when = _all_day_span(event)
        else:
            when = (
                f"{render_instant(event.start, self._zone)} – "
                f"{render_instant(event.end, self._zone)}"
            )
        line = f"{when} {render_title(event.title)}"
        if holds(event, self._calendar, events_enabled=self._events_enabled):
            line += " (holds alerts)"
        return line


# --- calendar_create_event ----------------------------------------------------


class ApprovalReferences:
    """Hands the create tool the reference of the approval that authorized it.

    The gate writes the ``authorization`` record, reference included, and then the
    SDK runs the tool with the model's arguments only; nothing carries the
    reference across. The ``calendar`` record must name it (audit-log spec), and
    the gate's code is out of scope for this change, so this sits where the gate
    already reports every decision: it wraps the gate's decision recorder,
    forwards every decision unchanged, and keeps the reference of the latest
    *approved* ``calendar_create_event`` for the tool to take once.

    Latest-wins, single slot, cleared on take: an approval whose tool never ran
    (the turn died in between) is overwritten by the next approval rather than
    queued ahead of it, so one lost run can never shift every later link by one.
    The gate allows one pending approval at a time, so two approvals cannot both
    be waiting for their runs.
    """

    def __init__(self, inner: Any = None) -> None:
        self._inner = inner
        self._latest: str | None = None

    def record(self, **fields: Any) -> Any:
        if (
            fields.get("tool") == CREATE_TOOL_NAME
            and fields.get("outcome") == APPROVED_OUTCOME
        ):
            reference = fields.get("reference")
            self._latest = reference if isinstance(reference, str) and reference else None
        if self._inner is None:
            return None
        return self._inner.record(**fields)

    def take(self) -> str | None:
        reference, self._latest = self._latest, None
        return reference


class CalendarCreateEventTool(Tool):
    name = CREATE_TOOL_NAME
    description = (
        "Put one Homelab item on the owner's calendar. Only when the owner asked "
        "for this event in this conversation, or agreed to a suggestion you made "
        "in it; the owner approves every call with yes or no before anything is "
        "written. `title` must start with `Homelab`, at most 100 characters, single "
        "spaces only, no < or >. `start` and `end` are the owner's local wall "
        f"clock as Ddd YYYY-MM-DD HH:MM (for example {WALL_TIME_EXAMPLE}), and the "
        "weekday must match the date: work it out from the current time and say it "
        "the way the owner would. The event gets no description and no attendees. "
        "Refused: an end not after the start, an event longer than the configured "
        "maximum (8 hours by default), a start more than 15 minutes ago or more "
        "than 90 days ahead, and a time the clocks skip. The result says whether "
        "the event will hold alerts to Henk while it runs."
    )
    label = "Add a calendar event"
    tool_class = ToolClass.MUTATING
    authorization = AuthorizationTier.PER_INSTANCE
    turn_scope = (TurnType.OWNER,)
    raises_taint = False
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "maxLength": TITLE_MAX_CHARS,
                "pattern": TITLE_SCHEMA_PATTERN,
                "description": (
                    "The event title, starting with `Homelab`, e.g. "
                    "`Homelab: patch round`."
                ),
            },
            "start": {
                "type": "string",
                "pattern": WALL_TIME_SCHEMA_PATTERN,
                "description": (
                    "Local start, Ddd YYYY-MM-DD HH:MM, e.g. " f"{WALL_TIME_EXAMPLE}."
                ),
            },
            "end": {
                "type": "string",
                "pattern": WALL_TIME_SCHEMA_PATTERN,
                "description": "Local end, same format as start.",
            },
        },
        "required": ["title", "start", "end"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        client: CalendarClient,
        *,
        calendar: CalendarConfig,
        zone: ZoneInfo,
        events_enabled: bool,
        receipts: "CalendarReceipts | None" = None,
        references: ApprovalReferences | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._calendar = calendar
        self._zone = zone
        self._events_enabled = events_enabled
        self._receipts = receipts
        self._references = references
        self._clock = clock

    async def _run(self, **arguments: Any) -> ToolResult:
        # Taken first, on every run: whatever happens next, this run consumed the
        # approval that let it start.
        reference = self._references.take() if self._references is not None else None
        problem = self._argument_problem(arguments)
        if problem is not None:
            return ToolResult.failure(f"{_NOTHING_CREATED}: {problem}")
        title = arguments["title"]
        start_wall, start_problem = _wall_time("start", arguments["start"])
        if start_problem is not None:
            return ToolResult.failure(f"{_NOTHING_CREATED}: {start_problem}")
        end_wall, end_problem = _wall_time("end", arguments["end"])
        if end_problem is not None:
            return ToolResult.failure(f"{_NOTHING_CREATED}: {end_problem}")
        problem = self._timing_problem(start_wall, end_wall)
        if problem is not None:
            return ToolResult.failure(f"{_NOTHING_CREATED}: {problem}")
        if self._receipts is not None and reference is None:
            # The write could not be linked to the approval that allowed it, and an
            # unlinkable write breaks the receipt pair's meaning. A wiring fault,
            # never an owner decision: refuse before any call.
            logger.error("calendar_create_event ran with no approval reference")
            return ToolResult.failure(
                f"{_NOTHING_CREATED}: Henk found no approval reference to receipt "
                "this write against, so it was not made. This is a fault in Henk, "
                "not something the owner did."
            )

        try:
            inserted = await self._client.insert(title, start_wall, end_wall)
        except NonexistentLocalTime as exc:
            return ToolResult.failure(f"{_NOTHING_CREATED}: {exc}")
        except CalendarError as exc:
            logger.warning("calendar insert failed: %s", exc)
            return ToolResult.failure(self._insert_failure(exc))

        if self._receipts is not None and not self._receipts.created(
            event_id=inserted.event_id,
            start=inserted.start,
            end=inserted.end,
            reference=reference or "",
        ):
            # Loud, never blocking: the event exists at Google, and a result
            # saying otherwise would invite the owner to approve a duplicate.
            logger.error(
                "the calendar record of a created event could not be written "
                "(reference %s)",
                reference,
            )
        return ToolResult.success(self._created(title, inserted))

    @staticmethod
    def _argument_problem(arguments: dict[str, Any]) -> str | None:
        expected = ("title", "start", "end")
        unexpected = sorted(set(arguments) - set(expected))
        if unexpected:
            return (
                f"unexpected argument {', '.join(unexpected)}; "
                f"{CREATE_TOOL_NAME} takes exactly title, start and end"
            )
        missing = [name for name in expected if name not in arguments]
        if missing:
            return f"missing argument {', '.join(missing)}"
        return title_problem(arguments["title"])

    def _timing_problem(self, start: datetime, end: datetime) -> str | None:
        zone = self._zone
        for name, wall in (("start", start), ("end", end)):
            if classify_local(wall, zone) == IMAGINARY:
                return (
                    f"{name} {SHORT_WEEKDAYS[wall.weekday()]} "
                    f"{wall.year:04d}-{wall.month:02d}-{wall.day:02d} "
                    f"{wall.hour:02d}:{wall.minute:02d} does not exist in "
                    f"{zone.key}: the clocks skip that time"
                )
        # fold=0: an ambiguous reading is its first occurrence, as the client sends.
        begins = start.replace(tzinfo=zone, fold=0).timestamp()
        ends = end.replace(tzinfo=zone, fold=0).timestamp()
        if ends <= begins:
            return "the end must be after the start"
        limit = self._calendar.max_window_hours
        if ends - begins > limit * 3600:
            return (
                f"the event would last {_duration(ends - begins)}, over the "
                f"{limit}-hour limit (calendar.max_window_hours)"
            )
        now = self._clock()
        if begins < now - PAST_TOLERANCE_SECONDS:
            return "the start is more than 15 minutes in the past"
        if begins > now + MAX_DAYS_AHEAD * 86400:
            return f"the start is more than {MAX_DAYS_AHEAD} days ahead"
        return None

    @staticmethod
    def _insert_failure(exc: CalendarError) -> str:
        if exc.forbidden:
            return (
                f"{_NOTHING_CREATED}: Henk's account cannot write to the calendar "
                "(Google answered 403). The calendar's sharing must give it "
                "permission to make changes to events."
            )
        if exc.status is not None and 400 <= exc.status < 500:
            return (
                f"the event was not created: Google refused the insert "
                f"(answered {exc.status})"
            )
        # A timeout, a dropped connection, a 5xx or an answer without an id: the
        # request may have reached Google, so neither outcome can be claimed.
        return (
            f"Henk could not confirm the insert ({exc}). The event may have been "
            "created anyway: check the calendar before asking again."
        )

    def _created(self, title: str, inserted: InsertedEvent) -> str:
        begins = datetime.fromisoformat(inserted.start).timestamp()
        ends = datetime.fromisoformat(inserted.end).timestamp()
        text = (
            f'Created "{title}", {render_instant(begins, self._zone)} until '
            f"{render_instant(ends, self._zone)}."
        )
        event = CalendarEvent(id=inserted.event_id, title=title, start=begins, end=ends)
        if holds(event, self._calendar, events_enabled=self._events_enabled):
            held_until = ends + self._calendar.grace_minutes * 60
            text += (
                " Alerts to Henk are held until "
                f"{render_instant(held_until, self._zone)}."
            )
        return text


def _duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if minutes == 0:
        return _plural(hours, "hour")
    return f"{_plural(hours, 'hour')} {_plural(minutes, 'minute')}"
