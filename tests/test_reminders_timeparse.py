"""Time resolution and rendering — the DST core (group 4).

From the reminders spec's wall-clock, nonexistent, ambiguous, duration, renderer and
process-timezone requirements, and design D3/D4/D5/D10.

Three rules govern this module, all earned in review rather than assumed:

- **Every clock-touching test runs under a hostile process timezone.** The
  `process_tz` fixture parametrises over UTC / Pacific/Kiritimati (+14) /
  Europe/Amsterdam and asserts identical stored instants *and* identical rendered
  strings. Kiritimati is the value that earns its place: a leak there changes the
  *date*, not merely the hour.
- **Instants, not wall clocks.** Two aware date-times in one zone compare by wall
  clock, so `fold=0` and `fold=1` of one reading compare **equal** while denoting
  instants an hour apart. Every assertion about ordering or identity here is on the
  epoch value.
- **Real transition dates, pinned.** `Europe/Amsterdam`, forward
  2026-03-29 02:00 → 03:00 and back 2026-10-25 03:00 → 02:00. A synthesized
  `timedelta` offset would prove nothing about `zoneinfo`, which is the thing under
  test. Every epoch literal below was produced by running the zone, and the two
  imaginary/ambiguous pairs match the recorded probe output in
  `notes/dst-verified-facts.md`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from henk.reminders.timeparse import (
    AMBIGUOUS,
    COMMAND,
    IMAGINARY,
    NORMAL,
    TOOL,
    TimeResolutionError,
    TimeResolver,
    classify_local,
    render_instant,
)

AMS = ZoneInfo("Europe/Amsterdam")
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")  # 30-minute forward gap
TROLL = ZoneInfo("Antarctica/Troll")  # two-hour forward gap

# --- Pinned instants, from running the real zone --------------------------

#: 2026-10-25 02:30 happens TWICE. Both folds render "02:30"; only the epoch and
#: the abbreviation tell them apart, which is why every assertion here is on the
#: epoch.
FALL_02_30_FIRST = 1792888200.0  # CEST, the earlier instant
FALL_02_30_SECOND = 1792891800.0  # CET, one hour later

#: 2026-03-29 02:30 does NOT exist. PEP 495 still constructs it: fold=0 gives the
#: pre-transition offset and normalises to a reading of 03:30, which is the silent
#: one-hour error this whole module exists to prevent.
SPRING_02_30_FOLD0 = 1774747800.0  # renders back as 03:30 CEST
SPRING_02_30_FOLD1 = 1774744200.0  # renders back as 01:30 CET

NOW_SPRING_EVENING = 1774807200.0  # 2026-03-29 20:00 CEST
NOW_SPRING_NIGHT = 1774740600.0  # 2026-03-29 00:30 CET (before the gap)
NOW_DAY_BEFORE_SPRING = 1774724400.0  # 2026-03-28 20:00 CET
NOW_DAY_BEFORE_FALL = 1792864800.0  # 2026-10-24 20:00 CEST

NOW_FALL_02_15_FIRST = 1792887300.0
NOW_FALL_02_30_FIRST = 1792888200.0
NOW_FALL_02_45_FIRST = 1792889100.0
NOW_FALL_02_15_SECOND = 1792890900.0
NOW_FALL_02_30_SECOND = 1792891800.0
NOW_FALL_02_45_SECOND = 1792892700.0

DUE_2026_03_30_02_30 = 1774830600.0
DUE_2026_10_26_02_30 = 1792978200.0
DUE_2026_08_25_07_30 = 1787635800.0
DUE_2026_12_25_14_00 = 1798203600.0  # CET, not CEST — the target date's offset

NOW_ORDINARY = 1787203800.0  # 2026-08-20 07:30 CEST, no transition anywhere near


def _resolver(now: float, zone: ZoneInfo = AMS, **kwargs) -> TimeResolver:
    return TimeResolver(zone, clock=lambda: now, **kwargs)


# --- 4.1 The wall-clock family -------------------------------------------


def test_a_naive_iso_value_is_the_owner_zone_and_not_utc(process_tz):
    # DISCRIMINATING: asserted on the resolved INSTANT. The rendered string is the
    # same either way when the process zone happens to be the owner's, which is
    # exactly the case that hides the bug.
    got = _resolver(NOW_ORDINARY).resolve("2026-08-25T07:30", path=TOOL)
    assert got.due_at == DUE_2026_08_25_07_30
    # If it had been read as UTC the instant would be two hours later.
    assert got.due_at != DUE_2026_08_25_07_30 + 7200


def test_the_target_dates_own_offset_is_used_not_todays(process_tz):
    # Scheduled during summer time for a winter date: CET, not CEST.
    got = _resolver(NOW_ORDINARY).resolve("2026-12-25 14:00", path=TOOL)
    assert got.due_at == DUE_2026_12_25_14_00
    assert datetime.fromtimestamp(got.due_at, AMS).strftime("%H:%M %Z") == "14:00 CET"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-08-25 07:30", DUE_2026_08_25_07_30),
        ("2026-08-25T07:30", DUE_2026_08_25_07_30),
        ("2026-08-25T07:30:00", DUE_2026_08_25_07_30),
        # Fractional seconds are accepted deliberately: a common JSON-serializer
        # output, no DST implication, and refusing it costs a valid schedule.
        ("2026-08-25T07:30:00.000", DUE_2026_08_25_07_30),
        ("  2026-08-25 07:30  ", DUE_2026_08_25_07_30),
    ],
)
def test_the_accepted_dated_shapes(process_tz, value, expected):
    assert _resolver(NOW_ORDINARY).resolve(value, path=TOOL).due_at == expected


@pytest.mark.parametrize("fold_now", [False, True])
def test_the_wall_clock_round_trip_holds_on_both_folds_of_an_ambiguous_reading(
    process_tz, fold_now
):
    # The draft's "except where ambiguous" carve-out was wrong: BOTH folds of an
    # ambiguous reading round-trip to the wall clock submitted. That is precisely
    # why the nonexistent-time check cannot detect them.
    now = NOW_DAY_BEFORE_FALL if not fold_now else NOW_FALL_02_15_FIRST
    got = _resolver(now).resolve("2026-10-25 02:30", path=TOOL)
    assert got.due_at in (FALL_02_30_FIRST, FALL_02_30_SECOND)
    assert datetime.fromtimestamp(got.due_at, AMS).strftime("%H:%M") == "02:30"


def test_a_past_time_beyond_the_skew_tolerance_is_refused_naming_the_local_time(
    process_tz,
):
    resolver = _resolver(NOW_ORDINARY, clock_skew_tolerance_seconds=60)
    with pytest.raises(TimeResolutionError) as exc:
        resolver.resolve("2026-08-20 07:00", path=TOOL)
    message = str(exc.value)
    assert "past" in message.lower()
    # "the error names the current local time" — rendered, not an epoch float.
    assert render_instant(NOW_ORDINARY, AMS) in message


def test_the_past_check_inside_the_repeated_hour_compares_instants(process_tz):
    """DISCRIMINATING, and the reason this test exists at all.

    A mutation that performed the past check on aware date-times in the owner's zone
    survived every other assertion in this module. It is wrong because Python
    compares two aware date-times sharing one ``tzinfo`` by **wall clock**, ignoring
    ``fold`` — so inside the repeated hour it disagrees with the instants.

    Concretely, at 02:15 during the **second** pass of 2026-10-25 (CET, epoch
    1792890900), a dated request for ``2026-10-25 02:30`` resolves to the first
    occurrence (CEST, epoch 1792888200), which is 45 minutes in the **past**. The
    wall-clock comparison says ``02:30 >= 02:15`` and accepts it, storing a reminder
    that is already overdue.
    """
    now = NOW_FALL_02_15_SECOND
    candidate = datetime(2026, 10, 25, 2, 30, tzinfo=AMS, fold=0)
    assert candidate >= datetime.fromtimestamp(now, AMS)  # wall clock: "future"
    assert candidate.timestamp() < now  # the instant: 45 minutes ago

    resolver = _resolver(now, clock_skew_tolerance_seconds=60)
    with pytest.raises(TimeResolutionError) as exc:
        resolver.resolve("2026-10-25 02:30", path=TOOL)
    assert exc.value.shape == "past"


def test_a_past_time_inside_the_skew_tolerance_is_accepted(process_tz):
    # Absorbs the sub-second gap between the model reading the turn's time header
    # and the app resolving the value it composed from it.
    resolver = _resolver(NOW_ORDINARY, clock_skew_tolerance_seconds=120)
    got = resolver.resolve("2026-08-20 07:29", path=TOOL)
    assert got.due_at == NOW_ORDINARY - 60


def test_beyond_the_horizon_is_refused_naming_the_horizon(process_tz):
    resolver = _resolver(NOW_ORDINARY, horizon_days=30)
    with pytest.raises(TimeResolutionError) as exc:
        resolver.resolve("2026-12-25 14:00", path=TOOL)
    assert "30" in str(exc.value)


@pytest.mark.parametrize(
    "value,fragment",
    [
        ("2026-08-25", "time of day"),
        ("20260825", "form"),
        ("2026-W35", "form"),
        ("2026-W35-1", "form"),
        ("07:30", "form"),
        ("2026-08-25T07:30+02:00", "offset"),
        ("2026-08-25T07:30+02", "offset"),
        ("2026-08-25T07:30-00:00", "offset"),
        ("2026-08-25T07:30Z", "offset"),
        ("2026-08-25T07:30:00z", "offset"),
        ("2026-08-25T24:00", "form"),
        ("sometime next week", "form"),
        ("", "form"),
    ],
)
def test_unwhitelisted_shapes_are_each_refused_with_their_own_error(
    process_tz, value, fragment
):
    # The whitelist is matched BEFORE parsing: `fromisoformat` takes `20260825`,
    # `2026-W35` and `2026-W35-1`, and it makes `2026-08-25` indistinguishable from
    # `2026-08-25T00:00` afterwards, so "reject a date with no time of day" is only
    # decidable on the string.
    with pytest.raises(TimeResolutionError) as exc:
        _resolver(NOW_ORDINARY).resolve(value, path=TOOL)
    assert fragment in str(exc.value).lower()


def test_a_bare_clock_reading_is_refused_on_the_tool_path_but_taken_on_the_command_path(
    process_tz,
):
    resolver = _resolver(NOW_ORDINARY)
    with pytest.raises(TimeResolutionError):
        resolver.resolve("21:00", path=TOOL)
    assert resolver.resolve("21:00", path=COMMAND).due_at > NOW_ORDINARY


def test_an_ordinary_time_is_neither_refused_nor_annotated(process_tz):
    # NEGATIVE CASE. Without it, an implementation whose detection step is inverted
    # or stubbed True annotates every reminder and passes every other test here.
    got = _resolver(NOW_ORDINARY).resolve("2026-08-25 07:30", path=TOOL)
    assert got.due_at == DUE_2026_08_25_07_30
    assert got.disclosure == ""


# --- 4.2 The nonexistent-time rule ---------------------------------------


@pytest.mark.parametrize("path", [TOOL, COMMAND])
def test_a_nonexistent_dated_reading_is_refused_on_every_path(process_tz, path):
    with pytest.raises(TimeResolutionError) as exc:
        _resolver(NOW_DAY_BEFORE_SPRING).resolve("2026-03-29 02:30", path=path)
    message = str(exc.value)
    assert "02:30" in message
    assert "29 March" in message
    assert "Europe/Amsterdam" in message
    # The transition and BOTH valid neighbours, derived from its boundaries.
    assert "02:00" in message and "03:00" in message
    assert "01:59" in message


def test_the_tool_path_tells_the_agent_to_ask_rather_than_substitute(process_tz):
    with pytest.raises(TimeResolutionError) as exc:
        _resolver(NOW_DAY_BEFORE_SPRING).resolve("2026-03-29 02:30", path=TOOL)
    message = str(exc.value).lower()
    assert "ask the owner" in message
    # A rejection aimed at the model that invites a retry delegates the invention
    # of intent rather than preventing it.
    assert "do not" in message or "don't" in message


def test_no_path_stores_an_instant_that_renders_back_as_03_30(process_tz):
    # The actual failure being prevented: `02:30 CET` normalises to `01:30 UTC`,
    # which renders as `03:30 CEST` — an hour the owner never typed.
    assert (
        datetime.fromtimestamp(SPRING_02_30_FOLD0, AMS).strftime("%H:%M") == "03:30"
    )
    for path in (TOOL, COMMAND):
        with pytest.raises(TimeResolutionError):
            _resolver(NOW_DAY_BEFORE_SPRING).resolve("2026-03-29 02:30", path=path)


def test_a_bare_clock_reading_inside_the_gap_is_refused_when_it_is_tonights_reading(
    process_tz,
):
    # ORDERING ASSERTED: `now` is 00:30 on the transition date, so today's 02:30 is
    # still ahead and IS the reading the owner meant. Refusal is correct here — and
    # the same command at 20:00 that evening must SCHEDULE (next test), which is
    # why both rows are required.
    with pytest.raises(TimeResolutionError) as exc:
        _resolver(NOW_SPRING_NIGHT).resolve("02:30", path=COMMAND)
    assert "02:30" in str(exc.value)
    assert "01:59" in str(exc.value) and "03:00" in str(exc.value)


def test_a_one_date_advance_that_lands_in_the_gap_is_refused_not_skipped_again(
    process_tz,
):
    # ORDERING ASSERTED: today (03-28) 02:30 has passed, so the search advances one
    # date to 03-29 02:30 — which is imaginary. D4 forbids skipping a further day:
    # a reminder silently scheduled 24 hours late is a broken promise.
    with pytest.raises(TimeResolutionError) as exc:
        _resolver(NOW_DAY_BEFORE_SPRING).resolve("02:30", path=COMMAND)
    assert "29 March" in str(exc.value)


@pytest.mark.parametrize(
    "zone,date,reading,before,after",
    [
        # 30-minute gap: 02:00 -> 02:30 on 2026-10-04.
        (LORD_HOWE, "2026-10-04", "02:15", "01:59", "02:30"),
        # Two-hour gap: 01:00 -> 03:00 on 2026-03-29. The +1h neighbour (02:30) is
        # ITSELF imaginary, so an implementation that computes neighbours as +-1h
        # names an invalid reading and the error becomes useless.
        (TROLL, "2026-03-29", "01:30", "00:59", "03:00"),
    ],
)
def test_neighbours_come_from_the_transition_not_from_a_fixed_hour(
    process_tz, zone, date, reading, before, after
):
    # DISCRIMINATING: an implementation hardcoding +-1h passes every Amsterdam test
    # in this module and fails both of these.
    naive = datetime.strptime(f"{date} {reading}", "%Y-%m-%d %H:%M")
    assert classify_local(naive, zone) == IMAGINARY, "the case must actually be a gap"
    resolver = _resolver(naive.replace(tzinfo=timezone.utc).timestamp() - 86400, zone)
    with pytest.raises(TimeResolutionError) as exc:
        resolver.resolve(f"{date} {reading}", path=TOOL)
    message = str(exc.value)
    assert before in message and after in message
    # And both named readings must be valid readings in that zone.
    for named in (before, after):
        hour, minute = (int(p) for p in named.split(":"))
        candidate = naive.replace(hour=hour, minute=minute)
        if named == before and candidate > naive:
            candidate -= timedelta(days=1)
        assert classify_local(candidate, zone) != IMAGINARY, named


# --- 4.3 The ambiguous-time rule -----------------------------------------


def test_an_ambiguous_dated_reading_resolves_to_the_earlier_instant(process_tz):
    # DISCRIMINATING: asserted on the epoch AND the offset, never the wall clock —
    # both folds render "02:30", so a wall-clock assertion cannot fail.
    got = _resolver(NOW_DAY_BEFORE_FALL).resolve("2026-10-25 02:30", path=TOOL)
    assert got.due_at == FALL_02_30_FIRST
    assert got.due_at != FALL_02_30_SECOND
    resolved = datetime.fromtimestamp(got.due_at, AMS)
    assert resolved.utcoffset() == timedelta(hours=2)  # CEST, the earlier offset
    assert resolved.tzname() == "CEST"


def test_the_ambiguity_is_disclosed_and_says_which_occurrence(process_tz):
    got = _resolver(NOW_DAY_BEFORE_FALL).resolve("2026-10-25 02:30", path=TOOL)
    assert "twice" in got.disclosure
    assert "first" in got.disclosure
    assert "back" in got.disclosure  # the clocks go back that night


def test_the_nonexistent_check_does_not_fire_on_an_ambiguous_reading(process_tz):
    # Both folds of an ambiguous reading round-trip cleanly, so the round-trip step
    # cannot see them — and the offset-comparison step cannot tell the two kinds
    # apart. Neither step alone works, which is why there are two.
    naive = datetime(2026, 10, 25, 2, 30)
    assert classify_local(naive, AMS) == AMBIGUOUS
    assert _resolver(NOW_DAY_BEFORE_FALL).resolve(
        "2026-10-25 02:30", path=TOOL
    ).due_at == FALL_02_30_FIRST


def test_an_ordinary_date_produces_no_disclosure(process_tz):
    # NEGATIVE CASE, asserting the ABSENCE of the substring. Without it, a stubbed
    # or inverted detection step annotates everything and passes the rest.
    got = _resolver(NOW_ORDINARY).resolve("2026-08-25 07:30", path=TOOL)
    assert got.disclosure == ""
    assert "twice" not in got.disclosure


def test_fold_zero_is_the_earlier_instant_for_ambiguous_but_the_later_for_imaginary():
    # The natural generalisation is false, and leaning on it outside D5's scope is
    # a bug. Pinned so the comment in the implementation has a test behind it.
    amb = datetime(2026, 10, 25, 2, 30)
    assert amb.replace(tzinfo=AMS, fold=0).timestamp() < amb.replace(
        tzinfo=AMS, fold=1
    ).timestamp()
    imag = datetime(2026, 3, 29, 2, 30)
    assert imag.replace(tzinfo=AMS, fold=0).timestamp() > imag.replace(
        tzinfo=AMS, fold=1
    ).timestamp()
    # And the two folds of ONE reading compare EQUAL as aware date-times while
    # denoting instants an hour apart, which is why ordering is never done on them.
    assert amb.replace(tzinfo=AMS, fold=0) == amb.replace(tzinfo=AMS, fold=1)


# --- 4.4 The duration family ---------------------------------------------


@pytest.mark.parametrize("path", [TOOL, COMMAND])
@pytest.mark.parametrize(
    "label,start",
    [
        ("spring", 1774609200.0),  # 2026-03-27 12:00 CET, three days before
        ("fall", 1792749600.0),  # 2026-10-23 12:00 CEST, three days before
    ],
)
def test_plus_three_days_is_exactly_72_hours_across_either_transition(
    process_tz, path, label, start
):
    # DISCRIMINATING: `aware + timedelta(days=3)` is WALL-CLOCK arithmetic and
    # yields 71 hours across the spring transition and 73 across the autumn one.
    # This is the assertion that pins the rule, and it covers the tool path too,
    # where a 71-hour result would otherwise be invisible.
    got = _resolver(start).resolve("+3d", path=path)
    assert got.due_at - start == 3 * 86400

    started = datetime.fromtimestamp(start, AMS)
    landed = datetime.fromtimestamp(got.due_at, AMS)
    offset_change = landed.utcoffset() - started.utcoffset()
    # The resulting local time differs from the starting wall clock by exactly the
    # offset change — disclosed by the echo, not hidden.
    wall_shift = (
        landed.replace(tzinfo=None) - started.replace(tzinfo=None)
    ) - timedelta(days=3)
    assert wall_shift == offset_change
    assert offset_change != timedelta(0), f"{label} must actually cross a transition"

    # And the wall-clock alternative really is wrong by an hour, both ways.
    wall_arithmetic = (started + timedelta(days=3)).timestamp() - start
    assert wall_arithmetic in (71 * 3600, 73 * 3600)


@pytest.mark.parametrize(
    "value,seconds",
    [("+90m", 5400), ("+2h", 7200), ("+3d", 259200), ("+1m", 60), ("+999999m", 59999940)],
)
@pytest.mark.parametrize("path", [TOOL, COMMAND])
def test_the_accepted_duration_shapes(process_tz, value, seconds, path):
    resolver = _resolver(NOW_ORDINARY, horizon_days=100000)
    assert resolver.resolve(value, path=path).due_at == NOW_ORDINARY + seconds


def test_plus_24h_equals_plus_1d_only_as_a_guard_on_the_rejected_alternative(
    process_tz,
):
    # LABELLED AS SUCH: on its own this is a tautology, since
    # `timedelta(days=1) == timedelta(hours=24)`. It earns its place only as a
    # guard on the REJECTED calendar-day alternative for `+Nd`, under which the two
    # forms would mean different things across a transition.
    assert timedelta(days=1) == timedelta(hours=24)
    resolver = _resolver(1774609200.0)
    assert resolver.resolve("+24h", path=TOOL).due_at == resolver.resolve(
        "+1d", path=TOOL
    ).due_at


def test_a_duration_landing_inside_the_spring_gap_still_succeeds(process_tz):
    # The DST evaluation is SKIPPED on this path. A check there can only produce a
    # false rejection: the offset denotes a real instant however its wall clock
    # reads.
    start = 1774744200.0 - 1800  # half an hour before the transition instant
    got = _resolver(start).resolve("+30m", path=TOOL)
    assert got.due_at == start + 1800
    assert got.disclosure == ""


def test_a_duration_crossing_the_fall_back_carries_no_ambiguity_disclosure(process_tz):
    # Same rule, the other transition: a duration is never a wall clock, so it can
    # never be ambiguous and must never be annotated as if it were.
    got = _resolver(FALL_02_30_FIRST - 1800).resolve("+2h", path=TOOL)
    assert got.due_at == FALL_02_30_FIRST - 1800 + 7200
    assert got.disclosure == ""


@pytest.mark.parametrize(
    "value",
    [
        "+0m",
        "+0h",
        "+0d",
        "+9999999d",  # 7 digits: past the grammar's bound
        "+999999999d",  # would raise ValueError from the arithmetic
        "+99999999999999d",  # would raise OSError from the parse
        "-2h",
        "+2w",
        "+2",
        "2h",
        "+ 2h",
    ],
)
@pytest.mark.parametrize("path", [TOOL, COMMAND])
def test_zero_and_over_long_magnitudes_are_refused_by_the_grammar(
    process_tz, value, path
):
    # The bound belongs in the SHAPE step, not in an exception handler: the
    # arithmetic runs before the horizon check, so `+999999999d` raises
    # `ValueError: year ... out of range` and `+99999999999999d` an `OSError` from
    # the platform clock before the horizon step ever gets a turn.
    with pytest.raises(TimeResolutionError) as exc:
        _resolver(NOW_ORDINARY).resolve(value, path=path)
    assert "form" in str(exc.value).lower()


def test_no_arithmetic_error_escapes_as_itself(process_tz):
    resolver = _resolver(NOW_ORDINARY)
    for value in ("+999999999d", "+99999999999999d"):
        try:
            resolver.resolve(value, path=TOOL)
        except TimeResolutionError:
            pass
        except (OverflowError, OSError, ValueError) as exc:  # pragma: no cover
            pytest.fail(f"a raw {type(exc).__name__} escaped for {value}: {exc}")


def test_an_in_grammar_duration_past_the_horizon_is_refused_naming_it(process_tz):
    with pytest.raises(TimeResolutionError) as exc:
        _resolver(NOW_ORDINARY, horizon_days=365).resolve("+999999d", path=TOOL)
    assert "365" in str(exc.value)


# --- 4.5 The HH:MM next-occurrence rule ----------------------------------


@pytest.mark.parametrize(
    "now,reading,expected,note",
    [
        # A reading later today resolves today.
        (NOW_ORDINARY, "21:00", 1787252400.0, "later today"),
        # A reading already past resolves to the same reading on the next LOCAL
        # DATE — not to the instant 24 hours later, which is wrong by an hour twice
        # a year.
        (NOW_ORDINARY, "07:00", 1787288400.0, "past today, next date"),
        # ORDERING: 20:00 on the spring-forward date. Today's 02:30 is imaginary AND
        # past, so selection advances first and the reading the owner can actually
        # have is scheduled. Evaluated the other way round this is refused.
        (NOW_SPRING_EVENING, "02:30", DUE_2026_03_30_02_30, "select before evaluate"),
        # The advanced candidate is AMBIGUOUS rather than imaginary, and must still
        # schedule — with its disclosure (asserted separately below).
        (NOW_DAY_BEFORE_FALL, "02:30", FALL_02_30_FIRST, "advanced and ambiguous"),
    ],
)
def test_the_next_occurrence_table(process_tz, now, reading, expected, note):
    assert _resolver(now).resolve(reading, path=COMMAND).due_at == expected, note


@pytest.mark.parametrize(
    "now,today_candidate,expected,hours_apart",
    [
        # Autumn: the transition falls between today's 11:00 and tomorrow's, so the
        # two readings are 25 hours apart. "Add 24 hours" would land on 10:00.
        (1792836000.0, 1792832400.0, 1792922400.0, 25),
        # Spring, the mirror case: 23 hours apart, and "+24h" would land on 12:00.
        (1774695600.0, 1774692000.0, 1774774800.0, 23),
    ],
)
def test_the_next_occurrence_is_a_calendar_date_not_24_hours(
    process_tz, now, today_candidate, expected, hours_apart
):
    # DISCRIMINATING: the reading is already past today, so the search advances one
    # calendar DATE and re-resolves. Across a transition that is 23 or 25 hours from
    # today's occurrence, never 24 — so an implementation that adds 86400 seconds to
    # the same-date candidate is wrong by an hour, twice a year.
    got = _resolver(now).resolve("11:00", path=COMMAND)
    assert got.due_at == expected
    assert got.due_at - today_candidate == hours_apart * 3600
    assert got.due_at != today_candidate + 86400
    # The wall clock is preserved, which is the property the calendar-date rule buys.
    assert datetime.fromtimestamp(got.due_at, AMS).strftime("%H:%M") == "11:00"


@pytest.mark.parametrize(
    "now,expected,delta_hours",
    [
        # DISCRIMINATING ROW: at 02:45 during the FIRST pass, the fold=1 occurrence
        # is 45 minutes away. A fold=0-only rule skips it and lands 24.75 hours out,
        # which is exactly what D4 forbids.
        (NOW_FALL_02_45_FIRST, FALL_02_30_SECOND, 0.75),
        (NOW_FALL_02_15_FIRST, FALL_02_30_FIRST, 0.25),
        (NOW_FALL_02_30_FIRST, FALL_02_30_SECOND, 1.0),
        (NOW_FALL_02_15_SECOND, FALL_02_30_SECOND, 0.25),
        # Both occurrences have passed, so the next one is on the following local
        # date. Correct, and the point of the "never 25 hours out" bound: 24.0 and
        # 23.75, not 24.75.
        (NOW_FALL_02_30_SECOND, DUE_2026_10_26_02_30, 24.0),
        (NOW_FALL_02_45_SECOND, DUE_2026_10_26_02_30, 23.75),
    ],
)
def test_both_folds_of_the_repeated_hour_are_considered(
    process_tz, now, expected, delta_hours
):
    got = _resolver(now).resolve("02:30", path=COMMAND)
    assert got.due_at == expected
    assert got.due_at > now, "the selected occurrence must be strictly in the future"
    assert (got.due_at - now) / 3600 == delta_hours
    # Never the 24.75-hour answer a fold=0-only rule produces.
    assert got.due_at - now < 25 * 3600


def test_an_advanced_candidate_that_is_ambiguous_still_carries_its_disclosure(
    process_tz,
):
    got = _resolver(NOW_DAY_BEFORE_FALL).resolve("02:30", path=COMMAND)
    assert got.due_at == FALL_02_30_FIRST
    assert "twice" in got.disclosure


def test_detection_re_runs_on_the_advanced_candidate(process_tz):
    # Set `now` so today's reading is past and the ADVANCE lands in the gap. The
    # detection has to re-run on the advanced candidate, not on the original one.
    with pytest.raises(TimeResolutionError) as exc:
        _resolver(NOW_DAY_BEFORE_SPRING).resolve("02:30", path=COMMAND)
    assert "29 March" in str(exc.value)


def test_a_clock_reading_within_the_repeated_hour_never_compares_by_wall_clock(
    process_tz,
):
    # "Comparisons inside the repeated hour use instants": at 02:15 fold=1, the
    # fold=0 candidate at 02:30 is LATER in wall-clock terms and EARLIER as an
    # instant. Treating it as future would schedule a reminder 15 minutes in the
    # past.
    now = NOW_FALL_02_15_SECOND
    cand = datetime(2026, 10, 25, 2, 30, tzinfo=AMS, fold=0)
    now_aware = datetime.fromtimestamp(now, AMS)
    assert cand > now_aware  # the wall-clock comparison claims "future"
    assert cand.timestamp() < now  # the instant comparison says otherwise
    assert _resolver(now).resolve("02:30", path=COMMAND).due_at == FALL_02_30_SECOND


@pytest.mark.parametrize("value", ["24:00", "7:30", "0730", "07:60", "99:99"])
def test_malformed_clock_readings_are_refused(process_tz, value):
    with pytest.raises(TimeResolutionError):
        _resolver(NOW_ORDINARY).resolve(value, path=COMMAND)


# --- 4.6 The renderer ----------------------------------------------------


def test_the_render_carries_weekday_date_local_time_and_zone_marker(process_tz):
    rendered = render_instant(DUE_2026_08_25_07_30, AMS)
    assert rendered == "Tuesday 25 August at 07:30 CEST"


@pytest.mark.parametrize(
    "zone,marker",
    [
        ("Asia/Kolkata", "IST"),
        ("Asia/Kathmandu", "+0545"),
        ("Australia/Lord_Howe", "+1030"),
        ("America/Sao_Paulo", "-03"),
    ],
)
def test_the_zone_marker_may_be_numeric_rather_than_alphabetic(process_tz, zone, marker):
    # `tzname()` yields `+0545`, `+1030` and `-03` for some zones, so the wording
    # must not promise letters.
    assert render_instant(DUE_2026_08_25_07_30, ZoneInfo(zone)).endswith(marker)


def test_the_two_folds_of_a_repeated_reading_render_with_different_abbreviations(
    process_tz,
):
    # The abbreviation genuinely discriminates the two occurrences, which is what
    # makes the disclosure belt-and-braces rather than the only signal.
    assert render_instant(FALL_02_30_FIRST, AMS) == "Sunday 25 October at 02:30 CEST"
    assert render_instant(FALL_02_30_SECOND, AMS) == "Sunday 25 October at 02:30 CET"


def test_the_same_instant_renders_identically_wherever_it_appears(process_tz):
    resolver = _resolver(NOW_ORDINARY)
    once = render_instant(DUE_2026_08_25_07_30, AMS)
    assert resolver.render(DUE_2026_08_25_07_30) == once
    # The header the agent reasons from and the due time the owner is told use the
    # SAME function, so a due time can never read differently in two places.
    from henk.reminders.timeparse import current_time_header

    assert once in current_time_header(DUE_2026_08_25_07_30, AMS)


def test_the_renderer_never_uses_a_locale_sensitive_format_code(process_tz):
    """DISCRIMINATING: `%A` and `%B` follow LC_TIME; `%Z` does not.

    Asserted **structurally**, because the behavioural version of this test is
    vacuous on a host with no non-English locale installed — and this host has
    exactly four (`C`, `C.utf8`, `en_US.utf8`, `POSIX`), none of which changes a
    month name. A `strftime`-based renderer passed the behavioural check here and
    would have turned every reminder confirmation Dutch the day someone added
    locales to the image for an unrelated reason.
    """
    import ast
    import inspect

    from henk.reminders import timeparse as module

    source = inspect.getsource(module.render_instant)
    for code in ("%A", "%B", "%a", "%b", "%c", "%x", "%p"):
        assert code not in source, f"{code} follows LC_TIME and must not be used"
    # And the fixed tables ARE what it reads.
    names = {
        node.id
        for node in ast.walk(ast.parse(inspect.getsource(module.render_instant).strip()))
        if isinstance(node, ast.Name)
    }
    assert {"WEEKDAYS", "MONTHS"} <= names
    assert module.WEEKDAYS[0] == "Monday" and module.MONTHS[7] == "August"


def test_the_render_is_identical_under_every_locale_this_host_has(process_tz):
    # The behavioural half. Weak on a host with only English locales, which is why
    # the structural test above exists beside it — but it is the one that would
    # catch a locale dependency arriving through some route the source scan misses.
    import locale

    rendered = []
    for candidate in ("C", "C.utf8", "en_US.utf8", "POSIX", "nl_NL.UTF-8", "de_DE.UTF-8"):
        try:
            locale.setlocale(locale.LC_TIME, candidate)
        except locale.Error:
            continue
        try:
            rendered.append(render_instant(DUE_2026_08_25_07_30, AMS))
        finally:
            locale.setlocale(locale.LC_TIME, "C")
    assert len(rendered) >= 2
    assert len(set(rendered)) == 1, rendered
    assert rendered[0] == "Tuesday 25 August at 07:30 CEST"


def test_rendering_uses_the_currently_configured_zone_not_the_rows_due_tz(process_tz):
    # `due_tz` is a disclosure and a diagnostic, never the rendering source: the
    # instant is fixed, the wall clock is not, and the owner reads today's zone.
    assert render_instant(DUE_2026_08_25_07_30, AMS).endswith("07:30 CEST")
    assert render_instant(DUE_2026_08_25_07_30, ZoneInfo("UTC")).endswith("05:30 UTC")


def test_an_absurd_instant_renders_without_raising(process_tz):
    # The renderer sits on the reply path; a bad stored value must not take the
    # reply down with it.
    assert render_instant(float("nan"), AMS)
    assert render_instant(1e30, AMS)


# --- 4.7b One INFO line per rejection ------------------------------------


def test_a_rejected_schedule_logs_one_info_line_naming_the_shape_and_reason(
    process_tz, caplog
):
    # A rejection writes no audit record by design (receipts record state changes,
    # and none occurred), nothing is stored, and tool-result text is no longer
    # logged — so without this a model repeatedly submitting a bad form is
    # invisible except in the token bill.
    with caplog.at_level(logging.INFO, logger="henk.reminders.timeparse"):
        with pytest.raises(TimeResolutionError):
            _resolver(NOW_ORDINARY).resolve("2026-08-25T07:30Z", path=TOOL)
    lines = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(lines) == 1
    message = lines[0].getMessage()
    assert "offset" in message
    assert "tool" in message


def test_an_accepted_schedule_logs_no_rejection_line(process_tz, caplog):
    with caplog.at_level(logging.INFO, logger="henk.reminders.timeparse"):
        _resolver(NOW_ORDINARY).resolve("2026-08-25 07:30", path=TOOL)
    assert [r for r in caplog.records if r.levelno == logging.INFO] == []


# --- The single clock capture --------------------------------------------


def test_the_clock_is_read_exactly_once_per_resolution(process_tz):
    # Two reads make the "unreachable past check" cell of the ladder false in a
    # sub-second window: a candidate selected as future at 07:29:59.9 is past when
    # the ladder re-reads at 07:30:00.1, producing a spurious "that time is in the
    # past" on a valid schedule. One read is also one place for a process-zone leak
    # to hide.
    reads: list[int] = []

    def counting_clock() -> float:
        reads.append(1)
        return NOW_ORDINARY

    resolver = TimeResolver(AMS, clock=counting_clock)
    resolver.resolve("2026-08-25 07:30", path=TOOL)
    assert len(reads) == 1
    reads.clear()
    resolver.resolve("07:00", path=COMMAND)
    assert len(reads) == 1
    reads.clear()
    resolver.resolve("+2h", path=TOOL)
    assert len(reads) == 1


# --- 4.0 The guard itself: identical instants AND identical strings -------


@pytest.mark.parametrize(
    "value,path",
    [
        ("2026-08-25 07:30", TOOL),
        ("2026-10-25 02:30", TOOL),
        ("2026-12-25 14:00", TOOL),
        ("+3d", TOOL),
        ("07:00", COMMAND),
        ("02:30", COMMAND),
    ],
)
def test_the_stored_instant_and_the_rendered_string_are_process_zone_invariant(
    process_tz, value, path
):
    got = _resolver(NOW_ORDINARY).resolve(value, path=path)
    # Both halves are pinned to LITERALS rather than compared between runs: three
    # runs that agree with each other would still all be wrong together if the
    # expected value were itself derived from the process zone.
    expected = {
        ("2026-08-25 07:30", TOOL): (DUE_2026_08_25_07_30, "Tuesday 25 August at 07:30 CEST"),
        ("2026-10-25 02:30", TOOL): (FALL_02_30_FIRST, "Sunday 25 October at 02:30 CEST"),
        ("2026-12-25 14:00", TOOL): (DUE_2026_12_25_14_00, "Friday 25 December at 14:00 CET"),
        ("+3d", TOOL): (NOW_ORDINARY + 259200, "Sunday 23 August at 07:30 CEST"),
        ("07:00", COMMAND): (1787288400.0, "Friday 21 August at 07:00 CEST"),
        ("02:30", COMMAND): (1787272200.0, "Friday 21 August at 02:30 CEST"),
    }[(value, path)]
    assert got.due_at == expected[0]
    assert render_instant(got.due_at, AMS) == expected[1]


def test_the_local_date_the_search_starts_from_is_the_owners_not_the_processs(
    process_tz,
):
    """DISCRIMINATING, and the reason `Pacific/Kiritimati` is in the fixture.

    The `HH:MM` search starts from *today's local date*. A zone-less
    `datetime.fromtimestamp(now).date()` reads the **process** zone, and at +14 that
    is a different **date**, not merely a different hour.

    `now` is 2026-08-20 23:00 in the owner's zone (21:00 UTC, so still the 20th
    there) but already 2026-08-21 11:00 in Kiritimati. Asking for `23:30`: the owner
    means half an hour from now, on the 20th. A process-zone leak starts from the
    21st, finds 23:30 on the 21st, calls it future, and schedules the reminder
    **24.5 hours late** — a broken promise that no hour-only hostile zone would have
    exposed.

    The other rows in the next-occurrence table above all happen to agree across the
    three process zones, which is exactly how this leak survived the first mutation
    pass.
    """
    now = 1787259600.0  # 2026-08-20 23:00 CEST
    assert datetime.fromtimestamp(now, AMS).date() == date(2026, 8, 20)
    assert datetime.fromtimestamp(now, ZoneInfo("Pacific/Kiritimati")).date() == date(
        2026, 8, 21
    )
    got = _resolver(now).resolve("23:30", path=COMMAND)
    assert got.due_at == now + 1800
    assert render_instant(got.due_at, AMS) == "Thursday 20 August at 23:30 CEST"


def test_classify_local_needs_no_process_zone(process_tz):
    assert classify_local(datetime(2026, 8, 25, 7, 30), AMS) == NORMAL
    assert classify_local(datetime(2026, 3, 29, 2, 30), AMS) == IMAGINARY
    assert classify_local(datetime(2026, 10, 25, 2, 30), AMS) == AMBIGUOUS


# --- 4.0b The grep, as a suite guard rather than a note -------------------

#: Every shape through which the process timezone can leak. `.timestamp()` on an
#: AWARE value is correct and common, so the bar is "every hit reviewed and
#: justified", not "no hits" — a grep that must come back empty on a pattern with
#: legitimate hits gets deleted by the next person. This test encodes the review:
#: it walks the AST and fails only on the shapes that are actually wrong.
_TIMEZONE_LEAK_SCOPE = ("henk/reminders", "henk/agent")


def _leaky_calls(path):
    """Yield (lineno, description) for every process-zone leak in one module."""
    import ast

    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")

        # `datetime.now()` with no tz, `utcnow()`, `date.today()`, `datetime.today()`.
        # Deliberately matched on the METHOD NAME alone rather than on the receiver:
        # an alias (`import datetime as dt`) or a helper would slip past a
        # receiver-based check. The cost is false positives on any other zero-arg
        # `now()`, which is loud and cheap to fix — it already caught one, and the
        # fix was to rename that method, because sharing the name with the thing it
        # must never be was itself the confusion.
        if name in ("now", "today") and not node.args and not node.keywords:
            yield node.lineno, f"{name}() with no timezone"
        if name == "utcnow":
            yield node.lineno, "utcnow() (naive, and deprecated)"
        # `time.localtime` / `time.mktime`: the classic naive-local converters.
        if name in ("localtime", "mktime"):
            yield node.lineno, f"time.{name}()"
        # `fromtimestamp(x)` with no zone reads the PROCESS zone.
        if name == "fromtimestamp":
            has_zone = len(node.args) >= 2 or any(
                kw.arg == "tz" for kw in node.keywords
            )
            if not has_zone:
                yield node.lineno, "fromtimestamp() with no tz argument"
        # A bare `.astimezone()` converts to the PROCESS zone.
        if name == "astimezone" and not node.args and not node.keywords:
            yield node.lineno, "bare .astimezone()"
        # `fromisoformat` returns a NAIVE datetime, and `.timestamp()` on a naive
        # value silently uses the process zone — the natural two-line shape of the
        # bug here (verified: 1792888200 / 1792895400 / 1792845000 for one string
        # under Amsterdam / UTC / Kiritimati). This module parses with an explicit
        # whitelist instead, so any use at all is a regression.
        if name == "fromisoformat":
            yield node.lineno, "fromisoformat() (returns naive)"


def test_no_module_in_scope_reads_the_process_timezone():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for directory in _TIMEZONE_LEAK_SCOPE:
        for path in sorted((root / directory).rglob("*.py")):
            for lineno, what in _leaky_calls(path):
                offenders.append(f"{path.relative_to(root)}:{lineno}: {what}")
    assert offenders == [], (
        "these read the process timezone, which is Europe/Amsterdam on the "
        "development host and UTC in the container — green locally, wrong on rp5:\n"
        + "\n".join(offenders)
    )


def test_the_guard_itself_catches_each_leaky_shape(tmp_path):
    # A guard never seen to fail is not a guard.
    sample = tmp_path / "leaky.py"
    sample.write_text(
        "from datetime import datetime, date\n"
        "import time\n"
        "a = datetime.now()\n"
        "b = datetime.utcnow()\n"
        "c = date.today()\n"
        "d = datetime.fromtimestamp(1.0)\n"
        "e = datetime.now().astimezone()\n"
        "f = time.localtime(1.0)\n"
        "g = time.mktime((2026, 1, 1, 0, 0, 0, 0, 1, -1))\n"
        "h = datetime.fromisoformat('2026-10-25T02:30').timestamp()\n"
    )
    found = {what for _, what in _leaky_calls(sample)}
    assert found == {
        "now() with no timezone",
        "utcnow() (naive, and deprecated)",
        "today() with no timezone",
        "fromtimestamp() with no tz argument",
        "bare .astimezone()",
        "time.localtime()",
        "time.mktime()",
        "fromisoformat() (returns naive)",
    }


def test_the_guard_accepts_the_correct_shapes(tmp_path):
    # And it must not fire on the legitimate forms, or it gets deleted.
    sample = tmp_path / "correct.py"
    sample.write_text(
        "from datetime import datetime, timezone\n"
        "from zoneinfo import ZoneInfo\n"
        "z = ZoneInfo('Europe/Amsterdam')\n"
        "a = datetime.fromtimestamp(1.0, z)\n"
        "b = datetime.fromtimestamp(1.0, tz=timezone.utc)\n"
        "c = datetime.now(z)\n"
        "d = datetime(2026, 1, 1, tzinfo=z).timestamp()\n"
        "e = datetime(2026, 1, 1, tzinfo=z).astimezone(timezone.utc)\n"
    )
    assert list(_leaky_calls(sample)) == []
