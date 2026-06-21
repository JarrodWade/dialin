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
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(text="1. Onyx — washed Ethiopian")

    monkeypatch.setattr(bedrock, "_run_turn", fake_run_turn)

    out = bedrock.recommend_beans(USER)
    assert out == "1. Onyx — washed Ethiopian"
    assert captured["user_id"] == USER
    assert captured["history"] == []
    # The instruction must steer toward a grounded, guardrailed shortlist.
    assert "For You" in captured["user_text"]
    assert "get_preferences" in captured["user_text"]
    # Must NOT inherit trip-scouting behavior, and must cap web searches so the
    # turn cannot blow past the 30s API timeout.
    assert captured["kwargs"]["force_trip_appendix"] is False
    assert captured["kwargs"]["max_web_searches"] == 2


def test_run_turn_caps_web_searches(dynamodb_env, monkeypatch):
    """A model that keeps requesting search_web must be hard-capped so the turn
    cannot exceed the budget (and thus the 30s API timeout)."""
    import bedrock

    importlib.reload(bedrock)

    class AlwaysSearchClient:
        def converse(self, **kwargs):
            return {
                "stopReason": "tool_use",
                "output": {
                    "message": {
                        "content": [
                            {
                                "toolUse": {
                                    "name": "search_web",
                                    "toolUseId": "tool-1",
                                    "input": {"query": "boutique kenya roasters"},
                                }
                            }
                        ]
                    }
                },
                "usage": {},
            }

    monkeypatch.setattr(bedrock, "_client", AlwaysSearchClient())

    calls = {"search_web": 0}
    real_dispatch = bedrock.tools.dispatch

    def counting_dispatch(name, user_id, args):
        if name == "search_web":
            calls["search_web"] += 1
            return {"ok": True, "results": []}
        return real_dispatch(name, user_id, args)

    monkeypatch.setattr(bedrock.tools, "dispatch", counting_dispatch)

    result = bedrock._run_turn(
        "cap-user",
        [],
        "find me beans",
        force_trip_appendix=False,
        max_web_searches=2,
    )

    # Real searches stop at the budget even though the model never quits asking.
    assert calls["search_web"] == 2
    assert result.hit_iteration_cap is True


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
