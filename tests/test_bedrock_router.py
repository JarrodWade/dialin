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
for _k in ("AWS_DEFAULT_REGION", "AWS_REGION", "BEDROCK_REGION"):
    if not (os.environ.get(_k) or "").strip():
        os.environ[_k] = "us-east-1"


@pytest.fixture(scope="module")
def bedrock_mod():
    import importlib

    import bedrock

    importlib.reload(bedrock)
    return bedrock


@pytest.fixture(scope="module")
def router(bedrock_mod):
    return bedrock_mod.want_trip_place_discovery_appendix


def test_trip_router_city_scout(router):
    assert router([], "What are the best cafes in Kyoto?") is True


def test_trip_router_best_coffee_in(router):
    assert router([], "best coffee in Portland") is True


def test_trip_router_log_visit_only(router):
    assert router([], "log my visit to Heart Coffee today") is False


def test_trip_router_short_city_followup(router):
    hist = [{"role": "USER", "text": "I'm visiting Osaka next week — cafe recommendations?"}]
    assert router(hist, "Any third wave spots?") is True


# ── tool tier sanity checks ───────────────────────────────────────────────


def test_tool_tiers_partition_correctly():
    import tools

    total = len(tools.TOOL_SPECS)
    core = len(tools.CORE_TOOL_SPECS)
    trip = len(tools.TRIP_TOOL_SPECS)
    yt = len(tools.YOUTUBE_TOOL_SPECS)
    assert core + trip + yt == total
    assert core == 22
    assert trip == 8
    assert yt == 1


def test_archive_coffee_removed_from_specs():
    import tools

    names = {t["toolSpec"]["name"] for t in tools.TOOL_SPECS}
    assert "archive_coffee" not in names


def test_update_coffee_has_archived_field():
    import tools

    spec = next(t for t in tools.TOOL_SPECS if t["toolSpec"]["name"] == "update_coffee")
    props = spec["toolSpec"]["inputSchema"]["json"]["properties"]
    assert "archived" in props


# ── YouTube gate ──────────────────────────────────────────────────────────


def test_wants_youtube_url(bedrock_mod):
    assert bedrock_mod._wants_youtube("summarize this https://youtube.com/watch?v=abc") is True


def test_wants_youtube_shortlink(bedrock_mod):
    assert bedrock_mod._wants_youtube("what does this say? youtu.be/xyz") is True


def test_wants_youtube_negative(bedrock_mod):
    assert bedrock_mod._wants_youtube("add a new Colombian coffee") is False


def test_wants_youtube_transcript_keyword(bedrock_mod):
    assert bedrock_mod._wants_youtube("can you get the transcript of this video?") is True
