"""CI smoke test for the live eval *scenarios* (without a live model).

Every scenario is run once against a scripted fake model + canned external-IO
stubs, on moto DynamoDB. This proves seeds, checks, and fixture wiring don't
crash and that scenario ids are unique — the actual prompt-quality measurement
happens in ``make eval`` against real Bedrock.
"""

from __future__ import annotations

import pytest

from evals import fixtures
from evals import harness as H
from evals import scenarios as S
from evals.fakes import FakeBedrockClient


def test_scenario_ids_are_unique():
    ids = [sc.id for sc in S.all_scenarios()]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate scenario ids: {dupes}"
    assert len(ids) >= 15


def test_every_scenario_runs_without_error(dynamodb_env):
    tools = dynamodb_env["tools"]
    restore = fixtures.install(tools)
    try:
        for sc in S.all_scenarios():
            # A benign no-tool reply: we only assert the harness completes and
            # produces a result (checks may fail under the fake model — fine).
            fake = FakeBedrockClient([{"text": "ok"}])
            res = H.run_scenario(sc, model_client=fake)
            assert res.scenario_id == sc.id
            assert isinstance(res.results, list) and res.results, sc.id
            assert all(hasattr(r, "passed") for r in res.results)
    finally:
        restore()


def test_fixtures_install_and_restore(dynamodb_env):
    tools = dynamodb_env["tools"]
    original = tools._TOOL_FUNCS["search_web"]
    restore = fixtures.install(tools)
    try:
        out = tools._TOOL_FUNCS["search_web"]("u", {"query": "best coffee in Osaka"})
        assert out["_cache"]["stub"] is True
        assert any("osaka" in (r["title"] + r["snippet"]).lower() for r in out["results"])
    finally:
        restore()
    assert tools._TOOL_FUNCS["search_web"] is original
