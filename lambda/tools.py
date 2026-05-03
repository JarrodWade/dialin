"""LLM tool definitions and dispatcher.

We hand these "toolSpec" entries to Bedrock via the Converse API.
When the model emits a `toolUse` block, we look up the implementation
here, run it against DynamoDB, and feed the result back as a
`toolResult` so the model can compose its final answer.
"""

from __future__ import annotations

from typing import Any, Callable

import ddb


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
    """Heuristic dial-in advice from the most recent brews of a coffee+method.

    The model is expected to combine this with the user's free-text
    description of the brew to produce a final recommendation.
    """
    coffee_id = args["coffeeId"]
    method = args["method"]
    coffee = ddb.get_coffee(user_id, coffee_id)
    if coffee is None:
        return {"error": f"coffee {coffee_id} not found"}

    brews = ddb.list_brews(user_id, coffee_id=coffee_id, limit=10)
    brews = [b for b in brews if b.get("method") == method]

    last = brews[0] if brews else None
    last_taste = (last or {}).get("taste", "").lower() if last else ""
    heuristics: list[str] = []
    if "sour" in last_taste or "grassy" in last_taste or "weak" in last_taste:
        heuristics += [
            "likely under-extracted",
            "grind a step finer",
            "raise water temperature 1-2 degC",
            "extend total contact time",
        ]
    if "bitter" in last_taste or "astringent" in last_taste or "harsh" in last_taste:
        heuristics += [
            "likely over-extracted",
            "grind a step coarser",
            "lower water temperature 1-2 degC",
            "shorten total contact time",
        ]

    return {
        "coffee": {
            "name": coffee.get("name"),
            "roaster": coffee.get("roaster"),
            "process": coffee.get("process"),
            "roastDate": coffee.get("roastDate"),
        },
        "method": method,
        "lastBrew": last,
        "recentBrews": brews[:5],
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


# --- Coffee summary ----------------------------------------------------------


def _summarize_coffee(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return ddb.summarize_coffee(user_id, args["coffeeId"])


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
]


_TOOL_FUNCS: dict[str, Callable[[str, dict[str, Any]], Any]] = {
    "list_roasters": _list_roasters,
    "add_roaster": _add_roaster,
    "update_roaster": _update_roaster,
    "list_coffees": _list_coffees,
    "add_coffee": _add_coffee,
    "archive_coffee": _archive_coffee,
    "update_coffee": _update_coffee,
    "log_brew": _log_brew,
    "update_brew": _update_brew,
    "delete_brew": _delete_brew,
    "list_brews": _list_brews,
    "get_dialin_advice": _get_dialin_advice,
    "list_equipment": _list_equipment,
    "add_equipment": _add_equipment,
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
