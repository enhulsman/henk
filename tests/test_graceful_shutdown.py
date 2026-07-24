"""Graceful shutdown tests (task 1.9), from specs/secure-deployment.

``docker stop`` sends SIGTERM; without a handler ``asyncio.run`` never unwinds
``App.run``'s ``finally`` and the container is SIGKILLed (``Exited 137``), losing
the open session's audit record. ``serve`` routes SIGTERM/SIGINT to the existing
graceful path so the flush (``core.aclose()``) happens within the stop grace
period. Driven with a fake loop + fake app so no real signal is ever raised.
"""

from __future__ import annotations

import asyncio
import signal

from henk.__main__ import install_shutdown_handlers, serve


class FakeLoop:
    """Captures signal-handler registrations without touching the real loop."""

    def __init__(self) -> None:
        self.handlers: dict[int, object] = {}

    def add_signal_handler(self, sig, callback) -> None:
        self.handlers[sig] = callback


class FakeApp:
    """Mimics App.run's structure: blocks, and flushes via aclose in its finally."""

    def __init__(self) -> None:
        self.aclosed = False
        self.started = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()  # block until cancelled
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        self.aclosed = True


def test_install_shutdown_handlers_registers_sigterm_and_sigint():
    loop = FakeLoop()
    fired: list[int] = []
    installed = install_shutdown_handlers(loop, lambda: fired.append(1))
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    loop.handlers[signal.SIGTERM]()
    assert fired == [1]  # the registered callback is the graceful-stop trigger


def test_install_tolerates_platform_without_signal_handlers():
    class NoSignals:
        def add_signal_handler(self, *_a):
            raise NotImplementedError

    # Must not crash where add_signal_handler is unsupported (e.g. non-main thread).
    assert install_shutdown_handlers(NoSignals(), lambda: None) == []


async def test_sigterm_routes_to_graceful_flush():
    loop = FakeLoop()
    app = FakeApp()
    serve_task = asyncio.create_task(serve(app, loop=loop))
    await app.started.wait()
    assert signal.SIGTERM in loop.handlers and signal.SIGINT in loop.handlers
    loop.handlers[signal.SIGTERM]()  # simulate docker stop
    await asyncio.wait_for(serve_task, timeout=1.0)
    assert app.aclosed is True  # App.run's finally → core.aclose() ran


async def test_sigint_routes_to_graceful_flush_identically():
    loop = FakeLoop()
    app = FakeApp()
    serve_task = asyncio.create_task(serve(app, loop=loop))
    await app.started.wait()
    loop.handlers[signal.SIGINT]()
    await asyncio.wait_for(serve_task, timeout=1.0)
    assert app.aclosed is True
