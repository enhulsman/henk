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

from henk.config import Config
from henk.runtime import build_runtime

logger = logging.getLogger("henk")


async def _amain(config: Config) -> None:
    app, client = build_runtime(config)
    async with client:
        await app.run()


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
