import base64
import json

import pytest
from conftest import SECRET

from mcp_gateway.tokens import InvalidToken, issue_token, main, parse_bearer, verify_token


def b64(obj: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_round_trip(role):
    principal = verify_token(issue_token("alice", role, SECRET), SECRET)
    assert principal.role == role
    assert principal.subject == "alice"
    assert principal.is_admin == (role == "admin")


def test_wrong_secret_is_rejected():
    forged = issue_token("mallory", "admin", b"another-secret-of-at-least-32-bytes!!")
    with pytest.raises(InvalidToken, match="bad signature"):
        verify_token(forged, SECRET)


def test_tampered_role_is_rejected():
    header, _claims, signature = issue_token("bob", "viewer", SECRET).split(".")
    tampered = f"{header}.{b64({'sub': 'bob', 'role': 'admin', 'exp': 9999999999})}.{signature}"
    with pytest.raises(InvalidToken, match="bad signature"):
        verify_token(tampered, SECRET)


def test_alg_none_is_rejected():
    unsigned = f"{b64({'alg': 'none'})}.{b64({'role': 'admin', 'exp': 9999999999})}."
    with pytest.raises(InvalidToken, match="unsupported algorithm"):
        verify_token(unsigned, SECRET)


def test_expired_token_is_rejected():
    expired = issue_token("alice", "admin", SECRET, ttl_seconds=-1)
    with pytest.raises(InvalidToken, match="expired"):
        verify_token(expired, SECRET)


def test_token_without_expiry_is_rejected():
    header = b64({"alg": "HS256", "typ": "JWT"})
    claims = b64({"sub": "x", "role": "admin"})
    import hashlib
    import hmac

    signature = (
        base64.urlsafe_b64encode(
            hmac.new(SECRET, f"{header}.{claims}".encode(), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(InvalidToken, match="missing expiry"):
        verify_token(f"{header}.{claims}.{signature}", SECRET)


@pytest.mark.parametrize("token", ["", "abc", "a.b", "a.b.c.d", "!!!.###.$$$", "e30.e30.sig"])
def test_malformed_tokens_are_rejected(token):
    with pytest.raises(InvalidToken):
        verify_token(token, SECRET)


def test_unknown_role_cannot_be_issued():
    with pytest.raises(ValueError):
        issue_token("x", "superuser", SECRET)


@pytest.mark.parametrize(
    ("header", "expected"),
    [("Bearer abc", "abc"), ("bearer abc", "abc"), ("Bearer   abc  ", "abc")],
)
def test_parse_bearer(header, expected):
    assert parse_bearer(header) == expected


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer   ", "abc"])
def test_parse_bearer_rejects(header):
    with pytest.raises(InvalidToken):
        parse_bearer(header)


def test_cli_prints_valid_token(monkeypatch, capsys):
    monkeypatch.setenv("GATEWAY_JWT_SECRET", SECRET.decode())
    main(["--role", "viewer", "--sub", "carol"])
    printed = capsys.readouterr().out.strip()
    assert verify_token(printed, SECRET).subject == "carol"


def test_cli_requires_secret(monkeypatch):
    monkeypatch.delenv("GATEWAY_JWT_SECRET", raising=False)
    with pytest.raises(SystemExit):
        main(["--role", "viewer"])
