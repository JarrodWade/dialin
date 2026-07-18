"""Heuristic (non-LLM) routing for optional chat-turn prompt/tool surface.

Two independent, cheap regex-based gates decide what a turn gets *before* any
Bedrock call is made:

- ``want_trip_place_discovery_appendix`` — whether to attach the trip/city
  place-discovery system prompt appendix (``bedrock._APPENDIX_TRIP_PLACE_DISCOVERY``).
- ``_wants_youtube`` — whether to mount the YouTube transcript tool.

Both prefer false negatives over spamming their attachment on every turn.
"""

from __future__ import annotations

import re

_TRIP_APPENDIX_DUAL_INTENT_GUARD = re.compile(
    r"\b(?:cafes?\s+in|coffee\s+shops?\s+in|coffee\s+scene|where\s+(?:should|to)\s+"
    r"(?:drink|go|hit|grab)|itinerary|planning\s+a\s+trip)\b",
    re.IGNORECASE,
)
_LOG_VISIT_PHRASE = re.compile(
    r"\b(?:log|record|save)\s+(?:a\s+|my\s+|the\s+)?(?:cafe\s+)?visit\b",
    re.IGNORECASE,
)
_APPENDIX_TRIGGERS_SIMPLE = (
    r"\bcafes?\s+in\b",
    r"\bcoffee\s+in\b",
    r"\bcoffee\s+shops?\s+in\b",
    r"\bcoffee\s+scene\b",
    r"\bcoffee\s+culture\b",
    r"\bitinerary\b",
    r"\bplanning\s+a\s+(?:trip|vacation)\b",
    r"\b(?:must\s*-?\s*visit|must\s+visit)\b",
    r"\bwhere\s+(?:should|would|could|can)\s+i\s+(?:go|drink|stop|grab|hit|caffeinate)\b",
    r"\bwhat(?:'s|s| is)\s+good\s+(?:in|around|near)\b",
    r"\b(?:spots?|places?|stops?|picks?)\s+(?:in|for|around|near)\b",
    # "best coffee / cafes / coffee shops in X" — plus common shorthand "best shop/spot
    # in X" (users often drop "coffee"; without this the ask falls into §5d personal-
    # visit ranking instead of city discovery).
    r"\bbest\s+(?:coffee|cafes?|coffee\s+shops?|third[- ]?wave|shops?|spots?|places?|bars?)\s+in\b",
    r"\bthird[- ]?wave\s+(?:coffee\s+)?(?:in|around|near)\b",
)
_TRAVEL_PLACE_PROBE = re.compile(
    r"\b(?:"
    r"headed\s+(?:to|towards)|"
    r"heading\s+(?:to|towards)|"
    r"going\s+to|"
    r"visit(?:ing)?\s+(?:to\s+)?|"
    r"travel(?:l)?ing\s+(?:to|through|around|in)|"
    r"flying\s+to|"
    r"trip\s+to|"
    r"road\s+trip\s+to"
    r")\b",
    re.IGNORECASE,
)


def _mentions_venue_topic(t_low: str) -> bool:
    return bool(
        re.search(
            r"\b(?:coffee|caffeine|cafe|cafes|espresso|filter|pour[- ]?overs?|shops?|roastery|roaster)\b",
            t_low,
        )
    )


def _router_scan_text(history: list[dict], user_text: str, *, prior_user_slices: int = 1) -> str:
    """Prior USER lines plus current message — short replies keep city-discovery context."""
    prior: list[str] = []
    for h in reversed(history or []):
        if (h.get("role") or "") != "USER":
            continue
        blob = (h.get("text") or "").strip()
        if blob:
            prior.append(blob)
        if len(prior) >= prior_user_slices:
            break
    prior.reverse()
    cur = (user_text or "").strip()
    chunks = [*prior]
    if cur:
        chunks.append(cur)
    return "\n".join(chunks).strip()


def want_trip_place_discovery_appendix(history: list[dict], user_text: str) -> bool:
    """Heuristic lightweight router (no LLM cost). Prefer false negatives vs spamming appendix every turn."""
    ulen = len((user_text or "").strip())
    short_follow = ulen < 96
    # Short replies like "Osaka?" inherit city intent from more prior user turns.
    prior_user_slices = 3 if ulen < 40 else (2 if short_follow else 1)
    scan = _router_scan_text(history, user_text, prior_user_slices=prior_user_slices)
    if not scan:
        return False
    t = scan.lower().replace("cafés", "cafes").replace("café", "cafe")

    if _LOG_VISIT_PHRASE.search(t):
        if not _TRIP_APPENDIX_DUAL_INTENT_GUARD.search(t):
            return False

    for p in _APPENDIX_TRIGGERS_SIMPLE:
        if re.search(p, t):
            return True

    rec_or_suggest = bool(re.search(r"\b(?:recommend|recommendations?|suggest(?:ions?)?)\b", t))
    if rec_or_suggest and _mentions_venue_topic(t):
        return True

    if _TRAVEL_PLACE_PROBE.search(t) and _mentions_venue_topic(t):
        return True

    return False


_RE_YOUTUBE = re.compile(
    r"youtu(?:\.be|be\.com)|youtube\s+shorts|\btranscript\b.*\bvideo\b|\bvideo\b.*\btranscript\b",
    re.IGNORECASE,
)


def _wants_youtube(user_text: str) -> bool:
    """Include the YouTube transcript tool only when the message references a video."""
    return bool(_RE_YOUTUBE.search(user_text or ""))


# ---------------------------------------------------------------------------
# Deterministic city-scout routing (architecture_validation_review, P2).
#
# Most "open city scout" phrasings still go through the open agent + trip
# appendix above — that flexibility genuinely matters for itinerary talk and
# multi-turn refinement. But the single most common shape — a self-contained
# "best coffee/cafes in X" ask, with no itinerary or log-visit intent mixed
# in — is exactly what the deterministic "For You" café pipeline
# (recommend_cafes.py) already does better: same taste graph, same
# Reddit-scoped searches, same closed-pool ranker, zero narration risk, no
# tool-iteration variance. ``extract_open_city_scout`` recognizes only that
# one unambiguous shape and returns the destination text to route on; every
# other phrasing (follow-ups, "heading to X next week", itinerary building)
# returns None and falls through to the existing appendix-routed agent loop.
# ---------------------------------------------------------------------------

_OPEN_CITY_SCOUT_RE = re.compile(
    # "best" + up to two filler words (specialty, third, wave, ...) + a
    # required venue noun, so "best specialty coffee spots in X" and "best
    # coffee in X" both match, but the filler can't itself swallow the noun.
    r"\bbest\s+(?:[a-z\-]+\s+){0,2}?(?:cafes?|coffee(?:\s+shops?)?|shops?|spots?|places?|bars?)\s+"
    r"(?:in|around)\s+([A-Za-z][\w\s,.'\-]{1,60}?)\s*[?.!]*$",
    re.IGNORECASE,
)

_ITINERARY_GUARD = re.compile(
    r"\bitinerary\b|\bplanning\s+a\s+(?:trip|vacation)\b|\bday\s*\d\b|\b\d+[- ]day\b",
    re.IGNORECASE,
)


def extract_open_city_scout(history: list[dict], user_text: str) -> str | None:
    """Best-effort destination for a self-contained "best coffee/cafes in
    <city>" ask. Returns ``None`` (never guesses) for anything else —
    follow-ups, itinerary talk, or log-visit intent all fall through to the
    existing router/appendix path. ``history`` is accepted for symmetry with
    the other router functions but isn't consulted: the trigger phrase must
    be self-contained in the current message, so conversational follow-ups
    ("and Kyoto?") never match in the first place.
    """
    text = (user_text or "").strip()
    if not text or len(text) > 200:
        return None
    if _LOG_VISIT_PHRASE.search(text) or _ITINERARY_GUARD.search(text):
        return None
    m = _OPEN_CITY_SCOUT_RE.search(text)
    if not m:
        return None
    return m.group(1).strip() or None
