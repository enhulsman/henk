"""Capture-inbox tests (task 5.1), from specs/capture-inbox.

The headline verb of this change: one message or one command away, durable before
the confirmation, never silently evicted, drained oldest-first. Tool-level tests
run against BOTH backends (SQLite and a test double implementing the same seam), so
the spec's "backend swap is behavior-invariant" scenario is proven at the level it
is written about.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from henk.agent.commands import OwnerCommands
from henk.agent.core import AgentCore
from henk.agent.turns import EventTurn, EventTurnItem
from henk.audit import AuditLog, MutationReceipts
from henk.events.identity import derive_identity
from henk.events.types import Event
from henk.gate.approval import ApprovalGate, TurnContext, gated_invoke
from henk.store import SqliteInboxStore, Store, StoreError
from henk.tools.base import AuthorizationTier, ToolClass, TurnType
from henk.tools.capture import MAX_READ_LIMIT, CaptureTool, InboxReadTool
from tests.conftest import EventSessionFactory, FakeChannel, make_clock
from tests.test_store_seam import FakeInboxStore


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _authorizations(path: Path) -> list[dict]:
    return [r for r in _records(path) if r["record_type"] == "authorization"]


@pytest.fixture(params=["sqlite", "double"])
def inbox(request, tmp_path: Path):
    """The same tool tests, once per backend behind the seam."""
    if request.param == "sqlite":
        store = Store(tmp_path / "store" / "henk.db")
        yield SqliteInboxStore(store)
        store.close()
    else:
        counter = iter(range(1, 10_000))
        yield FakeInboxStore(clock=lambda: float(next(counter)))


# --- The capture tool -----------------------------------------------------


def test_capture_is_declared_standing_and_owner_turn_only():
    tool = CaptureTool(None)
    assert tool.tool_class is ToolClass.MUTATING
    assert tool.authorization is AuthorizationTier.STANDING
    assert tool.turn_scope == (TurnType.OWNER,)


async def test_capture_appends_and_confirms_with_the_item_id(inbox):
    result = await CaptureTool(inbox).run(text="buy bike lights")
    assert result.ok
    item = inbox.list_open().items[0]
    assert str(item.id) in result.content
    assert item.text == "buy bike lights"
    assert item.status == "open"


async def test_capture_records_its_source(inbox):
    await CaptureTool(inbox).run(text="a thought")
    assert inbox.list_open().items[0].source == "capture-tool"


async def test_empty_capture_fails_safe(inbox):
    result = await CaptureTool(inbox).run(text="   ")
    assert result.ok is False
    assert inbox.list_open().items == ()


async def test_failed_capture_is_never_reported_as_success():
    broken = FakeInboxStore(fail=True)
    result = await CaptureTool(broken).run(text="a thought")
    assert result.ok is False
    assert "captured" not in (result.content or "").lower()


async def test_capture_never_prompts_in_an_untainted_owner_conversation(
    inbox, tmp_path: Path
):
    channel = FakeChannel()
    path = tmp_path / "audit.jsonl"
    gate = ApprovalGate(channel, recorder=MutationReceipts(AuditLog(path)))
    gate.enter_turn(TurnContext(turn_type=TurnType.OWNER))
    result = await gated_invoke(gate, CaptureTool(inbox), {"text": "buy bike lights"})
    assert result.ok
    assert channel.sent == []  # standing tier: stored without asking
    receipt = _authorizations(path)[0]
    assert (receipt["tool"], receipt["tier"], receipt["outcome"]) == (
        "capture",
        "standing",
        "authorized",
    )


async def test_capture_denied_during_a_triage_turn(inbox):
    channel = FakeChannel()
    gate = ApprovalGate(channel)
    gate.enter_turn(TurnContext(turn_type=TurnType.EVENT, announceable=True))
    result = await gated_invoke(gate, CaptureTool(inbox), {"text": "planted"})
    assert result.ok is False
    assert inbox.list_open().items == ()
    assert channel.sent == []


def test_capture_durability_survives_a_sigkill(tmp_path: Path):
    # The spec's scenario, literally: killed non-gracefully AFTER the tool result,
    # the item is present on restart. Nothing in the write path may depend on a
    # graceful close.
    db = tmp_path / "store" / "henk.db"
    script = (
        "import asyncio, os, signal, sys;"
        "sys.path.insert(0, %r);"
        "from henk.store import SqliteInboxStore, Store;"
        "from henk.tools.capture import CaptureTool;"
        "tool = CaptureTool(SqliteInboxStore(Store(%r)));"
        "r = asyncio.run(tool.run(text='survive the kill'));"
        "assert r.ok, r.error;"
        "os.kill(os.getpid(), signal.SIGKILL)"
    ) % (str(Path.cwd()), str(db))
    completed = subprocess.run([sys.executable, "-c", script])
    assert completed.returncode == -9  # genuinely killed, nothing unwound

    reopened = SqliteInboxStore(Store(db))
    items = reopened.list_open().items
    assert [(it.text, it.status) for it in items] == [("survive the kill", "open")]


# --- inbox_read -----------------------------------------------------------


async def test_inbox_read_lists_a_captured_item_with_id_text_and_time(inbox):
    await CaptureTool(inbox).run(text="buy bike lights")
    result = await InboxReadTool(inbox).run()
    item = inbox.list_open().items[0]
    assert result.ok
    assert f"#{item.id}" in result.content
    assert "buy bike lights" in result.content
    from henk.store import format_created_at

    assert format_created_at(item.created_at) in result.content


async def test_inbox_read_is_oldest_first_with_a_newer_remainder(inbox):
    tool = CaptureTool(inbox)
    for i in range(25):
        await tool.run(text=f"item {i}")
    result = await InboxReadTool(inbox, page_size=20).run()
    assert "item 0" in result.content
    assert "item 19" in result.content
    assert "item 20" not in result.content
    assert "5 newer" in result.content


async def test_inbox_read_reports_an_empty_inbox_honestly(inbox):
    result = await InboxReadTool(inbox).run()
    assert result.ok
    assert "no open items" in result.content.lower()


async def test_unreadable_inbox_is_not_reported_as_empty():
    result = await InboxReadTool(FakeInboxStore(fail=True)).run()
    assert result.ok is False
    assert "empty" not in (result.content or "").lower()
    assert "could not be read" in (result.error or "")


async def test_inbox_read_clamps_its_limit(inbox):
    tool = CaptureTool(inbox)
    for i in range(5):
        await tool.run(text=f"item {i}")
    read = InboxReadTool(inbox, page_size=2)
    assert "item 2" not in (await read.run()).content  # page size honoured
    assert "item 2" in (await read.run(limit=MAX_READ_LIMIT + 1000)).content
    assert (await read.run(limit=0)).ok  # a nonsense limit never crashes the turn
    assert (await read.run(limit="lots")).ok


# --- The seam holds at the tool level ------------------------------------


async def test_tools_pass_the_same_behavioural_tests_on_either_backend(inbox):
    # The spec's scenario stated as one test: nothing above this line knows which
    # backend it ran against, and this is the assertion that says so out loud.
    capture, read = CaptureTool(inbox), InboxReadTool(inbox, page_size=20)
    assert (await capture.run(text="one")).ok
    assert (await capture.run(text="two")).ok
    listing = (await read.run()).content
    assert listing.index("one") < listing.index("two")  # oldest first, both backends


def test_inbox_persists_across_a_store_reopen(tmp_path: Path):
    store = Store(tmp_path / "store" / "henk.db")
    SqliteInboxStore(store).append("persisted")
    store.close()
    assert [
        it.text for it in SqliteInboxStore(Store(tmp_path / "store" / "henk.db"))
        .list_open().items
    ] == ["persisted"]


# --- Owner commands ------------------------------------------------------


def _commands(inbox, *, receipts=None, page_size: int = 20) -> OwnerCommands:
    return OwnerCommands(inbox=inbox, receipts=receipts, inbox_page_size=page_size)


def test_capture_command_confirms_with_the_item_id(inbox):
    reply = _commands(inbox).handle("/capture buy bike lights")
    item = inbox.list_open().items[0]
    assert f"#{item.id}" in reply
    assert item.text == "buy bike lights"
    assert item.source == "owner-command"


def test_empty_capture_command_stores_nothing(inbox):
    reply = _commands(inbox).handle("/capture")
    assert "empty" in reply.lower()
    assert inbox.list_open().items == ()


def test_failed_capture_command_never_claims_success():
    reply = _commands(FakeInboxStore(fail=True)).handle("/capture a thought")
    assert "captured" not in reply.lower()
    assert "couldn't" in reply.lower()


def test_inbox_command_lists_the_oldest_twenty_and_counts_the_rest(inbox):
    commands = _commands(inbox, page_size=20)
    for i in range(25):
        commands.handle(f"/capture item {i}")
    reply = commands.handle("/inbox")
    assert "item 0" in reply and "item 19" in reply
    assert "item 20" not in reply
    assert "5 newer" in reply
    # The oldest item's id is shown and is exactly what /inbox done takes.
    oldest = inbox.list_open().items[0]
    assert f"[{oldest.id}]" in reply
    from henk.store import format_created_at

    assert format_created_at(oldest.created_at) in reply  # id, text AND time
    assert "Done:" in commands.handle(f"/inbox done {oldest.id}")


def test_inbox_all_lists_everything(inbox):
    commands = _commands(inbox, page_size=20)
    for i in range(25):
        commands.handle(f"/capture item {i}")
    reply = commands.handle("/inbox all")
    for i in range(25):
        assert f"item {i}" in reply
    assert "newer" not in reply


def test_inbox_done_removes_from_the_open_listing_without_deleting(tmp_path: Path):
    store = Store(tmp_path / "store" / "henk.db")
    sqlite_inbox = SqliteInboxStore(store)
    commands = _commands(sqlite_inbox)
    commands.handle("/capture drain me")
    item_id = sqlite_inbox.list_open().items[0].id
    reply = commands.handle(f"/inbox done {item_id}")
    assert "drain me" in reply
    assert "empty" in commands.handle("/inbox").lower()
    archived = sqlite_inbox.get(item_id)
    assert archived is not None and archived.status == "done"  # archived, not deleted
    store.close()


def test_inbox_done_with_an_unknown_id_changes_nothing(inbox):
    commands = _commands(inbox)
    commands.handle("/capture keep me")
    reply = commands.handle("/inbox done 9999")
    assert "no open inbox item" in reply.lower()
    assert len(inbox.list_open().items) == 1


def test_inbox_done_without_an_id_is_explicit(inbox):
    reply = _commands(inbox).handle("/inbox done")
    assert "needs an item id" in reply.lower()


def test_unknown_inbox_subcommand_is_explicit(inbox):
    reply = _commands(inbox).handle("/inbox everything")
    assert "/inbox all" in reply


def test_unreadable_inbox_command_is_not_reported_as_empty():
    reply = _commands(FakeInboxStore(fail=True)).handle("/inbox")
    assert "empty" not in reply.lower()
    assert "couldn't read" in reply.lower()


def test_no_eviction_under_growth(inbox):
    commands = _commands(inbox, page_size=20)
    for i in range(120):
        commands.handle(f"/capture item {i}")
    everything = commands.handle("/inbox all")
    for i in range(120):
        assert f"item {i}" in everything
    assert f"[{inbox.list_open().items[0].id}] item 0 " in everything


# --- Command receipts and turn independence ------------------------------


def test_capture_command_writes_a_receipt(inbox, tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    commands = _commands(inbox, receipts=MutationReceipts(AuditLog(path)))
    commands.handle("/capture buy bike lights")
    receipt = _authorizations(path)[0]
    assert receipt["tool"] == "/capture"
    assert receipt["initiated_by"] == "owner-command"
    assert receipt["turn_type"] == "command"
    assert receipt["tier"] is None


def test_inbox_done_receipt_only_when_something_changed(inbox, tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    commands = _commands(inbox, receipts=MutationReceipts(AuditLog(path)))
    commands.handle("/capture drain me")
    commands.handle("/inbox done 9999")  # no-op: no receipt
    assert [r["tool"] for r in _authorizations(path)] == ["/capture"]
    commands.handle(f"/inbox done {inbox.list_open().items[0].id}")
    assert [r["tool"] for r in _authorizations(path)] == ["/capture", "/inbox done"]


def test_read_only_inbox_commands_write_no_receipt(inbox, tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    commands = _commands(inbox, receipts=MutationReceipts(AuditLog(path)))
    commands.handle("/inbox")
    commands.handle("/inbox all")
    assert not path.exists() or _authorizations(path) == []


async def test_capture_command_costs_no_agent_turn_and_works_mid_triage(
    inbox, tmp_path: Path
):
    # Owner-authored input never passes through the model, so taint — which stops
    # the capture TOOL for the session's lifetime — does not stop the command.
    factory = EventSessionFactory()
    channel = FakeChannel()
    core = AgentCore(
        factory,
        channel,
        commands=_commands(inbox),
        clock=make_clock([0, 0]),
    )
    event = Event(id="e1", title="Gatus: svc/api", message="down", arrival_time=0.0)
    await core.process(
        EventTurn(
            items=(EventTurnItem(event=event, identity=derive_identity(event)),),
            announceable=True,
        )
    )
    sessions_before = factory.create_count
    await core.process("/capture buy bike lights mid-incident")
    assert factory.create_count == sessions_before  # no new session, no tokens
    assert [it.text for it in inbox.list_open().items] == [
        "buy bike lights mid-incident"
    ]
    assert any("buy bike lights mid-incident" in s for s in channel.sent)
