"""End-to-end journal flow against moto DynamoDB (no Bedrock)."""

from __future__ import annotations

USER = "golden-user-1"


def test_journal_golden_path(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    tools = dynamodb_env["tools"]

    roaster = ddb.create_roaster(user_id=USER, name="Onibus", city="Portland, OR")
    assert roaster["roasterId"]

    coffee = ddb.create_coffee(
        user_id=USER,
        roaster="Onibus",
        name="Ethiopia Guji",
        roaster_id=roaster["roasterId"],
        weight_g=340,
        process="washed",
    )
    assert float(coffee["gramsRemaining"]) == 340

    brew = ddb.create_brew(
        user_id=USER,
        coffee_id=coffee["coffeeId"],
        method="V60",
        dose_g=15,
        water_g=250,
        rating=8,
        taste="bright, slightly sour",
    )
    assert brew["brewId"]

    after = ddb.get_coffee(USER, coffee["coffeeId"])
    assert float(after["gramsRemaining"]) == 325

    cafe = ddb.create_cafe(user_id=USER, name="Heart Coffee", city="Portland")
    visit = ddb.log_visit(
        user_id=USER,
        cafe_id=cafe["cafeId"],
        visit_date="2026-05-01",
        drinks=["pour-over"],
        rating=9,
        notes="excellent",
    )
    assert visit["visitId"]

    advice = tools.dispatch(
        "get_dialin_advice",
        USER,
        {"coffeeId": coffee["coffeeId"], "method": "V60"},
    )
    assert advice["ok"] is True
    assert advice["result"]["brewCount"] == 1
    assert advice["result"]["lastBrew"]["brewId"] == brew["brewId"]

    visits = ddb.list_visits(USER, cafe_id=cafe["cafeId"], limit=5)
    assert len(visits) == 1
    assert visits[0]["rating"] == 9

    listed = tools.dispatch("list_coffees", USER, {})
    assert listed["ok"] is True
    assert listed["result"]["count"] == 1


def test_visit_near_duplicate_merge(dynamodb_env):
    ddb = dynamodb_env["ddb"]

    cafe = ddb.create_cafe(user_id=USER, name="Merge Test Cafe", city="Austin")
    cid = cafe["cafeId"]

    v1 = ddb.log_visit(
        user_id=USER,
        cafe_id=cid,
        visit_date="2026-05-10",
        drinks=["latte"],
        rating=7,
    )
    v2 = ddb.log_visit(
        user_id=USER,
        cafe_id=cid,
        visit_date="2026-05-10",
        drinks=["espresso"],
        rating=9,
        notes="second round",
    )
    assert v2["visitId"] == v1["visitId"]
    assert "espresso" in v2.get("drinks", [])
    assert v2["rating"] == 9

    rows = ddb.list_visits(USER, cafe_id=cid, limit=10)
    assert len(rows) == 1


def test_delete_brew_restores_stock(dynamodb_env):
    ddb = dynamodb_env["ddb"]

    coffee = ddb.create_coffee(
        user_id=USER,
        roaster="Test Roaster",
        name="Stock Restore",
        weight_g=100,
    )
    cid = coffee["coffeeId"]
    brew = ddb.create_brew(user_id=USER, coffee_id=cid, method="V60", dose_g=20)
    assert float(ddb.get_coffee(USER, cid)["gramsRemaining"]) == 80

    ddb.delete_brew(USER, brew["brewId"])
    assert float(ddb.get_coffee(USER, cid)["gramsRemaining"]) == 100


def test_update_brew_dose_adjusts_stock(dynamodb_env):
    ddb = dynamodb_env["ddb"]

    coffee = ddb.create_coffee(
        user_id=USER,
        roaster="Test Roaster",
        name="Dose Edit",
        weight_g=100,
    )
    cid = coffee["coffeeId"]
    brew = ddb.create_brew(user_id=USER, coffee_id=cid, method="V60", dose_g=20)
    assert float(ddb.get_coffee(USER, cid)["gramsRemaining"]) == 80

    ddb.update_brew(USER, brew["brewId"], {"doseG": 25})
    assert float(ddb.get_coffee(USER, cid)["gramsRemaining"]) == 75

    ddb.update_brew(USER, brew["brewId"], {"doseG": 15})
    assert float(ddb.get_coffee(USER, cid)["gramsRemaining"]) == 85


def test_delete_coffee_cascades_brews(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    tools = dynamodb_env["tools"]

    coffee = ddb.create_coffee(
        user_id=USER,
        roaster="Test Roaster",
        name="Cascade Delete",
        weight_g=200,
    )
    cid = coffee["coffeeId"]
    b1 = ddb.create_brew(user_id=USER, coffee_id=cid, method="V60", dose_g=10)
    b2 = ddb.create_brew(user_id=USER, coffee_id=cid, method="Espresso", dose_g=18)

    deleted_brew_ids = ddb.delete_coffee(USER, cid)
    assert set(deleted_brew_ids) == {b1["brewId"], b2["brewId"]}
    assert ddb.get_coffee(USER, cid) is None
    assert ddb.list_brews(USER, coffee_id=cid, limit=10) == []

    advice = tools.dispatch(
        "get_dialin_advice",
        USER,
        {"coffeeId": cid, "method": "V60"},
    )
    assert advice["ok"] is False


def test_get_brew_paginates_past_first_query_page(dynamodb_env):
    """Regression: brew lookup must not stop after the first 200 SK rows."""
    ddb = dynamodb_env["ddb"]

    coffee = ddb.create_coffee(user_id=USER, roaster="R", name="Pager")
    cid = coffee["coffeeId"]
    target_id = None
    for i in range(210):
        brew = ddb.create_brew(user_id=USER, coffee_id=cid, method="V60")
        if i == 209:
            target_id = brew["brewId"]

    assert target_id
    found = ddb.get_brew(USER, target_id)
    assert found is not None
    assert found["brewId"] == target_id
    ddb.update_brew(USER, target_id, {"notes": "edited after deep scan"})
    assert ddb.get_brew(USER, target_id)["notes"] == "edited after deep scan"


def test_list_brews_includes_coffee_labels(dynamodb_env):
    ddb = dynamodb_env["ddb"]

    coffee = ddb.create_coffee(user_id=USER, roaster="Onibus", name="Kenya AA")
    ddb.create_brew(user_id=USER, coffee_id=coffee["coffeeId"], method="V60")

    brews = ddb.list_brews(USER, limit=5)
    assert len(brews) >= 1
    hit = next(b for b in brews if b["coffeeId"] == coffee["coffeeId"])
    assert hit["coffeeName"] == "Kenya AA"
    assert hit["coffeeRoaster"] == "Onibus"
