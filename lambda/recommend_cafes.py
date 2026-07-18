"""'For You' café recommendations — deterministic pipeline (city mode).

Same architecture as ``recommend_beans.py``: server gathers taste graph +
tracked venues in the destination, runs capped Reddit-scoped city searches,
then a single tool-less ranking call formats a café-first shortlist from the
closed candidate pool.

Consensus/praise/favorite-mention extraction (``consensus.py``) and the
ranker prompt/Bedrock client (``bedrock.py``) are referenced here as
``bedrock.<name>`` rather than imported by name — see ``turn.py``'s module
docstring for why (a test monkeypatches ``bedrock._extract_consensus_venues``
directly and expects this pipeline to see the patched version).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

import bedrock
import ddb
import tools
import turn

_CITY_MAX_LEN = 120
_FOR_YOU_CITY_MAX_SEARCHES = 2
_PEER_SEARCH_DOMAINS = ["reddit.com"]
_PEER_SEARCH_MAX_RESULTS = 8

# Cities that commonly collide across countries/states when the user omits a region.
_AMBIGUOUS_CITIES = frozenset({
    "athens", "portland", "springfield", "manchester", "birmingham", "rochester",
    "arlington", "cambridge", "richmond", "alexandria", "georgetown",
})

# Expand common abbreviations so Reddit queries anchor the right place.
_REGION_ALIASES: dict[str, str] = {
    "ga": "Georgia",
    "gr": "Greece",
    "or": "Oregon",
    "me": "Maine",
    "uk": "United Kingdom",
    "usa": "USA",
    "us": "USA",
}

# Per ambiguous city: (resolved label, marker phrases that indicate that place).
_AMBIGUOUS_CITY_REGIONS: dict[str, list[tuple[str, list[str]]]] = {
    "athens": [
        (
            "Athens, Georgia, USA",
            ["georgia", "athens ga", "athens, ga", "1000 faces", "uga", "jittery joe"],
        ),
        (
            "Athens, Greece",
            [
                "greece", "greek", "kolonaki", "plaka", "kifisia", "monastiraki",
                "athens greece", "samba coffee", "kudu", "thisseio", "underdog coffee",
            ],
        ),
    ],
    "portland": [
        ("Portland, Oregon, USA", ["oregon", "portland or", "portland oregon", "pdx"]),
        ("Portland, Maine, USA", ["maine", "portland me", "portland maine"]),
    ],
}

# Common abbreviations → canonical city name for search, home-turf, and venue matching.
_CITY_ALIASES: dict[str, str] = {
    "phx": "Phoenix",
    "pdx": "Portland",
    "sf": "San Francisco",
    "nyc": "New York",
    "new york city": "New York",
    "la": "Los Angeles",
    "dc": "Washington",
}

# Boroughs / inner metros that belong to a scout destination (lowercase).
_METRO_BOROUGHS: dict[str, frozenset[str]] = {
    "new york": frozenset({
        "new york", "new york city", "nyc", "manhattan", "brooklyn", "queens",
        "bronx", "staten island",
    }),
}


@dataclass(frozen=True)
class ParsedDestination:
    """User-supplied place, split into city + optional region for search anchoring."""

    raw: str
    city: str
    region: str | None
    search_label: str


def _normalize_city(raw: str) -> str:
    """Clamp untrusted city input from the client."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("city is required")
    # NL wrappers ("give me recommendations in Osaka, Japan") — take the last
    # plausible geographic clause, not the first "in it is" inside "where it is".
    _invalid = re.compile(
        r"^(it is|it|this|that|there|here|the area|the city|the place)$", re.I
    )
    candidates: list[str] = []
    for m in re.finditer(
        r"\b(?:in|for|at)\s+([^,?.!]+(?:,\s*[^,?.!]+)?)", s, re.I
    ):
        part = m.group(1).strip()
        if part and not _invalid.match(part):
            candidates.append(part)
    if candidates:
        s = candidates[-1]
    if _invalid.match(s):
        raise ValueError("city must be a place name, not a pronoun")
    if re.match(r"^(please|look|where|tell|give|show|find|recommend)", s, re.I):
        raise ValueError("city must be a place name")
    if re.search(
        r"\b(coffeehead|coffee\s*co|roasters?|caf[eé]|espresso|cupworks?)\b", s, re.I
    ):
        raise ValueError(
            "city must be a destination, not a venue name — use chat for a specific shop"
        )
    if re.search(
        r"\b\w*(head|hound|works|lab|beans?|roasts?|brews?|grinds?)\b", s, re.I
    ):
        raise ValueError(
            "city must be a destination, not a venue name — use chat for a specific shop"
        )
    if re.match(r"^[A-Z]{2,5}\s+\S", s) and not re.match(
        r"^(SF|NYC|LA|DC|PHX|PDX)(\s|,|$)", s
    ):
        head = s.split(None, 1)[0]
        if head.isupper() and len(head) <= 5:
            raise ValueError(
                "city must be a destination, not a venue name — use chat for a specific shop"
            )
    if len(s) > _CITY_MAX_LEN:
        s = s[:_CITY_MAX_LEN].strip()
    return s


def _expand_region(region: str) -> str:
    r = region.strip()
    return _REGION_ALIASES.get(r.lower(), r)


def _canonical_city(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return s
    return _CITY_ALIASES.get(s.lower(), s)


def _metro_key(city: str) -> str | None:
    c = _canonical_city(city).lower()
    for key, members in _METRO_BOROUGHS.items():
        if c in members:
            return key
    return None


def _in_same_metro(dest: ParsedDestination, venue_city: str) -> bool:
    key = _metro_key(dest.city) or _metro_key(dest.raw)
    if not key:
        return False
    vc = _canonical_city(str(venue_city).strip()).lower()
    return vc in _METRO_BOROUGHS[key]


def _parse_destination(raw: str) -> ParsedDestination:
    """Parse ``city`` input like ``Athens, GA`` or ``Athens, Greece``."""
    s = _normalize_city(raw)
    if "," in s:
        city_part, region_part = [p.strip() for p in s.split(",", 1)]
        city_canon = _canonical_city(city_part)
        region_exp = _expand_region(region_part)
        return ParsedDestination(
            raw=s,
            city=city_canon,
            region=region_part,
            search_label=f"{city_canon} {region_exp}",
        )
    city_canon = _canonical_city(s)
    return ParsedDestination(raw=s, city=city_canon, region=None, search_label=city_canon)


def _score_markers(text: str, markers: list[str]) -> int:
    lower = text.lower()
    return sum(len(re.findall(re.escape(m), lower)) for m in markers)


def _resolve_destination_region(dest: ParsedDestination, scout_text: str) -> str:
    """Pick one geography for ambiguous single-word cities using scout-result markers."""
    options = _AMBIGUOUS_CITY_REGIONS.get(dest.city.lower())

    if dest.region:
        region_exp = _expand_region(dest.region)
        if options:
            for label, _markers in options:
                if region_exp.lower() in label.lower():
                    return label
        return f"{dest.city}, {region_exp}"

    if not options:
        return dest.search_label

    scored = [(label, _score_markers(scout_text, markers)) for label, markers in options]
    scored.sort(key=lambda t: -t[1])
    if scored[0][1] == 0:
        return dest.search_label
    if len(scored) > 1 and scored[0][1] == scored[1][1]:
        return scored[0][0]
    return scored[0][0]


def _filter_results_for_region(
    results_text: str,
    resolved_label: str,
    dest: ParsedDestination,
) -> str:
    """Drop result lines that clearly belong to a losing geography for ambiguous cities."""
    options = _AMBIGUOUS_CITY_REGIONS.get(dest.city.lower())
    if not options:
        return results_text

    winning_markers: list[str] = []
    losing_markers: list[str] = []
    for label, markers in options:
        if label == resolved_label:
            winning_markers = markers
        else:
            losing_markers.extend(markers)
    if not winning_markers or not losing_markers:
        return results_text

    kept: list[str] = []
    for line in results_text.splitlines():
        if line.startswith("Search:"):
            kept.append(line)
            continue
        lower = line.lower()
        has_win = any(m in lower for m in winning_markers)
        has_lose = any(m in lower for m in losing_markers)
        if has_lose and not has_win:
            continue
        kept.append(line)
    return "\n".join(kept)


def _dispatch_city_search(user_id: str, query: str) -> str:
    """Run one Reddit-scoped city search and flatten to text."""
    res = tools.dispatch(
        "search_web",
        user_id,
        {
            "query": query,
            "maxResults": _PEER_SEARCH_MAX_RESULTS,
            "includeDomains": _PEER_SEARCH_DOMAINS,
        },
    )
    if not res.get("ok"):
        return f"Search: {query}\n(search unavailable: {res.get('error')})"
    payload = res.get("result") or {}
    lines = [f"Search: {query}"]
    # Omit Tavily's one-line Summary for city scouts — it often conflates ambiguous
    # places (e.g. mixing Athens GA with Athens Greece) and skews the ranker.
    for r in payload.get("results", []) or []:
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        if title or snippet:
            lines.append(f"- {title}: {snippet}")
    return "\n".join(lines)


def _gather_city_context(user_id: str, dest: ParsedDestination) -> tuple[str, str]:
    """Return ``(taste_block, tracked_block)`` for the city ranker."""
    profile = ddb.get_profile(user_id) or {}
    ctx: list[str] = []

    home = str(profile.get("homeCity") or "").strip()
    if home:
        ctx.append(f"Home city: {home}.")

    fav_roasters = [
        str(x).strip() for x in (profile.get("favoriteRoasters") or []) if str(x).strip()
    ]
    if fav_roasters:
        ctx.append("Favorite roasters (my class anchors): " + ", ".join(fav_roasters) + ".")
    fav_cafes = [str(x).strip() for x in (profile.get("favoriteCafes") or []) if str(x).strip()]
    if fav_cafes:
        ctx.append("Favorite cafés: " + ", ".join(fav_cafes) + ".")
    roast = str(profile.get("preferredRoastLevel") or "").strip()
    if roast:
        ctx.append(f"Preferred roast level: {roast}.")
    exp = str(profile.get("experimentalPreference") or "").strip()
    if exp:
        ctx.append(f"Experimental-processing appetite: {exp}.")
    disliked = [str(x).strip() for x in (profile.get("dislikedNotes") or []) if str(x).strip()]
    if disliked:
        ctx.append("Notes I dislike (avoid): " + ", ".join(disliked) + ".")

    taste = "\n".join(ctx) or "No saved preferences; infer my class from anchor roasters above."

    tracked: list[str] = []
    cafes = ddb.list_cafes(user_id, city=dest.raw)
    if not cafes and dest.city != dest.raw:
        cafes = ddb.list_cafes(user_id, city=dest.city)
    roasters = ddb.list_roasters(user_id, city=dest.raw)
    if not roasters and dest.city != dest.raw:
        roasters = ddb.list_roasters(user_id, city=dest.city)
    for c in cafes:
        name = str(c.get("name") or "").strip()
        if name:
            tracked.append(f"Café (already tracked): {name}")
    for r in roasters:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        if r.get("hasCafe"):
            tracked.append(f"Roaster-café (already tracked, roasts in-house): {name}")
        else:
            tracked.append(f"Roaster (already tracked): {name}")

    tracked_block = (
        "\n".join(tracked)
        if tracked
        else f"No cafés or roasters saved in {dest.raw} yet."
    )
    return taste, tracked_block


def _city_matches_dest(dest: ParsedDestination, venue_city: str | None) -> bool:
    if not venue_city or not str(venue_city).strip():
        return False
    dc = _canonical_city(dest.city).lower()
    vc = _canonical_city(str(venue_city).strip()).lower()
    if dc == vc:
        return True
    if dc in vc or vc in dc:
        return True
    return _in_same_metro(dest, venue_city)


def _is_home_destination(dest: ParsedDestination, home_city: str | None) -> bool:
    if not home_city or not str(home_city).strip():
        return False
    home_city_part = str(home_city).strip().split(",", 1)[0].strip()
    return _canonical_city(dest.city).lower() == _canonical_city(home_city_part).lower()


def _short_anchor_name(name: str) -> str:
    s = name.strip()
    for suffix in (
        " Coffee Roasters",
        " Coffee Co.",
        " Coffee Co",
        " Roasters",
        " Coffee",
    ):
        if s.endswith(suffix):
            return s[: -len(suffix)].strip()
    return s


def _local_anchors_in_city(user_id: str, dest: ParsedDestination, profile: dict) -> list[str]:
    """Favorite or tracked roaster-cafés in the destination city — always surface these."""
    fav_names = [
        str(x).strip()
        for x in (
            *(profile.get("favoriteRoasters") or []),
            *(profile.get("favoriteCafes") or []),
        )
        if str(x).strip()
    ]
    if not fav_names:
        return []
    favorites = [n.lower() for n in fav_names]

    def _favorite_label(venue_name: str) -> str | None:
        key = venue_name.lower()
        for orig, lower in zip(fav_names, favorites, strict=True):
            if lower in key or key in lower:
                return orig
        return None

    anchors: list[str] = []
    seen: set[str] = set()

    def _maybe_add(venue_name: str) -> None:
        label = _favorite_label(venue_name) or venue_name.strip()
        key = label.lower()
        if key in seen:
            return
        if _favorite_label(venue_name):
            seen.add(key)
            anchors.append(label)

    for r in ddb.list_roasters(user_id):
        if not _city_matches_dest(dest, r.get("city")):
            continue
        name = str(r.get("name") or "").strip()
        if name:
            _maybe_add(name)

    for c in ddb.list_cafes(user_id):
        if not _city_matches_dest(dest, c.get("city")):
            continue
        name = str(c.get("name") or "").strip()
        if name:
            _maybe_add(name)

    return anchors


def _anchor_followup_query(
    resolved: str,
    anchors: list[str],
    *,
    is_home: bool,
    scout_text: str,
) -> str:
    """Second search query — for home turf, seed with local anchors to pull peer lists."""
    if not anchors:
        return f"best coffee roasters {resolved}"
    shorts = [_short_anchor_name(a) for a in anchors[:2]]
    seed_parts = list(dict.fromkeys(shorts))
    if is_home:
        # Home-turf: co-search anchor name(s) with community-list peers. Reddit
        # threads often batch local favorites ("Moxie, Satellite, …") when the
        # query names the anchor explicitly.
        if re.search(r"\bsatellite\b", scout_text, re.I) and "Satellite" not in seed_parts:
            seed_parts.append("Satellite")
        elif len(seed_parts) == 1:
            seed_parts.append("Satellite")
    seed = " ".join(seed_parts)
    return f"best specialty coffee {resolved} {seed}"


def _run_city_searches(
    user_id: str,
    dest: ParsedDestination,
    profile: dict,
    local_anchors: list[str],
) -> tuple[str, str]:
    """Run capped Reddit city searches; resolve ambiguous geographies after scout pass.

    Returns ``(results_text, resolved_destination_label)``."""
    is_home = _is_home_destination(dest, profile.get("homeCity"))
    scout_query = f"best specialty coffee shops {dest.search_label}"
    scout_block = _dispatch_city_search(user_id, scout_query)
    resolved = _resolve_destination_region(dest, scout_block)

    blocks = [scout_block]
    if _FOR_YOU_CITY_MAX_SEARCHES > 1:
        # Home turf: seed follow-up with local anchor names (pulls peer lists).
        # Everywhere else: original roaster-led second query.
        if is_home and local_anchors:
            followup = _anchor_followup_query(
                resolved, local_anchors, is_home=True, scout_text=scout_block
            )
        else:
            followup = f"best coffee roasters {resolved}"
        blocks.append(_dispatch_city_search(user_id, followup))

    combined = "\n\n".join(blocks).strip()
    filtered = _filter_results_for_region(combined, resolved, dest)
    return filtered, resolved


def _format_cafe_recommendations(
    user_id: str,
    dest: ParsedDestination,
    resolved_destination: str,
    taste_block: str,
    tracked_block: str,
    results_text: str,
    consensus: list[str] | None = None,
    praise: list[str] | None = None,
    favorite_mentions: list[str] | None = None,
    local_anchors: list[str] | None = None,
    is_home: bool = False,
) -> str:
    """Single tool-less model call: rank + format cafés strictly from ``results_text``."""
    user_block = _cafes_rank_user_block(
        dest,
        resolved_destination,
        taste_block,
        tracked_block,
        results_text,
        consensus,
        praise,
        favorite_mentions,
        local_anchors,
        is_home,
    )
    return turn._converse_text(bedrock._FOR_YOU_CAFES_RANKER_SYSTEM, user_block)


def _cafes_rank_user_block(
    dest: ParsedDestination,
    resolved_destination: str,
    taste_block: str,
    tracked_block: str,
    results_text: str,
    consensus: list[str] | None = None,
    praise: list[str] | None = None,
    favorite_mentions: list[str] | None = None,
    local_anchors: list[str] | None = None,
    is_home: bool = False,
) -> str:
    consensus_block = ""
    if consensus:
        consensus_block = bedrock._format_consensus_block(consensus, results_text)
    praise_block = ""
    if praise:
        praise_block = (
            "PRAISE HIGHLIGHTS (explicit praise in community snippets)\n"
            + "\n".join(f"- {name}" for name in praise)
            + "\n\n"
        )
    favorite_block = ""
    if favorite_mentions:
        favorite_block = (
            "FAVORITE MENTIONS IN RESULTS (my taste-graph favorites in the search pool)\n"
            + "\n".join(f"- {name}" for name in favorite_mentions)
            + "\n\n"
        )
    anchors_block = ""
    if local_anchors:
        label = (
            "LOCAL ANCHORS — lead with these (home city)"
            if is_home
            else "LOCAL ANCHORS — include if in results"
        )
        anchors_block = (
            f"{label}\n"
            + "\n".join(f"- {name}" for name in local_anchors)
            + "\n\n"
        )

    return (
        f"RESOLVED DESTINATION (recommend for THIS place only): {resolved_destination}\n"
        f"USER INPUT: {dest.raw}\n"
        + ("HOME CITY: yes — lead with my local anchors.\n" if is_home else "")
        + "\n"
        "MY TASTE GRAPH\n"
        + taste_block
        + f"\n\nMY TRACKED VENUES IN {dest.raw.upper()}\n"
        + tracked_block
        + "\n\n"
        + anchors_block
        + favorite_block
        + praise_block
        + consensus_block
        + "CITY-SEARCH RESULTS (your only candidate pool)\n"
        + (results_text or "(no results returned)")
    )


def recommend_cafes(user_id: str, city: str) -> str:
    """Directional 'For You' café recommendations for a destination city.

    Server gathers taste graph + tracked venues in the city, runs capped
    Reddit-scoped city searches, then asks the model to rank + format strictly
    from those candidates."""
    dest = _parse_destination(city)
    profile = ddb.get_profile(user_id) or {}
    is_home = _is_home_destination(dest, profile.get("homeCity"))
    local_anchors = _local_anchors_in_city(user_id, dest, profile)
    taste_block, tracked_block = _gather_city_context(user_id, dest)
    results_text, resolved = _run_city_searches(user_id, dest, profile, local_anchors)
    consensus = bedrock._extract_consensus_venues(results_text)
    praise = bedrock._extract_praise_venues(results_text)
    favorite_mentions = bedrock._favorite_mentions_in_results(profile, results_text)
    return _format_cafe_recommendations(
        user_id,
        dest,
        resolved,
        taste_block,
        tracked_block,
        results_text,
        consensus,
        praise,
        favorite_mentions,
        local_anchors or None,
        is_home,
    )


def stream_recommend_cafes(user_id: str, city: str) -> Iterator[turn.StreamEvent]:
    """Same pipeline as ``recommend_cafes``, with status + token streaming."""
    yield turn.StreamEvent("status", {"tool": "_start", "label": f"scouting cafés in {city.strip()}…"})
    dest = _parse_destination(city)
    profile = ddb.get_profile(user_id) or {}
    is_home = _is_home_destination(dest, profile.get("homeCity"))
    local_anchors = _local_anchors_in_city(user_id, dest, profile)
    taste_block, tracked_block = _gather_city_context(user_id, dest)
    yield turn.StreamEvent("status", {"tool": "search_web", "label": "checking specialty consensus…"})
    results_text, resolved = _run_city_searches(user_id, dest, profile, local_anchors)
    consensus = bedrock._extract_consensus_venues(results_text)
    praise = bedrock._extract_praise_venues(results_text)
    favorite_mentions = bedrock._favorite_mentions_in_results(profile, results_text)
    yield turn.StreamEvent("status", {"tool": "_rank", "label": "narrowing the shortlist…"})
    user_block = _cafes_rank_user_block(
        dest,
        resolved,
        taste_block,
        tracked_block,
        results_text,
        consensus,
        praise,
        favorite_mentions,
        local_anchors or None,
        is_home,
    )
    yield from turn._stream_converse_text(bedrock._FOR_YOU_CAFES_RANKER_SYSTEM, user_block)
