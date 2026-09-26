"""Bounded owner acknowledgement (owner-acknowledgement task 4.1).

From specs/channel-adapter, requirement *Owner-only acknowledgement of inbound
messages*. Every test that can drives a real ``SignalAdapter`` over a
``FakeBridge`` scripted to refuse or hang (standing rule 1); only the cadence
tests swap the clock, and even they keep the real adapter.

Two kinds of time are in play and they are kept apart on purpose:

- the **acknowledge bound** is real time, because the module enforces it with
  ``asyncio.timeout``. Tests that only need a bound to exist use a small real
  timeout (``T``); tests that measure one use ``BOUND`` and hold the elapsed
  time to ``[LOWER, UPPER) x BOUND``, so a bound multiplied by 2 or more fails.
  The structural test pins the exact value handed to ``asyncio.timeout``.
- the **poll and refresh cadence** runs on ``FakeTime``, injected as both
  ``sleep`` and ``clock``, so a 50-second turn takes no real time and "the
  interval has elapsed" is exact.

Every await on a hanging bridge is wrapped in ``asyncio.wait_for(…, 2.0)``
(standing rule 7), and the autouse fixture fails any test that leaves a task
running.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

import pytest

from henk.channel.acknowledge import PAUSE_POLL_SECONDS, OwnerAcknowledgement
from henk.channel.signal import TYPING_REFRESH_SECONDS, SignalAdapter, SignalBridgeError
from tests.conftest import FakeBridge

OWNER = "+31600000000"
ACCOUNT = "+31611111111"
REF = "1700000000123"
LOGGER = "henk.channel.acknowledge"

#: Small real acknowledge timeout for tests that only need a bound to exist.
T = 0.05
#: The bound for tests that measure it, large enough that scheduling noise is small
#: against it. An elapsed time must fall in [LOWER, UPPER) x BOUND: under UPPER, so
#: a bound applied 2x (or 3x, 5x) fails; at least LOWER where the test waits the
#: bound out, so a hang that was never waited on fails too.
BOUND = 0.2
LOWER, UPPER = 0.9, 1.5
#: Fail-fast bound for anything that could hang (standing rule 7).
FAIL_FAST = 2.0
#: Signal clients expire a typing indicator about this long after the last start.
CLIENT_EXPIRY_SECONDS = 15.0

LOOP_LINE = "further start/refresh failures this turn are not logged"
CLOSE_LINE = "working indicator close"


async def _nosleep(_delay: float) -> None:
    return None


def _adapter(bridge: FakeBridge) -> SignalAdapter:
    return SignalAdapter(bridge, account=ACCOUNT, owner=OWNER, sleep=_nosleep)


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER]


def _lines(caplog, marker: str) -> list[str]:
    return [r.getMessage() for r in _records(caplog) if marker in r.getMessage()]


def _assert_one_bound(elapsed: float, bound: float = BOUND) -> None:
    """``elapsed`` is one ``bound``: waited out, and not a multiple of it."""
    assert elapsed < UPPER * bound, f"{elapsed:.3f}s is more than one {bound}s bound"
    assert elapsed >= LOWER * bound, f"{elapsed:.3f}s: the bound was not waited out"


class _AsyncioSpy:
    """``asyncio`` as ``henk.channel.acknowledge`` sees it, with two calls recorded.

    Installed as that module's ``asyncio`` global (``ack_asyncio`` below), so only
    the module's own calls are seen, never the test's or the event loop's.
    ``timeouts`` is every delay handed to ``asyncio.timeout``; ``tasks`` is every
    task created, with its coroutine's name taken at creation.
    """

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []
        self.tasks: list[tuple[str, asyncio.Task]] = []

    def __getattr__(self, name: str):
        return getattr(asyncio, name)

    def timeout(self, delay):
        self.timeouts.append(delay)
        return asyncio.timeout(delay)

    def create_task(self, coro, **kwargs) -> asyncio.Task:
        task = asyncio.create_task(coro, **kwargs)
        self.tasks.append((coro.__qualname__, task))
        return task

    def indicator_tasks(self) -> list[asyncio.Task]:
        return [task for name, task in self.tasks if name.endswith("._indicate")]


@pytest.fixture
def ack_asyncio(monkeypatch) -> _AsyncioSpy:
    """Spy on ``asyncio`` inside the acknowledgement module; restored after the test."""
    spy = _AsyncioSpy()
    monkeypatch.setattr(sys.modules[OwnerAcknowledgement.__module__], "asyncio", spy)
    return spy


async def _until(condition, what: str) -> None:
    """Yield to the loop until ``condition()`` holds, failing fast otherwise."""

    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.001)

    try:
        await asyncio.wait_for(poll(), FAIL_FAST)
    except TimeoutError:
        pytest.fail(f"timed out waiting for {what}")


@pytest.fixture(autouse=True)
async def no_leftover_tasks():
    """No task may outlive a scenario (task 4.1, 'no leftovers')."""
    yield
    await asyncio.sleep(0)
    leftovers = asyncio.all_tasks() - {asyncio.current_task()}
    for task in leftovers:  # clean up, so one failure does not cascade
        task.cancel()
    if leftovers:
        await asyncio.wait(leftovers, timeout=FAIL_FAST)
    assert not leftovers, f"tasks left running: {leftovers}"


class FakeTime:
    """A deterministic clock plus a ``sleep`` that only the test can end.

    ``clock()`` returns ``now``. ``sleep(delay)`` registers a sleeper due at
    ``now + delay`` and blocks until ``step()`` releases it; nothing else ever
    advances ``now`` (except a bridge that models a hang, see ``ClockedBridge``).
    ``step()`` waits (in real time, bounded) for the next sleeper, moves ``now``
    to its deadline and releases it, so each step is exactly one wake-up of the
    indicator loop. ``sleeping()`` returns once the loop has handled that wake-up
    and gone back to sleep, which is the moment a test may change what the next
    wake-up will see (the pause flag, a fault, the end of the body).
    ``requests`` keeps ``(now, delay)`` for every sleep asked for, which is how
    a test sees the poll the loop chose.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.requests: list[tuple[float, float]] = []
        self.released = 0
        self._sleepers: list[tuple[float, asyncio.Future]] = []
        self._registered = asyncio.Event()

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.requests.append((self.now, delay))
        entry = (self.now + delay, asyncio.get_running_loop().create_future())
        self._sleepers.append(entry)
        self._registered.set()
        try:
            await entry[1]
        finally:
            if entry in self._sleepers:
                self._sleepers.remove(entry)
            if not self._sleepers:
                self._registered.clear()

    async def sleeping(self) -> None:
        """Return once a sleeper is registered, i.e. the loop is between wake-ups.

        Fails the test, rather than raising ``TimeoutError``, when none is: the
        indicator loop is not running (its task died, or never went back to sleep).
        """
        try:
            await asyncio.wait_for(self._registered.wait(), FAIL_FAST)
        except TimeoutError:
            pytest.fail("the indicator loop never went back to sleep (task dead?)")

    async def step(self) -> None:
        await self.sleeping()
        entry = min(self._sleepers, key=lambda e: e[0])
        self._sleepers.remove(entry)
        if not self._sleepers:
            self._registered.clear()
        self.now = max(self.now, entry[0])
        self.released += 1
        entry[1].set_result(None)

    async def run_until(self, until: float, *, max_steps: int = 1000) -> None:
        for _ in range(max_steps):
            if self.now >= until:
                return
            await self.step()
        pytest.fail(f"fake clock stuck at {self.now} short of {until} (busy loop?)")


class ClockedBridge(FakeBridge):
    """A ``FakeBridge`` that stamps every typing request with the fake ISSUE time.

    ``hang_start`` maps the index of a start (0 is the entry start) to fake
    seconds: that start advances the fake clock by that much, modelling the time
    a hung request consumes, then blocks until the real acknowledge bound cancels
    it.
    """

    def __init__(self, fake: FakeTime) -> None:
        super().__init__()
        self.fake = fake
        #: (operation, fake issue time, len(fake.requests) at issue).
        self.issued: list[tuple[str, float, int]] = []
        self.hang_start: dict[int, float] = {}

    def issue_times(self, op: str) -> list[float]:
        return [at for kind, at, _ in self.issued if kind == op]

    def sleeps_between_starts(self, first: int, second: int) -> list[float]:
        """The sleep delays the loop asked for between two starts' issue."""
        marks = [mark for kind, _, mark in self.issued if kind == "start"]
        return [delay for _, delay in self.fake.requests[marks[first] : marks[second]]]

    async def start_typing(self, recipient: str) -> None:
        index = len(self.issue_times("start"))
        self.issued.append(("start", self.fake.now, len(self.fake.requests)))
        if index in self.hang_start:
            self.fake.now += self.hang_start[index]
            await asyncio.Event().wait()
        await super().start_typing(recipient)

    async def stop_typing(self, recipient: str) -> None:
        self.issued.append(("stop", self.fake.now, len(self.fake.requests)))
        await super().stop_typing(recipient)


def _timed(bridge: FakeBridge, fake: FakeTime, **kwargs) -> OwnerAcknowledgement:
    kwargs.setdefault("timeout", T)
    kwargs.setdefault("refresh_seconds", TYPING_REFRESH_SECONDS)
    return OwnerAcknowledgement(
        _adapter(bridge), sleep=fake.sleep, clock=fake.clock, **kwargs
    )


def _real(bridge: FakeBridge, **kwargs) -> OwnerAcknowledgement:
    kwargs.setdefault("timeout", T)
    kwargs.setdefault("refresh_seconds", TYPING_REFRESH_SECONDS)
    return OwnerAcknowledgement(_adapter(bridge), **kwargs)


# --- Receipts --------------------------------------------------------------


async def test_a_healthy_receipt_reaches_the_owner_and_logs_nothing(caplog):
    bridge = FakeBridge()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(_real(bridge).receipt(REF), FAIL_FAST)
    assert bridge.receipts == [(OWNER, int(REF))]
    assert _records(caplog) == []


async def test_a_hung_receipt_returns_within_the_bound_and_logs_one_line(caplog):
    # Scenario: An acknowledgement failure never fails or delays the turn beyond
    # the bound — a receipt that never answers.
    bridge = FakeBridge()
    bridge.ack_faults["receipt"] = asyncio.Event()  # never set
    ack = _real(bridge, timeout=BOUND)
    started = time.monotonic()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = await asyncio.wait_for(ack.receipt(REF), FAIL_FAST)
    elapsed = time.monotonic() - started
    assert result is None
    _assert_one_bound(elapsed)
    assert bridge.ack_attempts == [("receipt", OWNER)]
    assert bridge.receipts == []
    assert len(_records(caplog)) == 1


@pytest.mark.parametrize(
    "fault",
    [SignalBridgeError("refused"), RuntimeError("bridge bug")],
    ids=["refused", "raising"],
)
async def test_a_failing_receipt_logs_one_line_and_does_not_raise(fault, caplog):
    # A refusal comes back from the adapter as `False`; any other exception the
    # adapter lets through. Neither reaches the caller.
    bridge = FakeBridge()
    bridge.ack_faults["receipt"] = fault
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(_real(bridge).receipt(REF), FAIL_FAST)
    assert bridge.ack_attempts == [("receipt", OWNER)]
    assert len(_records(caplog)) == 1


async def test_a_message_without_a_channel_reference_is_not_acknowledged(caplog):
    # Scenario: A message without a channel reference is not acknowledged and
    # logs nothing.
    bridge = FakeBridge()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(_real(bridge).receipt(None), FAIL_FAST)
    assert bridge.ack_attempts == []
    assert _records(caplog) == []


async def test_an_outer_cancel_during_a_hung_receipt_propagates(caplog):
    # Shutdown must leave the receive loop, not be absorbed by the bound.
    bridge = FakeBridge()
    bridge.ack_faults["receipt"] = asyncio.Event()
    ack = _real(bridge, timeout=1.0)  # the cancel lands well inside the bound
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        task = asyncio.create_task(ack.receipt(REF))
        await _until(lambda: bridge.ack_attempts, "the receipt to be in flight")
        task.cancel()
        await asyncio.wait_for(asyncio.wait({task}), FAIL_FAST)
    assert task.cancelled(), "the cancellation was swallowed"
    assert _records(caplog) == []


# --- Entry and cadence -----------------------------------------------------


async def test_entering_issues_the_start_before_the_body_runs():
    bridge = FakeBridge()
    seen_at_body_start = None

    async def turn():
        nonlocal seen_at_body_start
        async with _real(bridge).working():
            seen_at_body_start = list(bridge.ack_attempts)

    await asyncio.wait_for(turn(), FAIL_FAST)
    assert seen_at_body_start == [("start", OWNER)]
    assert bridge.typing == [("start", OWNER), ("stop", OWNER)]


async def test_a_hanging_start_does_not_hold_the_body(caplog):
    # Scenario: An acknowledgement failure never ... delays the turn — the start
    # is issued but its response is not awaited by the body.
    bridge = FakeBridge()
    bridge.ack_faults["start"] = asyncio.Event()  # never set
    seen = None
    body_began = None

    async def turn():
        nonlocal seen, body_began
        entered = time.monotonic()
        async with _real(bridge, timeout=0.3).working():
            body_began = time.monotonic() - entered
            seen = (list(bridge.ack_attempts), list(bridge.typing))
            # Outlive part of the start's bound, so that bound (not the close's,
            # which begins later) is the one that cuts the start off.
            await asyncio.sleep(0.1)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(turn(), FAIL_FAST)
    assert seen == ([("start", OWNER)], []), "the body waited for the start"
    assert body_began < 0.1, body_began
    # The hung start ran out its own bound inside the close, then the stop went.
    assert bridge.typing == [("stop", OWNER)]
    assert len(_lines(caplog, LOOP_LINE)) == 1


def test_one_lost_refresh_stays_under_the_client_expiry():
    # Design D4 arithmetic: the gap after one lost refresh is 2 x interval.
    assert TYPING_REFRESH_SECONDS * 2 < CLIENT_EXPIRY_SECONDS


async def test_a_long_body_refreshes_at_every_interval_and_stops_once():
    # Scenario: The indicator stays up through a long turn.
    fake = FakeTime()
    bridge = ClockedBridge(fake)

    async def turn():
        async with _timed(bridge, fake).working():
            await fake.run_until(50.0)
            await fake.sleeping()

    await asyncio.wait_for(turn(), FAIL_FAST)
    starts = bridge.issue_times("start")
    assert starts == [0.0, 7.0, 14.0, 21.0, 28.0, 35.0, 42.0, 49.0]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(gap == TYPING_REFRESH_SECONDS for gap in gaps), gaps
    assert max(gaps) < CLIENT_EXPIRY_SECONDS
    # Exactly one stop, after the body, and last.
    assert [op for op, _ in bridge.typing].count("stop") == 1
    assert bridge.typing[-1] == ("stop", OWNER)
    # Every wake-up is at most one poll away.
    assert all(0 <= delay <= PAUSE_POLL_SECONDS for _, delay in fake.requests)


async def test_one_hung_refresh_does_not_let_the_indicator_expire(caplog):
    # Scenario: One hung refresh does not let the indicator expire. The refresh
    # issued at t=7 hangs for T (= the interval) until the bound cuts it off, at
    # t=14. The overdue start must go out at once: 14 s after the last successful
    # start at t=0, not a poll later (t=15) and not an interval after the hung
    # one returned (t=21).
    fake = FakeTime()
    bridge = ClockedBridge(fake)
    bridge.hang_start[1] = TYPING_REFRESH_SECONDS

    async def turn():
        async with _timed(bridge, fake).working():
            await fake.run_until(20.0)
            await fake.sleeping()

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(turn(), FAIL_FAST)
    starts = bridge.issue_times("start")
    assert starts[:3] == [0.0, 7.0, 14.0], starts
    last_successful = 0.0
    assert starts[2] - last_successful <= 2 * TYPING_REFRESH_SECONDS
    # No extra poll between the hung refresh returning and the overdue start.
    between = bridge.sleeps_between_starts(1, 2)
    assert between == [0.0], between
    assert len(_lines(caplog, LOOP_LINE)) == 1


async def test_the_indicator_is_suspended_while_paused_and_resumes_at_the_next_poll():
    # Scenario: The indicator is suspended during an approval wait.
    fake = FakeTime()
    bridge = ClockedBridge(fake)
    state = {"paused": False}

    async def turn():
        async with _timed(bridge, fake, paused=lambda: state["paused"]).working():
            await fake.run_until(3.0)
            await fake.sleeping()
            state["paused"] = True  # the approval prompt is now pending
            await fake.run_until(12.0)
            await fake.sleeping()
            state["paused"] = False  # the owner answered
            await fake.run_until(20.0)
            await fake.sleeping()

    await asyncio.wait_for(turn(), FAIL_FAST)
    starts = bridge.issue_times("start")
    stops = bridge.issue_times("stop")
    # One stop within a poll of the pause, and no start while it lasts, although
    # the refresh would have been due at t=7.
    assert stops[0] == 4.0 and stops[0] - 3.0 <= PAUSE_POLL_SECONDS
    assert [s for s in starts if 3.0 <= s <= 12.0] == []
    # The resume start at the next poll, well before an interval has elapsed, and
    # the refresh cadence restarts from it.
    assert starts == [0.0, 13.0, 20.0]
    assert 13.0 - 12.0 <= PAUSE_POLL_SECONDS < TYPING_REFRESH_SECONDS
    # The pause stop plus the final stop, nothing else.
    assert stops == [4.0, 20.0]
    # While paused the loop polls; it never spins on an overdue refresh.
    assert all(
        delay == PAUSE_POLL_SECONDS for at, delay in fake.requests if 4.0 <= at < 12.0
    )


# --- Close -----------------------------------------------------------------


async def test_a_healthy_close_wakes_on_exit_not_on_the_poll(caplog):
    # Scenario: A healthy close does not wait out a poll. The fake sleep is never
    # released, so a loop that only woke on its poll would never reach the stop.
    fake = FakeTime()
    bridge = ClockedBridge(fake)
    closed_in = None

    async def turn():
        nonlocal closed_in
        async with _timed(bridge, fake).working():
            await fake.sleeping()
            closing = time.monotonic()
        closed_in = time.monotonic() - closing

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(turn(), FAIL_FAST)
    assert fake.released == 0
    assert bridge.typing == [("start", OWNER), ("stop", OWNER)]
    assert closed_in < T, closed_in
    assert _records(caplog) == []


async def test_a_refresh_in_flight_at_exit_completes_before_the_stop(monkeypatch):
    # Scenario: A start in flight cannot land after the stop.
    fake = FakeTime()
    bridge = ClockedBridge(fake)
    release = asyncio.Event()
    ack = _timed(bridge, fake, timeout=1.0)
    callers: list[tuple[str, asyncio.Task | None]] = []
    adapter = ack._adapter
    real_start, real_stop = adapter.start_working, adapter.stop_working

    async def start_working():
        callers.append(("start", asyncio.current_task()))
        return await real_start()

    async def stop_working():
        callers.append(("stop", asyncio.current_task()))
        return await real_stop()

    monkeypatch.setattr(adapter, "start_working", start_working)
    monkeypatch.setattr(adapter, "stop_working", stop_working)
    closed_in = None

    async def turn():
        nonlocal closed_in
        async with ack.working():
            await fake.run_until(6.0)
            await fake.sleeping()
            bridge.ack_faults["start"] = release  # the next refresh blocks
            await fake.step()  # t=7: the refresh is issued
            await _until(
                lambda: len(bridge.ack_attempts) == 2, "the refresh to be in flight"
            )
            asyncio.get_running_loop().call_later(0.02, release.set)
            closing = time.monotonic()
        closed_in = time.monotonic() - closing

    owner = asyncio.current_task()
    await asyncio.wait_for(turn(), FAIL_FAST)
    assert bridge.typing == [("start", OWNER), ("start", OWNER), ("stop", OWNER)]
    assert closed_in < 1.0, "the refresh and the stop both fit in one bound"
    # One task issued every typing request, and it was not the turn's own.
    tasks = {task for _, task in callers}
    assert len(tasks) == 1 and owner not in tasks, callers
    assert [op for op, _ in callers] == ["start", "start", "stop"]


async def test_a_hung_stop_is_bounded(caplog):
    # Scenario: A hung stop is bounded.
    bridge = FakeBridge()
    bridge.ack_faults["stop"] = asyncio.Event()  # never set
    closed_in = None

    async def turn():
        nonlocal closed_in
        async with _real(bridge, timeout=BOUND).working():
            await _until(lambda: bridge.typing, "the entry start")
            closing = time.monotonic()
        closed_in = time.monotonic() - closing

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(turn(), FAIL_FAST)
    _assert_one_bound(closed_in)  # the close waited out its ONE bound on the stop
    assert bridge.ack_attempts[-1] == ("stop", OWNER)
    assert bridge.typing == [("start", OWNER)]  # the stop never completed
    assert len(_records(caplog)) == 1
    assert len(_lines(caplog, CLOSE_LINE)) == 1


async def test_the_bound_exhausted_before_the_stop_leaves_the_indicator_to_expiry(
    caplog,
):
    # A refresh is in flight at exit and hangs past the close's start; the stop
    # that would follow it hangs too. The close's single bound runs out before any
    # stop completes, so the task is cancelled and the indicator left to expire.
    fake = FakeTime()
    bridge = ClockedBridge(fake)
    bound = BOUND
    closed_in = None

    async def turn():
        nonlocal closed_in
        async with _timed(bridge, fake, timeout=bound).working():
            await fake.run_until(6.0)
            await fake.sleeping()
            bridge.ack_faults["start"] = asyncio.Event()
            bridge.ack_faults["stop"] = asyncio.Event()
            await fake.step()
            await _until(
                lambda: len(bridge.ack_attempts) == 2, "the refresh to be in flight"
            )
            closing = time.monotonic()
        closed_in = time.monotonic() - closing

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(turn(), FAIL_FAST)
    # One bound from the close's start: the refresh's own bound ran out inside it,
    # and the close's bound (not a multiple of it) cut the stop off.
    _assert_one_bound(closed_in, bound)
    assert ("stop", OWNER) not in bridge.typing
    # One close line; the hung refresh is the turn's one loop line.
    assert len(_lines(caplog, CLOSE_LINE)) == 1
    assert len(_lines(caplog, LOOP_LINE)) == 1
    assert len(_records(caplog)) == 2


async def test_the_body_raising_still_stops_and_the_exception_propagates():
    bridge = FakeBridge()
    boom = ValueError("turn failed")

    async def turn():
        async with _real(bridge).working():
            raise boom

    with pytest.raises(ValueError) as caught:
        await asyncio.wait_for(turn(), FAIL_FAST)
    assert caught.value is boom
    assert bridge.typing == [("start", OWNER), ("stop", OWNER)]


# --- Cancellation ----------------------------------------------------------


@pytest.mark.parametrize("start", ["idle", "hung"])
async def test_a_cancelled_body_sends_no_stop(start):
    # Scenario: Shutdown mid-turn sends no stop — with the indicator task idle in
    # its wait, and with it inside a hung start (which a helper that caught
    # BaseException would swallow, leaving the task looping).
    bridge = FakeBridge()
    if start == "hung":
        bridge.ack_faults["start"] = asyncio.Event()
    entered = asyncio.Event()

    async def turn():
        async with _real(bridge, timeout=1.0).working():
            entered.set()
            await asyncio.Event().wait()

    owner = asyncio.create_task(turn())
    await asyncio.wait_for(entered.wait(), FAIL_FAST)
    await _until(lambda: bridge.ack_attempts, "the entry start")
    owner.cancel()
    await asyncio.wait_for(asyncio.wait({owner}), FAIL_FAST)
    assert owner.cancelled(), "the cancellation did not propagate"
    assert asyncio.all_tasks() - {asyncio.current_task()} == set()
    assert ("stop", OWNER) not in bridge.ack_attempts
    assert ("stop", OWNER) not in bridge.typing


async def test_a_cancel_during_entry_leaves_no_indicator_task():
    # Cancelled while at the entry yield: the indicator task already exists, so
    # the yield must sit inside the bracket's try or the task is orphaned.
    bridge = FakeBridge()
    body_ran = False

    async def turn():
        nonlocal body_ran
        async with _real(bridge).working():
            body_ran = True

    owner = asyncio.create_task(turn())
    await asyncio.sleep(0)  # the owner runs up to the entry yield
    assert not owner.done()
    owner.cancel()
    await asyncio.wait_for(asyncio.wait({owner}), FAIL_FAST)
    assert owner.cancelled()
    assert body_ran is False
    assert asyncio.all_tasks() - {asyncio.current_task()} == set()
    assert ("stop", OWNER) not in bridge.ack_attempts


async def test_an_outer_cancel_during_the_close_propagates():
    # Shutdown arriving while the close waits on a hung stop: the owning task
    # must end cancelled (not absorbed by the await on the child), and the
    # indicator task must be done.
    bridge = FakeBridge()
    bridge.ack_faults["stop"] = asyncio.Event()

    async def turn():
        async with _real(bridge, timeout=1.0).working():
            await _until(lambda: bridge.typing, "the entry start")

    owner = asyncio.create_task(turn())
    await _until(lambda: ("stop", OWNER) in bridge.ack_attempts, "the stop in flight")
    owner.cancel()
    await asyncio.wait_for(asyncio.wait({owner}), FAIL_FAST)
    assert owner.cancelled(), "the outer cancellation was swallowed by the close"
    assert asyncio.all_tasks() - {asyncio.current_task()} == set()


# --- Logging ---------------------------------------------------------------


@pytest.mark.parametrize(
    "fault",
    [SignalBridgeError("refused"), RuntimeError("adapter bug")],
    ids=["refused", "raising"],
)
async def test_refresh_failures_are_logged_once_per_turn(fault, caplog):
    # Scenario: Refresh failures are logged once per turn — and the refresh keeps
    # being attempted. The close gets at most one line of its own. "raising" is an
    # adapter that lets an exception through instead of reporting `False` (the
    # Signal adapter does for anything but SignalBridgeError; a second adapter
    # might for anything): the loop must absorb it and go on.
    fake = FakeTime()
    bridge = ClockedBridge(fake)
    bridge.ack_faults["start"] = fault
    ack = _timed(bridge, fake)

    async def turn(until: float):
        async with ack.working():
            await fake.run_until(until)
            await fake.sleeping()

    # Outer bound above FakeTime's own, so a dead loop fails on its message.
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(turn(50.0), 2 * FAIL_FAST)
    assert len(bridge.issue_times("start")) == 8  # 0, 7, ..., 49: still attempted
    assert len(_records(caplog)) == 1
    assert len(_lines(caplog, LOOP_LINE)) == 1

    # A second turn has its own budget, and a failing stop adds exactly one line.
    caplog.clear()
    bridge.ack_faults["stop"] = fault
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(turn(100.0), 2 * FAIL_FAST)
    assert len(bridge.issue_times("start")) == 16  # and 50, 57, ..., 99
    assert len(_lines(caplog, LOOP_LINE)) == 1
    assert len(_lines(caplog, CLOSE_LINE)) == 1
    assert len(_records(caplog)) == 2


@pytest.mark.parametrize(
    "fault",
    [SignalBridgeError("refused"), RuntimeError("adapter bug")],
    ids=["refused", "raising"],
)
async def test_a_failing_stop_logs_one_close_line_and_the_close_returns(
    fault, caplog, ack_asyncio
):
    # The close fails without a hang: refused (`False`), or raised through by the
    # adapter. One close line, the close returns, nothing reaches the turn.
    bridge = FakeBridge()
    bridge.ack_faults["stop"] = fault

    async def turn():
        async with _real(bridge).working():
            await _until(lambda: bridge.typing, "the entry start")

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(turn(), FAIL_FAST)
    assert bridge.ack_attempts == [("start", OWNER), ("stop", OWNER)]
    lines = _lines(caplog, CLOSE_LINE)
    assert len(lines) == 1 and len(_records(caplog)) == 1
    expected = "not accepted" if isinstance(fault, SignalBridgeError) else repr(fault)
    assert expected in lines[0], lines
    # Absorbed where the stop is issued: the indicator task ends normally. The
    # close's dead-task line (next test) is a backstop, not this path; with both
    # gone a raising stop would go unlogged, so each is pinned on its own.
    (indicator,) = ack_asyncio.indicator_tasks()
    assert indicator.done() and not indicator.cancelled()
    assert indicator.exception() is None, indicator.exception()


async def test_an_indicator_task_that_dies_is_logged_once_by_the_close(caplog):
    # A fault outside every per-request handler (here the pause predicate raising,
    # as a buggy gate might) ends the indicator task with an exception. The close
    # still returns, logs exactly one line naming it, and leaves no task behind
    # (the autouse fixture).
    fake = FakeTime()
    bridge = ClockedBridge(fake)
    raised = asyncio.Event()

    def paused() -> bool:
        raised.set()
        raise RuntimeError("gate bug")

    async def turn():
        async with _timed(bridge, fake, paused=paused).working():
            await fake.step()  # the first wake-up reads the predicate
            await asyncio.wait_for(raised.wait(), FAIL_FAST)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await asyncio.wait_for(turn(), FAIL_FAST)
    assert bridge.typing == [("start", OWNER)]  # the task died before its stop
    lines = _lines(caplog, CLOSE_LINE)
    assert len(lines) == 1, lines
    assert "RuntimeError('gate bug')" in lines[0], lines
    assert len(_records(caplog)) == 1


# --- The configured bound, structurally -------------------------------------


async def test_every_bounded_operation_gets_exactly_the_configured_timeout(
    ack_asyncio,
):
    # Elapsed-time checks carry slack, so on their own they cannot tell the
    # configured bound from a small multiple of it. This pins the value itself:
    # a receipt, a turn's start, refreshes, a pause stop, a resume start and the
    # close each open ONE `asyncio.timeout`, with exactly the configured value.
    configured = 0.321  # distinctive, and no real wait: nothing here hangs
    fake = FakeTime()
    bridge = ClockedBridge(fake)
    state = {"paused": False}
    ack = _timed(bridge, fake, timeout=configured, paused=lambda: state["paused"])

    await asyncio.wait_for(ack.receipt(REF), FAIL_FAST)
    assert ack_asyncio.timeouts == [configured], "the receipt"

    async def turn():
        async with ack.working():
            await fake.run_until(8.0)  # the refresh at 7
            await fake.sleeping()
            state["paused"] = True  # the pause stop at 9
            await fake.run_until(12.0)
            await fake.sleeping()
            state["paused"] = False  # the resume start at 13, a refresh at 20
            await fake.run_until(20.0)
            await fake.sleeping()

    await asyncio.wait_for(turn(), FAIL_FAST)
    assert bridge.issue_times("start") == [0.0, 7.0, 13.0, 20.0]
    assert bridge.issue_times("stop") == [9.0, 20.0]
    # receipt, start, refresh, pause stop, resume start, refresh, close: seven
    # bounds. The final stop runs inside the close's bound and opens none.
    assert ack_asyncio.timeouts == [configured] * 7, ack_asyncio.timeouts


def test_the_pause_poll_is_one_second():
    assert PAUSE_POLL_SECONDS == 1.0
