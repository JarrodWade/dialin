"""Handler-level smoke tests for POST /chat (_handle_chat): happy path, auth,
validation, message-size, daily-quota, and model-failure branches. Bedrock is
mocked throughout — no live model calls.
"""

from __future__ import annotations

import importlib
import json


def _reload_handler(monkeypatch):
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    import handler

    importlib.reload(handler)
    return handler


def test_chat_happy_path_returns_reply_and_appends_history(dynamodb_env, monkeypatch):
    handler = _reload_handler(monkeypatch)
    monkeypatch.setattr(handler.bedrock, "generate_reply", lambda **kwargs: "Here's your dial-in advice.")

    event = {
        "body": json.dumps(
            {
                "userId": "u1",
                "message": "help me dial in my Kenya",
                "history": [{"role": "USER", "text": "hi"}, {"role": "BOT", "text": "hello!"}],
            }
        )
    }
    resp = handler._handle_chat(event)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["reply"] == "Here's your dial-in advice."
    assert body["history"][-2] == {"role": "USER", "text": "help me dial in my Kenya"}
    assert body["history"][-1] == {"role": "BOT", "text": "Here's your dial-in advice."}
    assert len(body["history"]) == 4


def test_chat_unauthorized_without_user(dynamodb_env, monkeypatch):
    handler = _reload_handler(monkeypatch)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "false")

    resp = handler._handle_chat({"body": json.dumps({"message": "hi"})})
    assert resp["statusCode"] == 401


def test_chat_missing_message_returns_400(dynamodb_env, monkeypatch):
    handler = _reload_handler(monkeypatch)

    resp = handler._handle_chat({"body": json.dumps({"userId": "u1", "message": ""})})
    assert resp["statusCode"] == 400
    assert "message" in json.loads(resp["body"])["error"]


def test_chat_oversized_message_returns_413(dynamodb_env, monkeypatch):
    monkeypatch.setenv("CHAT_MESSAGE_MAX_CHARS", "20")
    handler = _reload_handler(monkeypatch)

    resp = handler._handle_chat({"body": json.dumps({"userId": "u1", "message": "x" * 21})})
    assert resp["statusCode"] == 413
    body = json.loads(resp["body"])
    assert body["code"] == "MESSAGE_TOO_LONG"


def test_chat_daily_quota_exceeded_returns_429_without_calling_model(dynamodb_env, monkeypatch):
    monkeypatch.setenv("CHAT_DAILY_LIMIT_PER_USER", "1")
    handler = _reload_handler(monkeypatch)

    calls = {"n": 0}
    monkeypatch.setattr(handler.bedrock, "generate_reply", lambda **kwargs: calls.__setitem__("n", calls["n"] + 1) or "ok")

    event = {"body": json.dumps({"userId": "quota-user", "message": "hello"})}
    first = handler._handle_chat(event)
    assert first["statusCode"] == 200
    assert calls["n"] == 1

    second = handler._handle_chat(event)
    assert second["statusCode"] == 429
    body = json.loads(second["body"])
    assert body["code"] == "CHAT_QUOTA_EXCEEDED"
    assert body["limit"] == 1
    # Quota check short-circuits before the model call — no extra Bedrock spend.
    assert calls["n"] == 1


def test_chat_model_failure_returns_502_and_refunds_quota(dynamodb_env, monkeypatch):
    monkeypatch.setenv("CHAT_DAILY_LIMIT_PER_USER", "1")
    handler = _reload_handler(monkeypatch)

    def boom(**kwargs):
        raise RuntimeError("bedrock unavailable")

    monkeypatch.setattr(handler.bedrock, "generate_reply", boom)

    event = {"body": json.dumps({"userId": "refund-user", "message": "hello"})}
    resp = handler._handle_chat(event)
    assert resp["statusCode"] == 502

    # The failed turn's quota reservation was refunded, so a retry (with a
    # working model) still succeeds under the same daily_limit=1 cap.
    monkeypatch.setattr(handler.bedrock, "generate_reply", lambda **kwargs: "recovered")
    retry = handler._handle_chat(event)
    assert retry["statusCode"] == 200
    assert json.loads(retry["body"])["reply"] == "recovered"


def test_chat_passes_trimmed_history_and_client_timezone_to_bedrock(dynamodb_env, monkeypatch):
    handler = _reload_handler(monkeypatch)
    captured = {}

    def fake_reply(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(handler.bedrock, "generate_reply", fake_reply)

    long_history = [{"role": "USER" if i % 2 == 0 else "BOT", "text": f"msg {i}"} for i in range(30)]
    event = {
        "body": json.dumps(
            {
                "userId": "u1",
                "message": "what's next",
                "history": long_history,
                "clientTimezone": "America/Los_Angeles",
            }
        )
    }
    resp = handler._handle_chat(event)
    assert resp["statusCode"] == 200
    assert captured["client_timezone"] == "America/Los_Angeles"
    # _HISTORY_TURN_LIMIT caps the window sent to the model.
    assert len(captured["history"]) <= handler._HISTORY_TURN_LIMIT
    assert captured["user_text"] == "what's next"
