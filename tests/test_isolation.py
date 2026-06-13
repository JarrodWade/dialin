"""Tenant isolation: GSI-keyed reads must not leak across users, and the
atomic brew write must not persist a brew when stock is insufficient."""

from __future__ import annotations

import pytest

OWNER = "iso-owner"
OTHER = "iso-other"


def test_list_brews_by_coffee_id_scoped_to_owner(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    coffee = ddb.create_coffee(user_id=OWNER, roaster="R", name="Owned", weight_g=100)
    cid = coffee["coffeeId"]
    ddb.create_brew(user_id=OWNER, coffee_id=cid, method="V60", dose_g=15, rating=8)

    # Another user who somehow knows the coffeeId must not read its brews.
    assert ddb.list_brews(OTHER, coffee_id=cid, limit=10) == []
    assert len(ddb.list_brews(OWNER, coffee_id=cid, limit=10)) == 1


def test_list_visits_by_place_id_scoped_to_owner(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    cafe = ddb.create_cafe(user_id=OWNER, name="Owned Cafe", city="Portland")
    pid = cafe["cafeId"]
    ddb.log_visit(user_id=OWNER, cafe_id=pid, visit_date="2026-01-01", rating=9)

    assert ddb.list_visits(OTHER, cafe_id=pid, limit=10) == []
    assert len(ddb.list_visits(OWNER, cafe_id=pid, limit=10)) == 1


def test_summarize_and_advice_reject_foreign_coffee(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    tools = dynamodb_env["tools"]
    coffee = ddb.create_coffee(user_id=OWNER, roaster="R", name="Owned", weight_g=100)
    cid = coffee["coffeeId"]
    ddb.create_brew(user_id=OWNER, coffee_id=cid, method="V60", dose_g=15, rating=8)

    with pytest.raises(ValueError):
        ddb.summarize_coffee(OTHER, cid)

    advice = tools.dispatch("get_dialin_advice", OTHER, {"coffeeId": cid, "method": "V60"})
    assert advice["ok"] is False


def test_create_brew_insufficient_stock_is_atomic(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    coffee = ddb.create_coffee(user_id=OWNER, roaster="R", name="Tiny", weight_g=10)
    cid = coffee["coffeeId"]

    with pytest.raises(ValueError):
        ddb.create_brew(user_id=OWNER, coffee_id=cid, method="V60", dose_g=20)

    # Neither the stock nor the brew timeline should have changed.
    assert float(ddb.get_coffee(OWNER, cid)["gramsRemaining"]) == 10
    assert ddb.list_brews(OWNER, coffee_id=cid, limit=10) == []


def test_create_brew_untracked_stock_still_logs(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    coffee = ddb.create_coffee(user_id=OWNER, roaster="R", name="No Weight")
    cid = coffee["coffeeId"]
    assert ddb.get_coffee(OWNER, cid)["gramsRemaining"] is None

    brew = ddb.create_brew(user_id=OWNER, coffee_id=cid, method="V60", dose_g=18)
    assert brew["brewId"]
    assert len(ddb.list_brews(OWNER, coffee_id=cid, limit=10)) == 1
