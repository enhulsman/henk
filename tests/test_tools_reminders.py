"""The three reminder tools (group 6), from the reminders and approval-gate specs.

Real store files throughout. The gate tests here drive the real
:class:`ApprovalGate` with a real :class:`TurnContext`, because turn scope and
session taint are the gate's behaviour, not the tool's — a tool-level double would
assert the wrong thing.
"""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from henk.audit import AuditLog, MutationReceipts, ReminderReceipts
from henk.config import Config
from henk.gate.approval import ApprovalGate, ApprovalOutcome, TurnContext, gated_invoke
from henk.reminders.timeparse import TimeResolver, render_instant
from henk.store import Store
from henk.store.reminders import CANCELLED, PENDING, ReminderStore
from henk.tools import build_production_registry, build_time_resolver
from henk.tools.base import AuthorizationTier, ToolClass, TurnType
from henk.tools.reminders import (
    CancelReminderTool,
    RemindersReadTool,
    RemindTool,
)
from tests.conftest import FakeChannel
from tests.test_config import _minimal_raw

AMS = ZoneInfo("Europe/Amsterdam")
NOW = 1787203800.0  # 2026-08-20 07:30 CEST
DUE_TOMORROW_0730 = 1787290200.0  # 2026-08-21 07:30 CEST


@pytest.fixture
def bench(tmp_path: Path):
    """A real store, a fixed-clock resolver, and the three tools over both."""

    class Bench:
        def __init__(self) -> None:
            self.store = Store(tmp_path / "store" / "henk.db", clock=lambda: NOW)
            self.repo = ReminderStore(self.store, max_pending=5, text_length_limit=40)
            self.resolver = TimeResolver(AMS, clock=lambda: NOW)
            self.audit_path = tmp_path / "audit.jsonl"
            self.audit = AuditLog(self.audit_path)
            self.receipts = ReminderReceipts(self.audit)
            self.remind = RemindTool(self.repo, self.resolver, receipts=self.receipts)
            self.cancel = CancelReminderTool(
                self.repo, self.resolver, receipts=self.receipts
            )
            self.read = RemindersReadTool(self.repo, self.resolver)

        def records(self, record_type: str) -> list[dict]:
            if not self.audit_path.exists():
                return []
            return [
                r
                for r in (
                    json.loads(line)
                    for line in self.audit_path.read_text().splitlines()
                )
                if r["record_type"] == record_type
            ]

    instance = Bench()
    yield instance
    instance.store.close()


# --- 6.1 remind -----------------------------------------------------------


async def test_a_reminder_is_stored_and_echoed_with_its_id_and_due_time(bench):
    result = await bench.remind.run(text="buy bread", when="2026-08-21 07:30")
    assert result.ok
    stored = bench.repo.list_pending().items[0]
    assert stored.status == PENDING
    assert stored.text == "buy bread"
    assert stored.due_at == DUE_TOMORROW_0730
    assert f"#{stored.id}" in result.content
    # The echo is the safety mechanism: a mis-resolved time becomes a wrong-but-
    # VISIBLE confirmation in the same reply.
    assert render_instant(DUE_TOMORROW_0730, AMS) in result.content
    assert "buy bread" in result.content


async def test_a_relative_offset_works_on_the_tool_path(bench):
    result = await bench.remind.run(text="stir the risotto", when="+90m")
    assert result.ok
    assert bench.repo.list_pending().items[0].due_at == NOW + 5400


async def test_the_ambiguity_disclosure_reaches_the_tool_result(bench):
    result = await bench.remind.run(text="odd night", when="2026-10-25 02:30")
    assert result.ok
    assert "twice" in result.content
    assert bench.repo.list_pending().items[0].due_at == 1792888200.0


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
async def test_whitespace_only_text_is_rejected_with_nothing_stored(bench, text):
    result = await bench.remind.run(text=text, when="+2h")
    assert not result.ok
    assert bench.repo.count_pending() == 0


async def test_over_limit_text_is_rejected_naming_the_limit_and_nothing_is_stored(
    bench,
):
    result = await bench.remind.run(text="x" * 41, when="+2h")
    assert not result.ok
    assert "40" in result.error
    assert bench.repo.count_pending() == 0
    # And explicitly no truncated variant.
    assert bench.repo.list_pending().items == ()


async def test_a_rejected_time_stores_nothing_and_names_an_accepted_form(bench):
    result = await bench.remind.run(text="buy bread", when="2026-08-21T07:30Z")
    assert not result.ok
    assert "offset" in result.error
    assert bench.repo.count_pending() == 0


async def test_the_pending_cap_is_reported_honestly(bench):
    for n in range(5):
        assert (await bench.remind.run(text=f"item {n}", when="+2h")).ok
    result = await bench.remind.run(text="one too many", when="+2h")
    assert not result.ok
    assert "5" in result.error
    assert bench.repo.count_pending() == 5


async def test_a_store_failure_never_produces_a_confirmation_naming_a_due_time(
    bench, monkeypatch
):
    from henk.store.errors import StoreError

    def boom(*_a, **_kw):
        raise StoreError("disk on fire")

    monkeypatch.setattr(bench.repo, "schedule", boom)
    result = await bench.remind.run(text="buy bread", when="2026-08-21 07:30")
    assert not result.ok
    assert "disk on fire" in result.error
    # An owner who believes a reminder is set when it is not is the capability's
    # worst failure, so no rendered due time may appear anywhere in the result.
    assert render_instant(DUE_TOMORROW_0730, AMS) not in (result.content or "")
    assert render_instant(DUE_TOMORROW_0730, AMS) not in (result.error or "")
    assert "Reminder #" not in (result.content or "")


# --- 4.8 The forensic columns, on both families --------------------------


@pytest.mark.parametrize(
    "when,expected_due",
    [("2026-08-21 07:30", DUE_TOMORROW_0730), ("+90m", NOW + 5400)],
)
async def test_input_spec_and_due_tz_are_written_on_every_family(
    bench, when, expected_due
):
    # A forensic column whose meaning silently varies per row cannot be read at
    # all, so both are asserted on a duration row as well as a wall-clock one.
    await bench.remind.run(text="check the columns", when=when)
    stored = bench.repo.list_pending().items[-1]
    assert stored.due_at == expected_due
    assert stored.input_spec == when
    assert stored.due_tz == "Europe/Amsterdam"


async def test_input_spec_records_the_when_argument_and_not_the_text(bench):
    await bench.remind.run(text="a rather long reminder body", when="+2h")
    assert bench.repo.list_pending().items[0].input_spec == "+2h"


async def test_an_over_long_input_spec_is_truncated_rather_than_refused(bench):
    # It cannot happen through the grammar, but the column's bound must never be
    # the reason a valid schedule fails, so the truncation is silent.
    stored = bench.repo.schedule(
        "direct", due_at=NOW + 60, due_tz="Europe/Amsterdam",
        input_spec="+" + "1" * 200 + "m",
    )
    assert len(bench.repo.get(stored.id).input_spec) == bench.repo.input_spec_limit


# --- 6.2 cancel_reminder --------------------------------------------------


async def test_cancelling_is_a_status_change_that_echoes_text_and_due_time(bench):
    await bench.remind.run(text="buy bread", when="2026-08-21 07:30")
    reminder_id = bench.repo.list_pending().items[0].id

    result = await bench.cancel.run(reminder_id=reminder_id)
    assert result.ok
    row = bench.repo.get(reminder_id)
    assert row.status == CANCELLED
    assert row.text == "buy bread"  # retained
    assert row.due_at == DUE_TOMORROW_0730  # retained
    assert "buy bread" in result.content
    assert render_instant(DUE_TOMORROW_0730, AMS) in result.content


async def test_a_cancellation_names_its_undo(bench):
    await bench.remind.run(text="buy bread", when="+2h")
    reminder_id = bench.repo.list_pending().items[0].id
    result = await bench.cancel.run(reminder_id=reminder_id)
    assert f"/reminders reinstate {reminder_id}" in result.content


@pytest.mark.parametrize("bad", [9999, "nope", None])
async def test_an_unknown_or_non_pending_id_changes_nothing_and_says_so(bench, bad):
    await bench.remind.run(text="untouched", when="+2h")
    before = bench.repo.list_pending().items[0]
    result = await bench.cancel.run(reminder_id=bad)
    assert not result.ok
    assert bench.repo.get(before.id).status == PENDING


async def test_cancelling_an_already_cancelled_reminder_changes_nothing(bench):
    await bench.remind.run(text="once", when="+2h")
    reminder_id = bench.repo.list_pending().items[0].id
    await bench.cancel.run(reminder_id=reminder_id)
    again = await bench.cancel.run(reminder_id=reminder_id)
    assert not again.ok
    assert bench.repo.get(reminder_id).status == CANCELLED
    # And only ONE cancellation receipt exists.
    assert [
        r for r in bench.records("reminder") if r["transition"] == "cancelled"
    ] != []
    assert (
        len([r for r in bench.records("reminder") if r["transition"] == "cancelled"])
        == 1
    )


# --- 6.3 reminders_read ---------------------------------------------------


async def test_the_read_tool_lists_soonest_first_with_id_due_time_and_text(bench):
    await bench.remind.run(text="later", when="+3h")
    await bench.remind.run(text="sooner", when="+1h")
    result = await bench.read.run()
    assert result.ok
    assert result.content.index("sooner") < result.content.index("later")
    for item in bench.repo.list_pending().items:
        assert f"#{item.id}" in result.content
        assert render_instant(item.due_at, AMS) in result.content


async def test_an_empty_schedule_reads_as_empty_and_not_as_an_error(bench):
    result = await bench.read.run()
    assert result.ok
    assert "no pending reminders" in result.content.lower()


async def test_the_result_is_clamped_and_says_how_many_were_not_shown(tmp_path: Path):
    store = Store(tmp_path / "s.db", clock=lambda: NOW)
    repo = ReminderStore(store, max_pending=50, page_size=2)
    resolver = TimeResolver(AMS, clock=lambda: NOW)
    for n in range(5):
        repo.schedule(f"item {n}", due_at=NOW + 60 * (n + 1), due_tz="Europe/Amsterdam")
    result = await RemindersReadTool(repo, resolver).run()
    assert result.ok
    assert result.content.count("- #") == 2
    assert "3 later reminder(s) not shown" in result.content
    store.close()


async def test_the_read_tool_takes_no_limit_parameter(bench):
    # The bound is configured, one number shared with `/reminders`, so the model
    # cannot widen its own view of the schedule.
    assert bench.read.parameters["properties"] == {}
    assert bench.read.parameters["additionalProperties"] is False


async def test_an_unreadable_schedule_is_not_reported_as_an_empty_one(
    bench, monkeypatch
):
    from henk.store.errors import StoreError

    def boom(*_a, **_kw):
        raise StoreError("cannot read")

    monkeypatch.setattr(bench.repo, "list_pending", boom)
    result = await bench.read.run()
    assert not result.ok
    assert "not" in result.error and "empty" in result.error


# --- 6.4 The declared tier and scope (approval-gate delta) ---------------


def _gate(*, demote_standing: bool = False):
    channel = FakeChannel()
    return channel, ApprovalGate(
        channel, timeout_seconds=0.05, demote_standing=demote_standing
    )


def test_both_mutating_tools_declare_mutating_standing_owner_only(bench):
    for tool in (bench.remind, bench.cancel):
        assert tool.tool_class is ToolClass.MUTATING
        assert tool.authorization is AuthorizationTier.STANDING
        assert tuple(tool.turn_scope) == (TurnType.OWNER,)
        assert TurnType.EVENT not in tool.turn_scope


def test_the_read_tool_is_read_only(bench):
    assert bench.read.tool_class is ToolClass.READ_ONLY
    assert bench.read.authorization is None


async def test_an_untainted_owner_session_executes_without_a_prompt(bench):
    channel, gate = _gate()
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER, tainted=False))
    decision = await gate.authorize(bench.remind, {"text": "x", "when": "+2h"})
    assert decision.outcome is ApprovalOutcome.AUTHORIZED
    assert channel.sent == []


@pytest.mark.parametrize("tool_attr", ["remind", "cancel"])
async def test_both_are_denied_out_of_scope_on_an_event_turn(bench, tool_attr):
    channel, gate = _gate()
    gate.enter_turn(TurnContext(turn_type=TurnType.EVENT, tainted=True))
    tool = getattr(bench, tool_attr)
    result = await gated_invoke(gate, tool, {"text": "x", "when": "+2h"})
    assert not result.ok
    assert channel.sent == []  # fail closed, and silently
    assert bench.repo.count_pending() == 0


@pytest.mark.parametrize("tool_attr", ["remind", "cancel"])
async def test_both_are_denied_in_a_tainted_session_naming_the_remind_command(
    bench, tool_attr
):
    channel, gate = _gate()
    # An owner follow-up inside the session an event turn started.
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER, tainted=True))
    tool = getattr(bench, tool_attr)
    decision = await gate.authorize(tool, {"text": "x", "when": "+2h"})
    assert decision.outcome is ApprovalOutcome.OUT_OF_SCOPE
    assert "/remind" in decision.reason
    assert "/new" in decision.reason
    assert channel.sent == []


async def test_an_event_payload_cannot_schedule_or_cancel(bench):
    # The injection path, both halves: the event turn itself and the owner follow-up.
    await bench.remind.run(text="already scheduled", when="+2h")
    existing = bench.repo.list_pending().items[0]

    channel, gate = _gate()
    for context in (
        TurnContext(turn_type=TurnType.EVENT, tainted=True),
        TurnContext(turn_type=TurnType.OWNER, tainted=True),
    ):
        gate.enter_turn(context)
        assert not (
            await gated_invoke(gate, bench.remind, {"text": "injected", "when": "+1h"})
        ).ok
        assert not (
            await gated_invoke(gate, bench.cancel, {"reminder_id": existing.id})
        ).ok
    assert channel.sent == []
    assert bench.repo.count_pending() == 1
    assert bench.repo.get(existing.id).status == PENDING


@pytest.mark.parametrize("tool_attr", ["remind", "cancel"])
async def test_the_kill_switch_demotes_both_reminder_tools(bench, tool_attr):
    channel, gate = _gate(demote_standing=True)
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER, tainted=False))
    tool = getattr(bench, tool_attr)
    decision = await gate.authorize(tool, {"text": "x", "when": "+2h"})
    # Prompted, and it times out unanswered — so the effect never occurs.
    assert len(channel.sent) == 1
    assert "Approval needed" in channel.sent[0]
    assert decision.outcome is ApprovalOutcome.TIMEOUT
    assert not decision.permits


async def test_the_read_tool_bypasses_the_gate_entirely(bench):
    channel, gate = _gate(demote_standing=True)
    gate.enter_turn(TurnContext(turn_type=TurnType.EVENT, tainted=True))
    decision = await gate.authorize(bench.read, {})
    assert decision.permits
    assert channel.sent == []  # no prompt, even demoted and even on an event turn


# --- 6.5 No reinstate / reschedule / edit / delete tool ------------------


def _enabled_config(**reminders):
    raw = _minimal_raw("+31600000000")
    raw["owner"]["timezone"] = "Europe/Amsterdam"
    raw["reminders"] = {"enabled": True, **reminders}
    return Config.from_dict(raw, env={})


def _registry(config, tmp_path: Path):
    import httpx

    async def handler(request):  # pragma: no cover - never called at registration
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from henk.store import build_stores

    object.__setattr__(config.store, "path", str(tmp_path / "reg.db"))
    stores = build_stores(config.store, config.reminders)
    return build_production_registry(
        config, client, stores=stores, resolver=build_time_resolver(config)
    )


def test_the_registry_has_no_reinstate_reschedule_edit_or_delete_tool(tmp_path: Path):
    # Asserted against the REGISTRY, so adding one later fails a test rather than a
    # review. Reinstating re-arms a message the owner deliberately killed and as a
    # tool would need a pending-cap bypass; rescheduling is cancel + remind, and the
    # two echoes are the safety mechanism.
    registry = _registry(_enabled_config(), tmp_path)
    names = set(registry.names())
    for forbidden in (
        "reinstate_reminder",
        "reminder_reinstate",
        "reschedule_reminder",
        "reminder_reschedule",
        "edit_reminder",
        "update_reminder",
        "delete_reminder",
        "remove_reminder",
        "snooze_reminder",
    ):
        assert forbidden not in names
    reminder_tools = {n for n in names if "remind" in n}
    assert reminder_tools == {"remind", "cancel_reminder", "reminders_read"}


def test_no_registered_tool_can_rewrite_a_reminders_text_or_move_its_due_time(
    tmp_path: Path,
):
    registry = _registry(_enabled_config(), tmp_path)
    for name in ("remind", "cancel_reminder", "reminders_read"):
        params = registry.get(name).parameters.get("properties", {})
        # `remind` takes text+when for a NEW reminder; nothing takes an id AND a
        # time or an id AND text, which is the shape an edit would need.
        if "reminder_id" in params:
            assert "text" not in params and "when" not in params


# --- 6.6 Registration is behind the flag --------------------------------


def test_none_of_the_three_is_registered_when_reminders_are_disabled(tmp_path: Path):
    raw = _minimal_raw("+31600000000")
    config = Config.from_dict(raw, env={})
    registry = _registry(config, tmp_path)
    assert config.reminders.enabled is False
    for name in ("remind", "cancel_reminder", "reminders_read"):
        assert name not in registry.names()
    assert sorted(t.name for t in registry.mutating()) == ["capture", "store_memory"]


def test_all_three_are_registered_with_their_declared_classes_when_enabled(
    tmp_path: Path,
):
    registry = _registry(_enabled_config(), tmp_path)
    assert registry.get("remind").tool_class is ToolClass.MUTATING
    assert registry.get("remind").authorization is AuthorizationTier.STANDING
    assert registry.get("cancel_reminder").tool_class is ToolClass.MUTATING
    assert registry.get("reminders_read").tool_class is ToolClass.READ_ONLY
    assert sorted(t.name for t in registry.mutating()) == [
        "cancel_reminder",
        "capture",
        "remind",
        "store_memory",
    ]


def test_the_enabled_registry_shares_the_configured_bounds(tmp_path: Path):
    config = _enabled_config(max_pending=3, text_length_limit=25, page_size=2)
    registry = _registry(config, tmp_path)
    tool = registry.get("remind")
    assert tool._reminders.max_pending == 3
    assert tool._reminders.text_length_limit == 25
    assert registry.get("reminders_read")._reminders.page_size == 2


def test_the_tools_and_the_read_tool_share_one_resolver_instance(tmp_path: Path):
    # One resolver, because a due time rendered by two could differ and the owner
    # would have to adjudicate which is right.
    registry = _registry(_enabled_config(), tmp_path)
    resolvers = {
        id(registry.get(name)._resolver)
        for name in ("remind", "cancel_reminder", "reminders_read")
    }
    assert len(resolvers) == 1


def test_the_tools_and_the_read_tool_share_one_repository_instance(tmp_path: Path):
    registry = _registry(_enabled_config(), tmp_path)
    repos = {
        id(registry.get(name)._reminders)
        for name in ("remind", "cancel_reminder", "reminders_read")
    }
    assert len(repos) == 1


# --- 5.2 The two-records rule, at the tool ------------------------------


async def test_a_successful_remind_writes_an_authorization_and_a_reminder_record(
    bench,
):
    # Two records, two questions: was the agent allowed to act, and what changed.
    gate = ApprovalGate(FakeChannel(), recorder=MutationReceipts(bench.audit))
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER, tainted=False))
    result = await gated_invoke(
        gate, bench.remind, {"text": "buy bread", "when": "+2h"}
    )
    assert result.ok

    authorizations = bench.records("authorization")
    lifecycle = bench.records("reminder")
    assert len(authorizations) == 1
    assert authorizations[0]["tool"] == "remind"
    assert authorizations[0]["tier"] == "standing"
    assert authorizations[0]["outcome"] == "authorized"
    assert len(lifecycle) == 1
    assert lifecycle[0]["transition"] == "scheduled"
    assert lifecycle[0]["initiated_by"] == "model"
    assert lifecycle[0]["reminder_id"] == bench.repo.list_pending().items[0].id
    assert "buy bread" not in json.dumps(lifecycle[0])


async def test_an_authorized_call_whose_store_write_fails_leaves_the_asymmetry(
    bench, monkeypatch
):
    from henk.store.errors import StoreError

    monkeypatch.setattr(
        bench.repo, "schedule", lambda *a, **k: (_ for _ in ()).throw(StoreError("no"))
    )
    gate = ApprovalGate(FakeChannel(), recorder=MutationReceipts(bench.audit))
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER, tainted=False))
    result = await gated_invoke(gate, bench.remind, {"text": "x", "when": "+2h"})
    assert not result.ok
    # An authorization with no transition means the tool was allowed and then
    # failed — which is exactly what the log should say.
    assert len(bench.records("authorization")) == 1
    assert bench.records("reminder") == []


async def test_the_lifecycle_record_is_appended_only_after_the_commit(tmp_path: Path):
    """The record's ABSENCE is what pins the ordering.

    Driven with a store whose ``COMMIT`` fails at the driver, so the write is
    attempted, the transaction is rolled back, and nothing was stored. A
    lifecycle record written before the commit would survive that and claim a
    transition the store never made — and a log that claims state the store does
    not have is worse than a log with a gap.
    """
    import sqlite3

    class _CommitFails:
        """Proxy over a real connection whose COMMIT raises."""

        def __init__(self, conn) -> None:
            self._conn = conn

        def execute(self, sql, *args):
            if sql.strip().upper().startswith("COMMIT"):
                raise sqlite3.OperationalError("simulated disk I/O error at COMMIT")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    class _CommitFailingStore(Store):
        def connection(self):
            return _CommitFails(super().connection())

    store = _CommitFailingStore(tmp_path / "cf.db", clock=lambda: NOW)
    repo = ReminderStore(store)
    audit_path = tmp_path / "cf-audit.jsonl"
    tool = RemindTool(
        repo,
        TimeResolver(AMS, clock=lambda: NOW),
        receipts=ReminderReceipts(AuditLog(audit_path)),
    )
    result = await tool.run(text="never committed", when="+2h")
    assert not result.ok
    assert not audit_path.exists() or audit_path.read_text() == ""


async def test_a_rejected_attempt_writes_no_lifecycle_record(bench):
    # Receipts record state changes, and none occurred: a past time, a cap breach
    # and an unknown id all write nothing.
    assert not (await bench.remind.run(text="x", when="2020-01-01 07:30")).ok
    assert not (await bench.cancel.run(reminder_id=9999)).ok
    assert bench.records("reminder") == []


async def test_a_cancellation_receipt_carries_the_model_initiator(bench):
    await bench.remind.run(text="buy bread", when="+2h")
    reminder_id = bench.repo.list_pending().items[0].id
    await bench.cancel.run(reminder_id=reminder_id)
    cancelled = [r for r in bench.records("reminder") if r["transition"] == "cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["initiated_by"] == "model"
    assert cancelled[0]["reminder_id"] == reminder_id
    assert "buy bread" not in json.dumps(cancelled[0])


async def test_the_tools_work_with_no_audit_configured(bench, tmp_path: Path):
    # A no-audit deployment must behave identically, minus the records.
    tool = RemindTool(bench.repo, bench.resolver, receipts=None)
    assert (await tool.run(text="no audit", when="+2h")).ok
    assert bench.repo.count_pending() == 1
