"""The production wiring assembles without opening any connection (task 5.1)."""

from __future__ import annotations

import dataclasses

import httpx

from henk.app import App
from henk.config import Config
from henk.events.intake import SINCE_REJECTED_NOTICE
from henk.runtime import build_runtime
from tests.test_config import SAMPLE


async def test_build_runtime_wires_an_app_without_connecting():
    config = Config.load(SAMPLE, env={})
    app, client = build_runtime(config)
    try:
        assert isinstance(app, App)
        assert isinstance(client, httpx.AsyncClient)
    finally:
        await client.aclose()


async def test_events_enabled_wires_checkpoint_and_coordinator():
    # events.enabled (the sample config) → durable checkpoint on the core, a
    # coordinator to consume events, and rehydration of the audit tail — all
    # non-fatal even when the audit volume is absent in a test env.
    config = Config.load(SAMPLE, env={})
    assert config.events.enabled is True
    app, client = build_runtime(config)
    try:
        assert app._coordinator is not None
        assert app._core._checkpoint is not None
        assert app._core._handoff_sink is not None  # recurrence ref wired live
    finally:
        await client.aclose()


async def test_events_disabled_skips_intake_but_keeps_audit():
    # Disabling event intake is the documented rollback path. It must not disable
    # receipts: with mutating tools in the registry, an audit log that only exists
    # when events are enabled would make the rollback path unreceipted (design D11).
    base = Config.load(SAMPLE, env={})
    config = dataclasses.replace(
        base, events=dataclasses.replace(base.events, enabled=False)
    )
    app, client = build_runtime(config)
    try:
        assert app._coordinator is None       # subscriber never starts (v1 behaviour)
        assert app._core._checkpoint is None  # no checkpoint, no rehydration
        assert app._core._audit is not None   # receipts still land
        assert app._dispatcher._gate._recorder is not None
    finally:
        await client.aclose()


async def test_audit_path_falls_back_to_the_events_scoped_key():
    # rp5's deployed config.yaml is locally modified and only carries
    # events.audit_path; the fallback is what lets this change deploy without a
    # host edit.
    base = Config.load(SAMPLE, env={})
    assert base.audit.path == base.events.audit_path


async def test_receipts_are_wired_from_the_gate_to_the_core():
    config = Config.load(SAMPLE, env={})
    app, client = build_runtime(config)
    try:
        receipts = app._dispatcher._gate._recorder
        assert app._core._receipts is receipts
        # The core registered itself as the aggregation sink, so a decision lands
        # in both places: durable on disk, and in the session record.
        assert receipts.sink == app._core._note_receipt
    finally:
        await client.aclose()


async def test_since_rejected_notice_reaches_the_channel():
    # The wiring, not the intake logic, is under test: `on_since_rejected` must
    # actually deliver text to the owner's channel. Without this, a signature
    # change to `ChannelAdapter.send` would be swallowed by intake's best-effort
    # `except` and the alert would silently degrade to a log line — precisely
    # the silence design D8 exists to eliminate.
    config = Config.load(SAMPLE, env={})
    app, client = build_runtime(config)
    try:
        sent: list[str] = []

        async def capture(text: str) -> None:
            sent.append(text)

        intake = app._coordinator._intake
        assert intake._on_since_rejected is not None, "notice callback not wired"

        # Swap only the transport-facing send so nothing connects.
        app._adapter.send = capture  # type: ignore[method-assign]
        await intake._on_since_rejected()

        assert sent == [SINCE_REJECTED_NOTICE]
    finally:
        await client.aclose()


async def test_gate_is_wired_to_the_core_for_turn_framing():
    # Without this wiring the gate would fall back to its unframed context and
    # never see an event turn's taint — turn scope would be unenforceable (D10).
    config = Config.load(SAMPLE, env={})
    app, client = build_runtime(config)
    try:
        assert app._core._gate is app._dispatcher._gate
    finally:
        await client.aclose()


async def test_standing_demotion_flag_reaches_the_gate():
    import dataclasses

    base = Config.load(SAMPLE, env={})
    config = dataclasses.replace(
        base, gate=dataclasses.replace(base.gate, demote_standing=True)
    )
    app, client = build_runtime(config)
    try:
        assert app._dispatcher._gate._demote_standing is True
    finally:
        await client.aclose()


async def test_store_backed_surfaces_share_one_store():
    # The tool, the owner commands and recall must agree about what Henk knows;
    # separate store instances would be three subtly different answers.
    config = Config.load(SAMPLE, env={})
    app, client = build_runtime(config)
    try:
        core = app._core
        tool_memories = app._core._recall._memories
        assert core._commands.memories is tool_memories
        assert core._commands.inbox is not None
    finally:
        await client.aclose()


async def test_store_config_reaches_the_repositories():
    import dataclasses

    base = Config.load(SAMPLE, env={})
    config = dataclasses.replace(
        base,
        store=dataclasses.replace(
            base.store, fact_length_limit=42, memory_agent_cap=7, recall_render_limit=99
        ),
    )
    app, client = build_runtime(config)
    try:
        memories = app._core._commands.memories
        assert memories.length_limit == 42
        assert memories.cap("agent") == 7
        assert app._core._recall._limit == 99
    finally:
        await client.aclose()
