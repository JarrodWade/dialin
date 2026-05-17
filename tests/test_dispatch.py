"""Tool dispatch envelope — Bedrock success/error without calling DynamoDB."""

from __future__ import annotations


def test_unknown_tool_returns_ok_false(dynamodb_env):
    tools = dynamodb_env["tools"]
    out = tools.dispatch("not_a_real_tool", "user-1", {})
    assert out["ok"] is False
    assert "unknown tool" in out["error"]


def test_search_web_without_api_key_is_error(dynamodb_env):
    tools = dynamodb_env["tools"]
    out = tools.dispatch("search_web", "user-1", {"query": "best cafes in kyoto"})
    assert out["ok"] is False
    assert "not configured" in out["error"].lower()


def test_retrieve_journal_disabled_is_error(dynamodb_env):
    tools = dynamodb_env["tools"]
    out = tools.dispatch("retrieve_journal", "user-1", {"query": "sour ethiopia"})
    assert out["ok"] is False
    assert "not configured" in out["error"].lower()


def test_lookup_coffee_term_miss_is_success(dynamodb_env):
    tools = dynamodb_env["tools"]
    out = tools.dispatch("lookup_coffee_term", "user-1", {"term": "xyznotaterm"})
    assert out["ok"] is True
    assert out["result"]["found"] is False


def test_get_dialin_advice_missing_coffee_is_error(dynamodb_env):
    tools = dynamodb_env["tools"]
    out = tools.dispatch(
        "get_dialin_advice",
        "user-1",
        {"coffeeId": "cof-nonexistent", "method": "V60"},
    )
    assert out["ok"] is False
    assert "not found" in out["error"]


def test_list_coffees_empty_is_success(dynamodb_env):
    tools = dynamodb_env["tools"]
    out = tools.dispatch("list_coffees", "user-1", {})
    assert out["ok"] is True
    assert out["result"]["count"] == 0
