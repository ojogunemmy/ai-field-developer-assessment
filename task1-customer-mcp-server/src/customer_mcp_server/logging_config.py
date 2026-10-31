"""Application logging configuration.

STDOUT is reserved for MCP JSON-RPC messages when running over STDIO, so all
logs must go to STDERR. Any stray write to stdout would corrupt the protocol
stream.
"""

import logging
import os
import sys

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DEFAULT_LEVEL = "INFO"


def configure_logging(level: str | None = None) -> None:
    """Route all application logs to stderr.

    The level comes from the ``level`` argument, then the ``LOG_LEVEL``
    environment variable, then defaults to INFO.
    """
    level_name = (level or os.environ.get("LOG_LEVEL") or DEFAULT_LEVEL).upper()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root = logging.getLogger()
    # Replace any pre-existing handlers so nothing can fall back to stdout.
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level_name)
