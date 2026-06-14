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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import mean
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from equipment_canonical import resolve_equipment_display_name

_TABLE_NAME = os.environ["TABLE_NAME"]
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(_TABLE_NAME)
# Standalone low-level client for TransactWriteItems: the resource's own client
# auto-(de)serializes, which would double-transform our hand-marshalled items.
_client = boto3.client("dynamodb")
_SERIALIZER = TypeSerializer()

EQUIP_TYPES = {"MACHINE", "GRINDER", "BREWER", "KETTLE"}

# Usage counters (chat/web-search) self-expire via the table TTL so they do not
# accumulate forever; keep a long retention so a counter never resets mid-period.
_USAGE_COUNTER_TTL_SEC = 90 * 24 * 3600


def _marshal(d: dict[str, Any]) -> dict[str, Any]:
    """Low-level (client) attribute map for TransactWriteItems."""
    return {k: _SERIALIZER.serialize(v) for k, v in d.items()}


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


def _query_all_by_sk_prefix(user_id: str, sk_prefix: str) -> list[dict[str, Any]]:
    """All rows for a user under an SK prefix, paginating past the 1MB page cap.

    Single ``query`` calls silently truncate at 1MB; entity lists (coffees, roasters,
    cafes) feed the chat journal snapshot, so a truncated page would hide data."""
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with(sk_prefix),
    }
    while True:
        resp = _table.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def _normalize_place_name(name: str | None) -> str:
    """Lowercase, trim, collapse whitespace, strip punctuation for duplicate detection.

    User input and saved names often differ only by typography ("Co." vs "Co",
    commas, trademark symbols, etc.). We keep letters/digits across scripts,
    spaces, and "&".
    """
    if not name:
        return ""
    s = str(name).lower().strip()
    s = re.sub(r"[\s\-]+", " ", s)
    # Drop punctuation — common café/roaster dedupe misses when the model drops "Inc." etc.
    s = re.sub(r"[^\w\s&]+", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
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


def search_places(
    user_id: str,
    *,
    name_contains: str,
    city: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Match saved cafés and roasters by name substring (one call for the assistant)."""
    nc = (name_contains or "").strip()
    if not nc:
        return []
    out: list[dict[str, Any]] = []
    for c in list_cafes(
        user_id, city=city, name_contains=nc, include_archived=include_archived
    ):
        out.append(
            {
                "placeType": "cafe",
                "placeId": c.get("cafeId"),
                "cafeId": c.get("cafeId"),
                "name": c.get("name"),
                "city": c.get("city"),
                "isRoaster": c.get("isRoaster"),
            }
        )
    for r in list_roasters(
        user_id, city=city, name_contains=nc, include_archived=include_archived
    ):
        out.append(
            {
                "placeType": "roaster",
                "placeId": r.get("roasterId"),
                "roasterId": r.get("roasterId"),
                "name": r.get("name"),
                "city": r.get("city"),
                "hasCafe": r.get("hasCafe"),
            }
        )
    out.sort(key=lambda p: (p.get("name") or "").lower())
    return out


# ---------------------------------------------------------------------------
# Roaster
# ---------------------------------------------------------------------------


def create_roaster(
    user_id: str,
    name: str,
    *,
    city: str | None = None,
    state: str | None = None,
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
        "state": state,
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


def resolve_roaster_display_name(user_id: str, roaster_id: str | None) -> str:
    """Return the canonical `name` from the user's roaster row, or ''."""
    rid = str(roaster_id or "").strip()
    if not rid:
        return ""
    row = get_roaster(user_id, rid)
    if not row:
        return ""
    return str(row.get("name") or "").strip()


def list_roasters(
    user_id: str,
    *,
    city: str | None = None,
    name_contains: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    items = [_strip_keys(i) for i in _query_all_by_sk_prefix(user_id, "ROASTER#")]
    if not include_archived:
        items = [i for i in items if not i.get("archived")]
    nc = (name_contains or "").strip().lower()
    if nc:
        items = [i for i in items if nc in (i.get("name") or "").lower()]
    if city and city.strip():
        items = [i for i in items if _city_matches_user_filter(i.get("city"), city)]
    items.sort(key=lambda i: i.get("name", "").lower())
    return items


def update_roaster(user_id: str, roaster_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "city", "state", "country", "website", "notes", "archived", "hasCafe"}
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


def enrich_coffee_rows_roaster_denorm(user_id: str, rows: list[dict[str, Any]]) -> None:
    """Ensure API clients see `roaster` whenever `roasterId` resolves to a ROASTER# row."""
    by_rid: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rid = str(row.get("roasterId") or "").strip()
        if not rid:
            continue
        if str(row.get("roaster") or "").strip():
            continue
        by_rid.setdefault(rid, []).append(row)
    for rid, group in by_rid.items():
        label = resolve_roaster_display_name(user_id, rid)
        if not label:
            continue
        for r in group:
            r["roaster"] = label


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
    out = _strip_keys(item) | {"coffeeId": coffee_id}
    enrich_coffee_rows_roaster_denorm(user_id, [out])
    return out


def delete_coffee(user_id: str, coffee_id: str) -> list[str]:
    """Permanently delete a coffee and all brews logged against it.

    Returns deleted ``brewId`` values (for journal/RAG cleanup). Restores stock for
    each deleted brew when ``gramsRemaining`` is tracked on the bag.
    """
    if get_coffee(user_id, coffee_id) is None:
        raise ValueError(f"coffee {coffee_id} not found")

    deleted_brew_ids: list[str] = []
    for brew in _list_all_brews_for_coffee(user_id, coffee_id):
        bid = str(brew.get("brewId") or "").strip()
        if not bid:
            continue
        delete_brew(user_id, bid)
        deleted_brew_ids.append(bid)

    try:
        _table.delete_item(
            Key={"PK": f"USER#{user_id}", "SK": f"COFFEE#{coffee_id}"},
            ConditionExpression="attribute_exists(PK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError(f"coffee {coffee_id} not found") from e
        raise
    return deleted_brew_ids


def get_coffee(user_id: str, coffee_id: str) -> dict[str, Any] | None:
    resp = _table.get_item(
        Key={"PK": f"USER#{user_id}", "SK": f"COFFEE#{coffee_id}"}
    )
    item = resp.get("Item")
    row = _strip_keys(item) if item else None
    if row:
        enrich_coffee_rows_roaster_denorm(user_id, [row])
    return row


def list_coffees(user_id: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
    items = [_strip_keys(i) for i in _query_all_by_sk_prefix(user_id, "COFFEE#")]
    if not include_archived:
        items = [i for i in items if not i.get("archived")]
    items.sort(key=lambda i: i.get("createdAt", ""), reverse=True)
    enrich_coffee_rows_roaster_denorm(user_id, items)
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
    result = _strip_keys(resp.get("Attributes", {}))
    enrich_coffee_rows_roaster_denorm(user_id, [result])
    return result


# ---------------------------------------------------------------------------
# Brew
# ---------------------------------------------------------------------------


VALID_METHODS = {
    "V60", "AeroPress", "Espresso", "FrenchPress", "Chemex",
    "Kalita", "Origami", "OXO Rapid Brewer", "Moka", "ColdBrew",
}


def _dose_g_amount(raw: Any) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, Decimal):
        v = float(raw)
    else:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return 0.0
    return v if v > 0 else 0.0


def _apply_coffee_stock_delta(user_id: str, coffee_id: str, delta_g: float) -> None:
    """Adjust ``gramsRemaining`` when stock is tracked (+ restores, − consumes)."""
    if not coffee_id or delta_g == 0:
        return
    coffee = get_coffee(user_id, coffee_id)
    if coffee is None:
        raise ValueError(f"coffee {coffee_id} not found")
    if coffee.get("gramsRemaining") is None:
        return

    now = _now_iso()
    if delta_g > 0:
        _table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"COFFEE#{coffee_id}"},
            UpdateExpression="SET gramsRemaining = gramsRemaining + :d, updatedAt = :now",
            ConditionExpression="attribute_exists(PK) AND attribute_exists(gramsRemaining)",
            ExpressionAttributeValues={
                ":d": Decimal(str(delta_g)),
                ":now": now,
            },
        )
        return

    consume = abs(delta_g)
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
                ":dose": Decimal(str(consume)),
                ":now": now,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        refreshed = get_coffee(user_id, coffee_id)
        remaining = (refreshed or {}).get("gramsRemaining")
        raise ValueError(
            f"insufficient stock on coffee {coffee_id} for {consume}g "
            f"(remaining: {remaining}g)"
        ) from e


def _list_all_brews_for_coffee(user_id: str, coffee_id: str) -> list[dict[str, Any]]:
    """All brew rows for a bag (GSI1), newest first, scoped to the owning user."""
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "IndexName": "GSI1",
        "KeyConditionExpression": Key("GSI1PK").eq(f"COFFEE#{coffee_id}")
        & Key("GSI1SK").begins_with("BREW#"),
        "FilterExpression": Attr("userId").eq(user_id),
        "ScanIndexForward": False,
    }
    while True:
        resp = _table.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return [_strip_keys(i) for i in items]


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

    # Verify the coffee exists and learn whether stock is tracked. A light raw
    # get_item (no roaster enrichment) keeps the hot brew-logging path cheap.
    resp = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"COFFEE#{coffee_id}"})
    coffee_row = resp.get("Item")
    if coffee_row is None:
        raise ValueError(f"coffee {coffee_id} not found")
    stock_tracked = coffee_row.get("gramsRemaining") is not None

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

    # When stock is tracked, decrement and record the brew in a single
    # transaction so we can never lose grams without persisting the brew (or
    # vice versa). When it isn't tracked, a plain put is enough.
    if stock_tracked and dose_g is not None and dose_g > 0:
        try:
            _client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": _TABLE_NAME,
                            "Key": _marshal(
                                {"PK": f"USER#{user_id}", "SK": f"COFFEE#{coffee_id}"}
                            ),
                            "UpdateExpression": (
                                "SET gramsRemaining = gramsRemaining - :dose, updatedAt = :now"
                            ),
                            "ConditionExpression": (
                                "attribute_exists(PK) "
                                "AND attribute_exists(gramsRemaining) "
                                "AND gramsRemaining >= :dose"
                            ),
                            "ExpressionAttributeValues": _marshal(
                                {":dose": Decimal(str(dose_g)), ":now": iso_ts}
                            ),
                        }
                    },
                    {"Put": {"TableName": _TABLE_NAME, "Item": _marshal(item)}},
                ]
            )
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("TransactionCanceledException", "ConditionalCheckFailedException"):
                existing = get_coffee(user_id, coffee_id)
                raise ValueError(
                    f"insufficient stock on coffee {coffee_id} for {dose_g}g "
                    f"(remaining: {(existing or {}).get('gramsRemaining')}g)"
                ) from e
            raise
    else:
        _table.put_item(Item=item)
    return _strip_keys(item)


_TIMELINE_QUERY_PAGE = 200


def _find_timeline_item_by_id(
    user_id: str,
    sk_prefix: str,
    id_attribute: str,
    entity_id: str,
    *,
    projection: str | None = None,
) -> dict[str, Any] | None:
    """Paginate a user's time-ordered SK prefix until ``id_attribute`` matches."""
    eid = (entity_id or "").strip()
    if not eid:
        return None
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"USER#{user_id}")
        & Key("SK").begins_with(sk_prefix),
        "FilterExpression": Attr(id_attribute).eq(eid),
        "ScanIndexForward": False,
        "Limit": _TIMELINE_QUERY_PAGE,
    }
    if projection:
        kwargs["ProjectionExpression"] = projection
    while True:
        resp = _table.query(**kwargs)
        items = resp.get("Items", [])
        if items:
            row = items[0]
            return row if projection else _strip_keys(row)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return None
        kwargs["ExclusiveStartKey"] = lek


def get_brew(user_id: str, brew_id: str) -> dict[str, Any] | None:
    """Look up a single brew by ``brewId`` (full timeline scan, paginated)."""
    return _find_timeline_item_by_id(user_id, "BREW#", "brewId", brew_id)


def _brew_sk(user_id: str, brew_id: str) -> str:
    """Return the full SK for a brew, raising ValueError if not found."""
    row = _find_timeline_item_by_id(
        user_id, "BREW#", "brewId", brew_id, projection="SK"
    )
    if not row:
        raise ValueError(f"brew {brew_id} not found")
    return row["SK"]


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

    if "doseG" in updates:
        old_dose = _dose_g_amount(current.get("doseG"))
        new_dose = _dose_g_amount(updates.get("doseG"))
        stock_delta = old_dose - new_dose
        if stock_delta != 0:
            cid = str(current.get("coffeeId") or "").strip()
            if cid:
                _apply_coffee_stock_delta(user_id, cid, stock_delta)

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

    # Recalculate ratio if either side changed. Use explicit presence checks so
    # an updated value of 0 is not mistaken for "unchanged" (0 is falsy).
    new_dose = _to_decimal(updates["doseG"]) if "doseG" in updates else current.get("doseG")
    if "yieldG" in updates:
        new_yield = _to_decimal(updates["yieldG"])
    elif "waterG" in updates:
        new_yield = _to_decimal(updates["waterG"])
    else:
        new_yield = current.get("yieldG") or current.get("waterG")
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
    """Permanently delete a brew; restores ``gramsRemaining`` when stock is tracked."""
    sk = _brew_sk(user_id, brew_id)
    resp = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": sk})
    current = resp.get("Item") or {}
    cid = str(current.get("coffeeId") or "").strip()
    dose = _dose_g_amount(current.get("doseG"))
    if cid and dose > 0:
        _apply_coffee_stock_delta(user_id, cid, dose)
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


def _normalize_equipment_identity(raw_name: str) -> str:
    """Like _normalize_equipment_name but maps stored/typed names through gear_canonical aliases first.

    So a row saved as \"niche\" matches adding \"Niche Zero\", matching what users expect.
    """
    resolved, _ = resolve_equipment_display_name(raw_name)
    return _normalize_equipment_name(resolved)


def _equipment_row_active(item: dict[str, Any]) -> bool:
    """True if gear is not retired. Uses coerce_bool so string \"false\" is not treated as archived."""
    return not coerce_bool(item.get("archived"))


def _query_user_equipment_items(user_id: str) -> list[dict[str, Any]]:
    """All EQUIP# rows for a user (paginated — single-query truncation can hide gear / break deduping)."""
    out: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("EQUIP#"),
    }
    while True:
        resp = _table.query(**kwargs)
        out.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return out


def _find_active_equipment_same_name(
    user_id: str,
    equip_type: str,
    name: str,
) -> dict[str, Any] | None:
    """If a non-archived item of this type already has the same normalized name, return it."""
    want = _normalize_equipment_identity(name)
    if not want:
        return None
    et = (equip_type or "").strip().upper()
    for item in list_equipment(user_id, equip_type=et, include_archived=False):
        if _normalize_equipment_identity(item.get("name") or "") == want:
            return item
    return None


def _normalized_hario_v60_brewer_family(norm_identity: str) -> bool:
    """True if normalized identity is Hario V60 or a sized variant (01 / 02 / …)."""
    return norm_identity == "hario v60" or norm_identity.startswith("hario v60 ")


def _brewers_hario_v60_family(user_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in list_equipment(user_id, equip_type="BREWER", include_archived=False):
        ident = _normalize_equipment_identity(item.get("name") or "")
        if _normalized_hario_v60_brewer_family(ident):
            rows.append(item)
    return rows


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
        if existing.get("name") != resolved:
            existing = update_equipment(user_id, existing["equipId"], {"name": resolved})
        return existing, dup_meta

    # One saved Hario V60 brewer + add_equipment for another size: upgrade the row instead of a duplicate.
    if equip_type == "BREWER":
        want_ident = _normalize_equipment_identity(resolved)
        if _normalized_hario_v60_brewer_family(want_ident):
            family = _brewers_hario_v60_family(user_id)
            if len(family) == 1:
                sole = family[0]
                sole_ident = _normalize_equipment_identity(sole.get("name") or "")
                if sole_ident != want_ident:
                    variant_meta: dict[str, Any] = {**(name_meta or {}), "replacedVariant": True}
                    updated = update_equipment(user_id, sole["equipId"], {"name": resolved})
                    return updated, variant_meta

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
    raw = _query_user_equipment_items(user_id)
    items = [_strip_keys(i) for i in raw]
    if not include_archived:
        items = [i for i in items if _equipment_row_active(i)]
    if equip_type:
        want_et = (equip_type or "").strip().upper()
        items = [
            i for i in items if (i.get("equipType") or "").strip().upper() == want_et
        ]
    # Normalize type casing for clients (strict JS filters and legacy lowercase rows).
    for i in items:
        raw_et = i.get("equipType")
        if isinstance(raw_et, str) and raw_et.strip():
            i["equipType"] = raw_et.strip().upper()
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
    if "archived" in updates:
        updates["archived"] = coerce_bool(updates["archived"])
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
    out = _strip_keys(resp.get("Attributes", {}))
    raw_et = out.get("equipType")
    if isinstance(raw_et, str) and raw_et.strip():
        out["equipType"] = raw_et.strip().upper()
    return out


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
    "timezone",               # str IANA TZ, e.g. America/New_York (used when inferring relative visit dates)
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
    state: str | None = None,
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
        "state": state,
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


def _city_matches_user_filter(stored_raw: str | None, filter_city: str) -> bool:
    """True if a cafe row's ``city`` should match the user's filter.

    Users and LLMs often use "Kyoto" while the row stores "Kyoto, Japan", ward names, etc.
    Empty stored city never matches a non-empty filter.
    """
    q = (filter_city or "").strip().lower()
    if not q:
        return True
    s = (stored_raw or "").strip().lower()
    if not s:
        return False
    if s == q:
        return True
    head = s.split(",")[0].strip()
    if head == q:
        return True
    if head.startswith(q + "-") or head.startswith(q + " "):
        return True
    return False


def list_cafes(
    user_id: str,
    *,
    city: str | None = None,
    name_contains: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    items = [_strip_keys(i) for i in _query_all_by_sk_prefix(user_id, "CAFE#")]
    if not include_archived:
        items = [i for i in items if not i.get("archived")]
    nc = (name_contains or "").strip().lower()
    if nc:
        items = [i for i in items if nc in (i.get("name") or "").lower()]
    if city and (city.strip()):
        items = [i for i in items if _city_matches_user_filter(i.get("city"), city)]
    items.sort(key=lambda i: (i.get("city") or "", i.get("name") or ""))
    return items


def update_cafe(user_id: str, cafe_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "neighborhood", "city", "state", "country", "website", "notes", "archived", "isRoaster"}
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


def _calendar_day_visit(raw: Any) -> str:
    """Normalize visitDate strings to YYYY-MM-DD."""
    return str(raw or "").strip()[:10]


def _iso_ts_to_datetime(iso_ts: Any) -> datetime:
    """Parse Dynamo `createdAt` / ISO-ish UTC timestamps."""
    s = str(iso_ts or "").strip().replace("Z", "+00:00")
    if not s:
        raise ValueError("empty iso")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _visit_drinks_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [str(raw).strip()] if str(raw).strip() else []


def _merged_visit_drinks(existing: Any, incoming: Any) -> list[str] | None:
    """Dedupe-preserving concatenation for near-duplicate visit merges."""
    cur = _visit_drinks_list(existing)
    add = _visit_drinks_list(incoming)
    if not add:
        return None  # incoming absent or empty → do not PATCH drinks via merge
    seen = {x.strip().lower() for x in cur}
    out = list(cur)
    for d in add:
        k = d.strip().lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
    return out


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
    cid_raw = str(cafe_id or "").strip()
    rid_raw = str(roaster_id or "").strip()

    iso_ts = _now_iso()
    calendar_day = _calendar_day_visit(visit_date) or iso_ts[:10]

    # Suppress accidental double-submit from the assistant (often two tool calls seconds apart).
    try:
        win_secs = float(os.environ.get("VISIT_NEAR_DUP_WINDOW_SEC", "180"))
    except ValueError:
        win_secs = 180.0
    if win_secs > 0:
        if cid_raw:
            recent_scope = list_visits(user_id, cafe_id=cid_raw, limit=50)
        else:
            recent_scope = list_visits(user_id, roaster_id=rid_raw, limit=50)
        now_dt = datetime.now(timezone.utc)
        window_td = timedelta(seconds=win_secs)
        dup_row: dict[str, Any] | None = None
        for row in recent_scope:
            if _calendar_day_visit(row.get("visitDate")) != calendar_day:
                continue
            created = row.get("createdAt")
            if not created:
                continue
            try:
                if now_dt - _iso_ts_to_datetime(created) > window_td:
                    continue
            except ValueError:
                continue
            dup_row = row
            break

        if dup_row is not None:
            vid = str(dup_row.get("visitId") or "").strip()
            if vid:
                merge: dict[str, Any] = {}
                if rating is not None:
                    merge["rating"] = int(rating)
                if notes is not None and str(notes).strip():
                    merge["notes"] = str(notes).strip()
                md = _merged_visit_drinks(dup_row.get("drinks"), drinks)
                if md is not None:
                    merge["drinks"] = md
                if str(place_name or "").strip():
                    merge["placeName"] = str(place_name).strip()
                vd = _calendar_day_visit(visit_date)
                if vd:
                    merge["visitDate"] = vd
                if merge:
                    return update_visit(user_id, vid, merge)
                refreshed = get_visit(user_id, vid)
                return refreshed or dup_row

    visit_id = _new_id("vis")
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
    """Look up a single visit by ``visitId`` (full timeline scan, paginated)."""
    return _find_timeline_item_by_id(user_id, "VISIT#", "visitId", visit_id)


def _visit_sk(user_id: str, visit_id: str) -> str:
    row = _find_timeline_item_by_id(
        user_id, "VISIT#", "visitId", visit_id, projection="SK"
    )
    if not row:
        raise ValueError(f"visit {visit_id} not found")
    return row["SK"]


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


def _visit_matches_place_name(
    user_id: str, visit: dict[str, Any], needle: str
) -> bool:
    """True if needle matches placeName or the linked cafe/roaster display name."""
    if needle in (visit.get("placeName") or "").lower():
        return True
    cid = str(visit.get("cafeId") or "").strip()
    if cid:
        row = get_cafe(user_id, cid)
        if row and needle in (row.get("name") or "").lower():
            return True
    rid = str(visit.get("roasterId") or "").strip()
    if rid:
        row = get_roaster(user_id, rid)
        if row and needle in (row.get("name") or "").lower():
            return True
    return False


def list_visits(
    user_id: str,
    *,
    cafe_id: str | None = None,
    roaster_id: str | None = None,
    place_name_contains: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    place_id = (cafe_id or roaster_id or "").strip() or None
    name_nc = (place_name_contains or "").strip().lower()
    fetch_limit = limit
    if name_nc and not place_id:
        fetch_limit = min(max(limit, 1) * 4, 50)

    if place_id:
        resp = _table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(f"CAFE#{place_id}")
            & Key("GSI1SK").begins_with("VISIT#"),
            FilterExpression=Attr("userId").eq(user_id),
            ScanIndexForward=False,
            Limit=fetch_limit,
        )
        items = resp.get("Items", [])
    else:
        resp = _table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
            & Key("SK").begins_with("VISIT#"),
            ScanIndexForward=False,
            Limit=fetch_limit,
        )
        items = resp.get("Items", [])
    rows = [_strip_keys(i) for i in items]
    if name_nc:
        rows = [r for r in rows if _visit_matches_place_name(user_id, r, name_nc)]
    return rows[: max(1, min(limit, 50))]


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
        FilterExpression=Attr("userId").eq(user_id),
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


def enrich_brew_rows_coffee_denorm(user_id: str, rows: list[dict[str, Any]]) -> None:
    """Attach ``coffeeName`` / ``coffeeRoaster`` to brew rows for API clients."""
    by_cid: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        cid = str(row.get("coffeeId") or "").strip()
        if not cid:
            continue
        by_cid.setdefault(cid, []).append(row)
    for cid, group in by_cid.items():
        coffee = get_coffee(user_id, cid)
        if not coffee:
            continue
        name = str(coffee.get("name") or "").strip()
        roaster = str(coffee.get("roaster") or "").strip()
        for row in group:
            if name:
                row["coffeeName"] = name
            if roaster:
                row["coffeeRoaster"] = roaster


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
            FilterExpression=Attr("userId").eq(user_id),
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

    rows = [_strip_keys(i) for i in items]
    enrich_brew_rows_coffee_denorm(user_id, rows)
    return rows


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
            UpdateExpression=(
                "ADD callCount :one "
                "SET itemType = :it, updatedAt = :now, "
                "expiresAt = if_not_exists(expiresAt, :exp)"
            ),
            ExpressionAttributeValues={
                ":one": 1,
                ":lim": monthly_limit,
                ":it": "UsageCounter",
                ":now": _now_iso(),
                ":exp": int(time.time()) + _USAGE_COUNTER_TTL_SEC,
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


def consume_chat_quota(user_id: str, daily_limit: int) -> tuple[bool, int]:
    """Reserve one ``/chat`` turn for the user's current UTC day.

    Returns (allowed, count_after_increment_or_at_cap). ``daily_limit <= 0`` = unlimited.
    """
    if daily_limit <= 0:
        return True, -1

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pk = f"USER#{user_id}"
    sk = f"USAGE#CHAT#{day}"

    try:
        resp = _table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression=(
                "ADD turnCount :one "
                "SET itemType = :it, updatedAt = :now, "
                "expiresAt = if_not_exists(expiresAt, :exp)"
            ),
            ExpressionAttributeValues={
                ":one": 1,
                ":lim": daily_limit,
                ":it": "UsageCounter",
                ":now": _now_iso(),
                ":exp": int(time.time()) + _USAGE_COUNTER_TTL_SEC,
            },
            ConditionExpression="attribute_not_exists(turnCount) OR turnCount < :lim",
            ReturnValues="UPDATED_NEW",
        )
        return True, int(resp["Attributes"]["turnCount"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    got = _table.get_item(Key={"PK": pk, "SK": sk})
    cur = int((got.get("Item") or {}).get("turnCount") or daily_limit)
    return False, cur


def refund_chat_quota(user_id: str, daily_limit: int) -> None:
    """Best-effort release of a reserved ``/chat`` turn when the turn ultimately failed.

    ``consume_chat_quota`` reserves before doing model work; if that work raises we
    give the reservation back so a failed turn does not burn the user's daily budget."""
    if daily_limit <= 0:
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        _table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"USAGE#CHAT#{day}"},
            UpdateExpression="ADD turnCount :neg SET updatedAt = :now",
            ConditionExpression="attribute_exists(turnCount) AND turnCount > :zero",
            ExpressionAttributeValues={":neg": -1, ":zero": 0, ":now": _now_iso()},
        )
    except ClientError:
        # Best-effort: counter already at 0 / absent, or a transient error. The
        # daily window resets anyway, so never fail the request over a refund.
        pass
