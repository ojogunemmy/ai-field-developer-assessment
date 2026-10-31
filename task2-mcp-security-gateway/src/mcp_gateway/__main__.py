"""Run the gateway: ``python -m mcp_gateway`` or ``mcp-gateway``."""

import logging
import os
import sys

import uvicorn

from mcp_gateway.config import ConfigError, Settings
from mcp_gateway.proxy import create_app


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
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8080"))
    logging.getLogger(__name__).info(
        "Gateway on http://%s:%s/mcp -> %s", host, port, settings.downstream_url
    )
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
