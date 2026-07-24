"""Entrypoint: ``python -m henk``.

Loads config (path from ``HENK_CONFIG``, default ``config.yaml``), wires the
runtime, and runs the receive→dispatch→reply loop until interrupted. Secrets are
read from the process environment; the Anthropic credential is consumed by the
SDK directly (``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any, Callable

from henk.config import Config
from henk.runtime import build_runtime

logger = logging.getLogger("henk")

#: docker stop → SIGTERM; interactive Ctrl-C → SIGINT. Both must flush cleanly.
_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def install_shutdown_handlers(loop: Any, on_signal: Callable[[], None]) -> list:
    """Register ``on_signal`` for SIGTERM/SIGINT; return the signals installed.

    Tolerates platforms/contexts where ``add_signal_handler`` is unavailable
    (e.g. a non-main thread, Windows) — it logs and skips rather than crashing.
    """
    installed = []
    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, on_signal)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - platform
            logger.warning("could not install shutdown handler for %s", sig)
    return installed


async def serve(app: Any, *, loop: Any | None = None) -> None:
    """Run ``app`` until it exits or a shutdown signal fires.

    On a signal, the run task is cancelled so ``App.run``'s ``finally`` unwinds —
    cancelling the receive loop + coordinator and flushing the open session's
    audit record via ``core.aclose()`` — within ``docker stop``'s grace period,
    instead of escalating to SIGKILL (secure-deployment delta).
    """
    loop = loop or asyncio.get_running_loop()
    stop = asyncio.Event()
    install_shutdown_handlers(loop, stop.set)
    run_task = asyncio.create_task(app.run())
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait(
            {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        if not run_task.done():
            run_task.cancel()
        try:
            await run_task  # let App.run's finally flush before we exit
        except asyncio.CancelledError:
            pass
        stop_task.cancel()
        try:
            await stop_task
        except asyncio.CancelledError:
            pass


async def _amain(config: Config) -> None:
    app, client = build_runtime(config)
    async with client:
        await serve(app)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("HENK_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config_path = os.environ.get("HENK_CONFIG", "config.yaml")
    logger.info("starting henk with config=%s", config_path)
    config = Config.load(config_path)
    try:
        asyncio.run(_amain(config))
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("shutting down")


if __name__ == "__main__":
    main()
