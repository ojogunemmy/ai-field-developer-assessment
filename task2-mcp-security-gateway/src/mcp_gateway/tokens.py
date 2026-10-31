"""Bearer tokens: signed JWTs (HS256) that carry the caller's role.

Tokens are verified with the standard library only, so the security-critical
path has no third-party parsing. A token is accepted only if:

- it has exactly three base64url segments;
- the header's ``alg`` is ``HS256`` (``none`` and every other algorithm are
  refused, which blocks algorithm-confusion attacks);
- the HMAC-SHA256 signature matches, compared in constant time;
- it has an ``exp`` claim that is in the future;
- its ``role`` claim is one of ``ROLES``.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

ROLES = frozenset({"admin", "viewer"})
ALGORITHM = "HS256"


class InvalidToken(Exception):
    """The token is missing, malformed, forged, expired or has no valid role."""


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(segment: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, TypeError) as exc:
        raise InvalidToken("malformed token") from exc


def _sign(signing_input: bytes, secret: bytes) -> str:
    return _b64encode(hmac.new(secret, signing_input, hashlib.sha256).digest())


def issue_token(subject: str, role: str, secret: bytes, ttl_seconds: int = 3600) -> str:
    """Create a signed token. Used by the CLI and the tests."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}")
    header = {"alg": ALGORITHM, "typ": "JWT"}
    claims = {"sub": subject, "role": role, "exp": int(time.time()) + ttl_seconds}
    signing_input = ".".join(
        _b64encode(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
    )
    return f"{signing_input}.{_sign(signing_input.encode(), secret)}"


def _decode_json(segment: str) -> dict[str, Any]:
    try:
        value = json.loads(_b64decode(segment))
    except ValueError as exc:
        raise InvalidToken("malformed token") from exc
    if not isinstance(value, dict):
        raise InvalidToken("malformed token")
    return value


def verify_token(token: str, secret: bytes, now: float | None = None) -> Principal:
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidToken("malformed token")
    header_b64, claims_b64, signature = parts

    header = _decode_json(header_b64)
    if header.get("alg") != ALGORITHM:
        raise InvalidToken("unsupported algorithm")

    expected = _sign(f"{header_b64}.{claims_b64}".encode(), secret)
    if not hmac.compare_digest(expected, signature):
        raise InvalidToken("bad signature")

    claims = _decode_json(claims_b64)
    exp = claims.get("exp")
    if not isinstance(exp, int | float) or isinstance(exp, bool):
        raise InvalidToken("missing expiry")
    if (time.time() if now is None else now) >= exp:
        raise InvalidToken("token expired")

    role = claims.get("role")
    if role not in ROLES:
        raise InvalidToken("unknown role")
    subject = claims.get("sub")
    return Principal(subject=subject if isinstance(subject, str) else "", role=role)


def parse_bearer(authorization: str | None) -> str:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if not authorization:
        raise InvalidToken("missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise InvalidToken("Authorization header is not a Bearer token")
    return token.strip()


def main(argv: list[str] | None = None) -> None:
    """``mcp-gateway-token --role viewer --sub alice``: print a signed token."""
    parser = argparse.ArgumentParser(description="Issue a gateway Bearer token.")
    parser.add_argument("--role", choices=sorted(ROLES), required=True)
    parser.add_argument("--sub", default="cli-user")
    parser.add_argument("--ttl", type=int, default=3600, help="lifetime in seconds")
    args = parser.parse_args(argv)
    secret = os.environ.get("GATEWAY_JWT_SECRET")
    if not secret:
        sys.exit("GATEWAY_JWT_SECRET is not set")
    print(issue_token(args.sub, args.role, secret.encode(), args.ttl))
