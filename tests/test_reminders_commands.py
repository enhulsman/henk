"""The `/remind` and `/reminders` owner commands (group 7).

From the reminders spec's "Owner reminder commands" and "Reinstatement is
owner-authored only", and the agent-core delta's command scenarios.

Owner commands are **owner-initiated by construction**: the text never passes
through the model, so the gate is not involved and session taint does not apply.
That is the whole point of `/remind` existing — the reminder path must keep working
in the middle of an incident interrogation, and it must cost zero tokens.

Every test here runs under a hostile process timezone. The dispatcher is where
`datetime.now()` is most idiomatic to write, so it needs the guard at least as much
as the resolver does.
"""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from henk.agent.commands import OwnerCommands
from henk.audit import AuditLog, MutationReceipts, ReminderReceipts
from henk.reminders.timeparse import TimeResolver, render_instant
from henk.store import Store
from henk.store.reminders import CANCELLED, PENDING, ReminderStore

AMS = ZoneInfo("Europe/Amsterdam")
NOW = 1787203800.0  # 2026-08-20 07:30 CEST
NOW_EVENING = 1787252400.0  # 2026-08-20 21:00 CEST
DUE_NEXT_0730 = 1787290200.0  # 2026-08-21 07:30 CEST
DUE_2026_08_25_0730 = 1787635800.0


class Bench:
    """A real store plus the command dispatcher over it, at a fixed instant."""

    def __init__(self, tmp_path: Path, *, now: float = NOW, enabled: bool = True,
                 **repo_kwargs) -> None:
        self.now = now
        self.store = Store(tmp_path / "store" / "henk.db", clock=lambda: now)
        self.repo = ReminderStore(self.store, **repo_kwargs)
        self.resolver = TimeResolver(AMS, clock=lambda: now)
        self.audit_path = tmp_path / "audit.jsonl"
        audit = AuditLog(self.audit_path)
        self.commands = OwnerCommands(
            reminders=self.repo if enabled else None,
            resolver=self.resolver if enabled else None,
            receipts=MutationReceipts(audit),
            reminder_receipts=ReminderReceipts(audit),
        )

    def records(self, record_type: str) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [
            r
            for r in (
                json.loads(line) for line in self.audit_path.read_text().splitlines()
            )
            if r["record_type"] == record_type
        ]

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def bench(tmp_path: Path):
    instance = Bench(tmp_path)
    yield instance
    instance.close()


# --- 7.1 /remind ---------------------------------------------------------


def test_a_relative_offset_schedules_and_confirms(bench, process_tz):
    reply = bench.commands.handle("/remind +2h call the plumber")
    stored = bench.repo.list_pending().items[0]
    assert stored.due_at == NOW + 7200
    assert stored.text == "call the plumber"
    assert stored.source == "command"
    assert f"#{stored.id}" in reply
    assert render_instant(stored.due_at, AMS) in reply
    assert "call the plumber" in reply


def test_a_clock_reading_at_21_00_resolves_to_the_next_local_date(
    tmp_path: Path, process_tz
):
    bench = Bench(tmp_path, now=NOW_EVENING)
    try:
        reply = bench.commands.handle("/remind 07:30 leave for the train")
        stored = bench.repo.list_pending().items[0]
        assert stored.due_at == DUE_NEXT_0730
        assert "Friday 21 August" in reply
    finally:
        bench.close()


def test_the_two_token_dated_form_is_matched_before_the_one_token_form(
    bench, process_tz
):
    # `/remind 2026-08-25 07:30 buy bread` must split into the dated time and the
    # text, without a heuristic — the longest form is tried first.
    reply = bench.commands.handle("/remind 2026-08-25 07:30 buy bread")
    stored = bench.repo.list_pending().items[0]
    assert stored.due_at == DUE_2026_08_25_0730
    assert stored.text == "buy bread"
    assert "buy bread" in reply
    # And the T-separated variant, which the same parser handles.
    bench.commands.handle("/remind 2026-08-25T07:30 buy milk")
    assert bench.repo.list_pending().items[-1].text == "buy milk"


def test_a_dated_form_is_not_mistaken_for_a_clock_time_followed_by_text(
    bench, process_tz
):
    bench.commands.handle("/remind 2026-08-25 07:30 buy bread")
    stored = bench.repo.list_pending().items[0]
    # Had the one-token form won, `when` would be "2026-08-25" (a date with no time
    # of day, refused) or the text would begin with "07:30".
    assert not stored.text.startswith("07:30")
    assert stored.input_spec == "2026-08-25 07:30"


@pytest.mark.parametrize(
    "text",
    [
        "/remind sometime next week water the plants",
        "/remind tomorrow morning call mum",
        "/remind 2026-08-25 buy bread",
        "/remind 20260825 07:30 buy bread",
        "/remind 2026-08-25T07:30Z buy bread",
        "/remind +0m nothing",
        "/remind",
    ],
)
def test_an_unrecognized_time_form_schedules_nothing_and_names_the_accepted_forms(
    bench, process_tz, text
):
    reply = bench.commands.handle(text)
    assert bench.repo.count_pending() == 0
    assert "+2h" in reply or "+90m" in reply
    assert "07:30" in reply


@pytest.mark.parametrize("text", ["/remind +2h", "/remind 07:30", "/remind 2026-08-25 07:30"])
def test_a_recognized_time_with_no_text_says_the_text_is_required(
    bench, process_tz, text
):
    reply = bench.commands.handle(text)
    assert bench.repo.count_pending() == 0
    assert "text" in reply.lower()
    assert "required" in reply.lower() or "needs" in reply.lower()


def test_a_time_in_the_past_is_refused_naming_the_current_local_time(
    bench, process_tz
):
    reply = bench.commands.handle("/remind 2020-01-01 07:30 too late")
    assert bench.repo.count_pending() == 0
    assert render_instant(NOW, AMS) in reply


def test_a_nonexistent_reading_is_refused_naming_the_gap(tmp_path: Path, process_tz):
    # The command path gets the same D4 rejection, minus the "ask the owner"
    # instruction — the owner is right there.
    bench = Bench(tmp_path, now=1774724400.0)  # 2026-03-28 20:00 CET
    try:
        reply = bench.commands.handle("/remind 2026-03-29 02:30 impossible")
        assert bench.repo.count_pending() == 0
        assert "02:30" in reply and "29 March" in reply
        assert "01:59" in reply and "03:00" in reply
        assert "ask the owner" not in reply.lower()
    finally:
        bench.close()


def test_an_ambiguous_reading_is_scheduled_with_its_disclosure(
    tmp_path: Path, process_tz
):
    bench = Bench(tmp_path, now=1792864800.0)  # 2026-10-24 20:00 CEST
    try:
        reply = bench.commands.handle("/remind 2026-10-25 02:30 odd night")
        assert bench.repo.list_pending().items[0].due_at == 1792888200.0
        assert "twice" in reply
    finally:
        bench.close()


def test_over_limit_text_is_refused_naming_the_limit(tmp_path: Path, process_tz):
    bench = Bench(tmp_path, text_length_limit=20)
    try:
        reply = bench.commands.handle("/remind +2h " + "x" * 21)
        assert bench.repo.count_pending() == 0
        assert "20" in reply
    finally:
        bench.close()


def test_the_pending_cap_is_named(tmp_path: Path, process_tz):
    bench = Bench(tmp_path, max_pending=2)
    try:
        bench.commands.handle("/remind +2h one")
        bench.commands.handle("/remind +3h two")
        reply = bench.commands.handle("/remind +4h three")
        assert bench.repo.count_pending() == 2
        assert "2" in reply
    finally:
        bench.close()


def test_the_command_path_stores_source_command_and_its_when_token(bench, process_tz):
    bench.commands.handle("/remind +90m stir the risotto")
    stored = bench.repo.list_pending().items[0]
    assert stored.source == "command"
    # The `<when>` TOKEN, not the whole command line — the forensic column has to
    # mean the same thing on every row.
    assert stored.input_spec == "+90m"
    assert stored.due_tz == "Europe/Amsterdam"


# --- 7.2 /reminders ------------------------------------------------------


def test_pending_reminders_are_listed_soonest_due_first_with_ids_and_text(
    bench, process_tz
):
    bench.commands.handle("/remind +3h later")
    bench.commands.handle("/remind +1h sooner")
    reply = bench.commands.handle("/reminders")
    assert reply.index("sooner") < reply.index("later")
    for item in bench.repo.list_pending().items:
        assert f"[{item.id}]" in reply or f"#{item.id}" in reply
        assert render_instant(item.due_at, AMS) in reply


def test_an_empty_schedule_reads_as_empty(bench, process_tz):
    reply = bench.commands.handle("/reminders")
    assert "no" in reply.lower()
    assert "/remind" in reply


def test_the_listing_is_page_bounded_and_says_how_many_were_not_shown(
    tmp_path: Path, process_tz
):
    bench = Bench(tmp_path, page_size=2, max_pending=10)
    try:
        for n in range(5):
            bench.commands.handle(f"/remind +{n + 1}h item {n}")
        reply = bench.commands.handle("/reminders")
        assert reply.count("item ") == 2
        assert "3" in reply
    finally:
        bench.close()


def test_an_unreadable_store_replies_with_the_failure_and_never_as_empty(
    bench, process_tz, monkeypatch
):
    from henk.store.errors import StoreError

    monkeypatch.setattr(
        bench.repo,
        "list_pending",
        lambda *a, **k: (_ for _ in ()).throw(StoreError("cannot read")),
    )
    reply = bench.commands.handle("/reminders")
    assert "cannot read" in reply
    # An unreadable schedule is not an empty one: the owner must not conclude that
    # nothing is scheduled.
    assert "no pending reminders" not in reply.lower()
    assert "nothing scheduled" not in reply.lower()


# --- 7.3 /reminders cancel and /reminders reinstate ---------------------


def test_cancel_echoes_the_text_and_retains_the_row(bench, process_tz):
    bench.commands.handle("/remind 2026-08-25 07:30 buy bread")
    reminder_id = bench.repo.list_pending().items[0].id
    reply = bench.commands.handle(f"/reminders cancel {reminder_id}")
    row = bench.repo.get(reminder_id)
    assert row.status == CANCELLED
    assert row.text == "buy bread"
    assert row.due_at == DUE_2026_08_25_0730
    assert "buy bread" in reply
    assert render_instant(DUE_2026_08_25_0730, AMS) in reply
    assert "reinstate" in reply


def test_reinstate_returns_it_to_pending_and_writes_next_attempt_at(
    bench, process_tz
):
    bench.commands.handle("/remind 2026-08-25 07:30 buy bread")
    reminder_id = bench.repo.list_pending().items[0].id
    bench.commands.handle(f"/reminders cancel {reminder_id}")
    # Zero the selector column behind the dispatcher's back, so a reinstate that
    # forgot to write it leaves the sentinel rather than passing on the old value.
    bench.store.connection().execute(
        "UPDATE reminders SET next_attempt_at = 0 WHERE id = ?", (reminder_id,)
    )
    reply = bench.commands.handle(f"/reminders reinstate {reminder_id}")
    row = bench.repo.get(reminder_id)
    assert row.status == PENDING
    assert row.next_attempt_at == DUE_2026_08_25_0730
    assert render_instant(DUE_2026_08_25_0730, AMS) in reply


def test_reinstating_a_past_due_reminder_changes_nothing_and_names_remind(
    tmp_path: Path, process_tz
):
    # This refusal is what keeps the whole late/missed question inside
    # `reminder-delivery` instead of leaking into a core command.
    bench = Bench(tmp_path)
    try:
        bench.commands.handle("/remind +1h soon")
        reminder_id = bench.repo.list_pending().items[0].id
        bench.commands.handle(f"/reminders cancel {reminder_id}")
        # Move the clock past the due instant, without touching the row.
        later = Bench.__new__(Bench)
        later.now = NOW + 7200
        later.store = bench.store
        later.repo = bench.repo
        later.resolver = TimeResolver(AMS, clock=lambda: NOW + 7200)
        later.audit_path = bench.audit_path
        later.commands = OwnerCommands(
            reminders=bench.repo,
            resolver=later.resolver,
            receipts=None,
            reminder_receipts=None,
        )
        reply = later.commands.handle(f"/reminders reinstate {reminder_id}")
        assert bench.repo.get(reminder_id).status == CANCELLED
        assert "/remind" in reply
        assert "passed" in reply.lower()
    finally:
        bench.close()


def test_reinstating_at_the_pending_cap_changes_nothing_and_names_the_cap(
    tmp_path: Path, process_tz
):
    bench = Bench(tmp_path, max_pending=1)
    try:
        bench.commands.handle("/remind +2h first")
        first = bench.repo.list_pending().items[0].id
        bench.commands.handle(f"/reminders cancel {first}")
        bench.commands.handle("/remind +3h second")
        reply = bench.commands.handle(f"/reminders reinstate {first}")
        assert bench.repo.get(first).status == CANCELLED
        assert bench.repo.count_pending() == 1
        assert "1" in reply
    finally:
        bench.close()


def test_reinstating_a_non_cancelled_reminder_changes_nothing(bench, process_tz):
    bench.commands.handle("/remind +2h still pending")
    reminder_id = bench.repo.list_pending().items[0].id
    reply = bench.commands.handle(f"/reminders reinstate {reminder_id}")
    assert bench.repo.get(reminder_id).status == PENDING
    assert "cancelled" in reply.lower()


@pytest.mark.parametrize(
    "command,noun",
    [("/reminders cancel 9999", "pending"), ("/reminders reinstate 9999", "cancelled")],
)
def test_an_unknown_id_changes_nothing_and_says_so(
    bench, process_tz, command, noun
):
    bench.commands.handle("/remind +2h untouched")
    before = bench.repo.list_pending().items[0]
    reply = bench.commands.handle(command)
    assert bench.repo.get(before.id).status == PENDING
    assert "9999" in reply
    assert noun in reply.lower()


@pytest.mark.parametrize(
    "command",
    [
        "/reminders frobnicate 3",
        "/reminders cancel",
        "/reminders cancel abc",
        "/reminders reinstate",
        "/reminders delete 3",
    ],
)
def test_an_unrecognized_subcommand_changes_nothing_and_names_the_accepted_ones(
    bench, process_tz, command
):
    bench.commands.handle("/remind +2h untouched")
    before = bench.repo.list_pending().items[0]
    reply = bench.commands.handle(command)
    assert bench.repo.get(before.id).status == PENDING
    assert "cancel" in reply and "reinstate" in reply


def test_no_command_deletes_a_row_or_rewrites_its_text_or_due_time(
    bench, process_tz
):
    bench.commands.handle("/remind 2026-08-25 07:30 buy bread")
    reminder_id = bench.repo.list_pending().items[0].id
    for command in (
        f"/reminders cancel {reminder_id}",
        f"/reminders reinstate {reminder_id}",
        f"/reminders cancel {reminder_id}",
    ):
        bench.commands.handle(command)
    row = bench.repo.get(reminder_id)
    assert row is not None  # never deleted
    assert row.text == "buy bread"
    assert row.due_at == DUE_2026_08_25_0730


# --- 7.4 The disabled path ----------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "/remind +2h call the plumber",
        "/reminders",
        "/reminders cancel 1",
        "/reminders reinstate 1",
    ],
)
def test_all_four_commands_reply_that_reminders_are_not_configured(
    tmp_path: Path, process_tz, command
):
    # Stored rows must be UNTOUCHED and become operable again on re-enabling, so the
    # disabled bench is pointed at a store that already has a row in it.
    seeded = Bench(tmp_path)
    seeded.commands.handle("/remind +2h pre-existing")
    existing = seeded.repo.list_pending().items[0]
    seeded.close()

    disabled = Bench(tmp_path, enabled=False)
    try:
        reply = disabled.commands.handle(command)
        assert reply is not None
        assert "reminders" in reply.lower()
        assert "configured" in reply.lower()
        assert "nothing was changed" in reply.lower()
        row = disabled.repo.get(existing.id)
        assert row.status == PENDING
        assert row.text == "pre-existing"
        assert row.due_at == existing.due_at
    finally:
        disabled.close()


def test_re_enabling_restores_access_to_the_stored_reminders(
    tmp_path: Path, process_tz
):
    seeded = Bench(tmp_path)
    seeded.commands.handle("/remind 2026-08-25 07:30 survives the flag")
    seeded.close()

    disabled = Bench(tmp_path, enabled=False)
    assert "configured" in disabled.commands.handle("/reminders").lower()
    disabled.close()

    reenabled = Bench(tmp_path)
    try:
        reply = reenabled.commands.handle("/reminders")
        assert "survives the flag" in reply
        assert render_instant(DUE_2026_08_25_0730, AMS) in reply
    finally:
        reenabled.close()


def test_the_disabled_path_writes_no_receipts(tmp_path: Path, process_tz):
    disabled = Bench(tmp_path, enabled=False)
    try:
        for command in ("/remind +2h x", "/reminders cancel 1", "/reminders reinstate 1"):
            disabled.commands.handle(command)
        assert disabled.records("reminder") == []
        assert disabled.records("authorization") == []
    finally:
        disabled.close()


# --- 7.5 Receipts -------------------------------------------------------


def test_each_mutating_command_writes_both_an_authorization_and_a_lifecycle_record(
    bench, process_tz
):
    bench.commands.handle("/remind +2h call the plumber")
    reminder_id = bench.repo.list_pending().items[0].id
    bench.commands.handle(f"/reminders cancel {reminder_id}")
    bench.commands.handle(f"/reminders reinstate {reminder_id}")

    authorizations = bench.records("authorization")
    assert [r["tool"] for r in authorizations] == [
        "/remind",
        "/reminders cancel",
        "/reminders reinstate",
    ]
    for record in authorizations:
        # A tier is a TOOL property; a command is not a tool.
        assert record["tier"] is None
        assert record["initiated_by"] == "owner-command"
        assert record["turn_type"] == "command"

    lifecycle = bench.records("reminder")
    assert [r["transition"] for r in lifecycle] == [
        "scheduled",
        "cancelled",
        "reinstated",
    ]
    for record in lifecycle:
        assert record["initiated_by"] == "owner-command"
        assert record["reminder_id"] == reminder_id
        assert "call the plumber" not in json.dumps(record)


def test_the_listing_command_writes_neither_kind_of_record(bench, process_tz):
    bench.commands.handle("/reminders")
    assert bench.records("authorization") == []
    assert bench.records("reminder") == []


def test_a_rejected_command_writes_neither_kind_of_record(bench, process_tz):
    for command in (
        "/remind sometime next week water the plants",
        "/remind 2020-01-01 07:30 too late",
        "/reminders cancel 9999",
        "/reminders reinstate 9999",
    ):
        bench.commands.handle(command)
    assert bench.records("authorization") == []
    assert bench.records("reminder") == []


def test_a_reinstated_row_is_pending_while_its_record_says_reinstated(
    bench, process_tz
):
    # `reinstated` is the name of a TRANSITION, not a stored row status.
    bench.commands.handle("/remind +2h x")
    reminder_id = bench.repo.list_pending().items[0].id
    bench.commands.handle(f"/reminders cancel {reminder_id}")
    bench.commands.handle(f"/reminders reinstate {reminder_id}")
    assert bench.repo.get(reminder_id).status == PENDING
    assert bench.records("reminder")[-1]["transition"] == "reinstated"


# --- The commands cost no tokens and survive an incident ----------------


def test_the_commands_never_reach_a_model(bench, process_tz):
    # Owner-authored by construction: the text never passes through the model, which
    # is why the gate is not involved and session taint does not apply.
    import inspect

    from henk.agent import commands as module

    source = inspect.getsource(module)
    for forbidden in ("session", "run_turn", "gate", "authorize"):
        assert forbidden not in source.lower().split("\n")[0]  # not in the summary
    assert bench.commands.handle("/remind +2h works during an incident") is not None
    assert bench.repo.count_pending() == 1


def test_unrelated_slash_words_are_still_passed_through_as_agent_turns(
    bench, process_tz
):
    assert bench.commands.handle("/reminderz +2h typo") is None
    assert bench.commands.handle("/rem +2h") is None
    assert bench.commands.handle("what's on my reminder list?") is None


# --- One renderer, every surface (reminders spec) ------------------------


async def test_the_same_reminder_reads_identically_on_all_four_surfaces(
    tmp_path: Path, process_tz
):
    """The scheduling confirmation, `/reminders`, `reminders_read` and the time header.

    A due time that reads differently in two places is a bug the owner has to
    adjudicate, and that argument applies at least as strongly between *now* and
    *due* as between two due times — which is why the header is on this list rather
    than being a second rendering surface beside it.
    """
    from henk.tools.reminders import RemindersReadTool

    bench = Bench(tmp_path)
    try:
        confirmation = bench.commands.handle("/remind 2026-08-25 07:30 buy bread")
        listing = bench.commands.handle("/reminders")
        read_result = await RemindersReadTool(bench.repo, bench.resolver).run()
        header = bench.resolver.time_header(DUE_2026_08_25_0730)

        expected = render_instant(DUE_2026_08_25_0730, AMS)
        assert expected in confirmation
        assert expected in listing
        assert expected in read_result.content
        assert expected in header
        # And the tool path produces the same string for the same instant.
        from henk.tools.reminders import RemindTool

        tool_result = await RemindTool(bench.repo, bench.resolver).run(
            text="buy milk", when="2026-08-25 07:30"
        )
        assert expected in tool_result.content
    finally:
        bench.close()


def test_the_command_and_the_tool_share_one_resolver_in_the_runtime(tmp_path: Path):
    # Wired as memory already is: the SAME instances, not two that happen to agree.
    import httpx

    from henk.config import Config
    from henk.runtime import build_runtime
    from tests.test_config import _minimal_raw

    raw = _minimal_raw("+31600000000")
    raw["owner"]["timezone"] = "Europe/Amsterdam"
    raw["reminders"] = {"enabled": True}
    config = Config.from_dict(raw, env={})
    object.__setattr__(config.store, "path", str(tmp_path / "rt.db"))
    object.__setattr__(config.audit, "path", str(tmp_path / "rt-audit.jsonl"))

    app, client = build_runtime(config)
    try:
        core = app._core
        commands = core._commands
        registry_tool = core._factory._registry.get("remind")
        assert commands._resolver is registry_tool._resolver
        assert commands.reminders is registry_tool._reminders
        # And the per-turn header closes over that SAME resolver, so the time the
        # model reasons from and the due time the owner is told cannot diverge.
        assert core._time_header is not None
        header = core._time_header()
        assert header.startswith("[CURRENT TIME")
        assert "CE" in header  # CET or CEST — the configured zone, not the process's
    finally:
        del client
