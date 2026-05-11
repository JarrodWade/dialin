"""Verify Clerk session JWTs via Frontend API JWKS (no audience required).

Security checks:
  - RS256 signature via Clerk JWKS (auto-refreshed every ``_JWKS_LIFESPAN_S``)
  - ``iss`` must match configured issuer
  - ``exp`` / ``nbf`` validated (30 s leeway)
  - ``azp`` (authorized party) checked against ``CLERK_ALLOWED_ORIGINS`` if set
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

_JWKS_LIFESPAN_S = 300


@lru_cache(maxsize=4)
def _jwks_client(issuer: str) -> PyJWKClient:
    base = issuer.rstrip("/")
    return PyJWKClient(f"{base}/.well-known/jwks.json", lifespan=_JWKS_LIFESPAN_S)


def _allowed_origins() -> set[str]:
    raw = os.environ.get("CLERK_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return set()
    return {o.strip().rstrip("/").lower() for o in raw.split(",") if o.strip()}


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
            leeway=30,
        )
    except jwt.PyJWTError as e:
        logger.warning("JWT verify failed: %s", e)
        return None

    origins = _allowed_origins()
    if origins:
        azp = (payload.get("azp") or "").strip().rstrip("/").lower()
        if azp and azp not in origins:
            logger.warning("azp %r not in allowed origins %s", azp, origins)
            return None

    sub = (payload.get("sub") or "").strip()
    return sub or None
