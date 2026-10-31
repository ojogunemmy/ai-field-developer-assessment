"""Run the router: ``python -m llm_router`` or ``llm-router``."""

import logging
import os
import sys

import uvicorn

from llm_router.app import create_app
from llm_router.config import ConfigError, Settings


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"Configuration error: {exc}")
    host = os.environ.get("ROUTER_HOST", "127.0.0.1")
    port = int(os.environ.get("ROUTER_PORT", "8070"))
    logging.getLogger(__name__).info(
        "Router on http://%s:%s/v1 (primary %s, secondary %s, db %s)",
        host,
        port,
        settings.primary.base_url,
        settings.secondary.base_url,
        settings.db_path,
    )
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
