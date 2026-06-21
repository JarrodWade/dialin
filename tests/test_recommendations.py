"""'For You' bean recommendations: bedrock entry point + POST /recommendations/beans."""

from __future__ import annotations

import importlib
import json

USER = "rec-user-1"


def test_recommend_beans_runs_deterministic_pipeline(dynamodb_env, monkeypatch):
    """The deterministic pipeline: server seeds the peer search from MY roasters,
    runs a capped number of searches, then a single tool-less, temperature-0
    model call ranks/formats strictly from those candidates."""
    import bedrock

    importlib.reload(bedrock)

    # Favorites lead; logged roasters follow. "Sey" appears in both and must
    # dedupe to a single seed.
    monkeypatch.setattr(
        bedrock.ddb,
        "get_profile",
        lambda uid: {
            "favoriteRoasters": ["Sey", "Futuro"],
            "preferredRoastLevel": "light",
            "dislikedNotes": ["ashy"],
            "experimentalPreference": "seek",
        },
    )
    monkeypatch.setattr(
        bedrock.ddb,
        "list_roasters",
        lambda uid, **kw: [{"name": "Moxie"}, {"name": "Sey"}],
    )

    searches: list[dict] = []

    def fake_dispatch(name, uid, args):
        assert name == "search_web"
        searches.append(args)
        # tools.dispatch wraps a successful payload as {"ok": True, "result": {...}}.
        return {
            "ok": True,
            "result": {
                "answer": "Try Hydrangea and Prodigal.",
                "results": [
                    {"title": "Hydrangea Coffee", "snippet": "ultra-light clarity"},
                    {"title": "Prodigal", "snippet": "Scott Rao precision"},
                ],
            },
        }

    monkeypatch.setattr(bedrock.tools, "dispatch", fake_dispatch)

    captured = {}

    class FakeClient:
        def converse(self, **kwargs):
            captured.update(kwargs)
            return {
                "output": {
                    "message": {"content": [{"text": "**North America**\n- Hydrangea — clarity"}]}
                }
            }

    monkeypatch.setattr(bedrock, "_client", FakeClient())

    out = bedrock.recommend_beans(USER)
    assert "Hydrangea" in out

    # Server ran the capped peer search, seeded with my roaster names (deduped).
    assert len(searches) == bedrock._FOR_YOU_MAX_SEARCHES
    q0 = searches[0]["query"]
    assert q0.startswith("roasters like ")
    for name in ("Sey", "Futuro", "Moxie"):
        assert name in q0
    assert q0.count("Sey") == 1
    # Reddit-scoped retrieval is the key quality lever — every search must restrict
    # to the community domain rather than open-web SEO listicles.
    for s in searches:
        assert s["includeDomains"] == ["reddit.com"]

    # Ranking call is deterministic (temperature 0), tool-less, closed candidate pool.
    assert captured["inferenceConfig"]["temperature"] == 0.0
    assert "toolConfig" not in captured
    system_text = captured["system"][0]["text"]
    assert "CANDIDATE POOL IS CLOSED" in system_text
    user_block = captured["messages"][0]["content"][0]["text"]
    assert "PEER-SEARCH RESULTS" in user_block
    assert "Hydrangea Coffee" in user_block  # live search results fed to the ranker
    assert "DO NOT recommend these back" in user_block
    assert "Sey" in user_block  # known roasters are the exclusion list


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
