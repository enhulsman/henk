"""Triageable/announceable pipeline tests (task 2.1/2.2), from specs.

Covers the three quiet-keeping layers (design D6) as pure policy over hand-built
batches, so no real time is involved: arrival-time debounce collapse, per-identity
cooldown with per-pattern overrides, recurrence-window framing, and the daily cap
that gates Signal delivery only. Suppression bookkeeping (cooldown → audit record;
cap-overflow → surfaced count) is asserted here too.
"""

from __future__ import annotations

from henk.events.pipeline import (
    Debouncer,
    EventPipeline,
    PipelineConfig,
)
from henk.events.types import Event


def _event(mid: str, title: str, message: str = "", arrival: float = 0.0) -> Event:
    return Event(id=mid, title=title, message=message, arrival_time=arrival)


HOUR = 3600.0


def _cfg(**kw) -> PipelineConfig:
    base = dict(
        debounce_seconds=120.0,
        cooldown_seconds=6 * HOUR,
        recurrence_window_seconds=24 * HOUR,
        cap_per_24h=3,
        cooldown_overrides=[{"pattern": "swap", "cooldown_seconds": 24 * HOUR}],
    )
    base.update(kw)
    return PipelineConfig(**base)


# --- Debounce (arrival-time) ----------------------------------------------


def test_storm_within_window_is_one_batch():
    deb = Debouncer(window=120.0)
    for i in range(10):
        assert deb.feed(_event(f"e{i}", f"Gatus: svc/{i}", arrival=float(i))) is None
    batch = deb.flush()
    assert batch is not None and len(batch) == 10  # 10 events → one batch


def test_replayed_backlog_collapses_into_one_batch():
    # Reconnect delivers a backlog that all arrives ~instantly (same arrival window).
    deb = Debouncer(window=120.0)
    for i in range(25):
        deb.feed(_event(f"b{i}", f"Gatus: svc/{i}", arrival=0.5))
    assert len(deb.flush()) == 25  # one catch-up batch, not 25 conversations


def test_event_beyond_window_starts_new_batch():
    deb = Debouncer(window=120.0)
    assert deb.feed(_event("a", "Gatus: svc/a", arrival=0.0)) is None
    # Arrives 200s later → closes the first batch, opens a second.
    closed = deb.feed(_event("b", "Gatus: svc/b", arrival=200.0))
    assert closed is not None and [e.id for e in closed] == ["a"]
    assert [e.id for e in deb.flush()] == ["b"]


# --- Triageable end to end -------------------------------------------------


def test_batch_of_distinct_alerts_yields_one_turn_all_items():
    pipe = EventPipeline(_cfg())
    batch = [_event(f"e{i}", f"Gatus: svc/{i}") for i in range(10)]
    decision = pipe.evaluate(batch, now=0.0)
    assert decision.event_turn is not None
    assert len(decision.event_turn.items) == 10
    assert decision.event_turn.announceable is True
    assert decision.suppressions == []


def test_duplicate_identity_in_batch_collapses_to_one_item():
    pipe = EventPipeline(_cfg())
    batch = [
        _event("e1", "Gatus: svc/api", "triggered"),
        _event("e2", "Gatus: svc/api", "triggered"),
    ]
    decision = pipe.evaluate(batch, now=0.0)
    assert len(decision.event_turn.items) == 1


# --- Cooldown --------------------------------------------------------------


def test_refire_inside_cooldown_is_suppressed_with_audit():
    pipe = EventPipeline(_cfg())
    pipe.evaluate([_event("e1", "Gatus: svc/api")], now=0.0)
    decision = pipe.evaluate([_event("e2", "Gatus: svc/api")], now=1 * HOUR)
    assert decision.event_turn is None  # no new conversation
    assert len(decision.suppressions) == 1
    assert decision.suppressions[0].reason == "cooldown"
    assert decision.suppressions[0].identity_key == "gatus:svc/api"


def test_per_pattern_cooldown_override_for_chronic_identity():
    pipe = EventPipeline(_cfg())
    # Swap pressure: 24h override. Normal svc: default 6h.
    pipe.evaluate([_event("s1", "Grafana | HenkSwapPressure | firing")], now=0.0)
    pipe.evaluate([_event("n1", "Gatus: svc/api")], now=0.0)
    at8h = 8 * HOUR
    swap = pipe.evaluate([_event("s2", "Grafana | HenkSwapPressure | firing")], now=at8h)
    normal = pipe.evaluate([_event("n2", "Gatus: svc/api")], now=at8h)
    assert swap.event_turn is None            # still inside the 24h override
    assert normal.event_turn is not None      # past the 6h default


# --- Recurrence ------------------------------------------------------------


def test_refire_past_cooldown_within_recurrence_window_is_framed_recurrence():
    pipe = EventPipeline(_cfg())
    pipe.evaluate([_event("e1", "Gatus: svc/api")], now=0.0)
    decision = pipe.evaluate([_event("e2", "Gatus: svc/api")], now=8 * HOUR)
    assert decision.event_turn is not None
    item = decision.event_turn.items[0]
    assert item.recurrence is True  # survived cooldown but within recurrence window


def test_first_occurrence_is_not_a_recurrence():
    pipe = EventPipeline(_cfg())
    decision = pipe.evaluate([_event("e1", "Gatus: svc/api")], now=0.0)
    assert decision.event_turn.items[0].recurrence is False


# --- Cadence cap (gates Signal only) --------------------------------------


def test_cap_suppresses_signal_but_still_triages():
    pipe = EventPipeline(_cfg(cap_per_24h=2))
    a = pipe.evaluate([_event("a", "Gatus: svc/a")], now=0.0)
    b = pipe.evaluate([_event("b", "Gatus: svc/b")], now=1.0)
    c = pipe.evaluate([_event("c", "Gatus: svc/c")], now=2.0)
    assert a.event_turn.announceable is True
    assert b.event_turn.announceable is True
    assert c.event_turn is not None           # triage STILL runs
    assert c.event_turn.announceable is False  # but Signal is suppressed


def test_suppressed_count_surfaces_on_next_announceable_message():
    pipe = EventPipeline(_cfg(cap_per_24h=2))
    pipe.evaluate([_event("a", "Gatus: svc/a")], now=0.0)   # announce 1
    pipe.evaluate([_event("b", "Gatus: svc/b")], now=1.0)   # announce 2
    pipe.evaluate([_event("c", "Gatus: svc/c")], now=2.0)   # cap-suppressed
    pipe.evaluate([_event("d", "Gatus: svc/d")], now=3.0)   # cap-suppressed
    # After the 24h cap window slides, a new incident is announceable again.
    later = pipe.evaluate([_event("e", "Gatus: svc/e")], now=25 * HOUR)
    assert later.event_turn.announceable is True
    assert later.event_turn.suppressed_count == 2  # the two cap-suppressed ones
