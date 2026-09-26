"""Guards for owner acknowledgement, end to end through ``App.run`` (task group 8).

From specs/channel-adapter: *The channel reference never reaches the agent core*,
*The channel reference is never audited, persisted, sent or put in a turn*, *Stranger
gets nothing with acknowledgement enabled* and *Acknowledgements are not messages*;
design D2 and D10 say what each guard pins.

**The harness.** No other test drives ``App.run`` over a real ``SignalAdapter``: the
existing ``App.run`` tests use a one-shot adapter, and a plain ``FakeBridge`` replays
its script forever. Here a ``FakeBridge(script, hold_open=True)`` sits under a real
``SignalAdapter``, and one real ``OwnerAcknowledgement`` is wired into both the
Dispatcher and the core the way ``build_runtime`` wires it: on the App's adapter,
paused by the core's gate, its ``working`` as the core's bracket. The audit log and
the store live under ``tmp_path``; the store holds one memory, so the first turn
carries a recall block and the store file is really read and written.

``App.run`` runs as a task until the last owner DM's stop has completed at the
bridge. The task is then cancelled and awaited, as ``_cancel`` does; its ``finally``
calls ``core.aclose()``, which is what flushes the session's audit record, so every
file check runs only after that await. The whole drive is bounded (standing rule 7).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from henk.agent.commands import OwnerCommands
from henk.agent.core import AgentCore
from henk.agent.recall import MemoryRecall
from henk.agent.turns import OwnerTurn
from henk.app import App, Dispatcher
from henk.audit import AuditLog, MutationReceipts
from henk.channel.acknowledge import OwnerAcknowledgement
from henk.channel.allowlist import AllowlistFilter
from henk.channel.base import split_message
from henk.channel.signal import TYPING_REFRESH_SECONDS, SignalAdapter
from henk.config import StoreConfig
from henk.events.coordinator import EventCoordinator
from henk.events.intake import EventIntake
from henk.events.pipeline import EventPipeline, PipelineConfig
from henk.events.types import Event
from henk.gate.approval import ApprovalGate
from henk.store import build_stores
from tests.conftest import (
    TRIAGE_REPLY,
    EventSessionFactory,
    FakeBridge,
    FakeSessionFactory,
)
from tests.test_app import ACCOUNT, STRANGER, _cancel, _until

OWNER = "+31600000000"
#: The owner envelope's timestamp in the sentinel scan. Thirteen digits, like a
#: real millisecond timestamp, but in 2032: no clock value of this run, no id and
#: no other envelope's timestamp can contain it, and a float seconds form
#: (``1987654321.987``) cannot either, so a hit can only be the reference.
SENTINEL = 1987654321987
#: The whole drive's fail-fast bound (standing rule 7).
DRIVE_BOUND = 5.0
#: The acknowledge bound. Nothing in these runs hangs, so it only has to be real.
ACK_TIMEOUT = 0.5
MEMORY = "the owner keeps the spare Pi in the hallway cupboard"
ALLOWLIST_LOGGER = "henk.channel.allowlist"


@pytest.fixture(autouse=True)
async def no_leftover_tasks():
    """``App.run``'s shutdown must leave nothing running: no worker, no indicator."""
    yield
    await asyncio.sleep(0)
    leftovers = asyncio.all_tasks() - {asyncio.current_task()}
    for task in leftovers:  # clean up, so one failure does not cascade
        task.cancel()
    if leftovers:
        await asyncio.wait(leftovers, timeout=DRIVE_BOUND)
    assert not leftovers, f"tasks left running: {leftovers}"


def _dm(text: str, timestamp: int, *, source: str = OWNER) -> dict:
    """An envelope as signal-cli-rest-api reports it: both timestamps set."""
    return {
        "envelope": {
            "source": source,
            "sourceUuid": source,
            "timestamp": timestamp,
            "dataMessage": {"message": text, "timestamp": timestamp},
        }
    }


def _group(text: str, timestamp: int, *, source: str = OWNER) -> dict:
    return {
        "envelope": {
            "source": source,
            "timestamp": timestamp,
            "dataMessage": {
                "message": text,
                "timestamp": timestamp,
                "groupInfo": {"groupId": "g1"},
            },
        }
    }


def _harness(
    tmp_path: Path,
    script: list,
    *,
    acknowledge: bool = True,
    reply: str = "ok",
    safe_length: int = 2000,
) -> SimpleNamespace:
    """The App, wired as ``build_runtime`` wires it, over a held-open FakeBridge."""
    bridge = FakeBridge(script, hold_open=True)
    adapter = SignalAdapter(
        bridge, account=ACCOUNT, owner=OWNER, safe_length=safe_length
    )
    audit_path = tmp_path / "audit" / "audit.jsonl"
    audit = AuditLog(audit_path)
    receipts = MutationReceipts(audit)
    store_path = tmp_path / "audit" / "henk-store.db"
    stores = build_stores(StoreConfig(path=str(store_path)))
    stores.memories.add(MEMORY)
    gate = ApprovalGate(adapter, timeout_seconds=5, recorder=receipts)
    acknowledgement = (
        OwnerAcknowledgement(
            adapter,
            timeout=ACK_TIMEOUT,
            refresh_seconds=TYPING_REFRESH_SECONDS,
            paused=gate.has_pending,
        )
        if acknowledge
        else None
    )
    factory = FakeSessionFactory(reply)
    core = AgentCore(
        factory,
        adapter,
        audit=audit,
        gate=gate,
        receipts=receipts,
        commands=OwnerCommands(
            memories=stores.memories, inbox=stores.inbox, receipts=receipts
        ),
        recall=MemoryRecall(stores.memories),
        working_indicator=(
            acknowledgement.working if acknowledgement is not None else None
        ),
    )
    dispatcher = Dispatcher(
        AllowlistFilter(OWNER), gate, core, acknowledgement=acknowledgement
    )
    return SimpleNamespace(
        app=App(adapter, dispatcher, core),
        bridge=bridge,
        factory=factory,
        stores=stores,
        audit_path=audit_path,
        store_path=store_path,
    )


async def _drive(h: SimpleNamespace, done, *, settle: int = 0) -> None:
    """Run ``App.run`` until ``done()``, then cancel and await it like ``_cancel``.

    The await is what runs ``App.run``'s ``finally`` (and so ``core.aclose()``,
    which flushes the session's audit record). The store is closed afterwards, so
    its file holds everything it was given.
    """

    async def run() -> None:
        task = asyncio.create_task(h.app.run())
        try:
            await _until(done)
            for _ in range(settle):
                await asyncio.sleep(0)
        finally:
            await _cancel(task)

    try:
        await asyncio.wait_for(run(), DRIVE_BOUND)
    finally:
        h.stores.store.close()


def _stopped(h: SimpleNamespace, owner_dms: int = 1):
    """The drive's end: the last owner DM's stop has completed at the bridge."""
    return lambda: h.bridge.typing.count(("stop", OWNER)) == owner_dms


def _files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


# --- 8.1 The reference never reaches the core -------------------------------


def test_the_owner_turn_carries_the_text_alone():
    # Scenario: The channel reference never reaches the agent core. Structural on
    # purpose (design D2, finding 10): a change that queued the InboundMessage, or
    # the reference beside the text, would break the rule without touching one line
    # of channel_ref code, and no behavioural test would notice until it leaked.
    rule = (
        "design D2 / finding 10: the agent core receives an owner message as its "
        "text alone, so no channel reference can be queued into a turn. Keep the "
        "reference in the dispatcher; do not widen the owner turn or submit()."
    )
    assert [f.name for f in dataclasses.fields(OwnerTurn)] == ["text"], rule
    params = list(inspect.signature(AgentCore.submit).parameters)
    assert params[0] == "self"
    assert params[1:] == ["text"], rule


# --- 8.2 The sentinel scan --------------------------------------------------


async def test_the_channel_reference_is_never_audited_persisted_sent_or_put_in_a_turn(
    tmp_path: Path,
):
    # Scenario: The channel reference is never audited, persisted, sent or put in
    # a turn. Logs are deliberately not scanned (design D2): a log line may name a
    # timestamp, as it may today.
    h = _harness(tmp_path, [_dm("hello", SENTINEL)])
    await _drive(h, _stopped(h))
    needle = str(SENTINEL)

    # It reached the receipt, so the scan below is not vacuous.
    assert h.bridge.receipts == [(OWNER, SENTINEL)]

    # Every byte of every file the run left under tmp_path: the audit log and the
    # store (with its journal files, if any are left).
    files = _files_under(tmp_path)
    assert h.audit_path in files and h.store_path in files, files
    records = [json.loads(line) for line in h.audit_path.read_text().splitlines()]
    sessions = [r for r in records if r["record_type"] == "session"]
    assert len(sessions) == 1 and sessions[0]["turn_count"] == 1, records
    assert MEMORY.encode() in h.store_path.read_bytes()
    leaked = [str(p) for p in files if needle.encode() in p.read_bytes()]
    assert leaked == [], f"the channel reference was persisted in {leaked}"

    # Every session turn's content. The recall block proves the store was read.
    [session] = h.factory.created
    assert len(session.turns) == 1 and MEMORY in session.turns[0]
    assert "hello" in session.turns[0]
    assert all(needle not in turn for turn in session.turns), session.turns

    # Every outbound message, recipient and text.
    assert h.bridge.sends, "no reply was sent, so the send check would be vacuous"
    assert all(
        needle not in recipient and needle not in text
        for recipient, text in h.bridge.sends
    ), h.bridge.sends


# --- 8.4 The stranger, end to end, acknowledgement enabled -------------------


async def test_a_stranger_and_a_group_message_get_nothing_end_to_end(
    tmp_path: Path, caplog
):
    # Scenario: Stranger gets nothing with acknowledgement enabled. The owner DM
    # comes last, so the drive's stop condition is reached only after both drops.
    owner_ts = 1700000000303
    script = [
        _dm("hello from a stranger", 1700000000301, source=STRANGER),
        _group("hello group", 1700000000302),
        _dm("hello", owner_ts),
    ]
    h = _harness(tmp_path, script)
    with caplog.at_level(logging.WARNING, logger=ALLOWLIST_LOGGER):
        await _drive(h, _stopped(h))
    bridge = h.bridge

    assert bridge.receipts == [(OWNER, owner_ts)]
    assert bridge.typing, "no indicator at all: the owner turn was not bracketed"
    assert all(recipient == OWNER for _, recipient in bridge.typing), bridge.typing
    # Nothing addressed to the stranger in any of the bridge's records, attempts
    # that were refused or never completed included.
    addressed = (
        [recipient for recipient, _ in bridge.sends]
        + [recipient for recipient, _ in bridge.receipts]
        + [recipient for _, recipient in bridge.typing]
        + [recipient for _, recipient in bridge.ack_attempts]
    )
    assert STRANGER not in addressed, addressed
    assert set(addressed) == {OWNER}, addressed
    assert [op for op, _ in bridge.ack_attempts].count("receipt") == 1
    # Only the owner DM became a turn.
    [session] = h.factory.created
    assert len(session.turns) == 1 and session.turns[0].endswith("hello")
    assert [
        r.getMessage() for r in caplog.records if r.name == ALLOWLIST_LOGGER
    ] == [
        f"dropped message from non-owner sender={STRANGER}",
        f"dropped group message from sender={OWNER}",
    ]


# --- 8.5 Acknowledgements are not messages ----------------------------------

#: Long enough that the reply splits into several chunks at SAFE_LENGTH.
LONG_REPLY = "\n\n".join(f"paragraph {i}: " + "word " * 12 for i in range(4))
SAFE_LENGTH = 80


async def _reply_run(root: Path, *, acknowledge: bool) -> SimpleNamespace:
    """One owner DM with a multi-chunk reply, through the harness."""
    root.mkdir()
    h = _harness(
        root,
        [_dm("hello", 1700000000401)],
        acknowledge=acknowledge,
        reply=LONG_REPLY,
        safe_length=SAFE_LENGTH,
    )
    if acknowledge:
        await _drive(h, _stopped(h))
        return h

    # Disabled: there is no stop to wait for. Wait for the reply's last chunk, then
    # let the loop run on before cancelling, so a stray acknowledgement after the
    # reply would have been recorded.
    def replied() -> bool:
        if not h.factory.created or not h.factory.created[0].turns:
            return False
        reply = f"{LONG_REPLY}:{h.factory.created[0].turns[0]}"
        return len(h.bridge.sends) >= len(split_message(reply, SAFE_LENGTH))

    await _drive(h, replied, settle=50)
    return h


async def test_acknowledgements_are_not_messages(tmp_path: Path):
    # Scenario: Acknowledgements are not messages. The sends that reach the bridge
    # are exactly the reply's chunks, with acknowledgement on and off alike.
    runs = {
        enabled: await _reply_run(tmp_path / name, acknowledge=enabled)
        for name, enabled in (("enabled", True), ("disabled", False))
    }
    for enabled, h in runs.items():
        [session] = h.factory.created
        reply = f"{LONG_REPLY}:{session.turns[0]}"
        chunks = split_message(reply, SAFE_LENGTH)
        assert len(chunks) > 1, "the reply must span several chunks"
        assert h.bridge.sends == [(OWNER, c) for c in chunks], enabled
    assert runs[True].bridge.sends == runs[False].bridge.sends

    enabled = runs[True].bridge
    assert enabled.receipts == [(OWNER, 1700000000401)]
    assert enabled.typing[0] == ("start", OWNER)
    assert enabled.typing[-1] == ("stop", OWNER)

    disabled = runs[False].bridge
    assert disabled.receipts == []
    assert disabled.typing == []
    assert disabled.ack_attempts == []


# --- triage-working-indicator 2.1: the alert cap decides the indicator -------


class _SequencedBridge(FakeBridge):
    """A held-open ``FakeBridge`` that also records every completed send and typing
    request in one ordered list, so "the stop came after the triage's send" is a
    list-order assertion."""

    def __init__(self) -> None:
        super().__init__(hold_open=True)
        self.sequence: list[tuple[str, str]] = []

    async def send(self, recipient: str, text: str) -> None:
        await super().send(recipient, text)
        self.sequence.append(("send", recipient))

    async def start_typing(self, recipient: str) -> None:
        await super().start_typing(recipient)
        self.sequence.append(("start", recipient))

    async def stop_typing(self, recipient: str) -> None:
        await super().stop_typing(recipient)
        self.sequence.append(("stop", recipient))


def _alert(event_id: str, title: str) -> Event:
    return Event(id=event_id, title=title, message="triggered", arrival_time=0.0)


def _session_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in records if r.get("record_type") == "session"]


async def test_the_alert_cap_decides_which_triage_shows_the_indicator(tmp_path: Path):
    # Scenarios (channel-adapter): A triage the owner will receive shows the
    # indicator; A triage the owner will not receive shows nothing. The real cap
    # (cap_per_24h=1) decides, through EventCoordinator.dispatch_batch, and the
    # real core worker runs both triages over a real SignalAdapter + FakeBridge.
    bridge = _SequencedBridge()
    adapter = SignalAdapter(bridge, account=ACCOUNT, owner=OWNER, safe_length=SAFE_LENGTH)
    audit_path = tmp_path / "audit" / "audit.jsonl"
    audit = AuditLog(audit_path)
    gate = ApprovalGate(adapter, timeout_seconds=5)
    acknowledgement = OwnerAcknowledgement(
        adapter,
        timeout=ACK_TIMEOUT,
        refresh_seconds=TYPING_REFRESH_SECONDS,
        paused=gate.has_pending,
    )
    factory = EventSessionFactory()
    core = AgentCore(
        factory, adapter, audit=audit, gate=gate, working_indicator=acknowledgement.working
    )
    coordinator = EventCoordinator(
        EventIntake.__new__(EventIntake),  # intake unused by dispatch_batch
        EventPipeline(PipelineConfig(cap_per_24h=1)),
        core,
        audit=audit,
    )

    async def drive() -> None:
        worker = asyncio.create_task(core.run())
        try:
            await coordinator.dispatch_batch([_alert("e1", "Gatus: svc/api")], now=0.0)
            await coordinator.dispatch_batch([_alert("e2", "Gatus: svc/db")], now=60.0)
            await _until(lambda: len(_session_records(audit_path)) == 2)
            for _ in range(50):  # a stray request after the second triage would land
                await asyncio.sleep(0)
        finally:
            await _cancel(worker)

    await asyncio.wait_for(drive(), DRIVE_BOUND)
    await core.aclose()

    first, second = _session_records(audit_path)
    assert first["announceable"] is True
    assert second["announceable"] is False  # held back by the cap, still recorded
    assert len(factory.created) == 2  # both triages ran
    chunks = split_message(TRIAGE_REPLY, SAFE_LENGTH)
    assert len(chunks) > 1, "the triage must span several chunks"
    assert bridge.sends == [(OWNER, c) for c in chunks]  # the first triage alone
    # Exactly one start...stop span, all owner-addressed.
    assert bridge.typing == [("start", OWNER), ("stop", OWNER)]
    assert {recipient for _, recipient in bridge.ack_attempts} == {OWNER}
    # The span closes after the first triage's last chunk, and nothing follows it:
    # no send and no typing request for the cap-suppressed triage.
    kinds = [kind for kind, _ in bridge.sequence]
    last_send = max(i for i, kind in enumerate(kinds) if kind == "send")
    assert kinds.index("start") < kinds.index("stop")
    assert kinds.index("stop") > last_send
    assert kinds[-1] == "stop"
