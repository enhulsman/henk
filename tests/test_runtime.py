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


async def test_events_disabled_skips_all_durability_wiring():
    base = Config.load(SAMPLE, env={})
    config = dataclasses.replace(
        base, events=dataclasses.replace(base.events, enabled=False)
    )
    app, client = build_runtime(config)
    try:
        assert app._coordinator is None       # subscriber never starts (v1 behaviour)
        assert app._core._checkpoint is None  # no checkpoint, no rehydration
        assert app._core._audit is None
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
