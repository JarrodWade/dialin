"""Verify Clerk session JWTs via Frontend API JWKS (no audience required)."""

from __future__ import annotations

import logging
from functools import lru_cache

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _jwks_client(issuer: str) -> PyJWKClient:
    base = issuer.rstrip("/")
    return PyJWKClient(f"{base}/.well-known/jwks.json")


def verify_session_token(token: str, issuer: str) -> str | None:
    """Return Clerk ``sub`` if the token is valid for ``issuer``, else ``None``."""
    issuer = issuer.strip().rstrip("/")
    if not issuer or not token:
        return None
    try:
        signing_key = _jwks_client(issuer).get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False},
            leeway=60,
        )
    except jwt.PyJWTError as e:
        logger.warning("JWT verify failed: %s", e)
        return None
    sub = (payload.get("sub") or "").strip()
    return sub or None
