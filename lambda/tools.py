"""LLM tool definitions and dispatcher.

We hand these "toolSpec" entries to Bedrock via the Converse API.
When the model emits a `toolUse` block, we look up the implementation
here, run it against DynamoDB, and feed the result back as a
`toolResult` so the model can compose its final answer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import ddb
import journal_rag
import chat_context

_TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
_LOGGER = logging.getLogger(__name__)

# Production-shaped safeguards (still reasonable for solo use): shared DynamoDB cache + monthly quota per user.
_WEBSEARCH_CACHE_TTL_SEC = int(os.environ.get("WEBSEARCH_CACHE_TTL_SECONDS", "86400"))
_WEBSEARCH_MONTHLY_LIMIT = int(os.environ.get("WEBSEARCH_MONTHLY_LIMIT_PER_USER", "300"))
_LOG_TRIP_WEBSEARCH = os.environ.get("LOG_TRIP_WEBSEARCH", "").lower() in ("1", "true", "yes")


def _log_trip_search_web_summary(
    *,
    query: str,
    include_domains: list[Any],
    max_results: int,
    payload: dict[str, Any],
    cache_hit: bool,
) -> None:
    """Opt-in CloudWatch aid: did Tavily return title X (e.g. a missing roaster) on trip-discovery turns?"""
    if not _LOG_TRIP_WEBSEARCH or not chat_context.trip_place_discovery_active.get():
        return
    results = payload.get("results") or []
    titles = [str((r.get("title") or ""))[:160] for r in results[:12]]
    domains = list(include_domains) if include_domains else []
    _LOGGER.info(
        "trip_search_web cache_hit=%s maxResults=%s domains=%s n_results=%s query=%r titles=%s",
        cache_hit,
        max_results,
        domains,
        len(results),
        query[:800],
        titles,
    )


# ---------------------------------------------------------------------------
# Curated drink / menu glossary (coffee_glossary.json, shipped in Lambda zip)
# ---------------------------------------------------------------------------


def _normalize_glossary_query(s: str) -> str:
    s = (s or "").lower().strip()
    s = s.replace("&", " and ")
    for ch in "‐–—":
        s = s.replace(ch, "-")
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def _load_coffee_glossary_index() -> dict[str, dict[str, Any]]:
    path = os.path.join(os.path.dirname(__file__), "coffee_glossary.json")
    index: dict[str, dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _LOGGER.warning("coffee_glossary.json missing or invalid: %s", e)
        return index
    for ent in raw.get("entries", []):
        title = (ent.get("title") or "").strip()
        body = (ent.get("body") or "").strip()
        see_also = ent.get("seeAlso") or []
        if not title or not body:
            continue
        rec = {"title": title, "body": body, "seeAlso": see_also}
        for al in ent.get("aliases", []):
            k = _normalize_glossary_query(str(al))
            if k and k not in index:
                index[k] = rec
    return index


_COFFEE_GLOSSARY_INDEX = _load_coffee_glossary_index()


def _lookup_coffee_term(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    _ = user_id
    q = _normalize_glossary_query(str(args.get("term", "")))
    if not q:
        return {"found": False, "message": "Provide a non-empty term to look up."}
    if q in _COFFEE_GLOSSARY_INDEX:
        hit = _COFFEE_GLOSSARY_INDEX[q]
        return {"found": True, "title": hit["title"], "body": hit["body"], "seeAlso": hit["seeAlso"]}
    return {
        "found": False,
        "message": (
            "No curated glossary hit for that exact wording. Try the core drink name only, "
            "or call search_web for live menu/regional variants."
        ),
    }


# ---------------------------------------------------------------------------
# Implementations (each takes the user_id + the model-supplied args dict)
# ---------------------------------------------------------------------------


def _list_roasters(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    items = ddb.list_roasters(user_id, include_archived=bool(args.get("includeArchived")))
    return {"count": len(items), "roasters": items}


def _add_roaster(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("skipDuplicateCheck"):
        hit = ddb.find_matching_cafe_for_new_roaster(user_id, args["name"], args.get("city"))
        if hit:
            return {
                "duplicatePlace": True,
                "existingType": "cafe",
                "existingId": hit["cafeId"],
                "existingName": hit.get("name"),
                "hint": (
                    "Same name already exists as a cafe — use update_cafe with isRoaster, "
                    "or call add_roaster again with skipDuplicateCheck: true if the user insists."
                ),
            }
    return ddb.create_roaster(
        user_id=user_id,
        name=args["name"],
        city=args.get("city"),
        country=args.get("country"),
        website=args.get("website"),
        notes=args.get("notes"),
        has_cafe=bool(args.get("hasCafe")),
    )


def _update_roaster(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return ddb.update_roaster(user_id, args["roasterId"], args)


def _list_coffees(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    include_archived = bool(args.get("includeArchived", False))
    items = ddb.list_coffees(user_id, include_archived=include_archived)
    return {"count": len(items), "coffees": items}


def _add_coffee(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    # If roasterId supplied, resolve the display name from the roaster entity.
    roaster_id = args.get("roasterId")
    roaster_name = args.get("roaster") or ""
    if roaster_id and not roaster_name:
        r = ddb.get_roaster(user_id, roaster_id)
        roaster_name = r["name"] if r else ""
    row = ddb.create_coffee(
        user_id=user_id,
        roaster=roaster_name,
        name=args["name"],
        roaster_id=roaster_id,
        origin=args.get("origin"),
        process=args.get("process"),
        roast_date=args.get("roastDate"),
        weight_g=args.get("weightG"),
        notes=args.get("notes"),
    )
    journal_rag.try_sync_coffee(user_id, row)
    return row


def _archive_coffee(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    coffee_id = args["coffeeId"]
    row = ddb.update_coffee(user_id, coffee_id, {"archived": True})
    journal_rag.try_sync_coffee(user_id, row)
    return row


def _update_coffee(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    coffee_id = args["coffeeId"]
    row = ddb.update_coffee(user_id, coffee_id, args)
    journal_rag.try_sync_coffee(user_id, row)
    return row


def _delete_coffee(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    cid = args["coffeeId"]
    ddb.delete_coffee(user_id, cid)
    journal_rag.delete_chunk(user_id, "COFFEE", str(cid))
    return {"deleted": cid}


def _log_brew(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    row = ddb.create_brew(
        user_id=user_id,
        coffee_id=args["coffeeId"],
        method=args["method"],
        dose_g=args.get("doseG"),
        yield_g=args.get("yieldG"),
        water_g=args.get("waterG"),
        grind=args.get("grind"),
        grinder_id=args.get("grinderId"),
        machine_id=args.get("machineId"),
        brewer_id=args.get("brewerId"),
        time_s=args.get("timeS"),
        temp_c=args.get("tempC"),
        rating=args.get("rating"),
        taste=args.get("taste"),
        notes=args.get("notes"),
    )
    journal_rag.try_sync_brew(user_id, row)
    return row


def _update_brew(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    brew_id = args["brewId"]
    row = ddb.update_brew(user_id, brew_id, args)
    journal_rag.try_sync_brew(user_id, row)
    return row


def _delete_brew(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    bid = args["brewId"]
    ddb.delete_brew(user_id, bid)
    journal_rag.delete_chunk(user_id, "BREW", str(bid))
    return {"deleted": bid}


def _list_brews(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    items = ddb.list_brews(
        user_id,
        coffee_id=args.get("coffeeId"),
        method=args.get("method"),
        limit=int(args.get("limit", 10)),
    )
    return {"count": len(items), "brews": items}


def _get_dialin_advice(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Smarter dial-in advice grounded in the user's actual brew history.

    Returns:
    - bestBrew: the highest-rated brew for this coffee+method (the target to replicate)
    - lastBrew: the most recent brew
    - grindNote: if grind settings differ between best and last, surfaces that delta
    - ratioDelta: how far the last brew's ratio drifts from the best
    - trend: rating trajectory across the last 3 rated brews
    - heuristics: extraction heuristics from last brew's taste notes
    """
    from decimal import Decimal
    from statistics import mean

    coffee_id = args["coffeeId"]
    method = args["method"]
    coffee = ddb.get_coffee(user_id, coffee_id)
    if coffee is None:
        return {"error": f"coffee {coffee_id} not found"}

    all_brews = ddb.list_brews(user_id, coffee_id=coffee_id, limit=20)
    brews = [b for b in all_brews if b.get("method") == method]

    if not brews:
        return {
            "coffee": {"name": coffee.get("name"), "roaster": coffee.get("roaster")},
            "method": method,
            "message": f"No {method} brews logged for this coffee yet — log your first one and I can start advising.",
        }

    last = brews[0]
    rated = [b for b in brews if isinstance(b.get("rating"), (int, float, Decimal))]
    best = max(rated, key=lambda b: float(b["rating"]), default=None)

    # Extraction heuristics from last brew taste
    last_taste = (last.get("taste") or "").lower()
    heuristics: list[str] = []
    if any(w in last_taste for w in ("sour", "grassy", "weak", "thin", "tart", "bright")):
        heuristics += ["likely under-extracted — grind finer, raise temp 1-2°C, or extend brew time"]
    if any(w in last_taste for w in ("bitter", "astringent", "harsh", "dry", "burnt")):
        heuristics += ["likely over-extracted — grind coarser, lower temp 1-2°C, or shorten brew time"]
    if any(w in last_taste for w in ("flat", "dull", "bland", "boring")):
        heuristics += ["possibly stale coffee or channeling — try a slightly coarser grind or fresher beans"]

    # Ratio delta vs best
    ratio_delta = None
    last_ratio = float(last.get("ratio") or 0)
    best_ratio = float((best or {}).get("ratio") or 0)
    if last_ratio and best_ratio and best_ratio != last_ratio:
        delta = round(last_ratio - best_ratio, 2)
        direction = "higher (more dilute)" if delta > 0 else "lower (more concentrated)"
        ratio_delta = f"Last brew ratio 1:{last_ratio} vs best brew 1:{best_ratio} — last is {direction}"

    # Grind delta vs best
    grind_note = None
    if best and last.get("grind") and best.get("grind") and last.get("grind") != best.get("grind"):
        grind_note = f"Best brew: grind '{best['grind']}' (rated {best.get('rating')}/10) — last brew: '{last['grind']}'"

    # Rating trend over last 3 rated brews
    recent_rated = [float(b["rating"]) for b in brews[:5] if isinstance(b.get("rating"), (int, float, Decimal))]
    trend = None
    if len(recent_rated) >= 2:
        if recent_rated[0] > recent_rated[-1]:
            trend = f"improving (last {len(recent_rated)} rated brews: {' → '.join(str(int(r)) for r in reversed(recent_rated))})"
        elif recent_rated[0] < recent_rated[-1]:
            trend = f"declining (last {len(recent_rated)} rated brews: {' → '.join(str(int(r)) for r in reversed(recent_rated))})"
        else:
            trend = f"plateau at {recent_rated[0]}/10"

    return {
        "coffee": {
            "name": coffee.get("name"),
            "roaster": coffee.get("roaster"),
            "process": coffee.get("process"),
            "roastDate": coffee.get("roastDate"),
        },
        "method": method,
        "brewCount": len(brews),
        "avgRating": round(mean([float(b["rating"]) for b in rated]), 2) if rated else None,
        "bestBrew": best,
        "lastBrew": last,
        "recentBrews": brews[:3],
        "ratioDelta": ratio_delta,
        "grindNote": grind_note,
        "ratingTrend": trend,
        "heuristics": heuristics,
    }


# --- Equipment ---------------------------------------------------------------


def _list_equipment(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    items = ddb.list_equipment(user_id, equip_type=args.get("equipType"))
    return {"count": len(items), "equipment": items}


def _add_equipment(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    item, name_meta = ddb.create_equipment(
        user_id=user_id,
        equip_type=args["equipType"],
        name=args["name"],
        brand=args.get("brand"),
        model=args.get("model"),
        notes=args.get("notes"),
    )
    out: dict[str, Any] = {"equipment": item}
    if name_meta:
        out["nameResolution"] = name_meta
    return out


# --- Preferences -------------------------------------------------------------


def _get_preferences(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return ddb.get_profile(user_id) or {}


def _update_preferences(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return ddb.update_profile(user_id, args)


# --- Cafes & Visits ----------------------------------------------------------


def _add_cafe(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("skipDuplicateCheck"):
        existing_cafe = ddb.find_matching_existing_cafe_by_place(
            user_id, args["name"], args.get("city")
        )
        if existing_cafe:
            return {
                "duplicatePlace": True,
                "existingType": "cafe",
                "existingId": existing_cafe["cafeId"],
                "existingName": existing_cafe.get("name"),
                "hint": (
                    "This cafe is already on the user's list — use log_visit with cafeId \""
                    f'{existing_cafe["cafeId"]}" '
                    '(and list_cafes if you need to confirm). '
                    "Call add_cafe with skipDuplicateCheck: true only if the user explicitly wants a second entry."
                ),
            }
        hit = ddb.find_matching_roaster_for_new_cafe(user_id, args["name"], args.get("city"))
        if hit:
            return {
                "duplicatePlace": True,
                "existingType": "roaster",
                "existingId": hit["roasterId"],
                "existingName": hit.get("name"),
                "hint": (
                    "Same name already exists as a roaster — use update_roaster with hasCafe, "
                    "or call add_cafe again with skipDuplicateCheck: true if the user insists."
                ),
            }
    return ddb.create_cafe(
        user_id=user_id,
        name=args["name"],
        neighborhood=args.get("neighborhood"),
        city=args.get("city"),
        country=args.get("country"),
        website=args.get("website"),
        notes=args.get("notes"),
        is_roaster=bool(args.get("isRoaster")),
    )


def _list_cafes(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    nc_raw = args.get("nameContains") or args.get("name_contains")
    nc = str(nc_raw).strip() if nc_raw is not None else ""
    items = ddb.list_cafes(
        user_id,
        city=args.get("city"),
        name_contains=nc or None,
        include_archived=bool(args.get("includeArchived")),
    )
    return {"count": len(items), "cafes": items}


def _update_cafe(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return ddb.update_cafe(user_id, args["cafeId"], args)


def _log_visit(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    row = ddb.log_visit(
        user_id=user_id,
        cafe_id=args.get("cafeId"),
        roaster_id=args.get("roasterId"),
        place_name=args.get("placeName"),
        visit_date=args.get("visitDate"),
        drinks=args.get("drinks"),
        rating=args.get("rating"),
        notes=args.get("notes"),
    )
    journal_rag.try_sync_visit(user_id, row)
    return row


def _list_visits(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    items = ddb.list_visits(user_id, cafe_id=args.get("cafeId"), limit=int(args.get("limit", 10)))
    return {"count": len(items), "visits": items}


def _update_visit(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    visit_id = args["visitId"]
    patch = {
        k: args[k]
        for k in ("rating", "notes", "drinks", "visitDate", "placeName")
        if k in args and args[k] is not None
    }
    row = ddb.update_visit(user_id, visit_id, patch)
    journal_rag.try_sync_visit(user_id, row)
    return row


def _delete_visit(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    vid = args["visitId"]
    ddb.delete_visit(user_id, vid)
    journal_rag.delete_chunk(user_id, "VISIT", str(vid))
    return {"deleted": vid}


# --- Known roasters reference ------------------------------------------------

_KNOWN_ROASTERS: list[dict[str, str]] = [
    # Pacific Northwest
    {"name": "Stumptown Coffee Roasters", "city": "Portland", "state": "OR"},
    {"name": "Heart Coffee Roasters", "city": "Portland", "state": "OR"},
    {"name": "Water Avenue Coffee", "city": "Portland", "state": "OR"},
    {"name": "Coava Coffee Roasters", "city": "Portland", "state": "OR"},
    {"name": "Proud Mary Coffee", "city": "Portland", "state": "OR"},
    {"name": "Nossa Familia Coffee", "city": "Portland", "state": "OR"},
    {"name": "Olympia Coffee Roasting", "city": "Olympia", "state": "WA"},
    {"name": "Victrola Coffee Roasters", "city": "Seattle", "state": "WA"},
    {"name": "Slate Coffee Roasters", "city": "Seattle", "state": "WA"},
    {"name": "Herkimer Coffee", "city": "Seattle", "state": "WA"},
    {"name": "Anchorhead Coffee", "city": "Seattle", "state": "WA"},
    # NYC / Brooklyn
    {"name": "Sey Coffee", "city": "Brooklyn", "state": "NY"},
    {"name": "Partners Coffee", "city": "Brooklyn", "state": "NY"},
    {"name": "Devocion", "city": "Brooklyn", "state": "NY"},
    {"name": "Toby's Estate Coffee", "city": "Brooklyn", "state": "NY"},
    {"name": "Parlor Coffee", "city": "Brooklyn", "state": "NY"},
    # San Francisco / Bay Area
    {"name": "Ritual Coffee Roasters", "city": "San Francisco", "state": "CA"},
    {"name": "Sightglass Coffee", "city": "San Francisco", "state": "CA"},
    {"name": "Equator Coffees", "city": "San Francisco", "state": "CA"},
    {"name": "Blue Bottle Coffee", "city": "Oakland", "state": "CA"},
    {"name": "Verve Coffee Roasters", "city": "Santa Cruz", "state": "CA"},
    {"name": "Chromatic Coffee", "city": "San Jose", "state": "CA"},
    # Los Angeles
    {"name": "Go Get Em Tiger", "city": "Los Angeles", "state": "CA"},
    {"name": "Endorffeine", "city": "Los Angeles", "state": "CA"},
    {"name": "Coffee Manufactory", "city": "Los Angeles", "state": "CA"},
    {"name": "Intelligentsia Coffee", "city": "Los Angeles", "state": "CA"},
    # Chicago
    {"name": "Dark Matter Coffee", "city": "Chicago", "state": "IL"},
    {"name": "Metric Coffee", "city": "Chicago", "state": "IL"},
    # Arkansas
    {"name": "Onyx Coffee Lab", "city": "Rogers", "state": "AR"},
    # Texas
    {"name": "Greater Goods Coffee", "city": "Austin", "state": "TX"},
    {"name": "Cuvee Coffee", "city": "Austin", "state": "TX"},
    {"name": "Flat Track Coffee", "city": "Austin", "state": "TX"},
    {"name": "Merit Coffee", "city": "San Antonio", "state": "TX"},
    {"name": "Avoca Coffee Roasters", "city": "Fort Worth", "state": "TX"},
    {"name": "Noble Coyote Coffee Roasters", "city": "Dallas", "state": "TX"},
    # Phoenix / Arizona
    {"name": "Cartel Coffee Lab", "city": "Tempe", "state": "AZ"},
    {"name": "Peixoto Coffee Roasters", "city": "Chandler", "state": "AZ"},
    {"name": "Moxie Coffee Co", "city": "Phoenix", "state": "AZ"},
    {"name": "Press Coffee Roasters", "city": "Phoenix", "state": "AZ"},
    # Southeast
    {"name": "Revelator Coffee", "city": "Birmingham", "state": "AL"},
    {"name": "Methodical Coffee", "city": "Greenville", "state": "SC"},
    {"name": "Spiller Park Coffee", "city": "Atlanta", "state": "GA"},
    {"name": "Portrait Coffee", "city": "Atlanta", "state": "GA"},
    {"name": "Black & White Coffee Roasters", "city": "Durham", "state": "NC"},
    {"name": "Counter Culture Coffee", "city": "Durham", "state": "NC"},
    # Nashville
    {"name": "Crema Coffee Roasters", "city": "Nashville", "state": "TN"},
    {"name": "Steadfast Coffee", "city": "Nashville", "state": "TN"},
    # Mid-Atlantic / DC
    {"name": "La Colombe Coffee Roasters", "city": "Philadelphia", "state": "PA"},
    {"name": "Ultimo Coffee", "city": "Philadelphia", "state": "PA"},
    {"name": "Passenger Coffee", "city": "Lancaster", "state": "PA"},
    {"name": "Compass Coffee", "city": "Washington", "state": "DC"},
    {"name": "Peregrine Espresso", "city": "Washington", "state": "DC"},
    {"name": "Vigilante Coffee", "city": "Hyattsville", "state": "MD"},
    {"name": "Ceremony Coffee Roasters", "city": "Annapolis", "state": "MD"},
    # Midwest
    {"name": "Madcap Coffee", "city": "Grand Rapids", "state": "MI"},
    {"name": "PT's Coffee Roasting", "city": "Topeka", "state": "KS"},
    {"name": "Oddly Correct Coffee", "city": "Kansas City", "state": "MO"},
    {"name": "Dogwood Coffee", "city": "Minneapolis", "state": "MN"},
    # Denver / Colorado
    {"name": "Huckleberry Roasters", "city": "Denver", "state": "CO"},
    {"name": "Sweet Bloom Coffee Roasters", "city": "Lakewood", "state": "CO"},
    {"name": "Boxcar Coffee Roasters", "city": "Boulder", "state": "CO"},
    {"name": "Corvus Coffee Roasters", "city": "Denver", "state": "CO"},
    # Boston
    {"name": "George Howell Coffee", "city": "Acton", "state": "MA"},
    {"name": "Broadsheet Coffee Roasters", "city": "Cambridge", "state": "MA"},
    # Indianapolis
    {"name": "Weekenders Coffee", "city": "Indianapolis", "state": "IN"},
    # National / notable
    {"name": "Stumptown Coffee Roasters", "city": "New York", "state": "NY"},
    {"name": "Intelligentsia Coffee", "city": "Chicago", "state": "IL"},
]


def _search_known_roasters(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Search a curated reference list of ~70 well-known US specialty roasters.

    Use before calling add_roaster to verify canonical name + city and avoid typos.
    This list is not exhaustive — absence does NOT mean the roaster is not real.
    """
    query = (args.get("query") or "").lower().strip()
    city = (args.get("city") or "").lower().strip()
    state = (args.get("state") or "").lower().strip()

    results = []
    for r in _KNOWN_ROASTERS:
        name_match = not query or query in r["name"].lower()
        city_match = not city or city in r.get("city", "").lower()
        state_match = not state or state in r.get("state", "").lower()
        if name_match and city_match and state_match:
            results.append(r)

    return {
        "count": len(results),
        "results": results[:10],
        "note": "This list covers ~70 well-known US specialty roasters. Absence does not mean the roaster is not real.",
    }


# --- Coffee summary ----------------------------------------------------------


def _summarize_coffee(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return ddb.summarize_coffee(user_id, args["coffeeId"])


def _retrieve_journal(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return journal_rag.search(
        user_id,
        (args.get("query") or "").strip(),
        top_k=int(args.get("topK") or 8),
    )


# --- Web search --------------------------------------------------------------


def _search_web(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Live web search via Tavily — cached globally + metered per user on cache miss."""
    if not _TAVILY_API_KEY:
        return {"ok": False, "error": "web search is not configured (no TAVILY_API_KEY)"}

    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}

    include_domains = args.get("includeDomains") or []
    max_results = int(args.get("maxResults", 5))

    cached = ddb.websearch_cache_get(query, include_domains, max_results)
    if cached is not None:
        out = dict(cached)
        out["_cache"] = {"hit": True}
        _log_trip_search_web_summary(
            query=query,
            include_domains=include_domains,
            max_results=max_results,
            payload=out,
            cache_hit=True,
        )
        return out

    allowed, usage_count = ddb.consume_websearch_quota(user_id, _WEBSEARCH_MONTHLY_LIMIT)
    if not allowed:
        lim = _WEBSEARCH_MONTHLY_LIMIT
        if _LOG_TRIP_WEBSEARCH and chat_context.trip_place_discovery_active.get():
            _LOGGER.info("trip_search_web quota_exceeded query=%r", query[:800])
        return {
            "ok": False,
            "error": (
                f"Monthly live web search quota exhausted ({usage_count}/{lim} used). "
                "Repeated lookups for the same city often hit cache — broaden your query slightly "
                "or wait until next UTC month."
            ),
        }

    payload = json.dumps({
        "api_key": _TAVILY_API_KEY,
        "query": query,
        "search_depth": "advanced",
        "max_results": max_results,
        "include_answer": True,
        **({"include_domains": include_domains} if include_domains else {}),
    }).encode()

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"ok": False, "error": f"Tavily HTTP {e.code}: {body[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"search failed: {e}"}

    results = [
        {
            "title": r.get("title"),
            "url": r.get("url"),
            "snippet": r.get("content", "")[:400],
            "score": r.get("score"),
        }
        for r in data.get("results", [])
    ]
    result_body = {
        "query": query,
        "answer": data.get("answer"),
        "results": results,
    }
    try:
        ddb.websearch_cache_put(
            query,
            include_domains,
            max_results,
            result_body,
            _WEBSEARCH_CACHE_TTL_SEC,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception("websearch_cache_put failed")

    out = dict(result_body)
    meta = {"hit": False, "liveSearchCallsThisMonth": usage_count}
    if _WEBSEARCH_MONTHLY_LIMIT > 0:
        meta["monthlyLimit"] = _WEBSEARCH_MONTHLY_LIMIT
    out["_cache"] = meta
    _log_trip_search_web_summary(
        query=query,
        include_domains=include_domains,
        max_results=max_results,
        payload=out,
        cache_hit=False,
    )
    return out


_ID11 = re.compile(r"^[a-zA-Z0-9_-]{11}$")


def _youtube_video_id_from_input(raw: str) -> str | None:
    """Resolve an 11-char id from paste (URL or bare id)."""
    s = (raw or "").strip().split()[0].strip()
    if not s:
        return None
    if _ID11.match(s):
        return s
    u = urlparse(s)
    host = (u.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "youtu.be":
        part = u.path.strip("/").split("/")[0]
        return part if part and _ID11.match(part) else None
    if host in ("youtube.com", "youtube-nocookie.com", "m.youtube.com"):
        segments = [p for p in u.path.split("/") if p]
        if len(segments) >= 2 and segments[0] in ("embed", "shorts", "live"):
            cand = segments[1]
            return cand if _ID11.match(cand) else None
        qs = parse_qs(u.query)
        for key in ("v", "vi"):
            vals = qs.get(key)
            if vals and vals[0] and _ID11.match(vals[0][:11]):
                return vals[0][:11]
    return None


def _youtube_transcript(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Official captions transcript when available — shares Tavily quota + cache bucket."""

    vid_in = (
        args.get("video")
        or args.get("videoUrl")
        or args.get("videoId")
        or args.get("url")
        or ""
    ).strip()
    video_id = _youtube_video_id_from_input(vid_in)
    if not video_id:
        return {"ok": False, "error": "Need a youtube.com/watch, youtu.be, shorts, embed URL, or bare 11-char video id."}

    raw_langs = args.get("languages")
    langs: list[str]
    if isinstance(raw_langs, list) and raw_langs:
        langs = [str(x).strip() for x in raw_langs if str(x).strip()]
    elif isinstance(raw_langs, str) and raw_langs.strip():
        langs = [raw_langs.strip()]
    else:
        langs = ["en", "en-US", "en-GB"]

    max_chars = int(args.get("maxChars") or 22_000)
    max_chars = max(800, min(max_chars, 48_000))

    cache_query = f"youtube_transcript::{video_id}::{max_chars}::{'|'.join(langs[:8])}"

    cached = ddb.websearch_cache_get(cache_query, [], max_chars)
    if cached is not None:
        out = dict(cached)
        out["_cache"] = {"hit": True}
        return out

    allowed, usage_count = ddb.consume_websearch_quota(user_id, _WEBSEARCH_MONTHLY_LIMIT)
    if not allowed:
        lim = _WEBSEARCH_MONTHLY_LIMIT
        return {
            "ok": False,
            "error": (
                f"Monthly external-fetch quota exhausted ({usage_count}/{lim}) — transcript fetch shares search quota. "
                "Try later or broaden without extra video pulls."
            ),
        }

    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        api = YouTubeTranscriptApi()
        ft = api.fetch(video_id, languages=langs or ["en"])
    except ImportError:
        return {"ok": False, "error": "youtube_transcript_api is not installed in this deployment."}
    except Exception as e:  # noqa: BLE001
        msg_low = str(e).lower()
        hint = ""
        if "blocked" in msg_low or "ip" in msg_low and "block" in msg_low:
            hint = (
                " YouTube often blocks cloud datacenter IPs; try from a warmer path or summarize from search_web + "
                "reddit instead."
            )
        return {"ok": False, "error": f"could not fetch transcript: {e!s}.{hint}".strip()}

    try:
        parts = [snippet.text.strip() for snippet in ft if snippet.text.strip()]
        full_text = " ".join(parts)
        lang_iso = getattr(ft, "language_code", None)
        lang_name = getattr(ft, "language", None)
        is_gen = getattr(ft, "is_generated", None)
    except Exception:
        full_text = ""
        lang_iso, lang_name, is_gen = None, None, None

    truncated = len(full_text) > max_chars
    text_out = full_text[:max_chars] if truncated else full_text

    payload = {
        "videoId": video_id,
        "languageCode": lang_iso,
        "language": lang_name,
        "isGenerated": is_gen,
        "charLength": len(full_text),
        "truncated": truncated,
        "text": text_out,
        "note": (
            "Summarize the user's question from this narration; cite the video conversationally "
            "(no long verbatim dumps). Respect copyright."
        ),
    }

    try:
        ddb.websearch_cache_put(
            cache_query,
            [],
            max_chars,
            payload,
            _WEBSEARCH_CACHE_TTL_SEC,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception("youtube transcript cache_put failed")

    meta = {"hit": False, "liveSearchCallsThisMonth": usage_count}
    if _WEBSEARCH_MONTHLY_LIMIT > 0:
        meta["monthlyLimit"] = _WEBSEARCH_MONTHLY_LIMIT
    payload["_cache"] = meta
    return payload




TOOL_SPECS: list[dict[str, Any]] = [
    {
        "toolSpec": {
            "name": "list_roasters",
            "description": (
                "List the user's saved roasters. For trip / city scouting and \"do I already track X?\", "
                "call this alongside list_cafes — roaster-cafés (hasCafe) often live here only."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "includeArchived": {"type": "boolean"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "add_roaster",
            "description": (
                "Add a roaster to the user's roaster list. "
                "Set hasCafe: true if the roaster also has a physical cafe you can visit. "
                "Only call after the user confirms. Returns a roasterId for use in add_coffee."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string", "description": "Canonical roaster name, e.g. 'Sey'"},
                        "city": {"type": "string", "description": "City, e.g. 'Brooklyn'"},
                        "country": {"type": "string", "default": "US"},
                        "website": {"type": "string"},
                        "notes": {"type": "string"},
                        "hasCafe": {
                            "type": "boolean",
                            "description": "true if this roaster has a physical cafe/retail location you can visit",
                        },
                        "skipDuplicateCheck": {
                            "type": "boolean",
                            "description": (
                                "Set true only if add_roaster failed with DUPLICATE_PLACE and the user "
                                "explicitly wants a second entry anyway."
                            ),
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "update_roaster",
            "description": "Edit a roaster's details or mark it archived.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["roasterId"],
                    "properties": {
                        "roasterId": {"type": "string"},
                        "name": {"type": "string"},
                        "city": {"type": "string"},
                        "country": {"type": "string"},
                        "website": {"type": "string"},
                        "notes": {"type": "string"},
                        "archived": {"type": "boolean"},
                        "hasCafe": {"type": "boolean"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "list_coffees",
            "description": "List the user's coffees (active by default).",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "includeArchived": {"type": "boolean"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "add_coffee",
            "description": (
                "Add a new bag of coffee beans. "
                "Always call list_roasters first to resolve the roasterId. "
                "If the roaster isn't in the list, call add_roaster (after user confirms) "
                "to get a roasterId before calling this."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["roasterId", "name"],
                    "properties": {
                        "roasterId": {"type": "string", "description": "FK from list_roasters or add_roaster"},
                        "name": {"type": "string", "description": "Coffee/SKU name, e.g. 'Wote Ethiopia'"},
                        "origin": {"type": "string"},
                        "process": {"type": "string", "description": "e.g. washed, natural, anaerobic"},
                        "roastDate": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                        "weightG": {"type": "number", "description": "bag weight in grams"},
                        "notes": {"type": "string"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "search_known_roasters",
            "description": (
                "Search a curated reference list of ~70 well-known US specialty roasters. "
                "Call before add_roaster to verify the canonical name and city. "
                "Absence from this list does NOT mean the roaster is not real."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Partial name to search, e.g. 'cartel'"},
                        "city": {"type": "string"},
                        "state": {"type": "string", "description": "Two-letter state code, e.g. 'AZ'"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "archive_coffee",
            "description": "Mark a coffee as finished (archived).",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["coffeeId"],
                    "properties": {"coffeeId": {"type": "string"}},
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "delete_coffee",
            "description": (
                "Permanently delete a coffee. Use only when the user explicitly asks to delete "
                "(not archive). This cannot be undone. Call list_coffees first to confirm the coffeeId."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["coffeeId"],
                    "properties": {"coffeeId": {"type": "string"}},
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "update_coffee",
            "description": (
                "Edit a coffee's fields. Use when the user corrects info about a coffee "
                "(name, roaster, origin, process, roastDate, notes). "
                "Do NOT create a new coffee — call this instead."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["coffeeId"],
                    "properties": {
                        "coffeeId": {"type": "string"},
                        "roaster": {"type": "string"},
                        "name": {"type": "string"},
                        "origin": {"type": "string"},
                        "process": {"type": "string"},
                        "roastDate": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                        "notes": {"type": "string"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "log_brew",
            "description": (
                "Log a brewing attempt. Decrements the coffee's gramsRemaining if "
                "doseG is provided. Capture as much detail as the user gave. "
                "If the user mentions equipment by name (e.g. 'Niche Zero'), call "
                "list_equipment first to resolve the equipId; do NOT invent IDs."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["coffeeId", "method"],
                    "properties": {
                        "coffeeId": {"type": "string"},
                        "method": {
                            "type": "string",
                            "enum": sorted(ddb.VALID_METHODS),
                        },
                        "doseG": {"type": "number"},
                        "yieldG": {"type": "number"},
                        "waterG": {"type": "number"},
                        "grind": {"type": "string", "description": "Human-readable grinder setting, e.g. 'Ode 18'"},
                        "grinderId": {"type": "string"},
                        "machineId": {"type": "string"},
                        "brewerId": {"type": "string"},
                        "timeS": {"type": "integer"},
                        "tempC": {"type": "number"},
                        "rating": {"type": "integer", "minimum": 1, "maximum": 10},
                        "taste": {
                            "type": "string",
                            "description": "Comma-separated descriptors: sour, sweet, bitter, balanced, grassy, harsh, etc.",
                        },
                        "notes": {"type": "string"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "list_equipment",
            "description": "List the user's gear (machines, grinders, brewers, kettles).",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "equipType": {
                            "type": "string",
                            "enum": sorted(ddb.EQUIP_TYPES),
                        }
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "add_equipment",
            "description": (
                "Add brewing gear. Unknown names are stored with trimmed spacing; recognized names "
                "are normalized to a canonical display string (see gear_canonical). "
                "If the user already has active gear of the same type with the same normalized name, "
                "the existing item is reused — no duplicate row."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["equipType", "name"],
                    "properties": {
                        "equipType": {
                            "type": "string",
                            "enum": sorted(ddb.EQUIP_TYPES),
                        },
                        "name": {"type": "string", "description": "Display name, e.g. 'Niche Zero', 'Hario V60 02'"},
                        "brand": {"type": "string"},
                        "model": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_preferences",
            "description": (
                "Read stored taste preferences, home city, IANA timezone field (persistent fallback — the web app normally "
                "sends browser timezone each /chat anyway), discovery habits, and experimental openness. "
                "Call before recommending coffees, roasters, or cafés. "
                "Fields include preferredRoastLevel (may be ultralight), preferredProcesses, "
                "discoveryChannels, experimentalPreference, notes, timezone."
            ),
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "update_preferences",
            "description": (
                "Persist durable taste and discovery preferences so future sessions remember them. "
                "Lists are merged (deduped); strings replace. "
                "Use for roast philosophy, how they discover coffee (subscriptions, drops), openness to co-ferments, "
                "IANA timezone for relative visit dates, origins/processes — not one-off brew comments."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "preferredOrigins": {"type": "array", "items": {"type": "string"}},
                        "preferredProcesses": {"type": "array", "items": {"type": "string"}},
                        "preferredRoastLevel": {
                            "type": "string",
                            "enum": ["ultralight", "light", "medium-light", "medium", "medium-dark", "dark"],
                        },
                        "dislikedNotes": {"type": "array", "items": {"type": "string"}},
                        "favoriteRoasters": {"type": "array", "items": {"type": "string"}},
                        "favoriteCafes": {"type": "array", "items": {"type": "string"}},
                        "homeCity": {"type": "string"},
                        "timezone": {
                            "type": "string",
                            "description": (
                                "IANA timezone id for calendar-relative logging, e.g. America/Phoenix, "
                                "Europe/Berlin. Persist when user states where they usually log visits "
                                "from; improves \"last Sunday\" visitDate without asking them for dates."
                            ),
                        },
                        "notes": {"type": "string"},
                        "discoveryChannels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "How they find coffee long-term: e.g. 'curated subscription boxes', "
                                "'Instagram drops', 'local cafés only', 'direct from roasters'."
                            ),
                        },
                        "experimentalPreference": {
                            "type": "string",
                            "enum": ["open", "seek"],
                            "description": (
                                "open = willing to try co-ferments / funky lots; seek = actively prefers them. "
                                "Omit to leave neutral/classic-leaning unless user said otherwise."
                            ),
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "summarize_coffee",
            "description": (
                "Aggregate stats across all brews of a single coffee: avg rating, "
                "best brew, recent brew, method counts, common taste descriptors. "
                "Use to answer 'what's worked best for the X' questions."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["coffeeId"],
                    "properties": {"coffeeId": {"type": "string"}},
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "retrieve_journal",
            "description": (
                "Semantic search across the user's own journal text (brew tastes/notes, coffee bag notes, "
                "cafe visits). Use for fuzzy memory questions ('what patterns in my tasting notes?', "
                "'when did I mention bitterness?', thematic recall spanning many coffees). "
                "Prefer list_brews / get_dialin_advice for exact recent numbers on one coffee + method."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language question for similarity search"},
                        "topK": {"type": "integer", "minimum": 1, "maximum": 12, "description": "Snippets to return (default 8)"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "lookup_coffee_term",
            "description": (
                "Curated short definitions for drinks, regional café jargon, and specialty gear/prep slang "
                "(e.g. one-and-one, kopitiam; WDT, RDT, puck screen, SSP burrs, channeling, naked PF, flow profiling; "
                "Rao spin, TDS). "
                "Call first for 'what is X' when X is likely a menu, bar, or trendy home-barista term. "
                "If found is false or the user needs deep threads or brand wars, use search_web with reddit.com (rule 3d / 3b)."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["term"],
                    "properties": {
                        "term": {
                            "type": "string",
                            "description": "Term or phrase (e.g. 'one and one', 'WDT', 'puck screen', 'SSP burrs').",
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "update_brew",
            "description": (
                "Edit a logged brew. Use when the user corrects a brew they already logged "
                "(wrong dose, grind, rating, taste, etc.). "
                "Call list_brews first to get the brewId. "
                "Do NOT log a new brew — call this instead."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["brewId"],
                    "properties": {
                        "brewId": {"type": "string"},
                        "method": {"type": "string", "enum": sorted(ddb.VALID_METHODS)},
                        "doseG": {"type": "number"},
                        "yieldG": {"type": "number"},
                        "waterG": {"type": "number"},
                        "grind": {"type": "string"},
                        "grinderId": {"type": "string"},
                        "machineId": {"type": "string"},
                        "brewerId": {"type": "string"},
                        "timeS": {"type": "integer"},
                        "tempC": {"type": "number"},
                        "rating": {"type": "integer", "minimum": 1, "maximum": 10},
                        "taste": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "delete_brew",
            "description": (
                "Permanently delete a brew. Use to remove a duplicate or accidental entry. "
                "Call list_brews first to confirm the correct brewId. "
                "This cannot be undone."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["brewId"],
                    "properties": {"brewId": {"type": "string"}},
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "list_brews",
            "description": "List the user's recent brews. Filter by coffeeId or method.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "coffeeId": {"type": "string"},
                        "method": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "add_cafe",
                "description": (
                "Add a cafe to the user's tracked place list. "
                "Set isRoaster: true if the cafe also roasts/sources beans the user can buy. "
                "Always call list_cafes before add_cafe — same name+city returns DUPLICATE_PLACE; "
                "then log_visit with the existing cafeId instead of add_cafe again. "
                "Only add after the user confirms."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "neighborhood": {"type": "string", "description": "Neighborhood or specific location, e.g. Balboa"},
                        "city": {"type": "string"},
                        "country": {"type": "string", "default": "US"},
                        "website": {"type": "string"},
                        "notes": {"type": "string"},
                        "isRoaster": {
                            "type": "boolean",
                            "description": "true if this cafe also roasts / sources beans the user can purchase",
                        },
                        "skipDuplicateCheck": {
                            "type": "boolean",
                            "description": (
                                "Set true only if add_cafe failed with DUPLICATE_PLACE and the user "
                                "explicitly wants a second entry anyway."
                            ),
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "list_cafes",
            "description": (
                "List the user's tracked cafés, optionally filtered by city and/or name substring. "
                "Whenever the café name shows up anywhere in dialogue, correlate it with these rows "
                "before implying it isn't tracked. City filter matches flexible stored values "
                "(e.g. filter \"Kyoto\" matches \"Kyoto, Japan\"). Roaster-cafés saved under **Roasters** "
                "with hasCafe are NOT returned here — call list_roasters too. If city filter returns "
                "empty but the user expects a named shop, use nameContains or omit city and scan."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "nameContains": {
                            "type": "string",
                            "description": "Case-insensitive substring on cafe name (e.g. \"Weekenders\").",
                        },
                        "includeArchived": {"type": "boolean"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "update_cafe",
            "description": (
                "Edit a cafe or mark it archived. "
                "IMPORTANT: The app's 'also a roaster' badge for cafe-primary places is the boolean "
                "isRoaster only — you must pass isRoaster: true to turn it on. "
                "Putting 'they roast on site' only in notes does NOT set the badge. "
                "After updating, the tool result echoes the saved cafe — confirm isRoaster in your head before telling the user it's done."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["cafeId"],
                    "properties": {
                        "cafeId": {"type": "string"},
                        "name": {"type": "string"},
                        "neighborhood": {"type": "string"},
                        "city": {"type": "string"},
                        "country": {"type": "string"},
                        "website": {"type": "string"},
                        "notes": {"type": "string"},
                        "archived": {"type": "boolean"},
                        "isRoaster": {
                            "type": "boolean",
                            "description": "Set true so this cafe shows the roaster badge (on-site roasting). Required for that UI — not inferred from notes.",
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "log_visit",
            "description": (
                "Log a NEW visit to a cafe OR a roaster-cafe (hasCafe: true). "
                "Provide either cafeId (for pure cafes) or roasterId (for roasters that also have a cafe). "
                "Also pass visitDate when known (infer from Clock context for \"yesterday\"/\"last Sunday\" — YYYY-MM-DD). "
                "Also pass placeName so the visit can be displayed without a join. "
                "Call list_cafes or list_roasters first to get the right id. "
                "Do NOT call this to fix a rating or notes on an outing already logged — use update_visit "
                "(after list_visits for visitId) instead, or you will create duplicate visit rows."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "cafeId": {"type": "string", "description": "Use for pure cafe entities"},
                        "roasterId": {"type": "string", "description": "Use when visiting a roaster that has a cafe (hasCafe: true)"},
                        "placeName": {"type": "string", "description": "Display name of the place, stored for easy rendering"},
                        "visitDate": {
                            "type": "string",
                            "description": "YYYY-MM-DD; prefer Clock context + user phrasing rather than prompting for calendar trivia",
                        },
                        "drinks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "e.g. ['V60 Wote Ethiopia', 'cortado']",
                        },
                        "rating": {"type": "integer", "minimum": 1, "maximum": 10},
                        "notes": {"type": "string"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "list_visits",
            "description": "List the user's cafe visits, optionally filtered by cafe.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "cafeId": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "update_visit",
            "description": (
                "Edit a logged cafe visit (wrong rating, notes, drinks, date, or display name). "
                "Call list_visits first to get visitId. "
                "Do NOT log_visit again for the same outing — that duplicates rows."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["visitId"],
                    "properties": {
                        "visitId": {"type": "string"},
                        "rating": {"type": "integer", "minimum": 1, "maximum": 10},
                        "notes": {"type": "string"},
                        "drinks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Replaces the drinks list when supplied.",
                        },
                        "visitDate": {
                            "type": "string",
                            "description": "YYYY-MM-DD; prefer Clock context + user phrasing rather than prompting for calendar trivia",
                        },
                        "placeName": {"type": "string", "description": "Denormalized display label only"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "delete_visit",
            "description": (
                "Permanently delete a duplicate or mistaken visit row. "
                "Call list_visits first to confirm visitId. Cannot be undone."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["visitId"],
                    "properties": {"visitId": {"type": "string"}},
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_dialin_advice",
            "description": (
                "Pull recent brews for a coffee+method and apply simple "
                "extraction heuristics. Use the returned 'heuristics' list "
                "to guide the user's next adjustment."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["coffeeId", "method"],
                    "properties": {
                        "coffeeId": {"type": "string"},
                        "method": {"type": "string"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "search_web",
            "description": (
                "Live web search (Tavily). Cached globally; each distinct query+domain set costs quota "
                "on cache miss. "
                "Use case A — cafe/roaster discovery: new cities, open/closed verification, itinerary ideas. "
                "Use case B — technique & gear discourse: pass includeDomains ['reddit.com'] and queries like "
                "'James Hoffman bloom pour over reddit', 'r/espresso channeling puck prep', "
                "'Niche Zero espresso dial 2026', 'r/PourOver v60 swirling vs spoon'. "
                "Always ground user-specific extraction numbers in list_brews / get_dialin_advice; "
                "Reddit summarizes community lore and trends only. Skip when retrieval would add "
                "nothing beyond stable facts or purely local saved cafes."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Search query — be precise. Venue discovery: city + specialty coffee + reddit/year. "
                                "Technique chatter: brew method + symptom + optional 'James Hoffman' "
                                "or subreddit hints. Examples: "
                                "'Phoenix AZ specialty coffee reddit', "
                                "'r/espresso IMS basket vs stock 2026', "
                                "'Hoffmann blooming pour over technique reddit'"
                            ),
                        },
                        "includeDomains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Domain allowlist. For Reddit-heavy answers use [\"reddit.com\"] alone. "
                                "For cafes allow tripadvisor, etc. Omit for Tavily-wide results."
                            ),
                        },
                        "maxResults": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "description": (
                                "Number of results (default 5). For merging city café shortlists from "
                                "multiple queries, prefer 8–10 so roaster-led names surface."
                            ),
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_youtube_transcript",
            "description": (
                "Fetch YouTube captions (when enabled) — same caching + monthly quota bucket as search_web. "
                "Use when the user drops a Hoffman / espresso / PourOver tutorial link or asks what a specific "
                "video says about technique. Summarize; do not paste the whole transcript. "
                "If fetch fails from IP blocking, fall back to search_web with reddit.com."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
            "required": ["video"],
            "properties": {
                        "video": {
                            "type": "string",
                            "description": (
                                "Full youtube.com/watch?v=..., youtu.be/..., shorts, embed URL, "
                                "or bare 11-character ID."
                            ),
                        },
                        "languages": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Preferred subtitle languages (default tries en, en-US, en-GB).",
                        },
                        "maxChars": {
                            "type": "integer",
                            "minimum": 800,
                            "maximum": 48000,
                            "description": "Max narration characters returned (long videos are clipped). Default ~22000.",
                        },
                    },
                }
            },
        }
    },
]


_TOOL_FUNCS: dict[str, Callable[[str, dict[str, Any]], Any]] = {
    "search_web": _search_web,
    "get_youtube_transcript": _youtube_transcript,
    "search_known_roasters": _search_known_roasters,
    "list_roasters": _list_roasters,
    "add_roaster": _add_roaster,
    "update_roaster": _update_roaster,
    "list_coffees": _list_coffees,
    "add_coffee": _add_coffee,
    "archive_coffee": _archive_coffee,
    "delete_coffee": _delete_coffee,
    "update_coffee": _update_coffee,
    "log_brew": _log_brew,
    "update_brew": _update_brew,
    "delete_brew": _delete_brew,
    "list_brews": _list_brews,
    "get_dialin_advice": _get_dialin_advice,
    "list_equipment": _list_equipment,
    "add_equipment": _add_equipment,
    "add_cafe": _add_cafe,
    "list_cafes": _list_cafes,
    "update_cafe": _update_cafe,
    "log_visit": _log_visit,
    "list_visits": _list_visits,
    "update_visit": _update_visit,
    "delete_visit": _delete_visit,
    "get_preferences": _get_preferences,
    "update_preferences": _update_preferences,
    "summarize_coffee": _summarize_coffee,
    "retrieve_journal": _retrieve_journal,
    "lookup_coffee_term": _lookup_coffee_term,
}


def dispatch(name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        result = fn(user_id, args or {})
        if isinstance(result, dict) and result.get("duplicatePlace"):
            return {
                "ok": False,
                "code": "DUPLICATE_PLACE",
                "error": result.get("hint", "duplicate place"),
                "existingType": result.get("existingType"),
                "existingId": result.get("existingId"),
                "existingName": result.get("existingName"),
            }
        return {"ok": True, "result": result}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"internal error: {e}"}
