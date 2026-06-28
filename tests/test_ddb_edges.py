"""Dynamo edge cases: duplicates, equipment dedup, quotas."""

from __future__ import annotations

USER = "edges-user-1"


def test_duplicate_cafe_blocks_matching_roaster(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    cafe = ddb.create_cafe(user_id=USER, name="Heart Coffee", city="Portland, OR")
    assert cafe["cafeId"]
    hit = ddb.find_matching_roaster_for_new_cafe(USER, "Heart Coffee", "Portland")
    assert hit is None  # cafe exists; roaster duplicate is for new cafe creation path
    hit_ro = ddb.find_matching_cafe_for_new_roaster(USER, "Heart Coffee", "Portland")
    assert hit_ro and hit_ro["cafeId"] == cafe["cafeId"]


def test_hario_v60_brewer_variant_merges(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    first, _ = ddb.create_equipment(USER, "BREWER", "Hario V60 01")
    second, meta = ddb.create_equipment(USER, "BREWER", "Hario V60 02")
    assert second["equipId"] == first["equipId"]
    assert meta and meta.get("replacedVariant") is True
    brewers = ddb.list_equipment(USER, equip_type="BREWER")
    assert len(brewers) == 1


def test_websearch_cache_and_monthly_quota(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    q = "test query unique cache key xyzzy"
    assert ddb.websearch_cache_get(q, [], 5) is None
    ddb.websearch_cache_put(q, [], 5, {"results": [{"title": "A"}]}, 3600)
    cached = ddb.websearch_cache_get(q, [], 5)
    assert cached and cached["results"][0]["title"] == "A"

    allowed1, c1 = ddb.consume_websearch_quota(USER, 2)
    allowed2, c2 = ddb.consume_websearch_quota(USER, 2)
    allowed3, c3 = ddb.consume_websearch_quota(USER, 2)
    assert allowed1 and allowed2 and not allowed3
    assert c1 == 1 and c2 == 2 and c3 == 2


def test_list_roasters_city_filter_kyoto(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    ddb.create_roaster(user_id=USER, name="Weekenders Coffee", city="Kyoto, Japan", has_cafe=True)
    ddb.create_roaster(user_id=USER, name="Other Roaster", city="Portland, OR")

    kyoto = ddb.list_roasters(USER, city="Kyoto")
    assert len(kyoto) == 1
    assert "Weekenders" in kyoto[0]["name"]


def test_search_known_roasters_weekenders_kyoto(dynamodb_env):
    tools = dynamodb_env["tools"]
    out = tools.dispatch("search_known_roasters", USER, {"query": "weekenders", "city": "Kyoto"})
    assert out["ok"] is True
    names = [r["name"] for r in out["result"]["results"]]
    assert any("Weekenders" in n for n in names)
    assert not any("Indianapolis" in str(r) for r in out["result"]["results"])
    assert "webLookup" not in out["result"]


def test_search_known_roasters_falls_back_to_web(dynamodb_env, monkeypatch):
    tools = dynamodb_env["tools"]

    def fake_web(_user_id, args):
        assert "Foxtail" in args["query"]
        return {
            "query": args["query"],
            "answer": "Foxtail Coffee is a specialty roaster based in Orlando, Florida.",
            "results": [
                {
                    "title": "Foxtail Coffee",
                    "url": "https://foxtailcoffee.com",
                    "snippet": "Orlando specialty coffee roaster.",
                    "score": 0.9,
                }
            ],
        }

    monkeypatch.setattr(tools, "_search_web", fake_web)
    out = tools.dispatch("search_known_roasters", USER, {"query": "Foxtail"})
    assert out["ok"] is True
    body = out["result"]
    assert body["count"] == 0
    assert body["source"] == "curated_then_web"
    assert "Orlando" in body["webLookup"]["answer"]
    assert body["webLookup"]["results"][0]["title"] == "Foxtail Coffee"


def test_search_known_roasters_dak_curated(dynamodb_env, monkeypatch):
    tools = dynamodb_env["tools"]
    web_called: list[str] = []

    def spy_web(_user_id, args):
        web_called.append(args.get("query", ""))
        return {"query": args["query"], "answer": "should not run", "results": []}

    monkeypatch.setattr(tools, "_search_web", spy_web)
    out = tools.dispatch("search_known_roasters", USER, {"query": "DAK"})
    assert out["ok"] is True
    names = [r["name"] for r in out["result"]["results"]]
    assert any("Dak" in n for n in names)
    assert out["result"]["results"][0]["city"] == "Amsterdam"
    assert "webLookup" not in out["result"]
    assert not web_called


def test_chat_daily_quota(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    ok1, n1 = ddb.consume_chat_quota(USER, 2)
    ok2, n2 = ddb.consume_chat_quota(USER, 2)
    ok3, n3 = ddb.consume_chat_quota(USER, 2)
    assert ok1 and ok2 and not ok3
    assert n1 == 1 and n2 == 2 and n3 == 2
    ok_u, _ = ddb.consume_chat_quota(USER, 0)
    assert ok_u is True
