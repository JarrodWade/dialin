"""LLM tool definitions and dispatcher.

We hand these "toolSpec" entries to Bedrock via the Converse API.
When the model emits a `toolUse` block, we look up the implementation
here, run it against DynamoDB, and feed the result back as a
`toolResult` so the model can compose its final answer.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

import ddb

_TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")


# ---------------------------------------------------------------------------
# Implementations (each takes the user_id + the model-supplied args dict)
# ---------------------------------------------------------------------------


def _list_roasters(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    items = ddb.list_roasters(user_id, include_archived=bool(args.get("includeArchived")))
    return {"count": len(items), "roasters": items}


def _add_roaster(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
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
    return ddb.create_coffee(
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


def _archive_coffee(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    coffee_id = args["coffeeId"]
    return ddb.update_coffee(user_id, coffee_id, {"archived": True})


def _update_coffee(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    coffee_id = args["coffeeId"]
    return ddb.update_coffee(user_id, coffee_id, args)


def _delete_coffee(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    ddb.delete_coffee(user_id, args["coffeeId"])
    return {"deleted": args["coffeeId"]}


def _log_brew(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return ddb.create_brew(
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


def _update_brew(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    brew_id = args["brewId"]
    return ddb.update_brew(user_id, brew_id, args)


def _delete_brew(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    ddb.delete_brew(user_id, args["brewId"])
    return {"deleted": args["brewId"]}


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
    return ddb.create_equipment(
        user_id=user_id,
        equip_type=args["equipType"],
        name=args["name"],
        brand=args.get("brand"),
        model=args.get("model"),
        notes=args.get("notes"),
    )


# --- Preferences -------------------------------------------------------------


def _get_preferences(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return ddb.get_profile(user_id) or {}


def _update_preferences(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return ddb.update_profile(user_id, args)


# --- Cafes & Visits ----------------------------------------------------------


def _add_cafe(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
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
    items = ddb.list_cafes(user_id, city=args.get("city"), include_archived=bool(args.get("includeArchived")))
    return {"count": len(items), "cafes": items}


def _update_cafe(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return ddb.update_cafe(user_id, args["cafeId"], args)


def _log_visit(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return ddb.log_visit(
        user_id=user_id,
        cafe_id=args.get("cafeId"),
        roaster_id=args.get("roasterId"),
        place_name=args.get("placeName"),
        visit_date=args.get("visitDate"),
        drinks=args.get("drinks"),
        rating=args.get("rating"),
        notes=args.get("notes"),
    )


def _list_visits(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    items = ddb.list_visits(user_id, cafe_id=args.get("cafeId"), limit=int(args.get("limit", 10)))
    return {"count": len(items), "visits": items}


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


# --- Web search --------------------------------------------------------------


def _search_web(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Live web search via Tavily — use for current cafe/roaster recommendations."""
    if not _TAVILY_API_KEY:
        return {"ok": False, "error": "web search is not configured (no TAVILY_API_KEY)"}

    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}

    include_domains = args.get("includeDomains") or []

    payload = json.dumps({
        "api_key": _TAVILY_API_KEY,
        "query": query,
        "search_depth": "advanced",
        "max_results": int(args.get("maxResults", 5)),
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
    return {
        "query": query,
        "answer": data.get("answer"),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Bedrock toolSpecs (JSON Schema for the model)
# ---------------------------------------------------------------------------


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "toolSpec": {
            "name": "list_roasters",
            "description": "List the user's saved roasters.",
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
            "description": "Add a piece of brewing gear.",
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
                "Read the user's stored taste preferences and home city. "
                "Call this before recommending coffees, roasters, or cafes."
            ),
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "update_preferences",
            "description": (
                "Persist things you learn about the user's taste so future "
                "sessions remember them. Lists are merged (deduped); strings "
                "replace. Use sparingly: only durable preferences, not one-off comments."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "preferredOrigins": {"type": "array", "items": {"type": "string"}},
                        "preferredProcesses": {"type": "array", "items": {"type": "string"}},
                        "preferredRoastLevel": {"type": "string", "enum": ["light", "medium-light", "medium", "medium-dark", "dark"]},
                        "dislikedNotes": {"type": "array", "items": {"type": "string"}},
                        "favoriteRoasters": {"type": "array", "items": {"type": "string"}},
                        "favoriteCafes": {"type": "array", "items": {"type": "string"}},
                        "homeCity": {"type": "string"},
                        "notes": {"type": "string"},
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
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "list_cafes",
            "description": "List the user's tracked cafes, optionally filtered by city.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "includeArchived": {"type": "boolean"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "update_cafe",
            "description": "Edit a cafe or mark it archived.",
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
                        "isRoaster": {"type": "boolean"},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "log_visit",
            "description": (
                "Log a visit to a cafe OR a roaster-cafe (hasCafe: true). "
                "Provide either cafeId (for pure cafes) or roasterId (for roasters that also have a cafe). "
                "Also pass placeName so the visit can be displayed without a join. "
                "Call list_cafes or list_roasters first to get the right id."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "cafeId": {"type": "string", "description": "Use for pure cafe entities"},
                        "roasterId": {"type": "string", "description": "Use when visiting a roaster that has a cafe (hasCafe: true)"},
                        "placeName": {"type": "string", "description": "Display name of the place, stored for easy rendering"},
                        "visitDate": {"type": "string", "description": "ISO date YYYY-MM-DD"},
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
                "Live web search for current cafe and roaster recommendations. "
                "Use this whenever recommending places — especially for international cities, "
                "cities you're uncertain about, or when the user asks what's good right now. "
                "Searches Reddit, specialty coffee forums, and review sites for fresh intel. "
                "Good queries: 'best specialty coffee [city] [year] reddit', "
                "'[city] third wave coffee recommendations site:reddit.com', "
                "'[cafe name] [city] specialty coffee review'. "
                "Do NOT use for brew advice or coffee bean questions — only for place/cafe discovery."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Natural-language search query. Be specific: include city, "
                                "year, and 'specialty coffee' or 'third wave'. "
                                "E.g. 'best specialty coffee cafes Taipei 2025 reddit'"
                            ),
                        },
                        "includeDomains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional domain allowlist to focus results. "
                                "E.g. ['reddit.com', 'tripadvisor.com']. Leave empty for broad search."
                            ),
                        },
                        "maxResults": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 8,
                            "description": "Number of results to return (default 5).",
                        },
                    },
                }
            },
        }
    },
]


_TOOL_FUNCS: dict[str, Callable[[str, dict[str, Any]], Any]] = {
    "search_web": _search_web,
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
    "get_preferences": _get_preferences,
    "update_preferences": _update_preferences,
    "summarize_coffee": _summarize_coffee,
}


def dispatch(name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        result = fn(user_id, args or {})
        return {"ok": True, "result": result}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"internal error: {e}"}
