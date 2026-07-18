"""Shared user-id resolution: Clerk JWT verification or legacy client-supplied userId.

Extracted so the buffered API (``handler.py``, over API Gateway/Lambda event dicts)
and the streaming API (``stream_server.py``, over real HTTP requests once behind
the Lambda Web Adapter) resolve identity identically and can't drift apart.
"""

from __future__ import annotations

import os

import clerk_jwt


def extract_bearer(raw_header: str | None) -> str:
    """Pull the token out of a raw ``Authorization: Bearer <token>`` header value."""
    raw = (raw_header or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def resolve_user_id(
    *,
    bearer_token: str = "",
    body_user_id: str | None = None,
    query_user_id: str | None = None,
) -> str:
    """Resolve the signed-in user id.

    When ``CLERK_JWT_ISSUER`` is set and client ids are not explicitly allowed,
    only a verified Clerk session JWT can establish identity (no impersonation via
    a client-supplied ``userId``). Otherwise falls back to the legacy client-supplied
    ``userId`` (body first, then query) when ``ALLOW_CLIENT_USER_ID`` is true.
    """
    clerk_issuer = (os.environ.get("CLERK_JWT_ISSUER") or "").strip()
    allow_client = os.environ.get("ALLOW_CLIENT_USER_ID", "").lower() in (
        "1",
        "true",
        "yes",
    )

    if clerk_issuer and not allow_client:
        if bearer_token:
            verified = clerk_jwt.verify_session_token(bearer_token, clerk_issuer)
            if verified:
                return verified
        return ""

    if allow_client:
        legacy = (body_user_id or query_user_id or "").strip()
        if legacy:
            return legacy
    return ""
