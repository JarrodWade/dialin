"""DynamoDB helpers for the dialin coffee brew journal.

Single-table design.

  PK / SK                                  itemType    notes
  USER#<id> / PROFILE                      Profile     preferences (singleton per user)
  USER#<id> / ROASTER#<roasterId>          Roaster     canonical roaster entity (name, city, …)
  USER#<id> / EQUIP#<equipId>              Equipment   typed: MACHINE/GRINDER/BREWER/KETTLE
  USER#<id> / COFFEE#<coffeeId>            Coffee      one per bag; roasterId FK + denorm name
  USER#<id> / BREW#<isoTs>#<brewId>        Brew        time-ordered timeline
  CACHE#WEBSEARCH / <sha256>               WebSearchCache   shared Tavily cache (TTL via expiresAt)
  USER#<id> / RAGCHUNK#BREW#<brewId>        JournalRAGChunk  brew+coffee prose + embedding (retrieve_journal)
  USER#<id> / RAGCHUNK#COFFEE#<coffeeId>    JournalRAGChunk  bag notes + embedding
  USER#<id> / RAGCHUNK#VISIT#<visitId>      JournalRAGChunk  visit prose + embedding
  USER#<id> / USAGE#WEBSEARCH#YYYY-MM       UsageCounter     monthly live-search quota

GSI1 (brews by coffee, time-ordered):
  GSI1PK = COFFEE#<coffeeId>
  GSI1SK = BREW#<isoTs>#<brewId>
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from statistics import mean
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from equipment_canonical import resolve_equipment_display_name

_TABLE_NAME = os.environ["TABLE_NAME"]
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(_TABLE_NAME)

EQUIP_TYPES = {"MACHINE", "GRINDER", "BREWER", "KETTLE"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _to_decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def coerce_bool(v: Any) -> bool:
    """Normalize JSON / form / LLM tool values to bool.

    Python's bool(\"false\") is True — callers must use this for API bodies and tool input.
    """
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("", "0", "false", "no", "n", "off", "null", "none"):
            return False
        if s in ("1", "true", "yes", "y", "on"):
            return True
        return False
    return bool(v)


def _strip_keys(item: dict[str, Any]) -> dict[str, Any]:
    """Remove DynamoDB-only keys before returning items to API clients."""
    return {k: v for k, v in item.items() if k not in {"PK", "SK", "GSI1PK", "GSI1SK"}}


def _normalize_place_name(name: str | None) -> str:
    """Lowercase, trim, collapse whitespace for duplicate detection."""
    if not name:
        return ""
    s = str(name).lower().strip()
    s = re.sub(r"[\s\-]+", " ", s)
    return s


def _cities_soft_match(city_a: str | None, city_b: str | None) -> bool:
    """If either city is missing, treat as compatible (name-only match). Otherwise substring/equality."""
    a = _normalize_place_name(city_a)
    b = _normalize_place_name(city_b)
    if not a or not b:
        return True
    return a == b or a in b or b in a


def find_matching_existing_cafe_by_place(
    user_id: str, name: str, city: str | None = None
) -> dict[str, Any] | None:
    """Return an active cafe row if normalized name+city Soft-matches another café."""
    want = _normalize_place_name(name)
    if not want:
        return None
    for c in list_cafes(user_id, include_archived=False):
        if _normalize_place_name(c.get("name")) != want:
            continue
        if not _cities_soft_match(city, c.get("city")):
            continue
        return c
    return None


def find_matching_cafe_for_new_roaster(
    user_id: str, name: str, city: str | None = None
) -> dict[str, Any] | None:
    """Return an active cafe item if it likely duplicates this roaster (same place)."""
    return find_matching_existing_cafe_by_place(user_id, name, city)


def find_matching_roaster_for_new_cafe(
    user_id: str, name: str, city: str | None = None
) -> dict[str, Any] | None:
    """Return an active roaster item if it likely duplicates this cafe (same place)."""
    want = _normalize_place_name(name)
    if not want:
        return None
    for r in list_roasters(user_id, include_archived=False):
        if _normalize_place_name(r.get("name")) != want:
            continue
        if not _cities_soft_match(city, r.get("city")):
            continue
        return r
    return None


# ---------------------------------------------------------------------------
# Roaster
# ---------------------------------------------------------------------------


def create_roaster(
    user_id: str,
    name: str,
    *,
    city: str | None = None,
    country: str | None = None,
    website: str | None = None,
    notes: str | None = None,
    has_cafe: bool = False,
) -> dict[str, Any]:
    roaster_id = _new_id("rst")
    created_at = _now_iso()
    item = {
        "PK": f"USER#{user_id}",
        "SK": f"ROASTER#{roaster_id}",
        "itemType": "Roaster",
        "userId": user_id,
        "roasterId": roaster_id,
        "name": name,
        "city": city,
        "country": country or "US",
        "website": website,
        "notes": notes,
        "hasCafe": has_cafe,
        "archived": False,
        "createdAt": created_at,
        "updatedAt": created_at,
    }
    _table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )
    return _strip_keys(item)


def get_roaster(user_id: str, roaster_id: str) -> dict[str, Any] | None:
    resp = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"ROASTER#{roaster_id}"})
    item = resp.get("Item")
    return _strip_keys(item) if item else None


def list_roasters(user_id: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with("ROASTER#"),
    )
    items = [_strip_keys(i) for i in resp.get("Items", [])]
    if not include_archived:
        items = [i for i in items if not i.get("archived")]
    items.sort(key=lambda i: i.get("name", "").lower())
    return items


def update_roaster(user_id: str, roaster_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "city", "country", "website", "notes", "archived", "hasCafe"}
    updates = {k: v for k, v in updates.items() if k in allowed}
    if "hasCafe" in updates:
        updates["hasCafe"] = coerce_bool(updates["hasCafe"])
    if "archived" in updates:
        updates["archived"] = coerce_bool(updates["archived"])
    if not updates:
        raise ValueError("no allowed fields to update")

    set_parts = ["updatedAt = :now"]
    values: dict[str, Any] = {":now": _now_iso()}
    names: dict[str, str] = {}
    for i, (k, v) in enumerate(updates.items()):
        nk, vk = f"#k{i}", f":v{i}"
        names[nk] = k
        values[vk] = v
        set_parts.append(f"{nk} = {vk}")

    try:
        resp = _table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"ROASTER#{roaster_id}"},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError(f"roaster {roaster_id} not found") from e
        raise
    return _strip_keys(resp.get("Attributes", {}))


# ---------------------------------------------------------------------------
# Coffee
# ---------------------------------------------------------------------------


def create_coffee(
    user_id: str,
    roaster: str,
    name: str,
    *,
    roaster_id: str | None = None,
    origin: str | None = None,
    process: str | None = None,
    roast_date: str | None = None,
    weight_g: float | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    coffee_id = _new_id("cof")
    created_at = _now_iso()
    grams = _to_decimal(weight_g)
    item = {
        "PK": f"USER#{user_id}",
        "SK": f"COFFEE#{coffee_id}",
        "itemType": "Coffee",
        "userId": user_id,
        "coffeeId": coffee_id,
        "roasterId": roaster_id,
        "roaster": roaster,
        "name": name,
        "origin": origin,
        "process": process,
        "roastDate": roast_date,
        "weightG": grams,
        "gramsRemaining": grams,
        "notes": notes,
        "archived": False,
        "createdAt": created_at,
        "updatedAt": created_at,
    }
    _table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )
    return _strip_keys(item) | {"coffeeId": coffee_id}


def delete_coffee(user_id: str, coffee_id: str) -> None:
    """Permanently delete a coffee item. Associated brews are NOT deleted."""
    try:
        _table.delete_item(
            Key={"PK": f"USER#{user_id}", "SK": f"COFFEE#{coffee_id}"},
            ConditionExpression="attribute_exists(PK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError(f"coffee {coffee_id} not found") from e
        raise


def get_coffee(user_id: str, coffee_id: str) -> dict[str, Any] | None:
    resp = _table.get_item(
        Key={"PK": f"USER#{user_id}", "SK": f"COFFEE#{coffee_id}"}
    )
    item = resp.get("Item")
    return _strip_keys(item) if item else None


def list_coffees(user_id: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with("COFFEE#"),
    )
    items = [_strip_keys(i) for i in resp.get("Items", [])]
    if not include_archived:
        items = [i for i in items if not i.get("archived")]
    items.sort(key=lambda i: i.get("createdAt", ""), reverse=True)
    return items


def update_coffee(
    user_id: str,
    coffee_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Patch a coffee. Whitelist of editable fields."""
    allowed = {"roasterId", "roaster", "name", "origin", "process", "roastDate", "notes", "archived"}
    updates = {k: v for k, v in updates.items() if k in allowed}
    if not updates:
        raise ValueError("no allowed fields to update")

    set_parts = ["updatedAt = :now"]
    values: dict[str, Any] = {":now": _now_iso()}
    names: dict[str, str] = {}
    for i, (k, v) in enumerate(updates.items()):
        nk = f"#k{i}"
        vk = f":v{i}"
        names[nk] = k
        values[vk] = v
        set_parts.append(f"{nk} = {vk}")

    try:
        resp = _table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"COFFEE#{coffee_id}"},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError(f"coffee {coffee_id} not found") from e
        raise
    return _strip_keys(resp.get("Attributes", {}))


# ---------------------------------------------------------------------------
# Brew
# ---------------------------------------------------------------------------


VALID_METHODS = {
    "V60", "AeroPress", "Espresso", "FrenchPress", "Chemex",
    "Kalita", "Origami", "OXO Rapid Brewer", "Moka", "ColdBrew",
}


def create_brew(
    user_id: str,
    coffee_id: str,
    method: str,
    *,
    dose_g: float | None = None,
    yield_g: float | None = None,
    water_g: float | None = None,
    grind: str | None = None,
    grinder_id: str | None = None,
    machine_id: str | None = None,
    brewer_id: str | None = None,
    time_s: int | None = None,
    temp_c: float | None = None,
    rating: int | None = None,
    taste: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Log a brew. Atomically decrements the coffee's gramsRemaining if dose_g given.

    Raises ValueError if the coffee doesn't exist or has insufficient stock.
    """
    if method not in VALID_METHODS:
        raise ValueError(f"unknown method {method!r}; one of {sorted(VALID_METHODS)}")

    # 1) Verify coffee exists & (if we know the stock) decrement atomically.
    # If gramsRemaining isn't tracked on the coffee, just confirm the coffee
    # exists and skip the decrement -- we still want to log the brew.
    if dose_g is not None and dose_g > 0:
        try:
            _table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": f"COFFEE#{coffee_id}"},
                UpdateExpression="SET gramsRemaining = gramsRemaining - :dose, updatedAt = :now",
                ConditionExpression=(
                    "attribute_exists(PK) "
                    "AND attribute_exists(gramsRemaining) "
                    "AND gramsRemaining >= :dose"
                ),
                ExpressionAttributeValues={
                    ":dose": Decimal(str(dose_g)),
                    ":now": _now_iso(),
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Condition failed for one of three reasons: coffee missing,
            # gramsRemaining never set, or insufficient stock. Disambiguate.
            existing = get_coffee(user_id, coffee_id)
            if existing is None:
                raise ValueError(f"coffee {coffee_id} not found") from e
            if existing.get("gramsRemaining") is None:
                pass  # bag weight wasn't tracked; log the brew without decrement
            else:
                raise ValueError(
                    f"insufficient stock on coffee {coffee_id} for {dose_g}g "
                    f"(remaining: {existing.get('gramsRemaining')}g)"
                ) from e
    else:
        if get_coffee(user_id, coffee_id) is None:
            raise ValueError(f"coffee {coffee_id} not found")

    # 2) Record the brew.
    brew_id = _new_id("brew")
    iso_ts = _now_iso()
    ratio = None
    if dose_g and (yield_g or water_g):
        ratio = round(float(yield_g or water_g) / float(dose_g), 2)

    item = {
        "PK": f"USER#{user_id}",
        "SK": f"BREW#{iso_ts}#{brew_id}",
        "GSI1PK": f"COFFEE#{coffee_id}",
        "GSI1SK": f"BREW#{iso_ts}#{brew_id}",
        "itemType": "Brew",
        "userId": user_id,
        "brewId": brew_id,
        "coffeeId": coffee_id,
        "method": method,
        "doseG": _to_decimal(dose_g),
        "yieldG": _to_decimal(yield_g),
        "waterG": _to_decimal(water_g),
        "ratio": _to_decimal(ratio),
        "grind": grind,
        "grinderId": grinder_id,
        "machineId": machine_id,
        "brewerId": brewer_id,
        "timeS": time_s,
        "tempC": _to_decimal(temp_c),
        "rating": rating,
        "taste": taste,
        "notes": notes,
        "createdAt": iso_ts,
    }
    _table.put_item(Item=item)
    return _strip_keys(item)


def get_brew(user_id: str, brew_id: str) -> dict[str, Any] | None:
    """Look up a single brew by brewId (scans recent 200 SK entries)."""
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with("BREW#"),
        FilterExpression=Attr("brewId").eq(brew_id),
        ScanIndexForward=False,
        Limit=200,
    )
    items = resp.get("Items", [])
    return _strip_keys(items[0]) if items else None


def _brew_sk(user_id: str, brew_id: str) -> str:
    """Return the full SK for a brew, raising ValueError if not found."""
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with("BREW#"),
        FilterExpression=Attr("brewId").eq(brew_id),
        ProjectionExpression="SK",
        ScanIndexForward=False,
        Limit=200,
    )
    items = resp.get("Items", [])
    if not items:
        raise ValueError(f"brew {brew_id} not found")
    return items[0]["SK"]


_BREW_EDITABLE = {
    "method", "doseG", "yieldG", "waterG", "grind",
    "grinderId", "machineId", "brewerId",
    "timeS", "tempC", "rating", "taste", "notes",
}


def update_brew(user_id: str, brew_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Patch editable fields on a brew. Recalculates ratio if dose/yield change."""
    updates = {k: v for k, v in updates.items() if k in _BREW_EDITABLE}
    if not updates:
        raise ValueError("no editable fields provided")
    if "method" in updates and updates["method"] not in VALID_METHODS:
        raise ValueError(f"unknown method {updates['method']!r}")

    sk = _brew_sk(user_id, brew_id)

    # Fetch current item so we can recalculate ratio if needed.
    resp = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": sk})
    current = resp.get("Item") or {}

    set_parts = ["updatedAt = :now"]
    values: dict[str, Any] = {":now": _now_iso()}
    names: dict[str, str] = {}

    numeric_fields = {"doseG", "yieldG", "waterG", "tempC"}
    int_fields = {"timeS", "rating"}
    for i, (k, v) in enumerate(updates.items()):
        nk, vk = f"#k{i}", f":v{i}"
        names[nk] = k
        if k in numeric_fields and v is not None:
            values[vk] = _to_decimal(v)
        elif k in int_fields and v is not None:
            values[vk] = int(v)
        else:
            values[vk] = v
        set_parts.append(f"{nk} = {vk}")

    # Recalculate ratio if either side changed.
    new_dose = _to_decimal(updates.get("doseG")) or current.get("doseG")
    new_yield = _to_decimal(updates.get("yieldG") or updates.get("waterG")) or \
                current.get("yieldG") or current.get("waterG")
    if new_dose and new_yield:
        ratio = round(float(new_yield) / float(new_dose), 2)
        names["#ratio"] = "ratio"
        values[":ratio"] = _to_decimal(ratio)
        set_parts.append("#ratio = :ratio")

    _table.update_item(
        Key={"PK": f"USER#{user_id}", "SK": sk},
        UpdateExpression="SET " + ", ".join(set_parts),
        ConditionExpression="attribute_exists(PK)",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    return get_brew(user_id, brew_id) or {}


def delete_brew(user_id: str, brew_id: str) -> None:
    """Permanently delete a brew. Does NOT restore gramsRemaining."""
    sk = _brew_sk(user_id, brew_id)
    _table.delete_item(
        Key={"PK": f"USER#{user_id}", "SK": sk},
        ConditionExpression="attribute_exists(PK)",
    )


# ---------------------------------------------------------------------------
# Equipment
# ---------------------------------------------------------------------------


def _normalize_equipment_name(name: str) -> str:
    """Lowercase, strip, collapse whitespace — for deduping user-visible names."""
    s = (name or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _find_active_equipment_same_name(
    user_id: str,
    equip_type: str,
    name: str,
) -> dict[str, Any] | None:
    """If a non-archived item of this type already has the same normalized name, return it."""
    want = _normalize_equipment_name(name)
    if not want:
        return None
    et = equip_type.upper()
    for item in list_equipment(user_id, equip_type=et, include_archived=False):
        if _normalize_equipment_name(item.get("name") or "") == want:
            return item
    return None


def create_equipment(
    user_id: str,
    equip_type: str,
    name: str,
    *,
    brand: str | None = None,
    model: str | None = None,
    notes: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    equip_type = equip_type.upper()
    if equip_type not in EQUIP_TYPES:
        raise ValueError(f"unknown equipType {equip_type!r}; one of {sorted(EQUIP_TYPES)}")

    resolved, name_meta = resolve_equipment_display_name(name)
    if not resolved:
        raise ValueError("equipment name is required")

    existing = _find_active_equipment_same_name(user_id, equip_type, resolved)
    if existing:
        dup_meta: dict[str, Any] = {**(name_meta or {}), "reusedDuplicate": True}
        return existing, dup_meta

    equip_id = _new_id("eq")
    created_at = _now_iso()
    item = {
        "PK": f"USER#{user_id}",
        "SK": f"EQUIP#{equip_id}",
        "itemType": "Equipment",
        "userId": user_id,
        "equipId": equip_id,
        "equipType": equip_type,
        "name": resolved,
        "brand": brand,
        "model": model,
        "notes": notes,
        "createdAt": created_at,
        "updatedAt": created_at,
    }
    _table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )
    return _strip_keys(item), name_meta


def list_equipment(
    user_id: str,
    *,
    equip_type: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with("EQUIP#"),
    )
    items = [_strip_keys(i) for i in resp.get("Items", [])]
    if not include_archived:
        items = [i for i in items if not i.get("archived")]
    if equip_type:
        items = [i for i in items if i.get("equipType") == equip_type.upper()]
    items.sort(key=lambda i: (i.get("equipType", ""), i.get("name", "")))
    return items


def get_equipment(user_id: str, equip_id: str) -> dict[str, Any] | None:
    resp = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"EQUIP#{equip_id}"})
    item = resp.get("Item")
    return _strip_keys(item) if item else None


def update_equipment(
    user_id: str,
    equip_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Patch an equipment item. Whitelist of editable fields."""
    allowed = {"name", "brand", "model", "notes", "equipType", "archived"}
    updates = {k: v for k, v in updates.items() if k in allowed}
    if "name" in updates:
        raw_name = updates["name"]
        if not isinstance(raw_name, str):
            raise ValueError("name must be a string")
        resolved, _meta = resolve_equipment_display_name(raw_name)
        if not resolved:
            raise ValueError("equipment name cannot be empty")
        updates["name"] = resolved
    if "equipType" in updates:
        updates["equipType"] = updates["equipType"].upper()
        if updates["equipType"] not in EQUIP_TYPES:
            raise ValueError(
                f"unknown equipType {updates['equipType']!r}; one of {sorted(EQUIP_TYPES)}"
            )
    if not updates:
        raise ValueError("no allowed fields to update")

    set_parts = ["updatedAt = :now"]
    values: dict[str, Any] = {":now": _now_iso()}
    names: dict[str, str] = {}
    for i, (k, v) in enumerate(updates.items()):
        nk = f"#k{i}"
        vk = f":v{i}"
        names[nk] = k
        values[vk] = v
        set_parts.append(f"{nk} = {vk}")

    try:
        resp = _table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"EQUIP#{equip_id}"},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError(f"equipment {equip_id} not found") from e
        raise
    return _strip_keys(resp.get("Attributes", {}))


# ---------------------------------------------------------------------------
# Profile / preferences
# ---------------------------------------------------------------------------


_PROFILE_FIELDS = {
    "preferredOrigins",       # list[str]
    "preferredProcesses",     # list[str]
    "preferredRoastLevel",    # str (light/medium/dark/ultralight)
    "dislikedNotes",          # list[str]
    "favoriteRoasters",       # list[str]
    "favoriteCafes",          # list[str]
    "homeCity",               # str
    "notes",                  # str (freeform memory)
    "discoveryChannels",      # list[str] — how they find coffee (e.g. subscription boxes)
    "experimentalPreference",  # str: "" | "open" | "seek" — taste for funky/co-ferment lots
}


def get_profile(user_id: str) -> dict[str, Any]:
    resp = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": "PROFILE"})
    item = resp.get("Item") or {}
    return _strip_keys(item)


def update_profile(
    user_id: str,
    updates: dict[str, Any],
    *,
    replace_lists: bool = False,
) -> dict[str, Any]:
    """Upsert preference fields. Whitelisted; ignores anything else.

    Strings always replace. For lists:
      - replace_lists=False (default, used by the LLM tool): union-merge with
        existing values, deduped case-insensitively.
      - replace_lists=True (used by the UI PATCH /profile): the supplied list
        replaces whatever is on the item, so removing a chip in the UI sticks.
    """
    if not isinstance(updates, dict):
        raise ValueError("updates must be a dict")

    current = get_profile(user_id)
    merged: dict[str, Any] = {**current}

    for field in _PROFILE_FIELDS:
        if field not in updates:
            continue
        new_val = updates[field]
        if isinstance(new_val, list):
            if replace_lists:
                seen: dict[str, str] = {}
                for v in new_val:
                    if not isinstance(v, str):
                        continue
                    key = v.strip().lower()
                    if key and key not in seen:
                        seen[key] = v.strip()
                merged[field] = list(seen.values())
            else:
                existing = current.get(field) or []
                seen = {}
                for v in (*existing, *new_val):
                    if not isinstance(v, str):
                        continue
                    key = v.strip().lower()
                    if key and key not in seen:
                        seen[key] = v.strip()
                merged[field] = list(seen.values())
        else:
            merged[field] = new_val

    now = _now_iso()
    item = {
        "PK": f"USER#{user_id}",
        "SK": "PROFILE",
        "itemType": "Profile",
        "userId": user_id,
        "updatedAt": now,
        "createdAt": current.get("createdAt", now),
        **{k: v for k, v in merged.items() if k in _PROFILE_FIELDS},
    }
    _table.put_item(Item=item)
    return _strip_keys(item)


# ---------------------------------------------------------------------------
# Cafe & Visit
# ---------------------------------------------------------------------------


def create_cafe(
    user_id: str,
    name: str,
    *,
    neighborhood: str | None = None,
    city: str | None = None,
    country: str | None = None,
    website: str | None = None,
    notes: str | None = None,
    is_roaster: bool = False,
) -> dict[str, Any]:
    cafe_id = _new_id("cafe")
    created_at = _now_iso()
    item = {
        "PK": f"USER#{user_id}",
        "SK": f"CAFE#{cafe_id}",
        "itemType": "Cafe",
        "userId": user_id,
        "cafeId": cafe_id,
        "name": name,
        "neighborhood": neighborhood,
        "city": city,
        "country": country or "US",
        "website": website,
        "notes": notes,
        "isRoaster": is_roaster,
        "archived": False,
        "createdAt": created_at,
        "updatedAt": created_at,
    }
    _table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )
    return _strip_keys(item)


def get_cafe(user_id: str, cafe_id: str) -> dict[str, Any] | None:
    resp = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"CAFE#{cafe_id}"})
    item = resp.get("Item")
    return _strip_keys(item) if item else None


def list_cafes(
    user_id: str,
    *,
    city: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with("CAFE#"),
    )
    items = [_strip_keys(i) for i in resp.get("Items", [])]
    if not include_archived:
        items = [i for i in items if not i.get("archived")]
    if city:
        items = [i for i in items if (i.get("city") or "").lower() == city.lower()]
    items.sort(key=lambda i: (i.get("city") or "", i.get("name") or ""))
    return items


def update_cafe(user_id: str, cafe_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "neighborhood", "city", "country", "website", "notes", "archived", "isRoaster"}
    updates = {k: v for k, v in updates.items() if k in allowed}
    if "isRoaster" in updates:
        updates["isRoaster"] = coerce_bool(updates["isRoaster"])
    if "archived" in updates:
        updates["archived"] = coerce_bool(updates["archived"])
    if not updates:
        raise ValueError("no allowed fields to update")

    set_parts = ["updatedAt = :now"]
    values: dict[str, Any] = {":now": _now_iso()}
    names: dict[str, str] = {}
    for i, (k, v) in enumerate(updates.items()):
        nk, vk = f"#k{i}", f":v{i}"
        names[nk] = k
        values[vk] = v
        set_parts.append(f"{nk} = {vk}")

    try:
        resp = _table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"CAFE#{cafe_id}"},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError(f"cafe {cafe_id} not found") from e
        raise
    return _strip_keys(resp.get("Attributes", {}))


def log_visit(
    user_id: str,
    cafe_id: str | None = None,
    *,
    roaster_id: str | None = None,
    place_name: str | None = None,
    visit_date: str | None = None,
    drinks: list[str] | None = None,
    rating: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Log a visit. Accepts either a cafeId or a roasterId (for roaster-cafes).
    place_name is stored denormalized for easy display without a join."""
    place_id = cafe_id or roaster_id
    if not place_id:
        raise ValueError("cafeId or roasterId required")
    # Validate the place exists
    if cafe_id and get_cafe(user_id, cafe_id) is None:
        raise ValueError(f"cafe {cafe_id} not found")
    if roaster_id and get_roaster(user_id, roaster_id) is None:
        raise ValueError(f"roaster {roaster_id} not found")
    visit_id = _new_id("vis")
    iso_ts = _now_iso()
    item = {
        "PK": f"USER#{user_id}",
        "SK": f"VISIT#{iso_ts}#{visit_id}",
        "GSI1PK": f"CAFE#{place_id}",
        "GSI1SK": f"VISIT#{iso_ts}#{visit_id}",
        "itemType": "Visit",
        "userId": user_id,
        "visitId": visit_id,
        "cafeId": cafe_id,
        "roasterId": roaster_id,
        "placeId": place_id,
        "placeName": place_name,
        "visitDate": visit_date or iso_ts[:10],
        "drinks": drinks or [],
        "rating": rating,
        "notes": notes,
        "createdAt": iso_ts,
    }
    _table.put_item(Item=item)
    return _strip_keys(item)


def get_visit(user_id: str, visit_id: str) -> dict[str, Any] | None:
    """Look up a single visit by visitId (queries recent VISIT# SK entries)."""
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with("VISIT#"),
        FilterExpression=Attr("visitId").eq(visit_id),
        ScanIndexForward=False,
        Limit=200,
    )
    items = resp.get("Items", [])
    return _strip_keys(items[0]) if items else None


def _visit_sk(user_id: str, visit_id: str) -> str:
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with("VISIT#"),
        FilterExpression=Attr("visitId").eq(visit_id),
        ProjectionExpression="SK",
        ScanIndexForward=False,
        Limit=200,
    )
    items = resp.get("Items", [])
    if not items:
        raise ValueError(f"visit {visit_id} not found")
    return items[0]["SK"]


_VISIT_EDITABLE = {"rating", "notes", "drinks", "visitDate", "placeName"}


def update_visit(user_id: str, visit_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Patch fields on a logged visit. Does not move the visit to another cafe/roaster."""
    updates = {k: v for k, v in updates.items() if k in _VISIT_EDITABLE}
    if not updates:
        raise ValueError("no editable fields provided")

    sk = _visit_sk(user_id, visit_id)

    set_parts = ["updatedAt = :now"]
    values: dict[str, Any] = {":now": _now_iso()}
    names: dict[str, str] = {}

    for i, (k, v) in enumerate(updates.items()):
        nk, vk = f"#k{i}", f":v{i}"
        names[nk] = k
        if k == "rating" and v is not None:
            values[vk] = int(v)
        elif k == "drinks":
            if v is None:
                values[vk] = []
            elif isinstance(v, list):
                values[vk] = [str(x).strip() for x in v if str(x).strip()]
            else:
                raise ValueError("drinks must be a list of strings or null")
        else:
            values[vk] = v
        set_parts.append(f"{nk} = {vk}")

    _table.update_item(
        Key={"PK": f"USER#{user_id}", "SK": sk},
        UpdateExpression="SET " + ", ".join(set_parts),
        ConditionExpression="attribute_exists(PK)",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    return get_visit(user_id, visit_id) or {}


def delete_visit(user_id: str, visit_id: str) -> None:
    sk = _visit_sk(user_id, visit_id)
    _table.delete_item(
        Key={"PK": f"USER#{user_id}", "SK": sk},
        ConditionExpression="attribute_exists(PK)",
    )


def list_visits(
    user_id: str,
    *,
    cafe_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if cafe_id:
        resp = _table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(f"CAFE#{cafe_id}")
            & Key("GSI1SK").begins_with("VISIT#"),
            ScanIndexForward=False,
            Limit=limit,
        )
        items = resp.get("Items", [])
    else:
        resp = _table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
            & Key("SK").begins_with("VISIT#"),
            ScanIndexForward=False,
            Limit=limit,
        )
        items = resp.get("Items", [])
    return [_strip_keys(i) for i in items]


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


def summarize_coffee(user_id: str, coffee_id: str) -> dict[str, Any]:
    """Pull all brews for a coffee and compute simple stats useful to the LLM."""
    coffee = get_coffee(user_id, coffee_id)
    if coffee is None:
        raise ValueError(f"coffee {coffee_id} not found")

    resp = _table.query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"COFFEE#{coffee_id}")
        & Key("GSI1SK").begins_with("BREW#"),
        ScanIndexForward=False,
    )
    brews = [_strip_keys(i) for i in resp.get("Items", [])]

    ratings = [int(b["rating"]) for b in brews if isinstance(b.get("rating"), (int, Decimal))]
    methods = Counter(b.get("method") for b in brews if b.get("method"))
    taste_words: Counter[str] = Counter()
    for b in brews:
        t = (b.get("taste") or "").lower()
        for w in t.replace(",", " ").split():
            w = w.strip(" .;:!?-")
            if len(w) >= 3:
                taste_words[w] += 1

    best = max(
        (b for b in brews if isinstance(b.get("rating"), (int, Decimal))),
        key=lambda b: int(b["rating"]),
        default=None,
    )

    return {
        "coffee": coffee,
        "brewCount": len(brews),
        "avgRating": round(mean(ratings), 2) if ratings else None,
        "bestBrew": best,
        "mostRecentBrew": brews[0] if brews else None,
        "methodCounts": dict(methods),
        "topTasteWords": [w for w, _ in taste_words.most_common(8)],
    }


def list_brews(
    user_id: str,
    *,
    coffee_id: str | None = None,
    method: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List brews for a user, optionally filtered by coffee or method.

    - coffee_id given -> queries GSI1 (efficient even cross-user scope).
    - otherwise -> queries base table by user, newest first; method filtered post-query.
    """
    if coffee_id:
        resp = _table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(f"COFFEE#{coffee_id}")
            & Key("GSI1SK").begins_with("BREW#"),
            ScanIndexForward=False,
            Limit=limit,
        )
        items = resp.get("Items", [])
    else:
        resp = _table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
            & Key("SK").begins_with("BREW#"),
            ScanIndexForward=False,
            Limit=limit * (3 if method else 1),
        )
        items = resp.get("Items", [])
        if method:
            items = [i for i in items if i.get("method") == method][:limit]

    return [_strip_keys(i) for i in items]


# ---------------------------------------------------------------------------
# Web search cache + quota (Tavily)
# ---------------------------------------------------------------------------


def _websearch_cache_keys(query: str, include_domains: list[Any], max_results: int) -> tuple[str, str, str]:
    """Return (PK, SK, normalizedFingerprint) for deduplicating Tavily queries."""
    q = " ".join(query.strip().lower().split())
    domains = sorted({str(d).strip().lower() for d in include_domains if str(d).strip()})
    fingerprint = json.dumps({"q": q, "d": domains, "n": int(max_results)}, separators=(",", ":"))
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()
    return "CACHE#WEBSEARCH", digest, fingerprint


def websearch_cache_get(query: str, include_domains: list[Any], max_results: int) -> dict[str, Any] | None:
    """Return cached Tavily-shaped payload or None."""
    pk, sk, _ = _websearch_cache_keys(query, include_domains, max_results)
    resp = _table.get_item(Key={"PK": pk, "SK": sk})
    item = resp.get("Item")
    if not item:
        return None
    raw = item.get("resultJson")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def websearch_cache_put(
    query: str,
    include_domains: list[Any],
    max_results: int,
    result: dict[str, Any],
    ttl_seconds: int,
) -> None:
    """Persist a Tavily response for shared reuse across users (TTL via expiresAt)."""
    pk, sk, fingerprint = _websearch_cache_keys(query, include_domains, max_results)
    now = int(time.time())
    item = {
        "PK": pk,
        "SK": sk,
        "itemType": "WebSearchCache",
        "normalizedFingerprint": fingerprint,
        "resultJson": json.dumps(result),
        "createdAt": _now_iso(),
        "expiresAt": now + max(60, int(ttl_seconds)),
    }
    _table.put_item(Item=item)


def consume_websearch_quota(user_id: str, monthly_limit: int) -> tuple[bool, int]:
    """Reserve one live Tavily call for the user's current UTC month.

    Returns (allowed, current_count_after_increment_or_existing_at_cap).
    monthly_limit <= 0 means unlimited (always allowed, count -1).
    """
    if monthly_limit <= 0:
        return True, -1

    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    pk = f"USER#{user_id}"
    sk = f"USAGE#WEBSEARCH#{ym}"

    try:
        resp = _table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression="ADD callCount :one SET itemType = :it, updatedAt = :now",
            ExpressionAttributeValues={
                ":one": 1,
                ":lim": monthly_limit,
                ":it": "UsageCounter",
                ":now": _now_iso(),
            },
            ConditionExpression="attribute_not_exists(callCount) OR callCount < :lim",
            ReturnValues="UPDATED_NEW",
        )
        return True, int(resp["Attributes"]["callCount"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    got = _table.get_item(Key={"PK": pk, "SK": sk})
    cur = int((got.get("Item") or {}).get("callCount") or monthly_limit)
    return False, cur
