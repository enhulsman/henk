"""The calendar client: two calls on one calendar, and the one admission rule.

maintenance-calendar design D1, D2 and D9. Everything Henk learns from or writes to
Google goes through :class:`CalendarClient`, on the runtime's shared
``httpx.AsyncClient``:

- :meth:`CalendarClient.list_window` reads ``events.list`` with a fixed field mask
  and **no** ``q`` filter (Google's ``q`` matches whole words across several fields
  and would silently narrow the prefix rule), follows at most :data:`MAX_PAGES`
  pages, and returns only what :func:`admit` admits. A non-admitted item is dropped
  where it is read: never logged, returned or counted.
- :meth:`CalendarClient.insert` writes ``summary``, ``start`` and ``end`` and no
  other key, with ``sendUpdates=none``. The missing ``attendees`` key is what keeps
  Henk without an outbound messaging path through the calendar.

Confinement to one calendar is code, not Google: the service account can reach its
own primary calendar and any calendar shared with it, so every URL is built from
the configured id here, percent-encoded so no id can leave the events path, and no
method takes an id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from henk.calendar.token import AccessTokenSource, TokenError
from henk.config import CalendarConfig
from henk.reminders.timeparse import IMAGINARY, classify_local

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3/calendars"
#: The only fields Henk ever asks for: no description, location, attendees or
#: organizer, so none can reach a result even by mistake.
LIST_FIELDS = "items(id,status,summary,start,end,creator/email),nextPageToken"
PAGE_SIZE = 250
#: 1000 events in the window is far beyond any real maintenance calendar; past it
#: the read fails loudly rather than acting on a partial list.
MAX_PAGES = 4
#: Case-sensitive, and a prefix: ``homelab patch`` and ``My Homelab`` are not admitted.
TITLE_PREFIX = "Homelab"


class CalendarError(Exception):
    """A calendar read or write that did not happen.

    ``status`` is the HTTP status when Google answered, None for a timeout or a
    transport failure. The message never quotes Google's body.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def forbidden(self) -> bool:
        """Henk's account lacks permission on the calendar."""
        return self.status == 403


class NonexistentLocalTime(ValueError):
    """A wall-clock reading that falls in the owner's zone's DST gap."""


@dataclass(frozen=True)
class CalendarEvent:
    """One admitted event. Instants for a timed event, dates for an all-day one.

    ``end_date`` is Google's exclusive end date.
    """

    id: str
    title: str
    start: float | None
    end: float | None
    start_date: str | None = None
    end_date: str | None = None

    @property
    def all_day(self) -> bool:
        return self.start is None


@dataclass(frozen=True)
class InsertedEvent:
    """A confirmed insert: Google's id and the two instants exactly as sent."""

    event_id: str
    start: str
    end: str


def admit(
    item: Mapping[str, Any], *, creator_allowlist: Iterable[str], own_email: str
) -> CalendarEvent | None:
    """The one admission rule (D2). None means "drop it here, say nothing".

    Admitted only when the status is not ``cancelled``, the summary starts with
    ``Homelab`` (case-sensitive), and the creator is allowlisted or is Henk's own
    account. An item whose id or times cannot be read is dropped the same way.
    Deliberately silent: logging a dropped item would log a title Henk may not read.
    """
    if not isinstance(item, Mapping) or item.get("status") == "cancelled":
        return None
    title = item.get("summary")
    if not isinstance(title, str) or not title.startswith(TITLE_PREFIX):
        return None
    creator = item.get("creator")
    email = creator.get("email") if isinstance(creator, Mapping) else None
    if not isinstance(email, str):
        return None
    allowed = {entry.casefold() for entry in creator_allowlist}
    allowed.add(own_email.casefold())
    if email.casefold() not in allowed:
        return None
    event_id = item.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None
    return _with_times(event_id, title, item.get("start"), item.get("end"))


def _with_times(event_id: str, title: str, start: Any, end: Any) -> CalendarEvent | None:
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        return None
    if isinstance(start.get("date"), str) and isinstance(end.get("date"), str):
        return CalendarEvent(
            id=event_id,
            title=title,
            start=None,
            end=None,
            start_date=start["date"],
            end_date=end["date"],
        )
    begins, ends = _instant(start.get("dateTime")), _instant(end.get("dateTime"))
    if begins is None or ends is None:
        return None
    return CalendarEvent(id=event_id, title=title, start=begins, end=ends)


def _instant(value: Any) -> float | None:
    """An RFC 3339 date-time with an offset, as epoch seconds; else None."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    # An offset-less reading would be read in the process zone: refuse it.
    return None if stamp.tzinfo is None else stamp.timestamp()


def holds(event: CalendarEvent, calendar: CalendarConfig, *, events_enabled: bool) -> bool:
    """Whether an admitted event is a hold window (D9): the ONLY predicate.

    The ``(holds alerts)`` marker, the create result's hold sentence and the window
    provider all ask this, so no surface can claim a hold that will not happen.
    """
    if not (calendar.enabled and calendar.hold_alerts and events_enabled):
        return False
    if event.all_day or event.start is None or event.end is None:
        return False
    duration = event.end - event.start
    return 0 <= duration <= calendar.max_window_hours * 3600


def rfc3339_local(wall: datetime, zone: ZoneInfo) -> str:
    """A naive wall-clock reading in ``zone`` as RFC 3339 with its offset.

    An ambiguous reading takes its first occurrence (``fold=0``), whatever fold the
    caller passed. A reading in the DST gap is refused: it names no instant, and
    PEP 495 would silently shift it an hour.
    """
    if wall.tzinfo is not None:
        raise TypeError("rfc3339_local takes a naive wall-clock reading")
    wall = wall.replace(fold=0)
    if classify_local(wall, zone) == IMAGINARY:
        raise NonexistentLocalTime(
            f"{wall:%Y-%m-%d %H:%M} does not exist in {zone.key}"
        )
    return wall.replace(tzinfo=zone).isoformat(timespec="seconds")


def _utc(epoch: float) -> str:
    stamp = datetime.fromtimestamp(epoch, timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


class CalendarClient:
    """``events.list`` and ``events.insert`` on the configured calendar, nothing else."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: AccessTokenSource,
        *,
        calendar_id: str,
        time_zone: str,
        creator_allowlist: Iterable[str],
        timeout: float,
    ) -> None:
        self._http = http
        self._tokens = tokens
        self._events_url = f"{CALENDAR_API_BASE}/{quote(calendar_id, safe='')}/events"
        self._zone = ZoneInfo(time_zone)
        self._allowlist = tuple(creator_allowlist)
        self._timeout = timeout

    def __repr__(self) -> str:
        return "CalendarClient(<configured calendar>)"

    async def list_window(self, time_min: float, time_max: float) -> list[CalendarEvent]:
        """The admitted events overlapping ``[time_min, time_max)``, by start.

        Raises :class:`CalendarError` on any failure, including a fifth page; a
        partial list is never returned.
        """
        params: dict[str, str] = {
            "timeMin": _utc(time_min),
            "timeMax": _utc(time_max),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(PAGE_SIZE),
            "fields": LIST_FIELDS,
        }
        items: list[Any] = []
        for _page in range(MAX_PAGES):
            body = await self._call("GET", params=params)
            page_items = body.get("items", [])
            if not isinstance(page_items, list):
                raise CalendarError("the calendar's answer was not readable")
            items.extend(page_items)
            next_token = body.get("nextPageToken")
            if not next_token:
                break
            params = {**params, "pageToken": str(next_token)}
        else:
            raise CalendarError(
                f"the calendar holds more than {MAX_PAGES} pages "
                f"({MAX_PAGES * PAGE_SIZE} events) in the window; nothing was used"
            )
        admitted = (
            admit(
                entry,
                creator_allowlist=self._allowlist,
                own_email=self._tokens.client_email,
            )
            for entry in items
        )
        return [event for event in admitted if event is not None]

    async def insert(self, title: str, start: datetime, end: datetime) -> InsertedEvent:
        """Insert one event from naive wall-clock times in the owner's zone.

        The body is exactly ``summary``, ``start`` and ``end``; ``sendUpdates=none``.
        Validation that names rules for the owner is the tool's job; this refuses
        only what must never reach Google whatever the caller checked.
        """
        if not title.startswith(TITLE_PREFIX):
            raise ValueError(f"a calendar title must start with {TITLE_PREFIX!r}")
        if start.tzinfo is not None or end.tzinfo is not None:
            raise TypeError("insert takes naive wall-clock times in the owner's zone")
        start_text = rfc3339_local(start, self._zone)
        end_text = rfc3339_local(end, self._zone)
        body = {
            "summary": title,
            "start": {"dateTime": start_text, "timeZone": self._zone.key},
            "end": {"dateTime": end_text, "timeZone": self._zone.key},
        }
        answer = await self._call("POST", params={"sendUpdates": "none"}, json=body)
        event_id = answer.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise CalendarError("the calendar did not confirm the insert")
        return InsertedEvent(event_id=event_id, start=start_text, end=end_text)

    async def _call(
        self,
        method: str,
        *,
        params: Mapping[str, str],
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One request to the events collection, retried once on a 401.

        A 401 means the cached token is no longer accepted: it is dropped, a new
        one is signed for, and the call is retried exactly once.
        """
        response = await self._send(method, params, json)
        if response.status_code == 401:
            self._tokens.invalidate()
            response = await self._send(method, params, json)
        if response.status_code >= 400:
            raise CalendarError(
                f"the calendar answered {response.status_code}",
                status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError:
            raise CalendarError("the calendar's answer was not readable") from None
        if not isinstance(body, dict):
            raise CalendarError("the calendar's answer was not readable")
        return body

    async def _send(
        self, method: str, params: Mapping[str, str], json: Mapping[str, Any] | None
    ) -> httpx.Response:
        try:
            token = await self._tokens.token()
        except TokenError as exc:
            raise CalendarError(f"Henk could not sign in to Google: {exc}") from None
        try:
            return await self._http.request(
                method,
                self._events_url,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise CalendarError("the calendar request timed out") from None
        except httpx.HTTPError as exc:
            raise CalendarError(
                f"the calendar request failed ({type(exc).__name__})"
            ) from None
