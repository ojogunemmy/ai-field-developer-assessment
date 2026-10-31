"""Entry point: ``python -m customer_mcp_server`` or ``customer-mcp-server``."""

import logging
import os
import signal
from types import FrameType

import anyio

from customer_mcp_server.logging_config import configure_logging
from customer_mcp_server.server import run_stdio

logger = logging.getLogger(__name__)


def _exit_on_interrupt(signum: int, frame: FrameType | None) -> None:  # pragma: no cover
    # Covered by test_stdio.test_sigint_shuts_down_cleanly; os._exit skips the
    # subprocess coverage hook, so the lines cannot be recorded.
    #
    # The SDK reads stdin in a worker thread that cannot be cancelled, so a
    # KeyboardInterrupt would not take effect until the next line of input.
    # All state is in memory and nothing needs flushing, so exit immediately.
    logger.info("Interrupted, shutting down")
    logging.shutdown()
    os._exit(0)


def main() -> None:
    configure_logging()
    signal.signal(signal.SIGINT, _exit_on_interrupt)
    anyio.run(run_stdio)


if __name__ == "__main__":
    main()
