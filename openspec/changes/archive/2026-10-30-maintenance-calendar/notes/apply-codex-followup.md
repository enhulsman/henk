# maintenance-calendar — Codex adversarial-review follow-up (groups 1/2)

Two findings from the `/codex:adversarial-review` run, fixed test-first on top of
861c366 and the groups 3–7 commits.

## 1. The calendar id reached INFO logs

**Finding.** httpx logs every request at INFO as `HTTP Request: <METHOD> <URL> "<version>
<status> <reason>"` (httpx 0.28.1, `_client.py`), and `henk/__main__.py:80-83` runs the
root logger at INFO. Every calendar call therefore logged the configured calendar id
inside its URL. The earlier "leave it" call (apply-decisions.md, group 2) is reversed by
the orchestrator: copied diagnostics are a real path off rp5.

**Fix.** `CalendarIdRedactor`, a `logging.Filter` in `henk/calendar/client.py`,
installed once on the `httpx` logger by `CalendarClient.__init__` through
`redact_calendar_id_in_logs`. It rewrites only `/calendars/<id>/` to
`/calendars/<calendar>/`, for the raw and the percent-encoded id; method, host, query
and status stay. A line without that segment is returned untouched, type included, so
no other httpx line changes, including one that mentions the id elsewhere.

**Tests** (`tests/test_calendar_log_redaction.py`, real pipeline: a root
`StreamHandler` at INFO with the production format, a real `httpx.AsyncClient` over a
mock transport): list and insert lines carry neither form of the id, for a plain id and
for one whose encoding differs in several characters; a raw-id line is redacted; the
token and an unrelated request line are byte-identical to httpx's own; building two
clients installs one filter.

## 2. Non-finite calendar settings slipped past the checks

**Finding.** `value <= 0` and the range checks accept NaN (every comparison is false)
and positive infinity. A YAML `.nan` for `stale_after_seconds` made the window
provider's age check always false, so a stale snapshot would hold alerts indefinitely.
Found while testing: `int(.inf)` raised `OverflowError`, which escaped both the enabled
path (a crash instead of a `ConfigError`) and the disabled path (a disabled section
could crash startup).

**Fix** (`henk/config.py`, `_finite_calendar_settings` and `_finite`). An enabled
section's six numeric settings must be finite before any range or positivity check;
NaN and ±infinity are refused naming the setting and "finite". A disabled section keeps
its lenient reading, and a non-finite value there falls back to the default, so a later
flip of `enabled` cannot inherit it unchecked.

**Tests** (`tests/test_calendar_config.py`): `.nan`, `.inf`, `-.inf` (YAML-parsed) for
each of the six settings, enabled (refused) and disabled (default kept); the strings
`"nan"` and `"inf"` refused when enabled.

**Not changed, worth a look:** the other sections' numeric settings (`reminders`,
`sessions`, `homelab_docs`, `homelab_query`, `events`) use the same positivity-only
pattern and accept NaN the same way.

## Mutations (each fix broken once, run with `PYTHONDONTWRITEBYTECODE=1 python -B`)

| Mutation | Result |
|---|---|
| redactor not installed | killed: `test_list_and_insert_lines_carry_no_calendar_id[placeholder-...]` |
| only the encoded form redacted | killed: `test_the_raw_id_is_redacted_too` |
| every occurrence of the id redacted, not only the path segment | killed: `test_a_line_mentioning_the_id_outside_the_calendar_path_is_unchanged` |
| finiteness check removed | killed: `test_non_finite_settings_refused_when_enabled[.nan-lookahead_days]` |

4 of 4 killed.
