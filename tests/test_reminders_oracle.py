"""Ground-truth oracle for the imaginary/ambiguous classifier (task 4.9).

This is the transferable artifact of the DST work — the same role
`verify_selector_invariants.py` played for delivery. It is the only test in this
change that cannot be satisfied by an implementation which happens to handle the
hand-written `Europe/Amsterdam` cases.

**What it does.** It builds the truth independently of the code under test: walk
every UTC minute of a year, map each to its local wall clock, and count how many
distinct instants map to each local minute. A count of 0 means the local reading
does not exist (imaginary), 1 means it exists once (normal), 2 means it exists twice
(ambiguous). Then it asserts `classify_local` agrees on **every** local minute of
that year — not just the transition hours.

**Stated non-coverage.** Read this before trusting the result:

- **One year, minute resolution.** Sub-minute transitions (historical LMT offsets
  with seconds) are outside it, and so is any year but the parametrised one. The
  years chosen are the ones the change's requirements are written against.
- **The zones actually run.** The default set is two zones — `Europe/Amsterdam` plus
  one whose transition is not an hour wide — because the full sweep is ~6.2M
  classifications and this suite runs on every change. The full 12-zone sweep is
  behind the `dst_sweep` marker: `pytest -m dst_sweep`. What ran in the design's
  scrutiny pass (12 zones, ~6.2M classifications, zero mismatches) is recorded in
  `openspec/changes/reminders-core/notes/dst-verified-facts.md`.
- **The classifier only.** It proves the imaginary/ambiguous *detection* sound. It
  says nothing about candidate selection, the validation ladder's ordering, the
  neighbour derivation, or any wording — those are
  `tests/test_reminders_timeparse.py`'s job.
- **It cannot catch a shared wrong assumption**, because it derives truth from
  `zoneinfo` too. What it rules out is the classifier *disagreeing with the zone it
  is classifying against*, which is where every real bug in this shape lives.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from henk.reminders.timeparse import AMBIGUOUS, IMAGINARY, NORMAL, classify_local

#: Run every change. Amsterdam is the owner's zone; Lord Howe's transition is 30
#: minutes wide, so an implementation that assumes an hour fails here too.
DEFAULT_ZONES = ("Europe/Amsterdam", "Australia/Lord_Howe")

#: The full sweep from the design's scrutiny pass. Behind a marker: ~6.2M
#: classifications is minutes of CPU, not the price of every test run.
SWEEP_ZONES = (
    "Europe/Amsterdam",
    "Australia/Lord_Howe",
    "Antarctica/Troll",
    "America/Santiago",
    "Pacific/Chatham",
    "Asia/Kolkata",
    "Asia/Tehran",
    "Africa/Cairo",
    "America/Havana",
    "America/Sao_Paulo",
    "Asia/Kathmandu",
    "Australia/Sydney",
)

YEAR = 2026


def _occurrence_counts(zone: ZoneInfo, year: int) -> Counter:
    """How many distinct instants map to each local (month, day, hour, minute).

    Built by walking UTC, so it owes nothing to the classifier under test.
    """
    counts: Counter = Counter()
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    step = timedelta(minutes=1)
    # Widen by a day either side of the UTC year so local readings near the
    # boundaries are not miscounted by a partially-walked day.
    cursor = start - timedelta(days=1)
    limit = end + timedelta(days=1)
    while cursor < limit:
        local = cursor.astimezone(zone)
        if local.year == year:
            counts[(local.month, local.day, local.hour, local.minute)] += 1
        cursor += step
    return counts


def _local_minutes(year: int):
    """Every local wall-clock minute of ``year``, existing or not."""
    cursor = datetime(year, 1, 1, 0, 0)
    end = datetime(year + 1, 1, 1, 0, 0)
    step = timedelta(minutes=1)
    while cursor < end:
        yield cursor
        cursor += step


_EXPECTED = {0: IMAGINARY, 1: NORMAL, 2: AMBIGUOUS}

#: One UTC walk per zone is ~527,000 iterations. Several tests below want the same
#: zone's result, and the truth side does not depend on the classifier, so the
#: counts are memoised. The classification pass is NOT memoised — the two
#: oracle-validation tests below swap the classifier out and must get a fresh pass.
_COUNTS_CACHE: dict[tuple[str, int], Counter] = {}


def _check_zone(key: str, year: int = YEAR) -> tuple[Counter, list[str]]:
    zone = ZoneInfo(key)
    cached = _COUNTS_CACHE.get((key, year))
    if cached is None:
        cached = _occurrence_counts(zone, year)
        _COUNTS_CACHE[(key, year)] = cached
    counts = cached
    tally: Counter = Counter()
    mismatches: list[str] = []
    for naive in _local_minutes(year):
        occurrences = counts.get(
            (naive.month, naive.day, naive.hour, naive.minute), 0
        )
        # More than two occurrences of one local minute does not happen in the IANA
        # database; if it ever did, the two-step detector could not express it and
        # the failure should be loud rather than folded into "ambiguous".
        expected = _EXPECTED.get(occurrences)
        got = classify_local(naive, zone)
        tally[got] += 1
        if expected is None or got != expected:
            mismatches.append(
                f"{key} {naive:%Y-%m-%d %H:%M}: occurs {occurrences}x, "
                f"classified {got} (expected {expected})"
            )
            if len(mismatches) >= 20:  # enough to diagnose; do not print 500k lines
                break
    return tally, mismatches


@pytest.mark.parametrize("key", DEFAULT_ZONES)
def test_the_classifier_agrees_with_the_oracle_on_every_local_minute(key: str):
    tally, mismatches = _check_zone(key)
    assert mismatches == [], "\n".join(mismatches)
    # A sweep that classified everything "normal" would pass the line above if the
    # zone had no transitions, so assert the interesting cases were actually reached.
    assert tally[IMAGINARY] > 0, f"{key}: no imaginary minutes were exercised"
    assert tally[AMBIGUOUS] > 0, f"{key}: no ambiguous minutes were exercised"
    assert tally[NORMAL] > 500_000, f"{key}: {tally[NORMAL]} normal minutes"


def test_the_gap_and_the_repeated_hour_are_the_expected_widths():
    # Pins the two zones' transition widths, so a tzdata bump that changed them
    # would surface here rather than as a puzzling failure above.
    ams, _ = _check_zone("Europe/Amsterdam")
    assert ams[IMAGINARY] == 60 and ams[AMBIGUOUS] == 60  # one hour each way
    lh, _ = _check_zone("Australia/Lord_Howe")
    assert lh[IMAGINARY] == 30 and lh[AMBIGUOUS] == 30  # thirty minutes each way


@pytest.mark.dst_sweep
@pytest.mark.parametrize("key", SWEEP_ZONES)
def test_the_full_zone_sweep(key: str):
    """The design's scrutiny-pass sweep, on demand: ``pytest -m dst_sweep``."""
    _, mismatches = _check_zone(key)
    assert mismatches == [], "\n".join(mismatches)


# --- Validating the oracle itself ----------------------------------------


def test_the_oracle_catches_a_stubbed_detection_step(monkeypatch):
    """A test that cannot fail proves nothing.

    Stubbing the classifier's first step to ``True`` — "every reading has two
    offsets" — is the plausible break: it makes every normal reading get the
    second-step treatment, which classifies it as ambiguous. The oracle must reject
    that, and it does so on ~520,000 minutes rather than on a hand-picked case.
    """
    zone = ZoneInfo("Europe/Amsterdam")
    naive = datetime(2026, 8, 25, 7, 30)
    assert classify_local(naive, zone) == NORMAL

    def stubbed_first_step(value, zone_):
        # As if `utcoffset() != replace(fold=1).utcoffset()` always held.
        fold0 = value.replace(tzinfo=zone_, fold=0)
        back = fold0.astimezone(timezone.utc).astimezone(zone_)
        return AMBIGUOUS if back.replace(tzinfo=None) == value else IMAGINARY

    # Patched on THIS module's global, which is the name the harness resolves — the
    # same route a regression in the real classifier would arrive by.
    monkeypatch.setattr(
        "tests.test_reminders_oracle.classify_local", stubbed_first_step
    )
    _, mismatches = _check_zone("Europe/Amsterdam")
    assert mismatches, "the oracle failed to notice a stubbed detection step"
    assert "classified ambiguous (expected normal)" in mismatches[0]


def test_the_oracle_catches_a_round_trip_only_detector(monkeypatch):
    """The other plausible break: dropping the offset-comparison step entirely.

    Both folds of an ambiguous reading round-trip cleanly, so a round-trip-only
    detector calls every ambiguous reading normal and misses all 60 of them.
    """
    def round_trip_only(value, zone_):
        fold0 = value.replace(tzinfo=zone_, fold=0)
        back = fold0.astimezone(timezone.utc).astimezone(zone_)
        return NORMAL if back.replace(tzinfo=None) == value else IMAGINARY

    monkeypatch.setattr("tests.test_reminders_oracle.classify_local", round_trip_only)
    _, mismatches = _check_zone("Europe/Amsterdam")
    assert mismatches, "the oracle failed to notice a round-trip-only detector"
    # Both folds of an ambiguous reading round-trip cleanly, so the misses are the
    # 60 ambiguous minutes and nothing else.
    assert all("expected ambiguous" in m for m in mismatches), mismatches
