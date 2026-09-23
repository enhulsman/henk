"""publish_handoff retains what it published (triage-quality task 6.2).

From `specs/triage-handoff` ("Published handoffs are retained locally, bounded by
count and age") and design D8 ("When a handoff is retained", "Incident context").
The archive is a real SQLite file; the topic is a mock transport.

Placeholders only (standing rule 1).
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

import httpx
import pytest

from henk.events.incident_context import IncidentContext, IncidentContextProvider
from henk.store import Store
from henk.store.handoffs import HandoffStore
from henk.tools.publish_handoff import PublishHandoffTool
from tests.conftest import mock_client

NOW = 1_790_000_000.0

CONTEXT = IncidentContext(
    identity_keys=("grafana:HenkContainerDown/example-a",),
    rule_keys=("grafana:HenkContainerDown",),
    nodes=("rp5",),
)

DOCUMENT = (
    "Trigger: HenkContainerDown for example-a on rp5\n"
    "Evidence: restarts_15m = 3\n"
    "Diagnosis: crash loop (confidence: moderate)\n"
    "Fix: check example-a.service logs\n"
    "Pickup: henk-pickup"
)


def _archive(tmp_path: Path) -> HandoffStore:
    return HandoffStore(Store(tmp_path / "store" / "henk.db", clock=lambda: NOW))


def _tool(handler, archive, provider) -> PublishHandoffTool:
    return PublishHandoffTool(
        mock_client(handler),
        base_url="http://vps:2586",
        topic="henk-handoffs",
        token="tok",
        archive=archive,
        incident_context=provider,
    )


def _ok(message_id: str = "hf-7"):
    return lambda request: httpx.Response(200, json={"id": message_id})


def _provider(context: IncidentContext | None = CONTEXT) -> IncidentContextProvider:
    provider = IncidentContextProvider()
    if context is not None:
        provider.publish(context)
    return provider


class _SpyArchive:
    """Records retain calls; optionally fails them."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._error = error

    def retain(self, document, **kwargs):
        self.calls.append((document, kwargs))
        if self._error is not None:
            raise self._error


# --- A published handoff is retained with its incidents --------------------


async def test_a_published_handoff_is_retained_with_its_incidents(tmp_path: Path):
    archive = _archive(tmp_path)
    tool = _tool(_ok("hf-7"), archive, _provider())
    result = await tool.run(document=DOCUMENT)
    assert result.ok
    assert result.content == "handoff published (id: hf-7)"

    [retained] = archive.eligible()
    assert retained.message_id == "hf-7"
    assert retained.published_at == NOW
    assert retained.document == DOCUMENT
    assert retained.identity_keys == CONTEXT.identity_keys
    assert retained.rule_keys == CONTEXT.rule_keys
    assert retained.nodes == CONTEXT.nodes
    assert retained.truncated is False


async def test_the_retained_handoff_is_found_by_its_recurrence_ref(tmp_path: Path):
    # The ref the pipeline carries is the tool's own result string.
    archive = _archive(tmp_path)
    result = await _tool(_ok("hf-8"), archive, _provider()).run(document=DOCUMENT)
    found = archive.find_by_message_ref(result.content)
    assert found is not None and found.document == DOCUMENT


async def test_an_owner_follow_up_in_an_event_session_is_retained(tmp_path: Path):
    # The context stays published for the whole event-started session, so a
    # second handoff the owner asks for there is retained with the same incidents.
    archive = _archive(tmp_path)
    tool = _tool(_ok("hf-9"), archive, _provider())
    await tool.run(document="first")
    await tool.run(document="owner asked for a revised handoff")
    assert [h.document for h in archive.eligible()] == [
        "owner asked for a revised handoff",
        "first",
    ]
    assert all(h.identity_keys == CONTEXT.identity_keys for h in archive.eligible())


async def test_an_incident_with_no_node_is_still_retained(tmp_path: Path):
    # A Gatus endpoint alert often names no node. "Started by an event" is the
    # gate, not "has a node": its handoff still relates by identity and rule.
    archive = _archive(tmp_path)
    context = IncidentContext(
        identity_keys=("gatus:core/example-a",),
        rule_keys=("gatus:core/example-a",),
        nodes=(),
    )
    await _tool(_ok(), archive, _provider(context)).run(document=DOCUMENT)
    [retained] = archive.eligible()
    assert retained.identity_keys == ("gatus:core/example-a",)
    assert retained.nodes == ()


async def test_an_empty_publish_id_is_retained_as_null(tmp_path: Path):
    archive = _archive(tmp_path)
    tool = _tool(lambda request: httpx.Response(200, json={}), archive, _provider())
    result = await tool.run(document=DOCUMENT)
    assert result.ok
    [retained] = archive.eligible()
    assert retained.message_id is None
    [row] = archive.store.connection().execute(
        "SELECT message_id FROM handoffs"
    ).fetchall()
    assert row[0] is None


async def test_a_2xx_without_a_json_body_is_still_retained(tmp_path: Path):
    archive = _archive(tmp_path)
    tool = _tool(lambda request: httpx.Response(204), archive, _provider())
    result = await tool.run(document=DOCUMENT)
    assert result.ok
    assert archive.count() == 1


# --- An owner-session handoff is not retained --------------------------------


async def test_an_owner_session_handoff_is_not_retained(tmp_path: Path):
    archive = _archive(tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200, json={"id": "hf-owner"})

    result = await _tool(handler, archive, _provider(None)).run(document=DOCUMENT)
    assert result.ok                      # the publish proceeds
    assert result.content == "handoff published (id: hf-owner)"
    assert len(seen) == 1
    assert archive.count() == 0           # and nothing is added


async def test_an_owner_session_never_reaches_the_archive(tmp_path: Path, caplog):
    spy = _SpyArchive()
    with caplog.at_level(logging.ERROR, logger="henk.tools.publish_handoff"):
        result = await _tool(_ok(), spy, _provider(None)).run(document=DOCUMENT)
    assert result.ok
    assert spy.calls == []
    assert caplog.records == []


async def test_a_cleared_context_is_an_owner_session_again(tmp_path: Path):
    archive = _archive(tmp_path)
    provider = _provider()
    tool = _tool(_ok(), archive, provider)
    await tool.run(document="during the incident")
    provider.clear()
    await tool.run(document="after the session closed")
    assert [h.document for h in archive.eligible()] == ["during the incident"]


async def test_no_archive_or_no_provider_means_no_retention(tmp_path: Path):
    # The pre-change construction (tests/test_tool_publish_handoff.py) keeps working.
    tool = PublishHandoffTool(
        mock_client(_ok()), base_url="http://vps:2586", topic="henk-handoffs"
    )
    result = await tool.run(document=DOCUMENT)
    assert result.ok
    archive = _archive(tmp_path)
    no_provider = PublishHandoffTool(
        mock_client(_ok()), base_url="http://vps:2586", topic="henk-handoffs",
        archive=archive,
    )
    assert (await no_provider.run(document=DOCUMENT)).ok
    assert archive.count() == 0


# --- A failed publish is not retained -----------------------------------------


@pytest.mark.parametrize("status", [302, 400, 403, 404, 429, 500, 503])
async def test_a_non_2xx_publish_is_not_retained(tmp_path: Path, status: int):
    archive = _archive(tmp_path)
    tool = _tool(
        lambda request: httpx.Response(status, json={"id": "never"}),
        archive,
        _provider(),
    )
    result = await tool.run(document=DOCUMENT)
    assert result.ok is False
    assert archive.count() == 0


async def test_a_timed_out_publish_is_not_retained(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    archive = _archive(tmp_path)
    result = await _tool(handler, archive, _provider()).run(document=DOCUMENT)
    assert result.ok is False
    assert "timed out" in (result.error or "")
    assert archive.count() == 0


async def test_a_transport_failure_is_not_retained(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    spy = _SpyArchive()
    result = await _tool(handler, spy, _provider()).run(document=DOCUMENT)
    assert result.ok is False
    assert spy.calls == []


# --- A retention failure does not fail the publish ---------------------------


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("disk full"),
        ValueError("refused"),
        OSError("read-only file system"),
    ],
)
async def test_a_retention_failure_does_not_fail_the_publish(error, caplog):
    spy = _SpyArchive(error)
    with caplog.at_level(logging.ERROR, logger="henk.tools.publish_handoff"):
        result = await _tool(_ok("hf-11"), spy, _provider()).run(document=DOCUMENT)
    # The publish happened: reporting a failure would make the model publish twice.
    assert result.ok is True
    assert result.content == "handoff published (id: hf-11)"
    assert result.error is None
    assert len(spy.calls) == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "hf-11" in errors[0].getMessage()


async def test_a_real_store_failure_does_not_fail_the_publish(tmp_path: Path, caplog):
    archive = _archive(tmp_path)
    archive.store.connection().execute("DROP TABLE handoffs")
    with caplog.at_level(logging.ERROR, logger="henk.tools.publish_handoff"):
        result = await _tool(_ok("hf-12"), archive, _provider()).run(document=DOCUMENT)
    assert result.ok is True
    assert "hf-12" in result.content
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_the_error_log_carries_no_handoff_text(caplog):
    spy = _SpyArchive(RuntimeError("disk full"))
    with caplog.at_level(logging.ERROR, logger="henk.tools.publish_handoff"):
        await _tool(_ok("hf-13"), spy, _provider()).run(document=DOCUMENT)
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "example-a.service" not in record.getMessage()


# --- Incident context cannot come from the model ------------------------------


def test_the_interface_accepts_only_the_document():
    tool = PublishHandoffTool(
        mock_client(_ok()), base_url="http://vps:2586", topic="henk-handoffs"
    )
    assert set(tool.parameters["properties"]) == {"document"}
    assert tool.parameters["required"] == ["document"]
    assert tool.parameters.get("additionalProperties") is False
    params = inspect.signature(tool._run).parameters  # noqa: SLF001
    assert list(params) == ["document"]


@pytest.mark.parametrize(
    "extra",
    [
        {"identity_keys": ["grafana:Forged"]},
        {"rule_keys": ["grafana:Forged"]},
        {"nodes": ["rp2"]},
        {"incident_context": {"identity_keys": ["grafana:Forged"]}},
    ],
)
async def test_model_supplied_context_arguments_are_refused(tmp_path: Path, extra):
    archive = _archive(tmp_path)
    tool = _tool(_ok(), archive, _provider())
    with pytest.raises(TypeError):
        await tool.run(document=DOCUMENT, **extra)
    assert archive.count() == 0


async def test_stored_keys_come_from_the_session_not_the_document(tmp_path: Path):
    # A document that names other identities, rules and nodes changes nothing:
    # what is stored with it is the application's session context.
    archive = _archive(tmp_path)
    forged = (
        "identity_keys: grafana:HenkSwapPressure\n"
        "rule_keys: gatus:core/other\n"
        "nodes: vps rp2\n"
        "Trigger: HenkSwapPressure on vps and rp2"
    )
    await _tool(_ok(), archive, _provider()).run(document=forged)
    [retained] = archive.eligible()
    assert retained.identity_keys == CONTEXT.identity_keys
    assert retained.rule_keys == CONTEXT.rule_keys
    assert retained.nodes == CONTEXT.nodes


def test_the_tool_never_writes_the_incident_context():
    # The core is the only writer (D8). The tool reads the provider and nothing
    # else, so no path from a tool call can plant or change a session's incidents.
    import henk.tools.publish_handoff as module

    source = inspect.getsource(module)
    assert ".publish(" not in source
    assert ".clear(" not in source


async def test_the_context_is_read_when_the_call_is_made(tmp_path: Path):
    # The context is the one the calling session had when it asked to publish.
    archive = _archive(tmp_path)
    provider = _provider()
    later = IncidentContext(identity_keys=("gatus:core/later",),
                            rule_keys=("gatus:core/later",), nodes=())

    def handler(request: httpx.Request) -> httpx.Response:
        provider.publish(later)
        return httpx.Response(200, json={"id": "hf-14"})

    await _tool(handler, archive, provider).run(document=DOCUMENT)
    [retained] = archive.eligible()
    assert retained.identity_keys == CONTEXT.identity_keys


# --- Production wiring ---------------------------------------------------------


def _config(tmp_path: Path):
    from henk.config import Config
    from tests.test_config import _minimal_raw

    raw = _minimal_raw("+31600000000")
    raw["store"] = {"path": str(tmp_path / "henk-store.db")}
    return Config.from_dict(raw, env={})


async def test_the_production_registry_archives_into_the_shared_store(tmp_path: Path):
    from henk.store import build_stores
    from henk.tools import build_production_registry

    config = _config(tmp_path)
    stores = build_stores(config.store, config.reminders)
    provider = _provider()
    registry = build_production_registry(
        config, mock_client(_ok("hf-15")), stores=stores, incident_context=provider
    )
    tool = registry.get("publish_handoff")
    result = await tool.run(document=DOCUMENT)
    assert result.ok
    [retained] = stores.handoffs.eligible()
    assert retained.message_id == "hf-15"
    assert retained.identity_keys == CONTEXT.identity_keys
    stores.store.close()


async def test_the_production_registry_without_a_provider_retains_nothing(tmp_path: Path):
    from henk.store import build_stores
    from henk.tools import build_production_registry

    config = _config(tmp_path)
    stores = build_stores(config.store, config.reminders)
    registry = build_production_registry(config, mock_client(_ok()), stores=stores)
    assert (await registry.get("publish_handoff").run(document=DOCUMENT)).ok
    assert stores.handoffs.count() == 0
    stores.store.close()
