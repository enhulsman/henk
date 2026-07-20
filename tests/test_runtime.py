"""The production wiring assembles without opening any connection (task 5.1)."""

from __future__ import annotations

import httpx

from henk.app import App
from henk.config import Config
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
