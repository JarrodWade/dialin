"""Trip discovery appendix router (no DynamoDB)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "lambda"
if str(LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(LAMBDA_DIR))

os.environ.setdefault("TABLE_NAME", "dialin-router-test")


@pytest.fixture(scope="module")
def router():
    import importlib

    import bedrock

    importlib.reload(bedrock)
    return bedrock.want_trip_place_discovery_appendix


def test_trip_router_city_scout(router):
    assert router([], "What are the best cafes in Kyoto?") is True


def test_trip_router_best_coffee_in(router):
    assert router([], "best coffee in Portland") is True


def test_trip_router_log_visit_only(router):
    assert router([], "log my visit to Heart Coffee today") is False


def test_trip_router_short_city_followup(router):
    hist = [{"role": "USER", "text": "I'm visiting Osaka next week — cafe recommendations?"}]
    assert router(hist, "Any third wave spots?") is True
