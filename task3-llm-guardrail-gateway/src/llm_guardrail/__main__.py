"""Run the gateway: ``python -m llm_guardrail`` or ``llm-guardrail-gateway``."""

import logging
import os
import sys

import uvicorn

from llm_guardrail.config import Settings
from llm_guardrail.gateway import create_app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    settings = Settings.from_env()
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8090"))
    logging.getLogger(__name__).info(
        "Guardrail gateway on http://%s:%s/v1 -> %s", host, port, settings.upstream_base_url
    )
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
