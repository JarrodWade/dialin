"""Plumbing tests for the eval harness: a scripted fake model drives the real
tool loop against moto DynamoDB, proving trace capture + the assertion DSL work.

This runs in CI (deterministic, free). Live prompt-quality scenarios live under
``evals/`` and run via ``make eval`` against a real model.
"""

from __future__ import annotations

from evals import harness as H
from evals.fakes import FakeBedrockClient

USER = "harness-user"


def _seed_cafe(ddb, user_id):
    ddb.create_cafe(user_id=user_id, name="Anchorhead", city="Seattle")


def test_trace_captures_tool_call_and_passes_checks(dynamodb_env):
    # Model is scripted to toggle the roaster badge, then reply.
    fake = FakeBedrockClient(
        [
            {"tools": [("list_cafes", {"nameContains": "Anchor"})]},
            {"tools": [("update_cafe", {"cafeId": "WILL_BE_REWRITTEN", "isRoaster": True})]},
            {"text": "Done — Anchorhead is now marked as roasting on site."},
        ]
    )

    # The scripted cafeId won't exist; the loop still records the call + (error) result.
    # Use a seed + a real cafeId so update_cafe actually succeeds.
    def seed(ddb, user_id):
        cafe = ddb.create_cafe(user_id=user_id, name="Anchorhead", city="Seattle")
        # Rewrite the script's placeholder id with the real one.
        fake._script[1]["tools"][0] = ("update_cafe", {"cafeId": cafe["cafeId"], "isRoaster": True})

    scenario = H.Scenario(
        id="cafe_roaster_badge",
        rule="§2e",
        user_id=USER,
        message="Anchorhead actually roasts their own beans now",
        seed=seed,
        checks=[
            H.called("update_cafe", where=lambda a: a.get("isRoaster") is True),
            H.called_before("list_cafes", "update_cafe"),
            H.not_called("add_cafe"),
            H.attachment(youtube=False),
            H.no_iteration_cap(),
            H.reply_excludes(["cafeId", "isRoaster", "DUPLICATE_PLACE"]),
        ],
    )

    res = H.run_scenario(scenario, model_client=fake)
    assert res.passed, [(r.label, r.detail) for r in res.results if not r.passed]
    assert res.tool_calls == ["list_cafes", "update_cafe"]
    assert res.usage.get("inputTokens", 0) > 0
    assert res.reply.startswith("Done")


def test_failed_checks_are_reported_not_raised(dynamodb_env):
    fake = FakeBedrockClient([{"text": "Sure, I'll just chat without doing anything."}])
    scenario = H.Scenario(
        id="missing_tool_call",
        rule="P1",
        user_id=USER,
        message="add a Colombian coffee from Onyx",
        checks=[H.called("add_coffee"), H.not_called("delete_coffee")],
    )
    res = H.run_scenario(scenario, model_client=fake)
    assert res.passed is False
    labels = {r.label: r.passed for r in res.results}
    assert labels["called(add_coffee)"] is False
    assert labels["not_called(delete_coffee)"] is True


def test_iteration_cap_is_flagged(dynamodb_env):
    # Always asks for a tool → never terminates → hits the cap.
    fake = FakeBedrockClient([{"tools": [("list_coffees", {})]}] * 50)
    scenario = H.Scenario(
        id="runaway_loop",
        user_id=USER,
        message="loop forever",
        checks=[H.no_iteration_cap()],
    )
    res = H.run_scenario(scenario, model_client=fake)
    assert res.hit_cap is True
    assert res.passed is False
