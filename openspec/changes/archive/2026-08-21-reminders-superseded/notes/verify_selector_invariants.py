"""Executable check of §2.8's four properties against a model built strictly from the plan.

Not the implementation — the implementation does not exist. This models the state machine
*as specified* (§2.1 batching, §2.2 transactions/counters, §2.5 selector and exits, §2.6
transitions) and asserts termination, detectability, quiescence and conservation with faults
injected at every stage boundary.

The model is DISPOSABLE. The transferable artifact is the fault-injection matrix: pre-work
commit, grace transition, composition, each send, each post-send commit, plus a tri-valued
channel double. Change C retargets the matrix at the real store and scheduler.

KNOWN NON-COVERAGE (stated so a green run is not read as broader than it is):
  - no framing-overhead model, so N-a's largest-framing rule and the config-load validation
    are not exercised;
  - lengths are characters, not bytes — A ships a byte-measured split_message (D2);
  - compose() produces messages that fit by construction, so B1's single-chunk atomicity is
    ASSUMED, not verified: this model cannot detect its violation;
  - the audit log is modelled as a counter, not as records.
"""
from dataclasses import dataclass

MAX_ATTEMPTS = 3
SCHEDULE = [30, 60, 120, 300, 900, 3600, 14400]  # L4: final element repeats
LATE_GRACE = 300          # N3: short enough that the grace path is actually reachable
SAFE_LEN = 2000
TICK = 30

DELIVERED, PARTIAL, FAILED = "delivered", "partial", "failed"   # N4: C1's tri-valued outcome


@dataclass
class Row:
    id: int
    due_at: float
    text: str = "x" * 40
    status: str = "pending"
    send_attempts: int = 0
    unconfirmed_sends: int = 0
    next_attempt_at: float = None
    terminal_at: float = None
    reported_at: float = None
    report_failed: bool = False
    delivered_at: float = None

    def __post_init__(self):
        if self.next_attempt_at is None:
            self.next_attempt_at = self.due_at      # H3


def selected(rows, now):
    """§2.5's selector predicate, verbatim."""
    out = []
    for r in rows:
        if r.next_attempt_at is None or r.next_attempt_at > now:
            continue
        if r.status == "pending":
            out.append(r)
        elif r.status in ("missed", "abandoned") and r.reported_at is None:
            out.append(r)
    return out


def backoff(n):
    return SCHEDULE[min(n, len(SCHEDULE) - 1)]      # L4's clamp


def compose(rows):
    """§2.1 greedy measure-before-add."""
    msgs, cur, size = [], [], 0
    for r in rows:
        line = len(r.text) + 40
        if cur and size + line > SAFE_LEN:
            msgs.append(cur); cur, size = [], 0
        cur.append(r); size += line
    if cur:
        msgs.append(cur)
    return msgs


class Crash(Exception):
    pass


class Stats:
    def __init__(self):
        self.error_logs = 0
        self.grace_transitions = 0
        self.report_abandoned = 0


def tick(rows, now, *, channel, crash_at=None, stats=None):
    sel = selected(rows, now)
    if not sel:
        return
    # ---------- pre-work transaction: grace transitions, increments, crash maximum ----------
    staged = [r for r in sel if r.status == "pending" and now > r.due_at + LATE_GRACE]
    if crash_at == "pre-work":
        raise Crash()                                # M2: nothing committed, nothing bounded
    if staged and crash_at == "grace":               # N3: new stage boundary
        raise Crash()
    for r in staged:
        r.status, r.terminal_at = "missed", now
        r.send_attempts = r.unconfirmed_sends = 0
        r.next_attempt_at = now
        if stats:
            stats.grace_transitions += 1
    hit_max = []
    for r in sel:
        r.send_attempts += 1                         # K1: before composition
        if r.send_attempts > MAX_ATTEMPTS:
            hit_max.append(r)
    # N1/M1: the maximum is EVALUATED HERE, beside the increment — a crash is what prevents
    # the post-send transaction, so a maximum evaluated there is never evaluated on the path
    # it exists to bound.
    for r in hit_max:
        if r.status == "pending":
            r.status, r.terminal_at = "abandoned", now
            r.send_attempts = r.unconfirmed_sends = 0
            r.next_attempt_at = now
        else:
            r.reported_at, r.report_failed = now, True
            if stats:
                stats.report_abandoned += 1
                stats.error_logs += 1
    ids = {id(r) for r in hit_max}                   # N6: identity, not field equality
    sel = [r for r in sel if id(r) not in ids]
    if not sel:
        return

    if crash_at == "compose":
        raise Crash()
    msgs = compose(sel)

    for i, msg in enumerate(msgs):
        if crash_at == f"send{i}":
            raise Crash()
        outcome = channel(msg)
        if crash_at == f"post{i}":
            raise Crash()
        # ---------- post-send transaction: all writes together ----------
        for r in msg:
            r.send_attempts = 0                      # cleared on any return
            if outcome == DELIVERED:
                if r.status == "pending":
                    r.status = "delivered" if now <= r.due_at + LATE_GRACE else "delivered-late"
                    r.delivered_at = now
                else:
                    r.reported_at = now              # K2: written exit (success)
            else:
                # N4: partial is handled as failed for the WHOLE batch (§2.1, E9)
                r.unconfirmed_sends += 1
                r.next_attempt_at = now + backoff(r.unconfirmed_sends)
        # L3: a failed message does not abort the tick


def run(rows, *, channel, crash_every=None, ticks=400, stats=None):
    now = 0.0
    for _ in range(ticks):
        now += TICK
        try:
            tick(rows, now, channel=channel, crash_at=crash_every, stats=stats)
        except Crash:
            if stats:
                stats.error_logs += 1                # M13: caught, logged, loop survives
    return now


# ------------------------------------------------------------------ doubles

def always(outcome, acked=None):
    """Channel double. N2: the acked set is recorded HERE, outside the code under test."""
    def send(msg):
        if outcome == DELIVERED and acked is not None:
            acked.extend(r.id for r in msg)
        return outcome
    return send


def alternating(acked):
    n = {"i": 0}

    def send(msg):
        n["i"] += 1
        out = DELIVERED if n["i"] % 2 else FAILED
        if out == DELIVERED:
            acked.extend(r.id for r in msg)
        return out
    return send


# ------------------------------------------------------------------ properties

#: N derived (L6/N6): a row needs at most MAX_ATTEMPTS+1 ticks to exhaust the delivery crash
#: budget, then at most MAX_ATTEMPTS+1 more to exhaust the report budget, plus slack.
N_TERMINATION = 2 * (MAX_ATTEMPTS + 1) + 2


def prop_termination():
    """Crash faults AT OR AFTER the pre-work commit terminate within N ticks."""
    out = {}
    # "grace" is deliberately absent: the grace transition happens INSIDE the pre-work
    # transaction, so a crash there is M2's pre-commit region (see prop_detectability),
    # not a termination case. Filing it here asserted the wrong property.
    for stage in ("compose", "send0", "post0"):
        rows = [Row(i, due_at=-10 * LATE_GRACE) for i in range(60)]   # N3: grace reachable
        now = run(rows, channel=always(DELIVERED), crash_every=stage, ticks=N_TERMINATION)
        left = selected(rows, now + 10 ** 7)
        out[stage] = "terminates" if not left else f"{len(left)} left, attempts={left[0].send_attempts}"
    return out


def prop_detectability():
    """M2/N5: a fault anywhere before the pre-work COMMIT is unbounded but LOUD.

    Both pre-commit stages are checked — the selector/arithmetic region and the grace
    transition — because both are inside the transaction whose commit they prevent.
    """
    out = {}
    for stage in ("pre-work", "grace"):
        rows = [Row(i, due_at=-10 * LATE_GRACE) for i in range(5)]
        st, acked = Stats(), []
        run(rows, channel=always(DELIVERED, acked), crash_every=stage, ticks=50, stats=st)
        out[stage] = {"logs_per_tick": st.error_logs / 50, "owner_visible": len(acked),
                      "still_selected": bool(selected(rows, 10 ** 9))}
    return out


def prop_quiescence():
    """K3: terminal-unreported rows stay selected, decay to the tail, send nothing."""
    rows = [Row(0, due_at=0)]
    rows[0].status, rows[0].terminal_at = "missed", 0.0
    sends, acked = [], []

    def send(msg):
        sends.append(msg)
        return FAILED
    run(rows, channel=send, ticks=3000)
    r = rows[0]
    return {"still_selected": bool(selected(rows, 10 ** 9)),
            "interval_at_tail": backoff(r.unconfirmed_sends) == SCHEDULE[-1],
            "index_clamped": r.unconfirmed_sends > len(SCHEDULE),
            "owner_visible": len(acked), "attempts": len(sends)}


def prop_conservation(bug=None):
    """L2/N2: success-terminal set == acknowledged set, truth taken from the double."""
    rows = [Row(i, due_at=-10 * LATE_GRACE) for i in range(40)]
    acked, st = [], Stats()
    ch = alternating(acked)
    if bug == "B":                     # outcome variable lies; log and write share the lie
        ch = lambda msg: DELIVERED     # noqa: E731  (the double never records)
    run(rows, channel=ch, ticks=300, stats=st)
    success = {r.id for r in rows
               if r.status in ("delivered", "delivered-late")
               or (r.reported_at is not None and not r.report_failed)}
    unmarked = [r.id for r in rows if r.status in ("missed", "abandoned")
                and r.reported_at is None and r.send_attempts > MAX_ATTEMPTS]
    return {"success_equals_acked": success == set(acked),
            "unmarked_failures": unmarked,
            "max_never_exceeded": all(r.send_attempts <= MAX_ATTEMPTS for r in rows),
            "grace_transitions": st.grace_transitions}


def prop_partial_leaves_pending():
    """N4/E9: a partial is handled as failed for the whole batch."""
    rows = [Row(i, due_at=0) for i in range(30)]
    run(rows, channel=always(PARTIAL), ticks=1)
    return {"none_delivered": all(r.status == "pending" for r in rows),
            "all_backed_off": all(r.unconfirmed_sends == 1 for r in rows)}


if __name__ == "__main__":
    print("N_TERMINATION (derived) =", N_TERMINATION)
    print("TERMINATION   ", prop_termination())
    print("DETECTABILITY ", prop_detectability())
    print("QUIESCENCE    ", prop_quiescence())
    print("CONSERVATION  ", prop_conservation())
    print("  oracle check, Bug B (outcome lies):", prop_conservation(bug="B"))
    print("PARTIAL       ", prop_partial_leaves_pending())
