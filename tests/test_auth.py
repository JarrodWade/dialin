"""handler._user_id resolution: JWT authorizer claims, Clerk-enforced mode, and
legacy client-id mode. No network — the Clerk-verify path is exercised only
without a bearer token so it short-circuits to empty."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "lambda"
if str(LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(LAMBDA_DIR))

os.environ.setdefault("TABLE_NAME", "dialin-auth-test")
for _k in ("AWS_DEFAULT_REGION", "AWS_REGION", "BEDROCK_REGION"):
    if not (os.environ.get(_k) or "").strip():
        os.environ[_k] = "us-east-1"


@pytest.fixture(scope="module")
def handler_mod():
    import handler

    importlib.reload(handler)
    return handler


def test_jwt_authorizer_claims_win(handler_mod):
    event = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "user_abc"}}}}}
    assert handler_mod._user_id(event) == "user_abc"


def test_legacy_user_id_from_query_and_body(handler_mod, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")

    assert handler_mod._user_id({"queryStringParameters": {"userId": "jarrod"}}) == "jarrod"
    assert handler_mod._user_id({"body": json.dumps({"userId": "from-body"})}) == "from-body"


def test_clerk_issuer_rejects_client_user_id(handler_mod, monkeypatch):
    monkeypatch.setenv("CLERK_JWT_ISSUER", "https://clerk.example.com")
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "false")

    # No bearer token: client-supplied userId must be ignored (no impersonation).
    assert handler_mod._user_id({"queryStringParameters": {"userId": "attacker"}}) == ""


def test_no_auth_configured_returns_empty(handler_mod, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "false")

    assert handler_mod._user_id({"queryStringParameters": {"userId": "x"}}) == ""
