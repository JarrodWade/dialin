"""'For You' recommendations: beans + cafés bedrock entry points and POST handlers."""

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


def test_recommend_cafes_runs_deterministic_pipeline(dynamodb_env, monkeypatch):
    """City mode: server runs capped Reddit city searches, then temp-0 ranking."""
    import bedrock

    importlib.reload(bedrock)

    city = "Chicago"
    monkeypatch.setattr(
        bedrock.ddb,
        "get_profile",
        lambda uid: {
            "favoriteRoasters": ["Sey", "Futuro"],
            "favoriteCafes": ["Intelligentsia"],
            "preferredRoastLevel": "light",
            "experimentalPreference": "seek",
        },
    )
    monkeypatch.setattr(
        bedrock.ddb,
        "list_cafes",
        lambda uid, **kw: [{"name": "Metric"}] if kw.get("city") == city else [],
    )
    monkeypatch.setattr(
        bedrock.ddb,
        "list_roasters",
        lambda uid, **kw: [{"name": "Sey Coffee", "hasCafe": True}] if kw.get("city") == city else [],
    )

    searches: list[dict] = []

    def fake_dispatch(name, uid, args):
        assert name == "search_web"
        searches.append(args)
        return {
            "ok": True,
            "result": {
                "answer": "Try Metric and Sawada.",
                "results": [
                    {"title": "Metric Coffee Chicago", "snippet": "third wave pour-over"},
                    {"title": "Sawada Coffee", "snippet": "roaster cafe"},
                ],
            },
        }

    monkeypatch.setattr(bedrock.tools, "dispatch", fake_dispatch)
    monkeypatch.setattr(bedrock, "_extract_consensus_venues", lambda _t: ["Metric Coffee"])

    captured = {}

    class FakeClient:
        def converse(self, **kwargs):
            captured.update(kwargs)
            return {
                "output": {
                    "message": {
                        "content": [{"text": "**Cafés in Chicago**\n- Metric — pour-over bar"}]
                    }
                }
            }

    monkeypatch.setattr(bedrock, "_client", FakeClient())

    out = bedrock.recommend_cafes(USER, city)
    assert "Metric" in out

    assert len(searches) == bedrock._FOR_YOU_CITY_MAX_SEARCHES
    assert city in searches[0]["query"]
    assert city in searches[1]["query"]
    assert "coffee shops" in searches[0]["query"].lower()
    assert "roasters" in searches[1]["query"].lower()
    for s in searches:
        assert s["includeDomains"] == ["reddit.com"]

    assert captured["inferenceConfig"]["temperature"] == 0.0
    assert "toolConfig" not in captured
    system_text = captured["system"][0]["text"]
    assert "CANDIDATE POOL IS CLOSED" in system_text
    user_block = captured["messages"][0]["content"][0]["text"]
    assert f"RESOLVED DESTINATION" in user_block
    assert "Chicago" in user_block
    assert "CITY-SEARCH RESULTS" in user_block
    assert "Metric Coffee Chicago" in user_block
    assert "CONSENSUS MENTIONS" in user_block
    assert "Roaster-café (already tracked" in user_block
    assert "MULTI-ROASTER BAR SLOT" in system_text


def test_extract_consensus_venues_finds_repeated_names(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    results = (
        "Search: best specialty coffee shops Chicago\n"
        "Summary: Try Metric and Sawada.\n"
        "- thread one: Coffee Movement, Ritual, Saint Frank, and more.\n"
        "- thread two: Coffee Movement (highly recommend), SPRO, etc.\n\n"
        "Search: best coffee roasters Chicago\n"
        "Summary: Sightglass and Andytown.\n"
        "- thread three: Sawada Coffee, Coffee Movement, Metric.\n"
        "- thread four: Sawada Coffee is excellent, try Metric too.\n"
    )
    consensus = bedrock._extract_consensus_venues(results)
    assert any("movement" in c.lower() for c in consensus)
    assert any("sawada" in c.lower() for c in consensus)


def test_extract_consensus_skips_neutral_wishlists(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    results = (
        "Search: best specialty coffee shops Phoenix\n"
        "- Favorite coffee shops around the valley?: The ones I have on my list to still visit are "
        "Jobot, Cartel, Echo, Copper Star, Gold Bar, and Esso.\n"
        "- Favorite coffee shops around the valley?: The ones I have on my list to still visit are "
        "Jobot, Cartel, Echo, Copper Star, Gold Bar, and Esso.\n"
        "- What are your favorite local coffee roasters?: Cartel and Press are the Valley's two "
        "biggest local coffee roasting companies.\n"
    )
    consensus = bedrock._extract_consensus_venues(results)
    assert not any("echo" in c.lower() for c in consensus)
    assert not any("copper" in c.lower() for c in consensus)


def test_extract_praise_venues_finds_xanadu(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    results = (
        "- Favorite coffee shops around the valley?: The ones I have on my list to still visit are "
        "Jobot and Echo. Xanadu was\n"
        "- Phoenix Coffee Tour!: Definitely should add Paircupworks to your list.\n"
    )
    praise = bedrock._extract_praise_venues(results)
    assert any("xanadu" in p.lower() for p in praise)
    assert any("pair" in p.lower() for p in praise)
    assert not any("echo" in p.lower() for p in praise)


def test_format_consensus_block_flags_bar_first(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    results = (
        "- thread: Coffee Movement, Ritual, Saint Frank.\n"
        "- other: Coffee Movement (highly recommend), SPRO.\n"
    )
    block = bedrock._format_consensus_block(["Coffee movement", "Saint Frank"], results)
    assert "bar-first" in block.lower()
    assert "Coffee movement" in block
    assert "Saint Frank" in block
    assert "bar-first" not in block.split("Saint Frank")[1].lower()


def test_parse_destination_splits_region(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    dest = bedrock._parse_destination("Athens, GA")
    assert dest.city == "Athens"
    assert dest.region == "GA"
    assert "Georgia" in dest.search_label


def test_resolve_athens_ambiguity_from_scout(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    dest = bedrock._parse_destination("Athens")
    ga_scout = "1000 Faces Coffee in Athens Georgia is great. UGA area roasters."
    gr_scout = "Kolonaki and Plaka cafes in Athens Greece. Greek specialty coffee."
    assert "Georgia" in bedrock._resolve_destination_region(dest, ga_scout)
    assert "Greece" in bedrock._resolve_destination_region(dest, gr_scout)

    ga_dest = bedrock._parse_destination("Athens, GA")
    assert bedrock._resolve_destination_region(ga_dest, gr_scout) == "Athens, Georgia, USA"


def test_local_anchors_in_city_finds_favorite_roaster(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    dest = bedrock._parse_destination("Phoenix")
    profile = {"favoriteRoasters": ["Moxie Coffee Co", "Sey Coffee"]}
    monkeypatch.setattr(
        bedrock.ddb,
        "list_roasters",
        lambda uid, **kw: [
            {"name": "Moxie Coffee Co", "city": "Phoenix", "hasCafe": True},
            {"name": "Sey Coffee", "city": "Brooklyn", "hasCafe": True},
        ],
    )
    monkeypatch.setattr(bedrock.ddb, "list_cafes", lambda uid, **kw: [])
    anchors = bedrock._local_anchors_in_city("u", dest, profile)
    assert any("moxie" in a.lower() for a in anchors)
    assert not any("sey" in a.lower() for a in anchors)


def test_local_anchors_in_city_finds_favorite_cafe(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    dest = bedrock._parse_destination("Phoenix")
    profile = {"favoriteRoasters": ["Moxie Coffee Co", "Futuro"]}
    monkeypatch.setattr(bedrock.ddb, "list_roasters", lambda uid, **kw: [])
    monkeypatch.setattr(
        bedrock.ddb,
        "list_cafes",
        lambda uid, **kw: [
            {"name": "Moxie Coffee Co. Phoenix", "city": "Phoenix"},
            {"name": "Futuro Phoenix", "city": "Phoenix"},
            {"name": "Satellite Coffee Bar Phoenix", "city": "Phoenix"},
        ],
    )
    anchors = bedrock._local_anchors_in_city("u", dest, profile)
    assert anchors == ["Moxie Coffee Co", "Futuro"]


def test_parse_destination_canonicalizes_phx(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    dest = bedrock._parse_destination("PHX")
    assert dest.raw == "PHX"
    assert dest.city == "Phoenix"
    assert dest.search_label == "Phoenix"
    assert bedrock._is_home_destination(dest, "Phoenix, AZ")


def test_parse_destination_strips_nl_scout_prefix(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    dest = bedrock._parse_destination("give me recommendations in Osaka, Japan")
    assert dest.city == "Osaka"
    assert dest.region == "Japan"
    assert dest.search_label == "Osaka Japan"


def test_parse_destination_rejects_pronoun_city(dynamodb_env, monkeypatch):
    import bedrock
    import pytest

    importlib.reload(bedrock)

    with pytest.raises(ValueError, match="place name"):
        bedrock._parse_destination("recommendations in it is")


def test_parse_destination_takes_last_geo_clause(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    dest = bedrock._parse_destination("please look where it is in Taoyuan")
    assert dest.city == "Taoyuan"


def test_parse_destination_rejects_venue_name(dynamodb_env, monkeypatch):
    import bedrock
    import pytest

    importlib.reload(bedrock)

    with pytest.raises(ValueError, match="venue"):
        bedrock._parse_destination("RBC Coffeehead")
    with pytest.raises(ValueError, match="venue"):
        bedrock._parse_destination("Anchorhead")


def test_clean_consensus_accepts_short_brand_sey(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    assert bedrock._clean_consensus_candidate("Sey") == "Sey"
    results = (
        "- thread one: Sey, Dayglow, and La Cabra.\n"
        "- thread two: SEY, Coffee Project NY, Black Fox.\n"
    )
    consensus = bedrock._extract_consensus_venues(results)
    assert any(c.lower() == "sey" for c in consensus)


def test_brooklyn_matches_nyc_destination(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    dest = bedrock._parse_destination("New York City")
    assert bedrock._city_matches_dest(dest, "Brooklyn")


def test_favorite_mentions_in_results(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    profile = {"favoriteRoasters": ["Sey Coffee", "Futuro"]}
    text = "- thread: Sey and Dayglow serve the best coffee in NYC.\n"
    hits = bedrock._favorite_mentions_in_results(profile, text)
    assert any("sey" in h.lower() for h in hits)


def test_anchor_followup_query_seeds_home_city(dynamodb_env, monkeypatch):
    import bedrock

    importlib.reload(bedrock)

    q = bedrock._anchor_followup_query(
        "Phoenix",
        ["Moxie Coffee Co"],
        is_home=True,
        scout_text="",
    )
    assert "Moxie" in q
    assert "Satellite" in q
    assert "Phoenix" in q


def test_handler_returns_cafe_recommendations(dynamodb_env, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")

    import handler

    importlib.reload(handler)
    monkeypatch.setattr(
        handler.bedrock,
        "recommend_cafes",
        lambda user_id, city: f"- Metric — {city}",
    )

    resp = handler._handle_recommend_cafes(
        {"body": json.dumps({"userId": USER, "city": "Chicago"})}
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["recommendations"] == "- Metric — Chicago"


def test_handler_cafes_requires_city(dynamodb_env, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")

    import handler

    importlib.reload(handler)

    resp = handler._handle_recommend_cafes({"body": json.dumps({"userId": USER})})
    assert resp["statusCode"] == 400


def test_handler_cafes_unauthorized_without_user(dynamodb_env, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "false")

    import handler

    importlib.reload(handler)

    resp = handler._handle_recommend_cafes(
        {"body": json.dumps({"city": "Chicago"})}
    )
    assert resp["statusCode"] == 401


def test_handler_cafes_502_on_model_failure(dynamodb_env, monkeypatch):
    monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
    monkeypatch.setenv("ALLOW_CLIENT_USER_ID", "true")

    import handler

    importlib.reload(handler)

    def boom(user_id, city):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(handler.bedrock, "recommend_cafes", boom)

    resp = handler._handle_recommend_cafes(
        {"body": json.dumps({"userId": USER, "city": "Chicago"})}
    )
    assert resp["statusCode"] == 502
