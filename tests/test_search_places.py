"""Place search and visit name filtering (roaster vs cafe split)."""

from __future__ import annotations

USER = "search-places-user"


def test_search_places_finds_roaster_primary_and_visits(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    tools = dynamodb_env["tools"]

    roaster = ddb.create_roaster(user_id=USER, name="Anchorhead Coffee", city="Austin, TX")
    rid = roaster["roasterId"]
    ddb.log_visit(
        user_id=USER,
        roaster_id=rid,
        place_name="Anchorhead Coffee",
        visit_date="2026-05-16",
        drinks=["Colombian pour-over"],
        rating=9,
    )

    cafes = ddb.list_cafes(USER, name_contains="Anchor")
    assert cafes == []

    places = ddb.search_places(USER, name_contains="Anchor")
    assert len(places) == 1
    assert places[0]["placeType"] == "roaster"
    assert places[0]["roasterId"] == rid

    via_tool = tools.dispatch("search_places", USER, {"nameContains": "Anchor"})
    assert via_tool["ok"] is True
    assert via_tool["result"]["count"] == 1
    assert via_tool["result"]["places"][0]["visitCount"] == 1

    by_name = ddb.list_visits(USER, place_name_contains="anchor", limit=10)
    assert len(by_name) == 1
    assert by_name[0]["roasterId"] == rid
