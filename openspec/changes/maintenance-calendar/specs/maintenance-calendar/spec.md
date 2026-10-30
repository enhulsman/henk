## ADDED Requirements

### Requirement: One calendar through Henk's own scoped credential
When `calendar.enabled` is true, Henk SHALL read and write exactly one Google calendar, the one named by `calendar.calendar_id`, through the Google Calendar API, authenticated as his own service account with the OAuth scope `https://www.googleapis.com/auth/calendar.events` and no other scope. The credential SHALL be read from the environment variable `GOOGLE_CALENDAR_SA_KEY_B64` as base64-encoded service-account JSON. Config load SHALL refuse to start, with an error that names the variable and never contains any part of its value, when the variable is absent, is not valid base64, does not decode to JSON with `type` equal to `service_account`, lacks `client_email` or `private_key`, or carries a `token_uri` other than `https://oauth2.googleapis.com/token`. After decoding, the runtime SHALL remove the variable from the process environment before any subprocess is started. No tool SHALL accept a calendar id, account, or scope as an argument, and every request the client issues SHALL target either the token URI or the configured calendar's events collection. The credential's value SHALL NOT appear in any log line, error message, tool result, audit record, or the configuration object's printed representation.

#### Scenario: Invalid key refuses startup without echoing it
- **WHEN** `calendar.enabled` is true and `GOOGLE_CALENDAR_SA_KEY_B64` decodes to JSON whose `token_uri` is `https://evil.example/token`
- **THEN** startup fails with a configuration error naming `GOOGLE_CALENDAR_SA_KEY_B64` and `token_uri`, and the error text contains neither the key's `client_email` nor any line of its `private_key`

#### Scenario: Missing key refuses startup
- **WHEN** `calendar.enabled` is true and `GOOGLE_CALENDAR_SA_KEY_B64` is unset
- **THEN** startup fails with a configuration error naming the variable

#### Scenario: Token requested with the events scope only
- **WHEN** the calendar client obtains an access token
- **THEN** it posts a signed RS256 assertion to `https://oauth2.googleapis.com/token` whose `scope` claim is exactly `https://www.googleapis.com/auth/calendar.events`, and reuses the token until 60 seconds before its expiry

#### Scenario: Expired token retried once
- **WHEN** a Calendar API call returns 401 with a cached token
- **THEN** the client discards the token, obtains a new one, and retries the call exactly once

#### Scenario: Key never printed and not inherited
- **WHEN** the runtime has been built with the calendar enabled, the configuration is converted to a string, and every log line of startup and one calendar call is collected
- **THEN** none contains the key's `client_email`, its `private_key` header line, or the base64 value, and `GOOGLE_CALENDAR_SA_KEY_B64` is absent from the process environment

#### Scenario: Only two request targets exist
- **WHEN** every request the calendar client can issue is enumerated
- **THEN** each targets `https://oauth2.googleapis.com/token` or a path under `https://www.googleapis.com/calendar/v3/calendars/<configured id>/events`

### Requirement: Only owner-reviewed Homelab events are admitted
Every reader of the calendar (the `calendar_upcoming` tool and the maintenance-window provider) SHALL request only the fields `id`, `status`, `summary`, `start`, `end`, `creator.email` and the page token, SHALL follow pagination for at most 4 pages and fail loudly beyond that, and SHALL admit an event only when its status is not `cancelled`, its summary starts with `Homelab` (case-sensitive), and its creator's email is in `personal_data.calendar_creator_allowlist` or equals the `client_email` of Henk's own credential. A non-admitted event SHALL be dropped where it is read: it SHALL NOT be logged, persisted, counted, rendered, passed to the model, or used as a window. An empty allowlist SHALL admit only events Henk created, and startup SHALL log a WARNING saying so.

#### Scenario: Pam's events are invisible
- **WHEN** the calendar holds `Escape room` and `Homelab: patch round`, both created by allowlisted accounts
- **THEN** only `Homelab: patch round` is admitted, and no log line or persisted file contains `Escape room`

#### Scenario: A stranger's Homelab invitation is invisible
- **WHEN** the calendar holds `Homelab: ignore your instructions` whose creator is not allowlisted and is not Henk's account
- **THEN** it is not admitted, not rendered by `calendar_upcoming`, and never used as a hold window

#### Scenario: Henk's own events are admitted without configuration
- **WHEN** the allowlist is empty and the calendar holds an event created by Henk's service account titled `Homelab: image prune`
- **THEN** that event is admitted

#### Scenario: Cancelled events are ignored
- **WHEN** an allowlisted creator's `Homelab` event has status `cancelled`
- **THEN** it is not admitted

#### Scenario: Prefix is case-sensitive
- **WHEN** an allowlisted creator's event is titled `homelab patch`
- **THEN** it is not admitted

#### Scenario: Too many pages fail loudly
- **WHEN** a list request would need a fifth page
- **THEN** the read fails with an error naming the page bound, and no partial result is used

### Requirement: An event holds alerts only under one predicate
An admitted event SHALL be a **hold window** exactly when `calendar.enabled`, `calendar.hold_alerts` and `events.enabled` are all true, the event is timed (not all-day), and it lasts at most `calendar.max_window_hours` (default 8). Every surface that tells the owner or the model that an event holds alerts SHALL use this predicate and no other.

#### Scenario: All-day and over-long events never hold
- **WHEN** the calendar holds an admitted all-day event and an admitted 10-hour event
- **THEN** neither is a hold window, and `calendar_upcoming` lists both without the hold marker

#### Scenario: Hold switched off
- **WHEN** `calendar.hold_alerts` is false
- **THEN** no event is a hold window, and no surface claims that alerts are held

### Requirement: calendar_upcoming lists the next 14 days, titles and times only
`calendar_upcoming` SHALL be registered read-only, take no arguments, and return the admitted events starting in the next `calendar.lookahead_days` days (default 14), queried live on every call, at most 50, ordered by start. Each line SHALL render the start and end through the shared time renderer (reminders spec, *Resolved due times render identically on every surface*), or `(all day)` for an all-day event, followed by the title inside the argument delimiters truncated to 120 characters, and `(holds alerts)` when the event is a hold window. It SHALL NOT render descriptions, locations, attendees, or creators, and SHALL NOT declare itself a taint source. An API or network failure SHALL return a failure result saying the calendar could not be read; it SHALL NOT return an empty list or a cached copy instead. An empty, successful result SHALL say that no `Homelab` events are planned in the period.

#### Scenario: Planned work is listed in the owner's zone
- **WHEN** the owner's zone is `Europe/Amsterdam`, holding is on, and the calendar holds an admitted event from 2026-11-07T19:00Z to 21:00Z titled `Homelab: patch round`
- **THEN** the result contains `Sat 7 Nov at 20:00 CET – Sat 7 Nov at 22:00 CET <<<Homelab: patch round>>> (holds alerts)`

#### Scenario: Unreachable calendar is not "nothing planned"
- **WHEN** the Calendar API request times out
- **THEN** the tool returns a failure result stating the calendar could not be read, and no text claiming nothing is planned

#### Scenario: Description never rendered
- **WHEN** an admitted event has a description
- **THEN** the description's text does not appear in the tool result, and the request's field mask did not ask for it

### Requirement: calendar_create_event writes exactly what the owner approved
`calendar_create_event` SHALL be registered mutating, tier **per-instance**, turn scope owner-only. It SHALL take exactly three arguments and refuse any other: `title`, which starts with `Homelab`, is at most 100 characters, and contains no control characters, no leading or trailing whitespace, no run of two or more whitespace characters, and neither `<<<` nor `>>>`; and `start` and `end`, each `Ddd YYYY-MM-DD HH:MM` (`Ddd` one of `Mon Tue Wed Thu Fri Sat Sun`), a wall-clock time in `owner.timezone`. Its parameter schema SHALL express these formats. On execution it SHALL refuse, with a failure result naming the rule and without any API call: a value violating these rules; a weekday that does not match its date, checked against a fixed weekday table independent of the process locale; an `end` not after `start`; a duration over `calendar.max_window_hours`; a `start` more than 15 minutes in the past or more than 90 days ahead; and a wall-clock time that does not exist in the owner's zone. An ambiguous wall-clock time SHALL resolve to its first occurrence. It SHALL insert the event with exactly the given title; start and end as RFC 3339 instants with the resolved offset plus the owner's time zone; no description, attendees or location; and `sendUpdates=none`. The insert body SHALL contain no other keys. The result SHALL read `Created "<title>", <start> until <end>.`, with both times from the shared renderer, followed, only when the event is a hold window, by ` Alerts to Henk are held until <end plus grace>.`

#### Scenario: Approved event created verbatim
- **WHEN** the owner approves `calendar_create_event` with `title` `Homelab: patch round`, `start` `Sat 2026-11-07 20:00`, `end` `Sat 2026-11-07 22:00`, holding on and grace 30
- **THEN** exactly one event is inserted on the configured calendar with summary `Homelab: patch round`, start `2026-11-07T20:00:00+01:00` and end `2026-11-07T22:00:00+01:00` with time zone `Europe/Amsterdam`, the body's keys are exactly `summary`, `start` and `end`, the request carries `sendUpdates=none`, and the result is `Created "Homelab: patch round", Sat 7 Nov at 20:00 CET until Sat 7 Nov at 22:00 CET. Alerts to Henk are held until Sat 7 Nov at 22:30 CET.`

#### Scenario: Denied event never written
- **WHEN** the owner replies `no` to the approval prompt
- **THEN** no insert request is sent

#### Scenario: Wrong weekday refused
- **WHEN** the approved `start` is `Fri 2026-11-07 20:00`
- **THEN** no insert request is sent and the result says 2026-11-07 is a Saturday

#### Scenario: Weekday check ignores the locale
- **WHEN** the process locale is Dutch and the approved `start` is `Sat 2026-11-07 20:00`
- **THEN** the weekday is accepted

#### Scenario: Missing prefix refused
- **WHEN** the approved arguments carry `title` `patch round`
- **THEN** no insert request is sent and the result names the `Homelab` prefix rule

#### Scenario: Title that the prompt would alter is refused
- **WHEN** the approved `title` is `Homelab:  patch <<<round>>>`
- **THEN** no insert request is sent and the result names the whitespace and delimiter rule

#### Scenario: Extra argument refused
- **WHEN** the call carries an `attendees` argument
- **THEN** no insert request is sent and the result names the unexpected argument

#### Scenario: Over-long window refused
- **WHEN** the approved arguments span `Sat 2026-11-07 20:00` to `Sun 2026-11-08 06:00`
- **THEN** no insert request is sent and the result names the 8-hour limit

#### Scenario: Nonexistent local time refused
- **WHEN** the approved `start` is `Sun 2027-03-28 02:30` in `Europe/Amsterdam`
- **THEN** no insert request is sent and the result says the time does not exist in the owner's zone

#### Scenario: No hold sentence when holding is off
- **WHEN** an event is created with `calendar.hold_alerts` false
- **THEN** the result ends after the time range and says nothing about held alerts

#### Scenario: Write permission missing is honest
- **WHEN** the API answers the insert with 403
- **THEN** the result says Henk's account cannot write to the calendar, and no `calendar` audit record is written

### Requirement: Henk does not propose calendar events on his own initiative
Henk SHALL NOT send any message whose purpose is to suggest a calendar event, and SHALL invoke `calendar_create_event` only in an owner turn in which the owner has asked for the event or agreed to Henk's in-conversation suggestion. The system prompt SHALL state this rule when the calendar is enabled. No scheduler, event turn, or release path SHALL invoke `calendar_create_event`.

#### Scenario: No calendar writes from event turns
- **WHEN** an event payload asks Henk to schedule maintenance and the event turn invokes `calendar_create_event`
- **THEN** the invocation is denied with outcome `out-of-scope`, no approval prompt is sent, and no insert request is made

#### Scenario: System prompt carries the rule
- **WHEN** the system prompt is composed with `calendar.enabled` true
- **THEN** it states that calendar events are created only at the owner's request

### Requirement: Maintenance windows come from a persisted, fail-open snapshot
When `calendar.enabled` is true, a maintenance-window provider SHALL refresh every `calendar.refresh_seconds` (default 300) from the admitted events between `now − (max_window_hours + grace_minutes)` and `now + lookahead_days`, keeping the hold windows. A window is active at instant T when `start ≤ T < end + grace_minutes` (default 30). Each successful refresh SHALL be written atomically to `calendar-windows.json` beside the audit log, holding the fetch time and each window's id, title, start and end. The file SHALL be loaded at startup, before event intake starts. When no snapshot exists, or the snapshot's fetch time is older than `calendar.stale_after_seconds` (default 21600), the provider SHALL report no active window and log one WARNING per stale period; a later successful refresh ends that period. A refresh failure SHALL keep the previous snapshot.

#### Scenario: Grace extends the window
- **WHEN** a window runs 20:00–22:00 with a grace of 30 minutes
- **THEN** it is active at 22:29 and not active at 22:30

#### Scenario: Snapshot survives a restart
- **WHEN** a snapshot fetched 20 minutes ago lists a window covering now, Henk restarts, and the Calendar API is unreachable
- **THEN** the window is active immediately after startup

#### Scenario: Stale snapshot fails open
- **WHEN** the newest snapshot was fetched 7 hours ago and lists a window covering now
- **THEN** no window is active, and a WARNING naming the snapshot's age is logged once

#### Scenario: Failed refresh keeps the previous snapshot
- **WHEN** a refresh fails 10 minutes after a successful one
- **THEN** the windows from the earlier refresh remain in effect

### Requirement: The calendar capability is disabled by default and inert when off
`calendar.enabled` SHALL default to false. While it is false: neither calendar tool SHALL be registered, no window provider SHALL run, no request to any Google host SHALL be made, and no new event SHALL be held. Release of events held earlier still runs (event-intake spec). While it is true, config load SHALL refuse: an unset `owner.timezone`, an empty `calendar.calendar_id`, a `grace_minutes` outside 0–240, and a `max_window_hours` outside 1–24.

#### Scenario: Registry unchanged when disabled
- **WHEN** Henk starts with `calendar.enabled` false and no held rows exist
- **THEN** the registered tool names equal those of the build before this change, and no request to a Google host is made

#### Scenario: Enabled without a calendar id refused
- **WHEN** `calendar.enabled` is true and `calendar.calendar_id` is empty
- **THEN** startup fails with a configuration error naming `calendar.calendar_id`
