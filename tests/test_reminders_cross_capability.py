"""The deltas with no code of their own (reminder-delivery group 9).

Four capabilities gain requirements from this change without gaining any
implementation: `incident-triage`'s cadence enumeration, `approval-gate`'s
app-initiated exemption, `reminders`' re-enablement rule, and `secure-deployment`'s
no-new-surface claim. A requirement whose only evidence is that nobody wrote the
offending code is a requirement that quietly stops being true, so each gets a test
that would fail if someone did write it.

(9.4's test-level half lives in `tests/test_reminders_runtime.py` — the module opens no
socket, registers no handler, and its public surface is exactly `run` and `tick`. Its
runtime half, comparing the container's listening sockets before and after, is task
11.2's and cannot be asserted here.)
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from henk.channel.base import SendOutcome
from henk.config import RemindersConfig
from henk.events.pipeline import EventPipeline, PipelineConfig
from henk.events.types import Event
from henk.gate.approval import ApprovalGate, ApprovalOutcome, Classification
from henk.reminders.note import NOTE_BEGIN
from henk.reminders.scheduler import SUMMARY_HEADING, ReminderScheduler
from henk.reminders.timeparse import TimeResolver, render_instant
from henk.store import Store
from henk.store.reminders import (
    DELIVERED,
    DELIVERED_LATE,
    MISSED,
    PENDING,
    ReminderStore,
)
from henk.tools.base import AuthorizationTier, Tool, ToolClass, ToolResult, TurnType
from tests.test_reminders_scheduler import (
    CRASH_LIMIT,
    GRACE,
    HORIZON,
    NOW,
    THRESHOLD,
    TZ,
    Clock,
    OutcomeChannel,
)

ZONE = ZoneInfo(TZ)


def _scheduler(repo, channel, clock, **overrides):
    config = RemindersConfig(
        enabled=True,
        late_grace_seconds=GRACE,
        retry_floor_seconds=900,
        report_horizon_seconds=HORIZON,
        late_delivery_threshold_seconds=THRESHOLD,
        crash_attempt_limit=CRASH_LIMIT,
        tick_delivery_limit=10,
        **overrides,
    )
    return ReminderScheduler(
        repo,
        channel,
        config=config,
        resolver=TimeResolver(ZONE, clock=clock),
        clock=clock,
        sleep=_yield,
    )


async def _yield(_):
    await asyncio.sleep(0)


def _open(tmp_path: Path, clock):
    store = Store(tmp_path / "store" / "henk.db", clock=clock)
    return store, ReminderStore(store)


def _seed(repo, *, due_at, text="call the plumber"):
    return repo.schedule(text, due_at=due_at, due_tz=TZ, input_spec="+1h")


# --- 9.1 Cadence (incident-triage delta) --------------------------------


async def test_a_reminder_delivery_does_not_consume_the_incident_cap(tmp_path: Path):
    """The cap counts announceable INCIDENTS. A reminder is not one.

    Structurally true — the scheduler never touches the pipeline — but that is exactly
    the kind of truth that stops holding when someone routes deliveries through the
    cadence machinery to "reuse the rate limiting". Asserted by measuring the remaining
    cap on either side of a batch of deliveries.
    """
    pipeline = EventPipeline(PipelineConfig(cap_per_24h=3, cooldown_seconds=0))

    def announce(name: str) -> bool:
        decision = pipeline.evaluate([Event(id=name, title=name, message="", arrival_time=NOW)], NOW)
        return decision.event_turn.announceable

    assert announce("first") is True  # 1 of 3 used

    clock = Clock()
    store, repo = _open(tmp_path, clock)
    channel = OutcomeChannel()
    scheduler = _scheduler(repo, channel, clock)
    for i in range(5):
        _seed(repo, due_at=NOW - 600 - i, text=f"reminder {i}")
    await scheduler.tick()
    assert len(channel.sent) == 5, "the deliveries should have happened"

    # Two announceable incidents still available, exactly as before the deliveries.
    assert announce("second") is True
    assert announce("third") is True
    assert announce("fourth") is False  # the cap, unmoved by five reminders
    store.close()


async def test_a_catch_up_summary_does_not_consume_the_incident_cap(tmp_path: Path):
    pipeline = EventPipeline(PipelineConfig(cap_per_24h=1, cooldown_seconds=0))
    clock = Clock()
    store, repo = _open(tmp_path, clock)
    channel = OutcomeChannel()
    scheduler = _scheduler(repo, channel, clock)
    _seed(repo, due_at=NOW - GRACE - 600, text="missed thing")
    await scheduler.tick()
    assert any(SUMMARY_HEADING in t for t in channel.sent)

    decision = pipeline.evaluate([Event(id="e", title="boiler", message="", arrival_time=NOW)], NOW)
    assert decision.event_turn.announceable is True
    store.close()


async def test_nothing_due_means_zero_unprompted_messages_over_a_long_run(
    tmp_path: Path,
):
    """No digest, no heartbeat, no "all is well" — there is no such path to fire.

    A week of simulated ticks with reminders enabled, one reminder scheduled for the
    future, and nothing else. The cadence contract's "never on a timer" survives as a
    two-class enumeration only if the second class is genuinely empty when the owner
    scheduled nothing.
    """
    clock = Clock()
    store, repo = _open(tmp_path, clock)
    channel = OutcomeChannel()
    scheduler = _scheduler(repo, channel, clock, poll_interval_seconds=30)
    _seed(repo, due_at=NOW + 30 * 86400, text="far in the future")

    ticks = int(7 * 86400 / 3600)  # a week, sampled hourly
    for _ in range(ticks):
        await scheduler.tick()
        clock.advance(3600)
    assert channel.calls == [], channel.calls
    assert repo.get(1).status == PENDING
    store.close()


def test_the_pipeline_has_no_reminder_surface_at_all():
    """Design D9 is spec text plus scheduler behaviour, and NO PipelineConfig field.

    `reminders-core` shipped a guard saying the cadence amendment had not ridden along
    early. This is its counterpart for the change that owns the amendment: the
    amendment lands as requirement text and as the scheduler bypassing the pipeline —
    not as a knob. A `reminder_class` field would make reminder volume a cadence
    concern, which is exactly what "they do not consume the cap" denies.
    """
    fields = {f.name for f in dataclasses.fields(PipelineConfig)}
    assert not any("reminder" in name for name in fields), fields

    import ast
    import inspect

    from henk.events import pipeline as module

    source = inspect.getsource(module)
    assert "reminder" not in source.lower()
    # And nothing in the scheduler reaches for the pipeline either.
    from henk.reminders import scheduler as sched_module

    sched_source = inspect.getsource(sched_module)
    for forbidden in ("pipeline", "cap_per_24h", "announceable", "EventTurn"):
        assert forbidden not in sched_source, forbidden
    ast.parse(sched_source)  # cheap sanity that we read real source


# --- 9.2 The gate (approval-gate delta) ---------------------------------


class _GateChannel:
    """A channel that records prompts and reminders separately."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.proactive: list[str] = []

    async def send(self, text: str) -> SendOutcome:
        self.replies.append(text)
        return SendOutcome.DELIVERED

    async def send_proactive(self, text, *, failure_notice=None) -> SendOutcome:
        self.proactive.append(text)
        return SendOutcome.DELIVERED


class _Recorder:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, **fields) -> dict:
        self.records.append(fields)
        return fields


class _MutatingTool(Tool):
    """A per-instance mutating tool, so the gate actually prompts for it."""

    name = "capture"
    description = "capture something"
    tool_class = ToolClass.MUTATING
    authorization = AuthorizationTier.PER_INSTANCE
    parameters: dict = {}

    async def _run(self, **arguments) -> ToolResult:
        return ToolResult(ok=True, content="done")


def _mutating_tool() -> Tool:
    return _MutatingTool()


async def test_a_delivery_while_an_approval_is_pending_sends_and_prompts_nothing(
    tmp_path: Path,
):
    """Three claims at once, because they are one property seen three ways.

    The reminder goes out; no approval record is created for the send; and the pending
    approval is still there afterwards, resolvable by the owner's next keyword. That
    last part is what makes the exemption safe: a delivery that quietly consumed the
    pending-approval slot would strand a mutation the owner had been asked about.
    """
    channel = _GateChannel()
    recorder = _Recorder()
    gate = ApprovalGate(channel, timeout_seconds=5.0, recorder=recorder)
    from henk.gate.approval import TurnContext

    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER, announceable=True))

    # Start an approval and let it become pending.
    authorization = asyncio.create_task(gate.authorize(_mutating_tool(), {"text": "x"}))
    for _ in range(10):
        await asyncio.sleep(0)
        if gate.has_pending():
            break
    assert gate.has_pending(), "the approval never became pending"
    prompts_before = len(channel.replies)
    records_before = len(recorder.records)

    # A reminder comes due mid-approval.
    clock = Clock()
    store, repo = _open(tmp_path, clock)
    scheduler = _scheduler(repo, channel, clock)
    row = _seed(repo, due_at=NOW - 600, text="the reminder still arrives")
    await scheduler.tick()

    assert any("the reminder still arrives" in t for t in channel.proactive)
    assert repo.get(row.id).status == DELIVERED_LATE
    # No prompt, and no approval record, for the delivery.
    assert len(channel.replies) == prompts_before
    assert len(recorder.records) == records_before
    # The pending approval is untouched and still resolvable.
    assert gate.has_pending()
    classification, requeue = gate.deliver("yes")
    assert classification is Classification.APPROVE
    assert requeue is False
    decision = await authorization
    assert decision.outcome is ApprovalOutcome.APPROVED
    gate.exit_turn()
    store.close()


async def test_a_delivered_reminder_is_not_classifiable_as_an_approval_prompt(
    tmp_path: Path,
):
    """The gate classifies INBOUND text only, and the marker settles it anyway.

    Asserted both ways: the delivered message is not an approval keyword, and the gate
    has no pending approval to attach it to in the first place.
    """
    clock = Clock()
    store, repo = _open(tmp_path, clock)
    channel = _GateChannel()
    gate = ApprovalGate(channel, timeout_seconds=1.0)
    scheduler = _scheduler(repo, channel, clock)
    _seed(repo, due_at=NOW - 600, text="yes")  # deliberately an approval keyword
    await scheduler.tick()

    delivered = channel.proactive[-1]
    assert "yes" in delivered
    # The marker makes it unmistakably a reminder, and the gate is not waiting.
    assert gate.has_pending() is False
    # Even if the delivered text were fed back in, an outbound message is never
    # classified — the gate's only input is inbound.
    assert gate.classify(delivered) is Classification.UNRELATED
    store.close()


def test_the_scheduler_never_touches_the_gate():
    """Authority was granted at scheduling; the scheduler is not a model turn."""
    import inspect

    from henk.reminders import scheduler as module

    source = inspect.getsource(module)
    for forbidden in ("gate", "authorize", "ApprovalGate", "approval", "TurnContext"):
        assert forbidden.lower() not in source.lower(), forbidden
    params = set(inspect.signature(ReminderScheduler.__init__).parameters)
    assert "gate" not in params
    assert params == {
        "self",
        "reminders",
        "channel",
        "config",
        "resolver",
        "receipts",
        "clock",
        "sleep",
    }, params


# --- 9.3 Re-enablement (reminders delta) --------------------------------


async def test_re_enabling_catches_up_under_the_ordinary_grace_rules(tmp_path: Path):
    """Two app lifetimes over ONE store, across a flag flip.

    The stale offset is deliberate: three days is older than the grace window **plus**
    the report horizon, so reportability arrives later than `due_at + grace` and the
    post-send horizon placement is what keeps the row from being retired unnamed. A
    25-hour offset would exercise the easy case and miss that entirely.
    """
    clock = Clock()

    # --- lifetime one: reminders DISABLED. Rows are stored, nothing is delivered. ---
    store, repo = _open(tmp_path, clock)
    within_grace = _seed(
        repo, due_at=NOW - GRACE + 3600, text="still worth telling me"
    )
    stale = _seed(
        repo, due_at=NOW - 3 * 86400, text="far too old to deliver"
    )
    # No scheduler exists while disabled — that is the whole of the disabled path.
    store.close()

    # --- lifetime two: reminders ENABLED, same file. ---
    store2, repo2 = _open(tmp_path, clock)
    channel = OutcomeChannel()
    scheduler = _scheduler(repo2, channel, clock)
    await scheduler.tick()

    # The within-grace row is delivered LATE, stating its original due time.
    late = repo2.get(within_grace.id)
    assert late.status == DELIVERED_LATE
    delivery = next(t for t in channel.sent if "still worth telling me" in t)
    assert render_instant(within_grace.due_at, ZONE) in delivery

    # The stale row is missed and summarised — and NAMED before any give-up, which is
    # the property the post-send horizon exists for.
    missed = repo2.get(stale.id)
    assert missed.status == MISSED
    summary = next(t for t in channel.sent if SUMMARY_HEADING in t)
    assert "far too old to deliver" in summary
    assert render_instant(stale.due_at, ZONE) in summary
    # Delivered summary, so it is marked reported rather than given up on.
    assert missed.reported_at is not None

    # Individual delivery before the summary: the timely message first.
    assert channel.sent.index(delivery) < channel.sent.index(summary)
    store2.close()


async def test_a_disabled_lifetime_leaves_every_delivery_column_untouched(
    tmp_path: Path,
):
    """Re-enablement has to find the rows as the owner left them."""
    clock = Clock()
    store, repo = _open(tmp_path, clock)
    row = _seed(repo, due_at=NOW - 3 * 86400, text="untouched while off")
    before = repo.get(row.id)
    store.close()

    # A disabled lifetime: the store is opened and read, and no scheduler runs.
    store2, repo2 = _open(tmp_path, clock)
    assert repo2.list_pending().items[0].id == row.id
    store2.close()

    store3, repo3 = _open(tmp_path, clock)
    after = repo3.get(row.id)
    assert after == before
    assert after.send_attempts == 0
    assert after.delivered_at is None
    assert after.surfaced_at is None
    assert after.reported_at is None
    assert after.next_attempt_at == after.due_at
    store3.close()


async def test_the_stale_row_is_named_even_when_the_summary_only_partly_lands(
    tmp_path: Path,
):
    """The re-enablement case crossed with the horizon's worst case.

    Re-enabled after long downtime AND a summary that only delivers its head: the row
    must still be named once. With the horizon evaluated pre-work it would have been
    retired silently on this very tick.
    """
    clock = Clock()
    store, repo = _open(tmp_path, clock)
    stale = _seed(repo, due_at=NOW - 5 * 86400, text="named before give-up")
    store.close()

    store2, repo2 = _open(tmp_path, clock)
    channel = OutcomeChannel()
    channel.default = SendOutcome.PARTIAL
    scheduler = _scheduler(repo2, channel, clock)
    await scheduler.tick()

    summary = next(t for t in channel.sent if SUMMARY_HEADING in t)
    assert "named before give-up" in summary
    # Retired, but only after being named — and with reported_at written as the
    # give-up exit rather than as a claim the owner read it.
    assert repo2.get(stale.id).reported_at is not None
    store2.close()


async def test_re_enablement_surfaces_the_delivery_in_the_next_owner_turn(
    tmp_path: Path,
):
    """The two halves of the capability meeting: a catch-up delivery, then a reply."""
    from henk.agent.core import AgentCore
    from henk.agent.turns import OwnerTurn
    from henk.reminders.note import DeliveredReminderNote
    from tests.conftest import FakeChannel, FakeSessionFactory

    clock = Clock()
    store, repo = _open(tmp_path, clock)
    _seed(repo, due_at=NOW - GRACE + 3600, text="the catch-up delivery")
    channel = OutcomeChannel()
    scheduler = _scheduler(repo, channel, clock)
    await scheduler.tick()

    note = DeliveredReminderNote(
        repo,
        TimeResolver(ZONE, clock=clock),
        window_seconds=43200,
        max_items=10,
        clock=clock,
    )
    factory = FakeSessionFactory()
    core = AgentCore(factory, FakeChannel(), deliveries=note)
    await core.process(OwnerTurn("what was that about?"))
    content = factory.created[0].turns[0]
    assert NOTE_BEGIN in content
    assert "the catch-up delivery" in content
    await core.aclose()
    store.close()
