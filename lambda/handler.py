"""API Gateway -> Lambda router for dialin.

Identity: ``CLERK_JWT_ISSUER`` verifies ``Authorization: Bearer`` session JWTs via
JWKS (Clerk Frontend API URL). Legacy mode uses ``ALLOW_CLIENT_USER_ID`` and
optional ``userId`` in query/body. API Gateway JWT authorizer is not used so
tokens without ``aud`` (Clerk default) still work.

Routes (representative):

  POST /chat
      body: {message, history?: [{role: "USER"|"BOT", text}]}  (+ legacy userId)
      -> calls Bedrock with tools, returns {reply, history}

  GET  /coffees?includeArchived=   (legacy: ?userId=)
  POST /coffees                    body: full coffee fields
  PATCH /coffees/{coffeeId}        body: patch fields

  GET  /brews?coffeeId=&method=&limit=   (legacy: ?userId=)
  POST /brews                            body: full brew fields

Chat is stateless from the server's POV: the client sends recent
history each turn. That keeps DynamoDB writes cheap and keeps the
backend simple. Coffees and brews are the durable data.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any

import bedrock
import clerk_jwt
import ddb
import journal_rag


def _configure_lambda_logging(level: int = logging.INFO) -> None:
    """Ensure INFO logs reach CloudWatch.

    The runtime attaches StreamHandlers on the root logger that often remain at WARNING;
    setting only logger.setLevel(logging.INFO) is not enough for INFO lines from child loggers."""

    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        handler.setLevel(level)


_configure_lambda_logging(logging.INFO)

logger = logging.getLogger()


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


def _bearer_token(event: dict[str, Any]) -> str:
    h = event.get("headers") or {}
    raw = ""
    if isinstance(h, dict):
        raw = h.get("authorization") or h.get("Authorization") or ""
    if not isinstance(raw, str):
        return ""
    raw = raw.strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def _user_id(event: dict[str, Any]) -> str:
    """Resolve the signed-in user. Prefer API Gateway JWT (if present),
    verify ``Authorization`` against Clerk JWKS when ``CLERK_JWT_ISSUER``
    is set, else legacy ``userId`` when ``ALLOW_CLIENT_USER_ID`` is true."""
    req = event.get("requestContext") or {}
    auth = req.get("authorizer") or {}
    jwt_blob = auth.get("jwt") or {}
    claims = jwt_blob.get("claims") or {}
    sub = (claims.get("sub") or "").strip()
    if sub:
        return sub

    clerk_issuer = (os.environ.get("CLERK_JWT_ISSUER") or "").strip()
    allow_client = os.environ.get("ALLOW_CLIENT_USER_ID", "").lower() in (
        "1",
        "true",
        "yes",
    )

    if clerk_issuer and not allow_client:
        bearer = _bearer_token(event)
        if bearer:
            verified = clerk_jwt.verify_session_token(bearer, clerk_issuer)
            if verified:
                return verified
        return ""

    if allow_client:
        body = _parse_body(event)
        qs = _qs(event)
        legacy = (body.get("userId") or qs.get("userId") or "").strip()
        if legacy:
            return legacy
    return ""


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


_HISTORY_TURN_LIMIT = 12  # last N messages from the client (rolling window)


def _handle_chat(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not isinstance(history, list):
        history = []

    err = _require({"message": message}, "message")
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
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    include_archived = qs.get("includeArchived", "").lower() in {"1", "true", "yes"}
    items = ddb.list_coffees(user_id, include_archived=include_archived)
    return _response(200, {"count": len(items), "coffees": items})


def _handle_create_coffee(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    err = _require(body, "name")
    if err:
        return _response(400, {"error": err})
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    if not body.get("roaster") and not body.get("roasterId"):
        return _response(400, {"error": "missing required field: roaster or roasterId"})
    try:
        item = ddb.create_coffee(
            user_id=user_id,
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
    journal_rag.try_sync_coffee(user_id, item)
    return _response(201, {"coffee": item})


def _handle_update_coffee(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = _user_id(event)
    coffee_id = (_path_params(event).get("coffeeId") or "").strip()
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    if not coffee_id:
        return _response(400, {"error": "coffeeId is required"})
    try:
        updated = ddb.update_coffee(user_id, coffee_id, body)
    except ValueError as e:
        return _response(404, {"error": str(e)})
    except Exception:  # noqa: BLE001
        logger.exception("update_coffee failed")
        return _response(500, {"error": "could not update coffee"})
    journal_rag.try_sync_coffee(user_id, updated)
    return _response(200, {"coffee": updated})


# ---------------------------------------------------------------------------
# Brews
# ---------------------------------------------------------------------------


def _handle_list_brews(event: dict[str, Any]) -> dict[str, Any]:
    qs = _qs(event)
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
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
    err = _require(body, "coffeeId", "method")
    if err:
        return _response(400, {"error": err})
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    try:
        item = ddb.create_brew(
            user_id=user_id,
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
    journal_rag.try_sync_brew(user_id, item)
    return _response(201, {"brew": item})

 
# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _handle_list_equipment(event: dict[str, Any]) -> dict[str, Any]:
    qs = _qs(event)
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    include_archived = qs.get("includeArchived", "").lower() in {"1", "true", "yes"}
    items = ddb.list_equipment(
        user_id,
        equip_type=qs.get("equipType") or None,
        include_archived=include_archived,
    )
    return _response(200, {"count": len(items), "equipment": items})


def _handle_create_equipment(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    err = _require(body, "equipType", "name")
    if err:
        return _response(400, {"error": err})
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    try:
        item = ddb.create_equipment(
            user_id=user_id,
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
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    include_archived = qs.get("includeArchived", "").lower() in {"1", "true", "yes"}
    items = ddb.list_roasters(user_id, include_archived=include_archived)
    return _response(200, {"count": len(items), "roasters": items})


def _handle_create_roaster(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    err = _require(body, "name")
    if err:
        return _response(400, {"error": err})
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    name = body["name"].strip()
    if not body.get("skipDuplicateCheck"):
        hit = ddb.find_matching_cafe_for_new_roaster(user_id, name, body.get("city"))
        if hit:
            return _response(
                409,
                {
                    "code": "DUPLICATE_PLACE",
                    "error": (
                        f'You already have "{hit.get("name", name)}" as a cafe. '
                        "Edit that cafe and enable \"Also roasts beans\", or create a duplicate anyway."
                    ),
                    "existingType": "cafe",
                    "existingId": hit["cafeId"],
                    "existingName": hit.get("name"),
                },
            )
    try:
        item = ddb.create_roaster(
            user_id=user_id,
            name=name,
            city=body.get("city"),
            country=body.get("country"),
            website=body.get("website"),
            notes=body.get("notes"),
            has_cafe=bool(body.get("hasCafe")),
        )
    except Exception:  # noqa: BLE001
        logger.exception("create_roaster failed")
        return _response(500, {"error": "could not create roaster"})
    return _response(201, {"roaster": item})


def _handle_update_roaster(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = _user_id(event)
    roaster_id = (_path_params(event).get("roasterId") or "").strip()
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    if not roaster_id:
        return _response(400, {"error": "roasterId is required"})
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
    user_id = _user_id(event)
    brew_id = (_path_params(event).get("brewId") or "").strip()
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    if not brew_id:
        return _response(400, {"error": "brewId is required"})
    try:
        updated = ddb.update_brew(user_id, brew_id, body)
    except ValueError as e:
        msg = str(e)
        status = 404 if "not found" in msg else 400
        return _response(status, {"error": msg})
    except Exception:  # noqa: BLE001
        logger.exception("update_brew failed")
        return _response(500, {"error": "could not update brew"})
    journal_rag.try_sync_brew(user_id, updated)
    return _response(200, {"brew": updated})


def _handle_delete_brew(event: dict[str, Any]) -> dict[str, Any]:
    qs = _qs(event)
    user_id = _user_id(event)
    brew_id = (_path_params(event).get("brewId") or "").strip()
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    if not brew_id:
        return _response(400, {"error": "brewId is required"})
    try:
        ddb.delete_brew(user_id, brew_id)
    except ValueError as e:
        return _response(404, {"error": str(e)})
    except Exception:  # noqa: BLE001
        logger.exception("delete_brew failed")
        return _response(500, {"error": "could not delete brew"})
    journal_rag.delete_chunk(user_id, "BREW", str(brew_id))
    return _response(200, {"deleted": brew_id})


def _handle_update_equipment(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = _user_id(event)
    equip_id = (_path_params(event).get("equipId") or "").strip()
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    if not equip_id:
        return _response(400, {"error": "equipId is required"})
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
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    return _response(200, {"profile": ddb.get_profile(user_id)})


def _handle_update_profile(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    updates = {k: v for k, v in body.items() if k != "userId"}
    try:
        item = ddb.update_profile(user_id, updates, replace_lists=True)
    except ValueError as e:
        return _response(400, {"error": str(e)})
    return _response(200, {"profile": item})


def _handle_delete_coffee(event: dict[str, Any]) -> dict[str, Any]:
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    coffee_id = _path_params(event).get("coffeeId")
    if not coffee_id:
        return _response(400, {"error": "coffeeId required"})
    try:
        ddb.delete_coffee(user_id, coffee_id)
    except ValueError as e:
        return _response(404, {"error": str(e)})
    journal_rag.delete_chunk(user_id, "COFFEE", str(coffee_id))
    return _response(200, {"deleted": coffee_id})


def _handle_list_cafes(event: dict[str, Any]) -> dict[str, Any]:
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    city = _qs(event).get("city")
    include_archived = _qs(event).get("includeArchived", "").lower() in ("1", "true")
    items = ddb.list_cafes(user_id, city=city, include_archived=include_archived)
    return _response(200, {"cafes": items})


def _handle_create_cafe(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    name = (body.get("name") or "").strip()
    if not name:
        return _response(400, {"error": "name required"})
    if not body.get("skipDuplicateCheck"):
        cafe_dup = ddb.find_matching_existing_cafe_by_place(user_id, name, body.get("city"))
        if cafe_dup:
            return _response(
                409,
                {
                    "code": "DUPLICATE_PLACE",
                    "error": (
                        f'You already track "{cafe_dup.get("name", name)}" as a cafe. '
                        "Log visits on that cafe or edit it instead of adding twice "
                        '(or skip duplicate check to create another entry anyway).'
                    ),
                    "existingType": "cafe",
                    "existingId": cafe_dup["cafeId"],
                    "existingName": cafe_dup.get("name"),
                },
            )
        hit = ddb.find_matching_roaster_for_new_cafe(user_id, name, body.get("city"))
        if hit:
            return _response(
                409,
                {
                    "code": "DUPLICATE_PLACE",
                    "error": (
                        f'You already have "{hit.get("name", name)}" as a roaster. '
                        "Edit that roaster and enable \"Also has a cafe\", or create a duplicate anyway."
                    ),
                    "existingType": "roaster",
                    "existingId": hit["roasterId"],
                    "existingName": hit.get("name"),
                },
            )
    item = ddb.create_cafe(
        user_id=user_id,
        name=name,
        city=body.get("city"),
        country=body.get("country"),
        website=body.get("website"),
        notes=body.get("notes"),
        is_roaster=bool(body.get("isRoaster")),
    )
    return _response(201, {"cafe": item})


def _handle_update_cafe(event: dict[str, Any]) -> dict[str, Any]:
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    cafe_id = _path_params(event).get("cafeId")
    if not cafe_id:
        return _response(400, {"error": "cafeId required"})
    body = _parse_body(event)
    try:
        item = ddb.update_cafe(user_id, cafe_id, body)
    except ValueError as e:
        msg = str(e)
        status = 404 if "not found" in msg else 400
        return _response(status, {"error": msg})
    return _response(200, {"cafe": item})


def _handle_list_visits(event: dict[str, Any]) -> dict[str, Any]:
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    cafe_id = _qs(event).get("cafeId")
    limit = int(_qs(event).get("limit", "20"))
    items = ddb.list_visits(user_id, cafe_id=cafe_id, limit=limit)
    return _response(200, {"visits": items})


def _handle_create_visit(event: dict[str, Any]) -> dict[str, Any]:
    body = _parse_body(event)
    user_id = _user_id(event)
    if not user_id:
        return _response(401, {"error": "Unauthorized"})
    cafe_id    = (body.get("cafeId")    or "").strip() or None
    roaster_id = (body.get("roasterId") or "").strip() or None
    place_name = (body.get("placeName") or "").strip() or None
    if not cafe_id and not roaster_id:
        return _response(400, {"error": "cafeId or roasterId required"})
    try:
        item = ddb.log_visit(
            user_id=user_id,
            cafe_id=cafe_id,
            roaster_id=roaster_id,
            place_name=place_name,
            visit_date=body.get("visitDate"),
            drinks=body.get("drinks"),
            rating=body.get("rating"),
            notes=body.get("notes"),
        )
    except ValueError as e:
        return _response(404, {"error": str(e)})
    journal_rag.try_sync_visit(user_id, item)
    return _response(201, {"visit": item})


_ROUTES = {
    "POST /chat": _handle_chat,
    "GET /roasters": _handle_list_roasters,
    "POST /roasters": _handle_create_roaster,
    "PATCH /roasters/{roasterId}": _handle_update_roaster,
    "GET /coffees": _handle_list_coffees,
    "POST /coffees": _handle_create_coffee,
    "PATCH /coffees/{coffeeId}": _handle_update_coffee,
    "DELETE /coffees/{coffeeId}": _handle_delete_coffee,
    "GET /brews": _handle_list_brews,
    "POST /brews": _handle_create_brew,
    "PATCH /brews/{brewId}": _handle_update_brew,
    "DELETE /brews/{brewId}": _handle_delete_brew,
    "GET /equipment": _handle_list_equipment,
    "POST /equipment": _handle_create_equipment,
    "PATCH /equipment/{equipId}": _handle_update_equipment,
    "GET /cafes": _handle_list_cafes,
    "POST /cafes": _handle_create_cafe,
    "PATCH /cafes/{cafeId}": _handle_update_cafe,
    "GET /visits": _handle_list_visits,
    "POST /visits": _handle_create_visit,
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
