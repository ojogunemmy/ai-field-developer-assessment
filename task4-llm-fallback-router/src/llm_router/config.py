"""Router settings, read from environment variables."""

import hashlib
import os
from dataclasses import dataclass, field

from llm_router.ratelimit import DEFAULT_LIMIT, DEFAULT_WINDOW_SECONDS

DEFAULT_TIMEOUT_MS = 3000


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str | None = None
    model: str | None = None  # overrides the request's "model" when set
    timeout_ms: int = DEFAULT_TIMEOUT_MS

    @property
    def timeout_seconds(self) -> float:
        return self.timeout_ms / 1000


def tenant_id(api_key: str) -> str:
    """Stable tenant identifier. Raw API keys are never stored or logged."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Settings:
    primary: ProviderConfig
    secondary: ProviderConfig
    tenant_keys: frozenset[str]
    db_path: str = "router.db"
    tokens_per_window: int = DEFAULT_LIMIT
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    tenants_by_hash: dict[str, str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.tenant_keys:
            raise ConfigError("at least one tenant API key is required")
        object.__setattr__(
            self, "tenants_by_hash", {tenant_id(key): tenant_id(key) for key in self.tenant_keys}
        )

    def tenant_for(self, api_key: str) -> str | None:
        return self.tenants_by_hash.get(tenant_id(api_key))

    @classmethod
    def from_env(cls) -> "Settings":
        keys = frozenset(
            k.strip() for k in os.environ.get("ROUTER_TENANT_KEYS", "").split(",") if k.strip()
        )
        if not keys:
            raise ConfigError("ROUTER_TENANT_KEYS is not set (comma-separated API keys)")
        timeout_ms = int(os.environ.get("ROUTER_PRIMARY_TIMEOUT_MS", DEFAULT_TIMEOUT_MS))

        def provider(prefix: str, default_url: str) -> ProviderConfig:
            return ProviderConfig(
                name=prefix.lower(),
                base_url=os.environ.get(f"{prefix}_BASE_URL", default_url),
                api_key=os.environ.get(f"{prefix}_API_KEY") or None,
                model=os.environ.get(f"{prefix}_MODEL") or None,
                timeout_ms=timeout_ms,
            )

        return cls(
            primary=provider("PRIMARY", "http://127.0.0.1:9201/v1"),
            secondary=provider("SECONDARY", "http://127.0.0.1:9202/v1"),
            tenant_keys=keys,
            db_path=os.environ.get("ROUTER_DB_PATH", "router.db"),
            tokens_per_window=int(os.environ.get("ROUTER_TOKENS_PER_MINUTE", DEFAULT_LIMIT)),
        )
