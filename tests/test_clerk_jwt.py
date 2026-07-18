"""clerk_jwt.verify_session_token: signature/issuer/exp/nbf/azp checks.

No network calls — ``_jwks_client`` is monkeypatched to hand back the public
half of a locally generated RSA keypair instead of fetching a real JWKS.
"""

from __future__ import annotations

import sys
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "lambda"
if str(LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(LAMBDA_DIR))

ISSUER = "https://clerk.example.com"


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(autouse=True)
def _patch_jwks(monkeypatch, keypair):
    """Route clerk_jwt's JWKS lookup to our local public key, for every test."""
    import clerk_jwt

    clerk_jwt._jwks_client.cache_clear()
    _private, public_key = keypair

    class _FakeSigningKey:
        def __init__(self, key):
            self.key = key

    class _FakeJWKSClient:
        def get_signing_key_from_jwt(self, token):
            # Real PyJWKClient raises PyJWTError-family exceptions for
            # malformed tokens it can't even parse a header from.
            jwt.get_unverified_header(token)
            return _FakeSigningKey(public_key)

    monkeypatch.setattr(clerk_jwt, "_jwks_client", lambda issuer: _FakeJWKSClient())
    yield clerk_jwt


def _make_token(private_key, *, issuer=ISSUER, sub="user_123", exp_delta=3600, nbf_delta=None, azp=None):
    import time

    now = int(time.time())
    payload = {"sub": sub, "iss": issuer, "iat": now, "exp": now + exp_delta}
    if nbf_delta is not None:
        payload["nbf"] = now + nbf_delta
    if azp is not None:
        payload["azp"] = azp
    return jwt.encode(payload, private_key, algorithm="RS256")


def test_valid_token_returns_sub(keypair, _patch_jwks):
    private_key, _ = keypair
    token = _make_token(private_key)
    assert _patch_jwks.verify_session_token(token, ISSUER) == "user_123"


def test_wrong_issuer_rejected(keypair, _patch_jwks):
    private_key, _ = keypair
    token = _make_token(private_key, issuer="https://not-clerk.example.com")
    assert _patch_jwks.verify_session_token(token, ISSUER) is None


def test_expired_token_rejected(keypair, _patch_jwks):
    private_key, _ = keypair
    token = _make_token(private_key, exp_delta=-3600)
    assert _patch_jwks.verify_session_token(token, ISSUER) is None


def test_not_yet_valid_token_rejected(keypair, _patch_jwks):
    private_key, _ = keypair
    token = _make_token(private_key, nbf_delta=3600)
    assert _patch_jwks.verify_session_token(token, ISSUER) is None


def test_azp_outside_allowed_origins_rejected(keypair, _patch_jwks, monkeypatch):
    monkeypatch.setenv("CLERK_ALLOWED_ORIGINS", "https://app.dialin.example")
    private_key, _ = keypair
    token = _make_token(private_key, azp="https://evil.example.com")
    assert _patch_jwks.verify_session_token(token, ISSUER) is None


def test_azp_inside_allowed_origins_accepted(keypair, _patch_jwks, monkeypatch):
    monkeypatch.setenv("CLERK_ALLOWED_ORIGINS", "https://app.dialin.example, https://staging.dialin.example")
    private_key, _ = keypair
    token = _make_token(private_key, azp="https://staging.dialin.example/")
    assert _patch_jwks.verify_session_token(token, ISSUER) == "user_123"


def test_no_allowed_origins_configured_skips_azp_check(keypair, _patch_jwks, monkeypatch):
    monkeypatch.delenv("CLERK_ALLOWED_ORIGINS", raising=False)
    private_key, _ = keypair
    token = _make_token(private_key, azp="https://anything.example.com")
    assert _patch_jwks.verify_session_token(token, ISSUER) == "user_123"


def test_empty_token_or_issuer_short_circuits(_patch_jwks):
    assert _patch_jwks.verify_session_token("", ISSUER) is None
    assert _patch_jwks.verify_session_token("sometoken", "") is None
    assert _patch_jwks.verify_session_token("", "") is None


def test_malformed_token_rejected(_patch_jwks):
    assert _patch_jwks.verify_session_token("not-a-jwt-at-all", ISSUER) is None


def test_wrong_signing_key_rejected(_patch_jwks):
    # Signed by a *different* keypair than the one _patch_jwks serves — signature must fail.
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _make_token(other_key)
    assert _patch_jwks.verify_session_token(token, ISSUER) is None


def test_issuer_trailing_slash_normalized(keypair, _patch_jwks):
    private_key, _ = keypair
    token = _make_token(private_key, issuer=ISSUER)
    assert _patch_jwks.verify_session_token(token, ISSUER + "/") == "user_123"
