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


def test_two_host_outages_in_24h_fit_under_the_shipped_cap():
    """Why `cap_per_24h` is 5 rather than 3 (sensor-routing-coverage task 4.3c).

    Measured 2026-08-07: one host outage produces **two** announceable conversations,
    not one. Stopping pi2's DNS and node_exporter together, the Gatus tier-1 alert
    arrived at T+36s and `HenkInstanceDown` at T+190s — a **153s** gap against the
    120s debounce, so the two never batch together.

    At the old cap of 3, a *second* outage inside the same 24h would have its second
    conversation silently gated, losing the `HenkInstanceDown` half of the report —
    the more diagnostic half. A cap of 5 leaves headroom for two full outages plus
    one unrelated incident. Widening the debounce past 153s was rejected: it would
    delay every triage to tidy a case that is genuinely two observations apart.
    """
    pipe = EventPipeline(_cfg(cap_per_24h=5))
    turns = [
        # outage 1: gatus alert, then instance-down ~153s later
        pipe.evaluate([_event("g1", "Gatus: Core Infrastructure/Pi2 DNS")], now=0.0),
        pipe.evaluate([_event("i1", "[FIRING:1] HenkInstanceDown a")], now=153.0),
        # outage 2, hours later, same shape on a different host
        pipe.evaluate([_event("g2", "Gatus: Core Infrastructure/Pi5 DNS")], now=6 * HOUR),
        pipe.evaluate([_event("i2", "[FIRING:1] HenkInstanceDown b")], now=6 * HOUR + 153),
    ]
    assert all(t.event_turn is not None for t in turns)
    assert all(t.event_turn.announceable for t in turns), (
        "both halves of both outages must reach the owner"
    )
    # The 5th slot is still free for something unrelated.
    spare = pipe.evaluate([_event("x", "Gatus: svc/other")], now=7 * HOUR)
    assert spare.event_turn.announceable is True
    # The 6th is correctly gated — the cap is still a cap.
    sixth = pipe.evaluate([_event("y", "Gatus: svc/another")], now=8 * HOUR)
    assert sixth.event_turn is not None
    assert sixth.event_turn.announceable is False


# --- Rehydration from the persisted audit log (design D2, D4) --------------
# A restart must not re-arm cooldowns, reset the daily cap, or lose recurrence
# refs. State is reconstructed from durable audit records, compared against a
# wall-clock `now` (monotonic would reset to an arbitrary origin on restart).

EPOCH = 1_700_000_000.0  # realistic wall-clock base, to prove monotonic-independence


def _triage_rec(identity: str, at: float, *, announceable=True, handoff=None) -> dict:
    return {
        "record_type": "session", "trigger": "event",
        "event": [{"identity_key": identity}],
        "announceable": announceable, "handoff_message_id": handoff, "at": at,
    }


def _supp_rec(identity: str, at: float) -> dict:
    return {"record_type": "suppression", "identity_key": identity,
            "reason": "cooldown", "at": at}


def test_rehydrated_cooldown_holds_across_restart():
    pipe = EventPipeline(_cfg())
    # Triaged 1h before the (post-restart) now — still inside the 6h cooldown.
    pipe.rehydrate([_triage_rec("gatus:svc/api", EPOCH)], now=EPOCH + 1 * HOUR)
    decision = pipe.evaluate([_event("e2", "Gatus: svc/api")], now=EPOCH + 1 * HOUR)
    assert decision.event_turn is None                 # cooldown survived the restart
    assert decision.suppressions[0].reason == "cooldown"


def test_rehydration_ignores_suppression_records_for_cooldown():
    # Cooldown is armed by an actual triage, never by a prior suppression
    # (a suppressed re-fire does not extend the cooldown in the live pipeline).
    pipe = EventPipeline(_cfg())
    pipe.rehydrate([_supp_rec("gatus:svc/api", EPOCH)], now=EPOCH + 1 * HOUR)
    decision = pipe.evaluate([_event("e2", "Gatus: svc/api")], now=EPOCH + 1 * HOUR)
    assert decision.event_turn is not None             # not cooled down → triaged


def test_rehydrated_cap_holds_across_restart():
    pipe = EventPipeline(_cfg(cap_per_24h=2))
    now = EPOCH + 1 * HOUR
    pipe.rehydrate(
        [_triage_rec("gatus:a", EPOCH), _triage_rec("gatus:b", EPOCH + 60)],
        now=now,
    )
    # Cap already reached (2/2) within the window → new incident triaged, not announced.
    decision = pipe.evaluate([_event("c", "Gatus: svc/c")], now=now)
    assert decision.event_turn is not None
    assert decision.event_turn.announceable is False   # cap held across restart


def test_rehydrated_recurrence_references_prior_handoff():
    pipe = EventPipeline(_cfg())
    # Triaged 8h ago: past the 6h cooldown, inside the 24h recurrence window.
    pipe.rehydrate(
        [_triage_rec("gatus:svc/api", EPOCH, handoff="hf-old")],
        now=EPOCH + 8 * HOUR,
    )
    decision = pipe.evaluate([_event("e2", "Gatus: svc/api")], now=EPOCH + 8 * HOUR)
    item = decision.event_turn.items[0]
    assert item.recurrence is True
    assert item.prior_handoff_ref == "hf-old"          # recurrence framing survived


def test_rehydrated_cap_suppressed_count_surfaces_after_restart():
    pipe = EventPipeline(_cfg(cap_per_24h=3))
    now = EPOCH + 1 * HOUR
    # 1 announced + 2 cap-suppressed since, all within the window, before the restart.
    pipe.rehydrate(
        [
            _triage_rec("gatus:a", EPOCH, announceable=True),
            _triage_rec("gatus:b", EPOCH + 10, announceable=False),
            _triage_rec("gatus:c", EPOCH + 20, announceable=False),
        ],
        now=now,
    )
    decision = pipe.evaluate([_event("d", "Gatus: svc/d")], now=now)
    assert decision.event_turn.announceable is True     # 1/3 used → room to announce
    assert decision.event_turn.suppressed_count == 2    # the 2 pre-restart cap drops


def test_rehydrated_cooldown_holds_when_override_exceeds_recurrence_window():
    # A per-pattern cooldown override (48h) longer than the recurrence window (24h):
    # a triage 30h ago is past recurrence but still inside the override cooldown, so
    # it must survive a restart and suppress the re-fire. Requires rehydration to
    # (a) select records up to the widest window INCLUDING override cooldowns, and
    # (b) arm cooldown regardless of the recurrence window.
    cfg = _cfg(
        recurrence_window_seconds=24 * HOUR,
        cooldown_overrides=[{"pattern": "svc/api", "cooldown_seconds": 48 * HOUR}],
    )
    pipe = EventPipeline(cfg)
    pipe.rehydrate([_triage_rec("gatus:svc/api", EPOCH)], now=EPOCH + 30 * HOUR)
    decision = pipe.evaluate([_event("e2", "Gatus: svc/api")], now=EPOCH + 30 * HOUR)
    assert decision.event_turn is None                       # 48h cooldown held
    assert decision.suppressions[0].reason == "cooldown"


def test_rehydration_beyond_windows_is_ignored():
    pipe = EventPipeline(_cfg())
    # Triaged 30h ago: outside both cooldown and the 24h recurrence window.
    pipe.rehydrate([_triage_rec("gatus:svc/api", EPOCH)], now=EPOCH + 30 * HOUR)
    decision = pipe.evaluate([_event("e2", "Gatus: svc/api")], now=EPOCH + 30 * HOUR)
    assert decision.event_turn is not None
    assert decision.event_turn.items[0].recurrence is False  # stale ref not resurrected
