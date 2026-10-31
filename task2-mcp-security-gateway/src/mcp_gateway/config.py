"""Gateway settings, read from environment variables."""

import os
from dataclasses import dataclass

DEFAULT_DOWNSTREAM_URL = "http://127.0.0.1:9001/mcp"
DEFAULT_MAX_BODY_BYTES = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 30.0
MIN_SECRET_BYTES = 32


class ConfigError(Exception):
    """Raised when required configuration is missing or unsafe."""


@dataclass(frozen=True)
class Settings:
    jwt_secret: bytes
    downstream_url: str = DEFAULT_DOWNSTREAM_URL
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    downstream_timeout: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if len(self.jwt_secret) < MIN_SECRET_BYTES:
            raise ConfigError(f"GATEWAY_JWT_SECRET must be at least {MIN_SECRET_BYTES} bytes long")

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("GATEWAY_JWT_SECRET")
        if not secret:
            # Refuse to start rather than fall back to a guessable default.
            raise ConfigError("GATEWAY_JWT_SECRET is not set")
        return cls(
            jwt_secret=secret.encode(),
            downstream_url=os.environ.get("GATEWAY_DOWNSTREAM_URL", DEFAULT_DOWNSTREAM_URL),
            max_body_bytes=int(os.environ.get("GATEWAY_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)),
            downstream_timeout=float(
                os.environ.get("GATEWAY_DOWNSTREAM_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
            ),
        )
