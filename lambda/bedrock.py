"""Bedrock Converse API wrapper with a tool-use loop — top-level facade.

This module owns the pieces that are shared, mutable, or load-order
sensitive: the Bedrock client (``_client``), model config, loaded prompt
text, and per-turn context helpers (clock, journal snapshot, timezone).
Everything else — chat-turn execution, routing heuristics, and the two "For
You" recommendation pipelines — lives in sibling modules and is re-exported
below so ``bedrock.<name>`` keeps working unchanged for ``handler.py``,
``stream_server.py``, ``evals/``, and the test suite:

- ``turn.py`` — ``_prepare_turn``, ``_run_turn``, ``stream_turn``, ``generate_reply``
- ``prompt_router.py`` — trip-appendix / YouTube routing heuristics
- ``consensus.py`` — deterministic text-mining over café search results
- ``recommend_beans.py`` / ``recommend_cafes.py`` — the two closed-pool rankers

Those sibling modules reference this module as ``bedrock.<name>`` (never
``from bedrock import <name>``) for anything that can be swapped at runtime —
most importantly ``_client``, which the eval harness and several tests
reassign directly to inject a fake model client.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import boto3
from botocore.config import Config
from zoneinfo import ZoneInfo

import chat_context
import ddb
import tools


# Prompt text lives in lambda/prompts/*.md, not as inline Python string literals —
# keeps large prompts diffable/reviewable on their own and out of the control-flow
# code. Rule taxonomy inside those files: CORE-* for the always-on system prompt,
# TRIP-* for the trip-place-discovery appendix's own steps (which also cross-
# reference CORE-* rules, e.g. "CORE-2e wins").
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text().strip()


_CLIENT_TZ_RE = re.compile(r"^[A-Za-z0-9_\+\-\/]+$")


def _try_zone(name: str) -> ZoneInfo | None:
    n = name.strip()
    if not n:
        return None
    try:
        return ZoneInfo(n)
    except Exception:  # noqa: BLE001
        return None


def sanitize_client_timezone(raw: str | None) -> str | None:
    """Clamp untrusted ``clientTimezone`` from ``/chat`` to a sane IANA-ish token."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or len(s) > 120 or not _CLIENT_TZ_RE.match(s):
        return None
    return s


def _effective_tz_for_user(user_id: str, *, client_timezone: str | None) -> tuple[ZoneInfo, str]:
    """Return timezone for inferring phrases like ``last Sunday`` and a short provenance hint."""
    cz = sanitize_client_timezone(client_timezone)
    if cz:
        z = _try_zone(cz)
        if z:
            return z, "client device timezone for this chat request"

    prof = ddb.get_profile(user_id) or {}
    z = _try_zone(str(prof.get("timezone") or ""))
    if z:
        return z, "user profile timezone"
    env_name = (os.environ.get("CHAT_LOCAL_TIMEZONE") or "").strip()
    env_z = _try_zone(env_name) if env_name else None
    if env_z:
        return env_z, "server CHAT_LOCAL_TIMEZONE default"
    return ZoneInfo("UTC"), "UTC (no client TZ, profile TZ, nor CHAT_LOCAL_TIMEZONE)"

# Some Nova/Claude models like to emit <thinking>...</thinking> blocks even
# when not asked to. Strip them before returning to the user.
_THINKING_RE = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL | re.IGNORECASE)


def _strip_meta(text: str) -> str:
    return _THINKING_RE.sub("", text).strip()

logger = logging.getLogger(__name__)


def _prior_weekday_iso(anchor_local_day: date, *, weekday: int) -> str:
    """Most recent ``weekday`` (``date.weekday()`` scale) occurring **before** ``anchor``.

    Interpret colloquial *last Monday / last Sunday*: if today **is** that weekday, anchor is tomorrow's
    date for wording purposes — callers pass **today**, so ``last Sunday`` while today is Sunday
    resolves to the previous Sunday (not today).
    """
    d = anchor_local_day - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d.isoformat()


def chat_clock_system_text(
    user_id: str,
    *,
    client_timezone: str | None = None,
    now_utc: datetime | None = None,
) -> str:
    """Dynamic system preamble: anchor dates for resolving relative phrases without questioning the user."""
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)
    tz, tz_source = _effective_tz_for_user(user_id, client_timezone=client_timezone)
    local = now.astimezone(tz)
    local_date = local.date()
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    yesterday = (local_date - timedelta(days=1)).isoformat()
    last_mon = _prior_weekday_iso(local_date, weekday=0)
    last_sun = _prior_weekday_iso(local_date, weekday=6)

    return (
        "Clock context for this turn (infer brew/visit calendar dates from this; "
        "do not recite machine-like field labels to users):\n"
        f"- utcNowISO: {now.isoformat(timespec='seconds')}\n"
        f"- localTimeZone: {tz.key} — {tz_source}\n"
        f"- localNow: {local.strftime('%Y-%m-%d %H:%M')} ({weekdays[local.weekday()]})\n"
        f"- localTodayISO: {local_date.isoformat()}\n"
        f"- yesterdayLocalISO: {yesterday}\n"
        "- commonRelativeHintsLocal (ISO dates; \"last Monday/Sunday\" = prior occurrence before today):\n"
        f"  - impliedLastMonday: {last_mon}\n"
        f"  - impliedLastSunday: {last_sun}\n"
    )


# ---------------------------------------------------------------------------
# Prompt text (trip appendix + core). The routing heuristics that decide
# *whether* to attach the trip appendix, or mount the YouTube tool, live in
# prompt_router.py — re-exported below for backward compatibility.
# ---------------------------------------------------------------------------

_APPENDIX_TRIP_PLACE_DISCOVERY = _load_prompt("trip_appendix.md")

_JOURNAL_SNAPSHOT_MAX_ITEMS = int(os.environ.get("JOURNAL_SNAPSHOT_MAX_ITEMS", "20"))


def _journal_snapshot_text(user_id: str) -> str:
    """Compact current-state block so the model never needs list_* to know what exists.

    Injected verbatim into every turn's system prompt, so it is capped per category
    (``_JOURNAL_SNAPSHOT_MAX_ITEMS``) to keep cost/latency bounded for heavy users —
    without the cap this block grows linearly forever with no ceiling. Coffees come
    back most-recent-first, so truncating keeps the coffees a user is likely asking
    about; overflow items are still reachable via list_coffees/list_roasters/list_equipment.
    """
    coffees = ddb.list_coffees(user_id)
    roasters = ddb.list_roasters(user_id)
    equipment = ddb.list_equipment(user_id)

    cap = _JOURNAL_SNAPSHOT_MAX_ITEMS if _JOURNAL_SNAPSHOT_MAX_ITEMS > 0 else None

    lines = ["Current journal state (authoritative — do not contradict or invent beyond this):"]

    if coffees:
        lines.append(f"Coffees ({len(coffees)} active):")
        shown = coffees[:cap] if cap else coffees
        for c in shown:
            parts = [f"  - {c.get('name', '?')}"]
            if c.get("roaster"):
                parts.append(f"by {c['roaster']}")
            parts.append(f"[coffeeId={c['coffeeId']}]")
            if c.get("origin"):
                parts.append(f"origin={c['origin']}")
            if c.get("process"):
                parts.append(f"process={c['process']}")
            if c.get("roastLevel"):
                parts.append(f"roast={c['roastLevel']}")
            if c.get("gramsRemaining") is not None:
                parts.append(f"{c['gramsRemaining']}g left")
            lines.append(" ".join(parts))
        if cap and len(coffees) > cap:
            lines.append(
                f"  …and {len(coffees) - cap} more active coffees not shown; call list_coffees for the rest."
            )
    else:
        lines.append("Coffees: none active.")

    if roasters:
        lines.append(f"Roasters ({len(roasters)} active):")
        shown = roasters[:cap] if cap else roasters
        for r in shown:
            city = r.get("city", "")
            city_part = f" [{city}]" if city else ""
            lines.append(f"  - {r.get('name', '?')}{city_part} [roasterId={r['roasterId']}]")
        if cap and len(roasters) > cap:
            lines.append(
                f"  …and {len(roasters) - cap} more saved roasters not shown; call list_roasters for the rest."
            )
    else:
        lines.append("Roasters: none saved.")

    if equipment:
        lines.append(f"Equipment ({len(equipment)} active):")
        shown = equipment[:cap] if cap else equipment
        for e in shown:
            lines.append(
                f"  - {e.get('name', '?')} ({e.get('equipType', '')}) [equipId={e['equipId']}]"
            )
        if cap and len(equipment) > cap:
            lines.append(
                f"  …and {len(equipment) - cap} more saved equipment not shown; call list_equipment for the rest."
            )
    else:
        lines.append("Equipment: none saved.")

    lines.append(
        "Use these IDs for tool calls. Do not invent IDs not listed here. "
        "Call list_* tools only when you need brews, visits, archived items, or anything noted "
        "above as not shown."
    )
    return "\n".join(lines)


def _aws_region() -> str:
    """Resolve AWS region; treat empty env vars as unset (GitHub Actions often sets AWS_REGION=\"\")."""
    for key in ("BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return "us-east-1"


# Keep the code default aligned with Terraform's bedrock_model_id default.
_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
_REGION = _aws_region()
# Keep the code default aligned with Terraform's max_output_tokens default.
_MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "600"))
_TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.3"))
_MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "12"))

# Deterministic per-turn cap on search_web calls in normal chat. Without this,
# an over-eager model on a trip-discovery turn can chain enough Tavily calls
# (each several seconds) to blow past the 30s API Gateway/Lambda ceiling.
# 0 disables the cap (unlimited — not recommended in production).
_CHAT_MAX_WEB_SEARCHES = int(os.environ.get("CHAT_MAX_WEB_SEARCHES", "4"))

# Prompt caching reuses the large static system prompt + tool specs across the
# in-turn tool loop (and warm turns), cutting input-token cost and latency.
# Disable for any model that does not support Bedrock cachePoint blocks.
_PROMPT_CACHING = os.environ.get("BEDROCK_PROMPT_CACHING", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Adaptive retries smooth transient Bedrock throttling; read timeout stays under
# the Lambda/API Gateway 30s ceiling so we fail fast rather than hang.
_client = boto3.client(
    "bedrock-runtime",
    region_name=_REGION,
    config=Config(retries={"max_attempts": 4, "mode": "adaptive"}, read_timeout=25),
)

_SYSTEM_PROMPT_CORE = _load_prompt("core.md")
_FOR_YOU_RANKER_SYSTEM = _load_prompt("beans_ranker.md")
_FOR_YOU_CAFES_RANKER_SYSTEM = _load_prompt("cafes_ranker.md")


# ---------------------------------------------------------------------------
# Split modules (architecture_validation_review plan, P1). Each imports this
# module back (deferred to call time, never at their own module load time) to
# read the model config / prompt text / _client above — see this module's
# docstring for why. Re-exported below so `bedrock.<name>` keeps working
# unchanged for handler.py, stream_server.py, evals/, and the test suite.
# ---------------------------------------------------------------------------

import consensus  # noqa: E402
import prompt_router  # noqa: E402
import turn  # noqa: E402
import recommend_beans as _recommend_beans  # noqa: E402
import recommend_cafes as _recommend_cafes  # noqa: E402

# turn.py — chat-turn assembly/execution.
TurnResult = turn.TurnResult
ToolCall = turn.ToolCall
StreamEvent = turn.StreamEvent
_ThinkingStreamFilter = turn._ThinkingStreamFilter
_DecimalEncoder = turn._DecimalEncoder
_to_jsonable = turn._to_jsonable
_accumulate_usage = turn._accumulate_usage
_prepare_turn = turn._prepare_turn
_dispatch_tool_call = turn._dispatch_tool_call
_run_turn = turn._run_turn
stream_turn = turn.stream_turn
generate_reply = turn.generate_reply
_tool_status_label = turn._tool_status_label
_converse_text = turn._converse_text
_stream_converse_text = turn._stream_converse_text

# prompt_router.py — trip-appendix / YouTube routing heuristics.
want_trip_place_discovery_appendix = prompt_router.want_trip_place_discovery_appendix
_wants_youtube = prompt_router._wants_youtube

# consensus.py — deterministic text-mining over café search results.
_extract_consensus_venues = consensus._extract_consensus_venues
_extract_praise_venues = consensus._extract_praise_venues
_favorite_mentions_in_results = consensus._favorite_mentions_in_results
_format_consensus_block = consensus._format_consensus_block
_clean_consensus_candidate = consensus._clean_consensus_candidate
_candidate_names_from_text = consensus._candidate_names_from_text
_unique_result_snippets = consensus._unique_result_snippets

# recommend_beans.py — "For You" bean recommendations.
recommend_beans = _recommend_beans.recommend_beans
stream_recommend_beans = _recommend_beans.stream_recommend_beans
_FOR_YOU_MAX_SEARCHES = _recommend_beans._FOR_YOU_MAX_SEARCHES
_gather_seed_roasters = _recommend_beans._gather_seed_roasters
_peer_search_queries = _recommend_beans._peer_search_queries
_run_peer_searches = _recommend_beans._run_peer_searches
_format_recommendations = _recommend_beans._format_recommendations
_beans_rank_user_block = _recommend_beans._beans_rank_user_block

# recommend_cafes.py — "For You" café recommendations (city mode).
recommend_cafes = _recommend_cafes.recommend_cafes
stream_recommend_cafes = _recommend_cafes.stream_recommend_cafes
_FOR_YOU_CITY_MAX_SEARCHES = _recommend_cafes._FOR_YOU_CITY_MAX_SEARCHES
ParsedDestination = _recommend_cafes.ParsedDestination
_normalize_city = _recommend_cafes._normalize_city
_parse_destination = _recommend_cafes._parse_destination
_resolve_destination_region = _recommend_cafes._resolve_destination_region
_filter_results_for_region = _recommend_cafes._filter_results_for_region
_dispatch_city_search = _recommend_cafes._dispatch_city_search
_gather_city_context = _recommend_cafes._gather_city_context
_city_matches_dest = _recommend_cafes._city_matches_dest
_is_home_destination = _recommend_cafes._is_home_destination
_short_anchor_name = _recommend_cafes._short_anchor_name
_local_anchors_in_city = _recommend_cafes._local_anchors_in_city
_anchor_followup_query = _recommend_cafes._anchor_followup_query
_run_city_searches = _recommend_cafes._run_city_searches
_format_cafe_recommendations = _recommend_cafes._format_cafe_recommendations
_cafes_rank_user_block = _recommend_cafes._cafes_rank_user_block
