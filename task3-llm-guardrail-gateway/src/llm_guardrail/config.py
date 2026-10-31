"""Gateway settings, read from environment variables."""

import os
from dataclasses import dataclass

DEFAULT_UPSTREAM_BASE_URL = "http://127.0.0.1:9100/v1"


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str = DEFAULT_UPSTREAM_BASE_URL
    upstream_api_key: str | None = None
    connect_timeout: float = 5.0
    # Maximum wait between chunks, not for the whole response: long generations
    # are fine as long as tokens keep arriving.
    read_timeout: float = 60.0
    max_body_bytes: int = 1_000_000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            upstream_base_url=os.environ.get("UPSTREAM_BASE_URL", DEFAULT_UPSTREAM_BASE_URL),
            upstream_api_key=os.environ.get("UPSTREAM_API_KEY") or None,
            connect_timeout=float(os.environ.get("UPSTREAM_CONNECT_TIMEOUT", "5")),
            read_timeout=float(os.environ.get("UPSTREAM_READ_TIMEOUT", "60")),
        )
