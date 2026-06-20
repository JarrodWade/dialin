"""'For You' bean recommendations: bedrock entry point + POST /recommendations/beans."""

from __future__ import annotations

import importlib
import json
import types

USER = "rec-user-1"


def test_recommend_beans_runs_scoped_turn(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    captured = {}

    def fake_run_turn(user_id, history, user_text, **kwargs):
        captured["user_id"] = user_id
        captured["history"] = history
        captured["user_text"] = user_text
        return types.SimpleNamespace(text="1. Onyx — washed Ethiopian")

    monkeypatch.setattr(bedrock, "_run_turn", fake_run_turn)

    out = bedrock.recommend_beans(USER)
    assert out == "1. Onyx — washed Ethiopian"
    assert captured["user_id"] == USER
    assert captured["history"] == []
    # The instruction must steer toward a grounded, guardrailed shortlist.
    assert "For You" in captured["user_text"]
    assert "get_preferences" in captured["user_text"]


def test_handler_returns_recommendations(dynamodb_env, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")

    import handler

    importlib.reload(handler)
    monkeypatch.setattr(handler.bedrock, "recommend_beans", lambda user_id: "- Sey — light Kenyan")

    resp = handler._handle_recommend_beans({"body": json.dumps({"userId": USER})})
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["recommendations"] == "- Sey — light Kenyan"


def test_handler_unauthorized_without_user(dynamodb_env, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "false")

    import handler

    importlib.reload(handler)

    resp = handler._handle_recommend_beans({"body": "{}"})
    assert resp["statusCode"] == 401


def test_handler_502_on_model_failure(dynamodb_env, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")

    import handler

    importlib.reload(handler)

    def boom(user_id):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(handler.bedrock, "recommend_beans", boom)

    resp = handler._handle_recommend_beans({"body": json.dumps({"userId": USER})})
    assert resp["statusCode"] == 502
