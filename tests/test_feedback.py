"""Chat feedback storage (ddb) and the POST/GET /chat/feedback handler routes."""

from __future__ import annotations

import importlib
import json

USER = "feedback-user-1"


def test_create_and_list_feedback(dynamodb_env):
    ddb = dynamodb_env["ddb"]

    fb = ddb.create_chat_feedback(
        USER,
        user_message="how do I dial in my Guji?",
        bot_message="Grind finer and pull a shorter shot.",
        comment="suggested too fine",
    )
    assert fb["feedbackId"]
    assert fb["comment"] == "suggested too fine"

    rows = ddb.list_chat_feedback(USER, limit=10)
    assert len(rows) == 1
    assert rows[0]["userMessage"] == "how do I dial in my Guji?"
    assert rows[0]["botMessage"] == "Grind finer and pull a shorter shot."


def test_blank_comment_stored_as_none(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    fb = ddb.create_chat_feedback(USER, user_message="hi", bot_message="hello", comment="   ")
    assert fb["comment"] is None


def test_feedback_scoped_per_user(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    ddb.create_chat_feedback("user-a", user_message="a", bot_message="reply-a")
    ddb.create_chat_feedback("user-b", user_message="b", bot_message="reply-b")
    assert len(ddb.list_chat_feedback("user-a")) == 1
    assert len(ddb.list_chat_feedback("user-b")) == 1


def test_feedback_newest_first(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    ddb.create_chat_feedback(USER, user_message="first", bot_message="r1")
    ddb.create_chat_feedback(USER, user_message="second", bot_message="r2")
    rows = ddb.list_chat_feedback(USER, limit=10)
    assert len(rows) == 2
    assert rows[0]["userMessage"] == "second"


def test_handler_roundtrip(dynamodb_env, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")

    import handler

    importlib.reload(handler)

    post = handler._handle_chat_feedback(
        {
            "body": json.dumps(
                {
                    "userId": USER,
                    "userMessage": "dial in advice?",
                    "botMessage": "use a 1:2 ratio",
                    "comment": "ratio was off",
                }
            )
        }
    )
    assert post["statusCode"] == 201

    got = handler._handle_list_chat_feedback({"queryStringParameters": {"userId": USER}})
    assert got["statusCode"] == 200
    body = json.loads(got["body"])
    assert body["count"] == 1
    assert body["feedback"][0]["comment"] == "ratio was off"


def test_handler_requires_bot_message(dynamodb_env, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")

    import handler

    importlib.reload(handler)

    resp = handler._handle_chat_feedback(
        {"body": json.dumps({"userId": USER, "userMessage": "hi"})}
    )
    assert resp["statusCode"] == 400


def test_handler_unauthorized_without_user(dynamodb_env, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "false")

    import handler

    importlib.reload(handler)

    resp = handler._handle_list_chat_feedback({"queryStringParameters": {}})
    assert resp["statusCode"] == 401
