"""API Gateway -> Lambda router for dialin.

Routes:

  POST /chat
      body: {userId, message, history?: [{role: "USER"|"BOT", text}]}
      -> calls Bedrock with tools, returns {reply, history}

  GET  /coffees?userId=&includeArchived=
  POST /coffees                           body: full coffee fields
  PATCH /coffees/{coffeeId}?userId=       body: patch fields

  GET  /brews?userId=&coffeeId=&method=&limit=
  POST /brews                             body: full brew fields

Chat is stateless from the server's POV: the client sends recent
history each turn. That keeps DynamoDB writes cheap and keeps the
backend simple. Coffees and brews are the durable data.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import bedrock
import ddb

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o) if o % 1 else int(o)
        return super().default(o)


def _response(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _qs(event: dict[str, Any]) -> dict[str, str]:
    return event.get("queryStringParameters") or {}


def _path_params(event: dict[str, Any]) -> dict[str, str]:
    return event.get("pathParameters") or {}


def _require(d: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        if not d.get(k):
            return f"missing required field: {k}"
    return None


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


_HISTORY_TURN_LIMIT = 12  # last N messages from the client (rolling window)


def _handle_chat(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = (body.get("userId") or "").strip()
    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not isinstance(history, list):
        history = []

    err = _require({"userId": user_id, "message": message}, "userId", "message")
    if err:
        return _response(400, {"error": err})

    trimmed = history[-_HISTORY_TURN_LIMIT:]

    try:
        reply = bedrock.generate_reply(user_id=user_id, history=trimmed, user_text=message)
    except Exception:  # noqa: BLE001
        logger.exception("bedrock failed")
        return _response(502, {"error": "model invocation failed"})

    new_history = trimmed + [
        {"role": "USER", "text": message},
        {"role": "BOT", "text": reply},
    ]
    return _response(200, {"reply": reply, "history": new_history})


# ---------------------------------------------------------------------------
# Coffees
# ---------------------------------------------------------------------------


def _handle_list_coffees(event: dict[str, Any]) -> dict[str, Any]:
    qs = _qs(event)
    user_id = (qs.get("userId") or "").strip()
    if not user_id:
        return _response(400, {"error": "userId is required"})
    include_archived = qs.get("includeArchived", "").lower() in {"1", "true", "yes"}
    items = ddb.list_coffees(user_id, include_archived=include_archived)
    return _response(200, {"count": len(items), "coffees": items})


def _handle_create_coffee(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    err = _require(body, "userId", "name")
    if err:
        return _response(400, {"error": err})
    if not body.get("roaster") and not body.get("roasterId"):
        return _response(400, {"error": "missing required field: roaster or roasterId"})
    try:
        item = ddb.create_coffee(
            user_id=body["userId"].strip(),
            roaster=(body.get("roaster") or "").strip(),
            name=body["name"].strip(),
            roaster_id=body.get("roasterId"),
            origin=body.get("origin"),
            process=body.get("process"),
            roast_date=body.get("roastDate"),
            weight_g=body.get("weightG"),
            notes=body.get("notes"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("create_coffee failed")
        return _response(500, {"error": "could not create coffee"})
    return _response(201, {"coffee": item})


def _handle_update_coffee(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = (body.get("userId") or _qs(event).get("userId") or "").strip()
    coffee_id = (_path_params(event).get("coffeeId") or "").strip()
    if not user_id or not coffee_id:
        return _response(400, {"error": "userId and coffeeId are required"})
    try:
        updated = ddb.update_coffee(user_id, coffee_id, body)
    except ValueError as e:
        return _response(404, {"error": str(e)})
    except Exception:  # noqa: BLE001
        logger.exception("update_coffee failed")
        return _response(500, {"error": "could not update coffee"})
    return _response(200, {"coffee": updated})


# ---------------------------------------------------------------------------
# Brews
# ---------------------------------------------------------------------------


def _handle_list_brews(event: dict[str, Any]) -> dict[str, Any]:
    qs = _qs(event)
    user_id = (qs.get("userId") or "").strip()
    if not user_id:
        return _response(400, {"error": "userId is required"})
    try:
        limit = int(qs.get("limit", "20"))
    except ValueError:
        return _response(400, {"error": "limit must be an integer"})
    items = ddb.list_brews(
        user_id,
        coffee_id=qs.get("coffeeId") or None,
        method=qs.get("method") or None,
        limit=limit,
    )
    return _response(200, {"count": len(items), "brews": items})


def _handle_create_brew(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    err = _require(body, "userId", "coffeeId", "method")
    if err:
        return _response(400, {"error": err})
    try:
        item = ddb.create_brew(
            user_id=body["userId"].strip(),
            coffee_id=body["coffeeId"].strip(),
            method=body["method"].strip(),
            dose_g=body.get("doseG"),
            yield_g=body.get("yieldG"),
            water_g=body.get("waterG"),
            grind=body.get("grind"),
            time_s=body.get("timeS"),
            temp_c=body.get("tempC"),
            rating=body.get("rating"),
            taste=body.get("taste"),
            notes=body.get("notes"),
        )
    except ValueError as e:
        return _response(400, {"error": str(e)})
    except Exception:  # noqa: BLE001
        logger.exception("create_brew failed")
        return _response(500, {"error": "could not create brew"})
    return _response(201, {"brew": item})

 
# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _handle_list_equipment(event: dict[str, Any]) -> dict[str, Any]:
    qs = _qs(event)
    user_id = (qs.get("userId") or "").strip()
    if not user_id:
        return _response(400, {"error": "userId is required"})
    include_archived = qs.get("includeArchived", "").lower() in {"1", "true", "yes"}
    items = ddb.list_equipment(
        user_id,
        equip_type=qs.get("equipType") or None,
        include_archived=include_archived,
    )
    return _response(200, {"count": len(items), "equipment": items})


def _handle_create_equipment(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    err = _require(body, "userId", "equipType", "name")
    if err:
        return _response(400, {"error": err})
    try:
        item = ddb.create_equipment(
            user_id=body["userId"].strip(),
            equip_type=body["equipType"].strip(),
            name=body["name"].strip(),
            brand=body.get("brand"),
            model=body.get("model"),
            notes=body.get("notes"),
        )
    except ValueError as e:
        return _response(400, {"error": str(e)})
    except Exception:  # noqa: BLE001
        logger.exception("create_equipment failed")
        return _response(500, {"error": "could not create equipment"})
    return _response(201, {"equipment": item})


def _handle_list_roasters(event: dict[str, Any]) -> dict[str, Any]:
    qs = _qs(event)
    user_id = (qs.get("userId") or "").strip()
    if not user_id:
        return _response(400, {"error": "userId is required"})
    include_archived = qs.get("includeArchived", "").lower() in {"1", "true", "yes"}
    items = ddb.list_roasters(user_id, include_archived=include_archived)
    return _response(200, {"count": len(items), "roasters": items})


def _handle_create_roaster(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    err = _require(body, "userId", "name")
    if err:
        return _response(400, {"error": err})
    try:
        item = ddb.create_roaster(
            user_id=body["userId"].strip(),
            name=body["name"].strip(),
            city=body.get("city"),
            country=body.get("country"),
            website=body.get("website"),
            notes=body.get("notes"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("create_roaster failed")
        return _response(500, {"error": "could not create roaster"})
    return _response(201, {"roaster": item})


def _handle_update_roaster(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = (body.get("userId") or _qs(event).get("userId") or "").strip()
    roaster_id = (_path_params(event).get("roasterId") or "").strip()
    if not user_id or not roaster_id:
        return _response(400, {"error": "userId and roasterId are required"})
    try:
        updated = ddb.update_roaster(user_id, roaster_id, body)
    except ValueError as e:
        msg = str(e)
        status = 404 if "not found" in msg else 400
        return _response(status, {"error": msg})
    except Exception:  # noqa: BLE001
        logger.exception("update_roaster failed")
        return _response(500, {"error": "could not update roaster"})
    return _response(200, {"roaster": updated})


def _handle_update_brew(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = (body.get("userId") or _qs(event).get("userId") or "").strip()
    brew_id = (_path_params(event).get("brewId") or "").strip()
    if not user_id or not brew_id:
        return _response(400, {"error": "userId and brewId are required"})
    try:
        updated = ddb.update_brew(user_id, brew_id, body)
    except ValueError as e:
        msg = str(e)
        status = 404 if "not found" in msg else 400
        return _response(status, {"error": msg})
    except Exception:  # noqa: BLE001
        logger.exception("update_brew failed")
        return _response(500, {"error": "could not update brew"})
    return _response(200, {"brew": updated})


def _handle_delete_brew(event: dict[str, Any]) -> dict[str, Any]:
    qs = _qs(event)
    user_id = (qs.get("userId") or "").strip()
    brew_id = (_path_params(event).get("brewId") or "").strip()
    if not user_id or not brew_id:
        return _response(400, {"error": "userId and brewId are required"})
    try:
        ddb.delete_brew(user_id, brew_id)
    except ValueError as e:
        return _response(404, {"error": str(e)})
    except Exception:  # noqa: BLE001
        logger.exception("delete_brew failed")
        return _response(500, {"error": "could not delete brew"})
    return _response(200, {"deleted": brew_id})


def _handle_update_equipment(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = (body.get("userId") or _qs(event).get("userId") or "").strip()
    equip_id = (_path_params(event).get("equipId") or "").strip()
    if not user_id or not equip_id:
        return _response(400, {"error": "userId and equipId are required"})
    try:
        updated = ddb.update_equipment(user_id, equip_id, body)
    except ValueError as e:
        msg = str(e)
        status = 404 if "not found" in msg else 400
        return _response(status, {"error": msg})
    except Exception:  # noqa: BLE001
        logger.exception("update_equipment failed")
        return _response(500, {"error": "could not update equipment"})
    return _response(200, {"equipment": updated})


def _handle_get_profile(event: dict[str, Any]) -> dict[str, Any]:
    qs = _qs(event)
    user_id = (qs.get("userId") or "").strip()
    if not user_id:
        return _response(400, {"error": "userId is required"})
    return _response(200, {"profile": ddb.get_profile(user_id)})


def _handle_update_profile(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = (body.get("userId") or "").strip()
    if not user_id:
        return _response(400, {"error": "userId is required"})
    updates = {k: v for k, v in body.items() if k != "userId"}
    try:
        item = ddb.update_profile(user_id, updates, replace_lists=True)
    except ValueError as e:
        return _response(400, {"error": str(e)})
    return _response(200, {"profile": item})


_ROUTES = {
    "POST /chat": _handle_chat,
    "GET /roasters": _handle_list_roasters,
    "POST /roasters": _handle_create_roaster,
    "PATCH /roasters/{roasterId}": _handle_update_roaster,
    "GET /coffees": _handle_list_coffees,
    "POST /coffees": _handle_create_coffee,
    "PATCH /coffees/{coffeeId}": _handle_update_coffee,
    "GET /brews": _handle_list_brews,
    "POST /brews": _handle_create_brew,
    "PATCH /brews/{brewId}": _handle_update_brew,
    "DELETE /brews/{brewId}": _handle_delete_brew,
    "GET /equipment": _handle_list_equipment,
    "POST /equipment": _handle_create_equipment,
    "PATCH /equipment/{equipId}": _handle_update_equipment,
    "GET /profile": _handle_get_profile,
    "PATCH /profile": _handle_update_profile,
}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    route_key = event.get("routeKey") or ""
    logger.info("route=%s rawPath=%s", route_key, event.get("rawPath"))
    handler = _ROUTES.get(route_key)
    if handler is None:
        return _response(404, {"error": f"no route for {route_key}"})
    try:
        return handler(event)
    except Exception:  # noqa: BLE001
        logger.exception("unhandled error in %s", route_key)
        return _response(500, {"error": "internal server error"})
