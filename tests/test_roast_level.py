"""Roast level on coffees: storage, normalization, updates, and snapshot exposure.

Roast level is the first taste-graph signal that powers recommendation
guardrails (don't push a dark roast to a light-only brewer)."""

from __future__ import annotations

import importlib

USER = "roast-user-1"


def test_create_coffee_normalizes_roast_level(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    c = ddb.create_coffee(user_id=USER, roaster="Onyx", name="Geometry", roast_level="Light")
    assert c["roastLevel"] == "light"


def test_roast_level_aliases_and_spacing(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    assert ddb.create_coffee(user_id=USER, roaster="R", name="A", roast_level="Medium Dark")["roastLevel"] == "medium-dark"
    assert ddb.create_coffee(user_id=USER, roaster="R", name="B", roast_level="French")["roastLevel"] == "dark"
    assert ddb.create_coffee(user_id=USER, roaster="R", name="C", roast_level="ultra-light")["roastLevel"] == "ultralight"


def test_unknown_roast_level_kept_lenient(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    c = ddb.create_coffee(user_id=USER, roaster="R", name="D", roast_level="scorched")
    assert c["roastLevel"] == "scorched"


def test_missing_roast_level_is_none(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    c = ddb.create_coffee(user_id=USER, roaster="R", name="E")
    assert c["roastLevel"] is None


def test_update_coffee_sets_roast_level(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    c = ddb.create_coffee(user_id=USER, roaster="R", name="F")
    updated = ddb.update_coffee(USER, c["coffeeId"], {"roastLevel": "Medium"})
    assert updated["roastLevel"] == "medium"


def test_snapshot_exposes_roast_level(dynamodb_env):
    ddb = dynamodb_env["ddb"]
    ddb.create_coffee(user_id=USER, roaster="Onyx", name="Geometry", roast_level="light")

    import bedrock

    importlib.reload(bedrock)
    snapshot = bedrock._journal_snapshot_text(USER)
    assert "roast=light" in snapshot
