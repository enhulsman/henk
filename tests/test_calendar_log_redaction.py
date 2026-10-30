"""The calendar id never reaches a log line (Codex adversarial-review follow-up).

httpx logs every request at INFO as ``HTTP Request: <METHOD> <URL> "<version>
<status> <reason>"``, and ``henk.__main__`` runs the root logger at INFO, so every
calendar call would print the configured calendar id inside its URL. The calendar
client redacts that one path segment, raw or percent-encoded, to ``<calendar>``; the
method, the host and the status stay, and no other httpx line changes.

Exercised through the real logging pipeline: a root handler at INFO with the
production format, fed by a real ``httpx.AsyncClient`` over a mock transport.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from urllib.parse import quote

import httpx
import pytest

from henk.calendar.client import CalendarClient
from henk.calendar.token import AccessTokenSource, parse_service_account_key
from tests.calendar_fixtures import CALENDAR_ID, key_b64

#: The production format (henk/__main__.py, `logging.basicConfig`).
FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
#: An id whose encoded form differs from the raw one in several characters, so a
#: redaction that only knows one form leaks the other.
AWKWARD_ID = "team/maint b?x#y@group.calendar.google.com"


def _answer(request: httpx.Request) -> httpx.Response:
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    if request.method == "POST" and request.url.host == "www.googleapis.com":
        return httpx.Response(200, json={"id": "evt-placeholder-1"})
    return httpx.Response(200, json={"items": []})


@pytest.fixture
def log_stream():
    """A root handler at INFO, as production configures it, capturing to a buffer."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


async def _drive(calendar_id: str):
    async with httpx.AsyncClient(transport=httpx.MockTransport(_answer)) as http:
        tokens = AccessTokenSource(parse_service_account_key(key_b64()), http, timeout=10)
        client = CalendarClient(
            http,
            tokens,
            calendar_id=calendar_id,
            time_zone="Europe/Amsterdam",
            creator_allowlist=(),
            timeout=10,
        )
        await client.list_window(1_790_000_000.0, 1_790_003_600.0)
        await client.insert(
            "Homelab: patch round", datetime(2026, 11, 7, 20), datetime(2026, 11, 7, 22)
        )
        # A request on the same shared client that is not a calendar call.
        await http.get("https://ntfy.hulsman.dev/henk-events/json?poll=1")


def _lines(stream: io.StringIO) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if " httpx " in line]


@pytest.mark.parametrize("calendar_id", [CALENDAR_ID, AWKWARD_ID])
async def test_list_and_insert_lines_carry_no_calendar_id(log_stream, calendar_id):
    await _drive(calendar_id)
    lines = _lines(log_stream)
    encoded = quote(calendar_id, safe="")
    assert encoded != calendar_id
    text = "\n".join(lines)
    assert calendar_id not in text
    assert encoded not in text
    calendar_lines = [line for line in lines if "/calendar/v3/" in line]
    assert len(calendar_lines) == 2
    get_line, post_line = calendar_lines
    # Method, host and status stay readable; only the id segment is replaced.
    assert (
        'HTTP Request: GET https://www.googleapis.com/calendar/v3/calendars/'
        '<calendar>/events?timeMin=' in get_line
    )
    assert get_line.endswith('"HTTP/1.1 200 OK"')
    assert post_line.endswith(
        'HTTP Request: POST https://www.googleapis.com/calendar/v3/calendars/'
        '<calendar>/events?sendUpdates=none "HTTP/1.1 200 OK"'
    )


async def test_the_raw_id_is_redacted_too(log_stream):
    # httpx renders the encoded form today; a log call that carried the raw id (a
    # future httpx, or a message built from the configured value) is caught as well.
    await _drive(CALENDAR_ID)
    logging.getLogger("httpx").info(
        "HTTP Request: %s %s", "GET",
        f"https://www.googleapis.com/calendar/v3/calendars/{CALENDAR_ID}/events",
    )
    last = _lines(log_stream)[-1]
    assert CALENDAR_ID not in last
    assert last.endswith(
        "HTTP Request: GET https://www.googleapis.com/calendar/v3/calendars/<calendar>/events"
    )


async def test_other_httpx_lines_are_unchanged(log_stream):
    await _drive(CALENDAR_ID)
    lines = _lines(log_stream)
    token_lines = [line for line in lines if "oauth2.googleapis.com" in line]
    ntfy_lines = [line for line in lines if "ntfy.hulsman.dev" in line]
    assert token_lines and token_lines[0].endswith(
        'HTTP Request: POST https://oauth2.googleapis.com/token "HTTP/1.1 200 OK"'
    )
    assert len(ntfy_lines) == 1
    assert ntfy_lines[0].endswith(
        ' INFO httpx HTTP Request: GET https://ntfy.hulsman.dev/henk-events/json?poll=1'
        ' "HTTP/1.1 200 OK"'
    )


async def test_a_line_mentioning_the_id_outside_the_calendar_path_is_unchanged(log_stream):
    # "No other httpx log line may change": only the calendars/<id>/ segment of a
    # calendar URL is rewritten, not every occurrence of the string.
    await _drive(CALENDAR_ID)
    logging.getLogger("httpx").info("note %s", f"https://example.invalid/?ref={CALENDAR_ID}")
    assert _lines(log_stream)[-1].endswith(f"note https://example.invalid/?ref={CALENDAR_ID}")


async def test_building_clients_twice_installs_one_filter(log_stream):
    await _drive(CALENDAR_ID)
    await _drive(CALENDAR_ID)
    httpx_filters = logging.getLogger("httpx").filters
    assert len([f for f in httpx_filters if type(f).__name__ == "CalendarIdRedactor"]) == 1
    assert CALENDAR_ID not in log_stream.getvalue()
