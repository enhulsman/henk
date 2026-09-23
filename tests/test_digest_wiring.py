"""The digest through the core: the record, recurrence, restart, owner exclusion,
and the toolset (triage-quality group 8).

From `specs/triage-handoff` (*The record names what history was shown*, *Owner
sessions never see handoff history*, *No tool exposes the archive*),
`specs/incident-triage` (*Recurrence of a recently triaged incident*, *Recurrence
framing survives a restart*), `specs/audit-log` (v5 `prior_handoff_ids`) and
design D8/D9/D16. Tasks 8.1 (the record), 8.3 and 8.4. The renderer and composer
are in `test_handoff_digest.py`.

Placeholders only (standing rule 1).
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import pkgutil
from pathlib import Path

import httpx
import pytest

import henk.tools
from henk.agent.core import AgentCore
from henk.agent.digest import read_digest
from henk.agent.recall import MemoryRecall
from henk.agent.session import SessionStats, ToolCallRecord
from henk.agent.triage import PRIOR_HANDOFFS_HEADER, UNTRUSTED_BEGIN, UNTRUSTED_END
from henk.agent.turns import EventTurn, EventTurnItem
from henk.audit import AuditLog, read_audit_records
from henk.config import Config
from henk.events.identity import derive_identity
from henk.events.incident_context import IncidentContext, IncidentContextProvider
from henk.events.pipeline import EventPipeline, PipelineConfig
from henk.events.types import Event
from henk.store import MemoryStore, Store, StoreError
from henk.store.factory import HenkStores, build_stores
from henk.store.handoffs import HandoffStore, format_handoff_result
from henk.tools import build_production_registry
from henk.tools.publish_handoff import PublishHandoffTool
from tests.conftest import TRIAGE_REPLY, EventSessionFactory, FakeChannel, make_clock
from tests.test_config import SAMPLE, _minimal_raw

HOUR = 3600.0
DAY = 24 * HOUR
T0 = 1_790_144_043.0  # 2026-09-23T06:14:03Z

MESSAGE = (
    "Value: A=91.2\nLabels:\n - alertname = HenkDiskFull\n"
    " - identity_scope = host\n - host = host-a.example\n - node = vps\n"
)
TITLE = "[FIRING:1] HenkDiskFull henk (host-a.example)"


def _event(eid: str = "e1", at: float = T0) -> Event:
    return Event(id=eid, title=TITLE, message=MESSAGE, arrival_time=at,
                 raw={"event": "message", "time": int(at) - 60})


IDENTITY = derive_identity(_event()).key
RULE = "grafana:HenkDiskFull"


def _item(event: Event | None = None, **kw) -> EventTurnItem:
    event = event or _event()
    return EventTurnItem(event=event, identity=derive_identity(event), **kw)


def _turn(*items: EventTurnItem) -> EventTurn:
    return EventTurn(items=items or (_item(),))


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _sessions(path: Path) -> list[dict]:
    return [r for r in _records(path) if r.get("record_type") == "session"]


def _store(tmp_path: Path, at: float = T0) -> Store:
    return Store(tmp_path / "store" / "henk.db", clock=lambda: at)


def _retain(archive: HandoffStore, document: str, *, at: float,
            identity: str = IDENTITY, rule: str = RULE, nodes=("vps",),
            message_id: str | None = None):
    store = archive.store
    original = store._clock
    store._clock = lambda: at
    try:
        return archive.retain(document, message_id=message_id, identity_keys=(identity,),
                              rule_keys=(rule,), nodes=nodes)
    finally:
        store._clock = original


# --- 8.1 The record names what history was shown ------------------------------


async def test_the_record_names_what_history_was_shown(tmp_path: Path):
    """*The record names what history was shown*: the ids actually rendered, in
    order, after the bounds apply."""
    archive = HandoffStore(_store(tmp_path))
    retained = [
        _retain(archive, f"PRIOR-{n}: " + "p" * 50, at=T0 - n * DAY) for n in range(1, 6)
    ]
    retained.append(
        _retain(archive, "UNRELATED", at=T0 - DAY, identity="gatus:svc/web",
                rule="gatus:svc/web", nodes=("rp2",))
    )
    audit = tmp_path / "audit.jsonl"
    factory = EventSessionFactory()
    core = AgentCore(factory, FakeChannel(), clock=make_clock([0]),
                     audit=AuditLog(audit), handoff_archive=archive)
    await core.process(_turn())
    content = factory.created[0].contents[0]
    [record] = _sessions(audit)
    shown = record["prior_handoff_ids"]
    # Three of the five related handoffs, newest first; never the unrelated one.
    assert shown == [retained[0].id, retained[1].id, retained[2].id]
    by_id = {h.id: h for h in retained}
    positions = [content.index(by_id[i].document) for i in shown]
    assert positions == sorted(positions)
    for h in retained:
        assert (h.document in content) is (h.id in shown), h.id
    # The same selection the digest reports, read independently.
    assert shown == list(read_digest(archive, _turn()).shown_ids)
    # References only: no handoff text enters the record.
    assert "PRIOR-" not in json.dumps(record)


async def test_a_triage_with_no_related_history_records_an_empty_list(tmp_path: Path):
    archive = HandoffStore(_store(tmp_path))
    _retain(archive, "UNRELATED", at=T0 - DAY, identity="gatus:svc/web",
            rule="gatus:svc/web", nodes=("rp2",))
    audit = tmp_path / "audit.jsonl"
    factory = EventSessionFactory()
    core = AgentCore(factory, FakeChannel(), clock=make_clock([0]),
                     audit=AuditLog(audit), handoff_archive=archive)
    await core.process(_turn())
    assert PRIOR_HANDOFFS_HEADER not in factory.created[0].contents[0]
    assert _sessions(audit)[0]["prior_handoff_ids"] == []


async def test_a_triage_with_no_archive_records_an_empty_list(tmp_path: Path):
    audit = tmp_path / "audit.jsonl"
    core = AgentCore(EventSessionFactory(), FakeChannel(), clock=make_clock([0]),
                     audit=AuditLog(audit))
    await core.process(_turn())
    assert _sessions(audit)[0]["prior_handoff_ids"] == []


async def test_continuation_and_owner_records_carry_null(tmp_path: Path):
    """audit-log: records that are not event triages carry null ``prior_handoff_ids``."""
    archive = HandoffStore(_store(tmp_path))
    _retain(archive, "PRIOR", at=T0 - DAY)
    audit = tmp_path / "audit.jsonl"
    core = AgentCore(EventSessionFactory(), FakeChannel(), clock=make_clock([0]),
                     audit=AuditLog(audit), handoff_archive=archive)
    await core.process(_turn())
    await core.process("what did you find?")  # continuation inside the triage session
    await core.process("/new")
    await core.process("hello")  # a plain owner session
    await core.aclose()
    records = _sessions(audit)
    assert [r["trigger"] for r in records] == ["event", "owner-message", "owner-message"]
    assert len(records[0]["prior_handoff_ids"]) == 1
    assert records[1]["prior_handoff_ids"] is None
    assert records[2]["prior_handoff_ids"] is None


class _BrokenArchive:
    """Reads fail; the store clock still works."""

    def __init__(self, tmp_path: Path) -> None:
        self.store = _store(tmp_path)
        self.reads = 0

    def eligible(self, now=None):
        self.reads += 1
        raise StoreError("simulated read failure")

    def find_by_message_ref(self, ref, now=None):
        self.reads += 1
        raise StoreError("simulated read failure")


async def test_a_digest_read_failure_does_not_fail_the_triage(tmp_path: Path, caplog):
    audit = tmp_path / "audit.jsonl"
    channel = FakeChannel()
    factory = EventSessionFactory()
    archive = _BrokenArchive(tmp_path)
    core = AgentCore(factory, channel, clock=make_clock([0]),
                     audit=AuditLog(audit), handoff_archive=archive)
    item = _item(recurrence=True, prior_handoff_ref=format_handoff_result("hf-1"))
    with caplog.at_level("ERROR"):
        await core.process(_turn(item))
    assert archive.reads >= 1
    content = factory.created[0].contents[0]
    assert PRIOR_HANDOFFS_HEADER not in content
    assert "could not be read" in content[content.index(UNTRUSTED_END):]
    assert channel.sent == [TRIAGE_REPLY]  # the triage ran and was delivered
    [record] = _sessions(audit)
    assert record["outcome"] == "completed"
    assert record["prior_handoff_ids"] == []
    assert any("prior-handoff digest" in r.getMessage() for r in caplog.records)


# --- 8.3 Recurrence, live and across a restart ------------------------------------


def _ntfy_client(ids: list[str]) -> httpx.AsyncClient:
    queue = list(ids)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": queue.pop(0)})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _PublishingSession:
    """Publishes through the real tool and reports it the way the SDK session does."""

    def __init__(self, tool: PublishHandoffTool | None, document: str | None) -> None:
        self._tool, self._document = tool, document
        self._calls: list[ToolCallRecord] = []
        self.contents: list[str] = []

    async def run_turn(self, text: str) -> str:
        self.contents.append(text)
        if self._tool is not None and self._document is not None:
            result = await self._tool.run(document=self._document)
            assert result.ok, result.error
            self._calls.append(ToolCallRecord("publish_handoff", "notify-only",
                                              result.content))
        return TRIAGE_REPLY

    def stats(self) -> SessionStats:
        return SessionStats(tool_calls=tuple(self._calls))

    async def close(self) -> None:
        pass


class _PublishingFactory:
    def __init__(self, tool, document) -> None:
        self._tool, self._document = tool, document
        self.created: list[_PublishingSession] = []

    def create(self):
        session = _PublishingSession(self._tool, self._document)
        self.created.append(session)
        return session


PIPELINE = PipelineConfig(cooldown_seconds=6 * HOUR, recurrence_window_seconds=DAY)
FIRST_HANDOFF = "FIRST-TRIAGE: example-a.service filled /var on host-a.example"


def _wired(tmp_path: Path, *, at: float, client, document: str | None,
           pipeline: EventPipeline):
    """One process's worth of wiring, as runtime.py builds it."""
    store = _store(tmp_path, at)
    archive = HandoffStore(store)
    context = IncidentContextProvider()
    tool = PublishHandoffTool(client, base_url="http://ntfy.host-a.example",
                              topic="henk-handoffs", archive=archive,
                              incident_context=context)
    factory = _PublishingFactory(tool, document)
    core = AgentCore(
        factory, FakeChannel(), clock=make_clock([0]),
        audit=AuditLog(tmp_path / "audit" / "audit.jsonl", clock=lambda: at),
        handoff_sink=pipeline.note_handoff, incident_context=context,
        handoff_archive=archive,
    )
    return core, factory, archive, store


async def test_recurrence_of_a_recently_triaged_incident(tmp_path: Path):
    """*Recurrence of a recently triaged incident*, in one process."""
    client = _ntfy_client(["hf-1", "hf-2"])
    pipeline = EventPipeline(PIPELINE)
    core, factory, archive, _ = _wired(tmp_path, at=T0, client=client,
                                       document=FIRST_HANDOFF, pipeline=pipeline)
    first = pipeline.evaluate([_event("e1", T0)], now=T0).event_turn
    await core.process(first)
    [first_retained] = archive.eligible()
    later = T0 + 7 * HOUR  # past cooldown, inside the recurrence window
    archive.store._clock = lambda: later
    second = pipeline.evaluate([_event("e2", later)], now=later).event_turn
    [item] = second.items
    assert item.recurrence and item.prior_handoff_ref == format_handoff_result("hf-1")
    await core.process(second)
    await client.aclose()
    content = factory.created[1].contents[0]
    block = content[content.index(UNTRUSTED_BEGIN):content.index(UNTRUSTED_END)]
    assert "relation=recurrence reference" in block
    assert block.index("relation=recurrence reference") < block.index(FIRST_HANDOFF)
    note = content[content.index("\nRecurrence:"):]
    assert "prior handoff hf-1 is the recurrence reference" in note
    assert "instead of re-running full evidence gathering" in note
    assert FIRST_HANDOFF not in note
    records = _sessions(tmp_path / "audit" / "audit.jsonl")
    assert records[-1]["prior_handoff_ids"] == [first_retained.id]


async def test_recurrence_framing_survives_a_restart(tmp_path: Path):
    """*Recurrence framing survives a restart*: the ref comes back from the audit
    log, the content from the persistent archive, and nothing from memory."""
    client = _ntfy_client(["hf-1"])
    before = EventPipeline(PIPELINE)
    core, _, archive, store = _wired(tmp_path, at=T0, client=client,
                                     document=FIRST_HANDOFF, pipeline=before)
    await core.process(before.evaluate([_event("e1", T0)], now=T0).event_turn)
    await core.aclose()
    [first_retained] = archive.eligible()
    store.close()
    await client.aclose()
    del core, archive, store, before

    # The restart: new pipeline rehydrated from the log, new store on the same file,
    # new core. This process publishes nothing.
    later = T0 + 7 * HOUR
    after = EventPipeline(PIPELINE)
    after.rehydrate(read_audit_records(tmp_path / "audit" / "audit.jsonl"), now=later)
    core, factory, archive, _ = _wired(tmp_path, at=later, client=None,
                                       document=None, pipeline=after)
    turn = after.evaluate([_event("e2", later)], now=later).event_turn
    [item] = turn.items
    assert item.recurrence is True
    assert item.prior_handoff_ref == format_handoff_result("hf-1")
    await core.process(turn)
    content = factory.created[0].contents[0]
    block = content[content.index(UNTRUSTED_BEGIN):content.index(UNTRUSTED_END)]
    assert FIRST_HANDOFF in block
    assert "relation=recurrence reference" in block
    note = content[content.index("\nRecurrence:"):]
    assert "prior handoff hf-1 is the recurrence reference" in note
    records = _sessions(tmp_path / "audit" / "audit.jsonl")
    assert records[-1]["prior_handoff_ids"] == [first_retained.id]


# --- The runtime hands the core the registry's archive ---------------------------


@pytest.fixture
def ntfy(monkeypatch):
    import henk.runtime as runtime_module

    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "hf-rt"})

    monkeypatch.setattr(
        runtime_module.httpx, "AsyncClient",
        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw),
    )


def _runtime_config(tmp_path: Path) -> Config:
    base = Config.load(SAMPLE, env={})
    return dataclasses.replace(
        base,
        store=dataclasses.replace(base.store, path=str(tmp_path / "store" / "henk.db")),
        audit=dataclasses.replace(base.audit, path=str(tmp_path / "audit" / "audit.jsonl")),
    )


async def test_the_runtime_hands_the_core_the_tools_archive(tmp_path: Path, ntfy):
    from henk.runtime import build_runtime

    app, client = build_runtime(_runtime_config(tmp_path))
    try:
        tool = app._core._factory._registry.get("publish_handoff")
        assert isinstance(app._core._handoff_archive, HandoffStore)
        assert app._core._handoff_archive is tool._archive
    finally:
        await client.aclose()


async def test_an_event_turn_through_the_runtime_carries_the_digest(tmp_path: Path, ntfy):
    from henk.runtime import build_runtime

    config = _runtime_config(tmp_path)
    app, client = build_runtime(config)
    try:
        core = app._core
        _retain(core._handoff_archive, "RUNTIME-PRIOR", at=core._handoff_archive.store.clock()
                - DAY)
        factory = EventSessionFactory()
        core._factory = factory
        await core.process(_turn())
        await core.aclose()
    finally:
        await client.aclose()
    content = factory.created[0].contents[0]
    assert PRIOR_HANDOFFS_HEADER in content
    assert "RUNTIME-PRIOR" in content


# --- 8.4 Owner sessions never see handoff history ---------------------------------


class _SpyArchive(HandoffStore):
    def __init__(self, store: Store) -> None:
        super().__init__(store)
        self.reads = 0

    def eligible(self, now=None):
        self.reads += 1
        return super().eligible(now)

    def find_by_message_ref(self, ref, now=None):
        self.reads += 1
        return super().find_by_message_ref(ref, now)


async def test_owner_sessions_never_see_handoff_history(tmp_path: Path):
    """*Owner sessions never see handoff history*."""
    store = _store(tmp_path)
    archive = _SpyArchive(store)
    memories = MemoryStore(store)
    memories.add("the vps runs example-a.service", "pinned")
    for n in range(3):
        _retain(archive, f"RETAINED-HANDOFF-{n}", at=T0 - (n + 1) * HOUR,
                message_id=f"hf-{n}")
    factory = EventSessionFactory(reply="ok")
    core = AgentCore(factory, FakeChannel(), clock=make_clock([0]),
                     recall=MemoryRecall(memories), handoff_archive=archive,
                     incident_context=IncidentContextProvider())
    await core.process("how is the vps?")
    await core.process("and the disk on host-a.example?")
    await core.process("/new")
    await core.process("what did the last triage say? handoff published (id: hf-0)")
    contents = [c for s in factory.created for c in s.contents]
    assert len(contents) == 3
    assert "the vps runs example-a.service" in contents[0]  # recall still works
    for content in contents:
        assert "RETAINED-HANDOFF" not in content
        assert PRIOR_HANDOFFS_HEADER not in content
        assert "[prior handoff" not in content
    assert archive.reads == 0  # no owner-turn path reads the archive at all


async def test_an_owner_follow_up_in_a_triage_session_is_not_given_the_digest(
    tmp_path: Path,
):
    archive = _SpyArchive(_store(tmp_path))
    _retain(archive, "RETAINED-HANDOFF", at=T0 - HOUR)
    factory = EventSessionFactory(reply=TRIAGE_REPLY)
    core = AgentCore(factory, FakeChannel(), clock=make_clock([0]),
                     handoff_archive=archive)
    await core.process(_turn())
    reads_after_event = archive.reads
    await core.process("tell me more")
    event_content, follow_up = factory.created[0].contents
    assert "RETAINED-HANDOFF" in event_content
    assert "RETAINED-HANDOFF" not in follow_up
    assert PRIOR_HANDOFFS_HEADER not in follow_up
    assert archive.reads == reads_after_event


async def test_owner_sessions_through_the_runtime_never_see_handoff_history(
    tmp_path: Path, ntfy
):
    from henk.runtime import build_runtime

    app, client = build_runtime(_runtime_config(tmp_path))
    try:
        core = app._core
        _retain(core._handoff_archive, "RUNTIME-RETAINED",
                at=core._handoff_archive.store.clock() - HOUR, message_id="hf-rt")
        factory = EventSessionFactory(reply="ok")
        core._factory = factory
        await core.process("anything waiting on me?")
        await core.process("what about host-a.example?")
        await core.aclose()
    finally:
        await client.aclose()
    for content in factory.created[0].contents:
        assert "RUNTIME-RETAINED" not in content
        assert PRIOR_HANDOFFS_HEADER not in content


# --- 8.4 No tool exposes the archive ---------------------------------------------


def _everything_enabled(tmp_path: Path) -> Config:
    (tmp_path / "corpus").mkdir()
    raw = _minimal_raw("+31600000000")
    raw["owner"]["timezone"] = "Etc/UTC"
    raw.update(
        store={"path": str(tmp_path / "store" / "henk.db")},
        audit={"path": str(tmp_path / "audit.jsonl")},
        reminders={"enabled": True},
        sessions={"enabled": True},
        homelab_query={"enabled": True},
        homelab_docs={"enabled": True, "path": str(tmp_path / "corpus")},
        personal_data={
            "session_project_allowlist": ["henk"],
            "docs_path_allowlist": ["index.mdx"],
        },
    )
    return Config.from_dict(raw, env={})


def _held(obj, depth: int = 3, seen=None):
    """Every object reachable from ``obj``'s attributes, to ``depth`` levels."""
    seen = seen if seen is not None else set()
    if depth == 0 or id(obj) in seen:
        return
    seen.add(id(obj))
    values = []
    if isinstance(obj, dict):
        values = list(obj.values())
    elif isinstance(obj, (list, tuple, set, frozenset)):
        values = list(obj)
    elif hasattr(obj, "__dict__"):
        values = list(vars(obj).values())
    for value in values:
        yield value
        yield from _held(value, depth - 1, seen)


async def test_no_tool_exposes_the_archive(tmp_path: Path):
    """*No tool exposes the archive*: against the real production registry, every
    optional tool enabled."""
    config = _everything_enabled(tmp_path)
    stores = build_stores(config.store, config.reminders)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"id": "hf-reg"})
    ))
    registry = build_production_registry(
        config, client, stores=stores, incident_context=IncidentContextProvider()
    )
    names = set(registry.names())
    assert {"publish_handoff", "homelab_docs", "sessions_read", "remind"} <= names

    # 1. Only publish_handoff holds the archive (it writes), and no tool holds the
    #    store bundle that would reach it.
    holders = set()
    for name in names:
        tool = registry.get(name)
        for value in _held(tool):
            if isinstance(value, HenkStores):
                holders.add((name, "HenkStores"))
            if isinstance(value, HandoffStore):
                holders.add((name, "HandoffStore"))
    assert holders == {("publish_handoff", "HandoffStore")}

    # 2. publish_handoff never reads it, and its result carries no handoff text.
    tool = registry.get("publish_handoff")
    spy = _SpyArchive(stores.store)
    tool._archive = spy
    tool._incident_context.publish(IncidentContext.from_turn(_turn()))
    _retain(spy, "EARLIER-RETAINED", at=stores.store.clock() - HOUR)
    result = await tool.run(document="NEW-HANDOFF-TEXT")
    assert result.ok
    assert result.content == format_handoff_result("hf-reg")
    assert "EARLIER-RETAINED" not in result.content
    assert spy.reads == 0
    await client.aclose()
    stores.store.close()


def test_no_tool_module_calls_the_archives_reads():
    """The read methods are the digest's alone: no module under ``henk.tools``,
    and not recall, calls them."""
    from henk.agent import recall

    sources = [inspect.getsource(recall)]
    for info in pkgutil.walk_packages(henk.tools.__path__, "henk.tools."):
        module = __import__(info.name, fromlist=["_"])
        sources.append(inspect.getsource(module))
    for source in sources:
        assert ".eligible(" not in source
        assert "find_by_message_ref" not in source
    assert "handoff" not in inspect.getsource(recall).lower()
