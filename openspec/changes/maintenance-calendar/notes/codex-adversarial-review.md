# Task 8.1: Codex adversarial review

Run 2026-10-30 with `codex-companion.mjs adversarial-review --base 48b784b --scope branch`, focus: the credential, the external write, the hold/release durability path. Review text verbatim:

## Review

Target: branch diff against 48b784b
Verdict: needs-attention

Do not ship yet: provisional cooldown can permanently close another held window before triage is durable. Calendar identifiers also leak through normal logging, and nonfinite configuration bypasses stale-snapshot protection. Reproduced with in-memory probes.

Findings:
- [high] Provisional cooldown can permanently discard a later window's alert (henk/events/release.py:137-143)
  After an outage spanning two windows containing the same identity, releasing W1 arms cooldown before its queued triage runs. W2 then produces no turn and is immediately marked released. Killing the process before W1's audit flush leaves W2 permanently closed; rebuilding the runtime releases only W1's older event. The newer alert payload never reaches triage. This was reproduced without executing any triage. The in-flight protection covers W1 but does not protect windows suppressed by W1's provisional cooldown.
  Recommendation: Distinguish pending triage reservations from durable cooldown. Defer closing windows suppressed by pending triages until those triages become durable. Add a two-window crash/restart regression.
- [medium] Routine INFO logs expose the configured calendar identifier (henk/calendar/client.py:316-322)
  The request URL contains the calendar id and is passed directly to httpx. The application enables root INFO logging, which causes httpx to log the complete URL. A MockTransport probe confirmed the encoded calendar id appears in logs. The implementation notes explicitly leave this exposure in place. The id alone grants no access, but private calendar metadata reaches routine logs and copied diagnostics.
  Recommendation: Redact the calendar path segment from HTTP logs while retaining status diagnostics. Test actual INFO logging for both raw and encoded identifiers.
- [medium] Nonfinite settings disable the stale-snapshot safety bound (henk/config.py:1545-1555)
  The positivity check accepts NaN and positive infinity. With stale_after_seconds set to YAML .nan, windows.py's age comparison is always false, so a seven-day-old snapshot still supplies active hold windows. This was reproduced with accepted configuration. Obsolete maintenance plans can therefore suppress alerts instead of failing open at the configured stale boundary.
  Recommendation: Require finite calendar numeric settings before checking positivity or ranges. Add rejection tests for .nan, .inf and -.inf.

Next steps:
- Add the cross-window crash regression and fix provisional cooldown handling.
- Redact calendar identifiers from HTTP logs and reject nonfinite configuration.

## What was done

All three findings were fixed test-first, each fix mutated and confirmed red. Full suite
after: 4426 passed, 11 skipped, 12 deselected.

- **[high] Cross-window provisional cooldown.** Orchestrator's assessment: real, but lower
  than "high". After the restart the identity is still triaged once, with W1's older
  payload, and the live path already lets an unflushed triage's cooldown suppress a
  later batch (`henk/events/coordinator.py`, the comment above the marker branch). Fixed
  anyway, because the fix is cheap and removes the cross-window interaction: `HoldLedger`
  records each in-flight release's identity keys, and the releaser skips a window that
  shares an identity with one, so W2 is evaluated only after W1's triage is durable.
  Windows with no shared identity still release in the same tick. Two-window crash and
  restart regression added. Details: `notes/apply-group6.md`, "Codex follow-up".
- **[medium] Calendar id in httpx INFO logs.** Reverses the earlier "leave it" decision
  (`notes/apply-decisions.md`): copied diagnostics are a real path. A filter on the
  `httpx` logger rewrites the `/calendars/<id>/` segment, raw and percent-encoded, to
  `<calendar>`; method, host, query and status stay; other lines are unchanged. Tested
  through real INFO logging (`tests/test_calendar_log_redaction.py`).
- **[medium] Non-finite settings.** Every numeric `calendar.*` setting must be finite
  before range and positivity checks; `.nan`, `.inf` and `-.inf` are refused when
  enabled. This also fixed a startup crash `.inf` caused in an integer setting even with
  the calendar disabled (a disabled section now falls back to the default). Details:
  `notes/apply-codex-followup.md`.
