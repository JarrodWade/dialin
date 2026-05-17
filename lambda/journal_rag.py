"""Minimal journal RAG: embed user's brew / coffee-bag / visit prose in Dynamo, retrieve by similarity.

Uses Bedrock Titan embeddings + in-Lambda cosine over bounded chunk count. Index failures never fail writes."""

from __future__ import annotations

import json
import logging
import math
import os
import struct
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

import ddb

_LOGGER = logging.getLogger(__name__)

_TABLE_NAME = os.environ.get("TABLE_NAME", "")
_EMBED_MODEL = (os.environ.get("BEDROCK_EMBEDDING_MODEL_ID") or "").strip()

_MAX_INPUT_CHARS = int(os.environ.get("JOURNAL_RAG_EMBED_INPUT_CHARS", "12000"))
_MAX_CHUNKS_SCAN = int(os.environ.get("JOURNAL_RAG_MAX_CHUNKS", "2000"))

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(_TABLE_NAME) if _TABLE_NAME else None

_bedrock: Any = None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _br() -> Any:
    global _bedrock  # noqa: PLW0603
    if _bedrock is None:
        region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
        _bedrock = boto3.client("bedrock-runtime", region_name=region)
    return _bedrock


def enabled() -> bool:
    return bool(_TABLE_NAME and _EMBED_MODEL and _table is not None)


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _num(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        q = float(v)
        return str(int(q)) if q % 1 == 0 else f"{q:.2f}".rstrip("0").rstrip(".")
    if isinstance(v, (int, float)):
        q = float(v)
        return str(int(q)) if q % 1 == 0 else f"{q:.2f}".rstrip("0").rstrip(".")
    return str(v)


def embed_text(text: str) -> list[float] | None:
    """Return embedding vector or None on failure/disabled."""
    if not enabled():
        return None
    t = _truncate(text, _MAX_INPUT_CHARS)
    if not t:
        return None
    try:
        resp = _br().invoke_model(
            modelId=_EMBED_MODEL,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({"inputText": t}),
        )
        body = json.loads(resp["body"].read())
        emb = body.get("embedding")
        if isinstance(emb, list) and emb:
            return [float(x) for x in emb]
    except Exception:  # noqa: BLE001
        _LOGGER.exception("journal_rag embed_text failed model=%s", _EMBED_MODEL)
    return None


def _pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(blob: Any) -> list[float]:
    """Handle raw bytes / Dynamo Binary / memoryview."""
    if blob is None:
        return []
    if isinstance(blob, memoryview):
        b = blob.tobytes()
    elif hasattr(blob, "value"):
        b = blob.value  # boto3 Dynamo Binary
    else:
        b = bytes(blob)
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b[: n * 4]))


def chunk_sk(kind: str, entity_id: str) -> str:
    kind_u = kind.upper()
    if kind_u not in {"BREW", "COFFEE", "VISIT"}:
        raise ValueError(f"bad chunk kind {kind!r}")
    return f"RAGCHUNK#{kind_u}#{entity_id}"


def delete_chunk(user_id: str, kind: str, entity_id: str) -> None:
    if not enabled():
        return
    try:
        _table.delete_item(  # type: ignore[union-attr]
            Key={"PK": f"USER#{user_id}", "SK": chunk_sk(kind, entity_id)},
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception("journal_rag delete_chunk")


def upsert_chunk(
    user_id: str,
    *,
    kind: str,
    entity_id: str,
    text_for_embedding: str,
    display_text: str,
    refs: dict[str, Any],
) -> None:
    """Write or overwrite one journal chunk. Empty text deletes the chunk."""
    if not enabled():
        return
    text_for_embedding = (text_for_embedding or "").strip()
    display_text = (display_text or "").strip()
    if not text_for_embedding:
        delete_chunk(user_id, kind, entity_id)
        return
    vec = embed_text(text_for_embedding)
    if not vec:
        return
    try:
        _table.put_item(  # type: ignore[union-attr]
            Item={
                "PK": f"USER#{user_id}",
                "SK": chunk_sk(kind, entity_id),
                "itemType": "JournalRAGChunk",
                "userId": user_id,
                "ragKind": kind.upper(),
                "ragEntityId": entity_id,
                "displayText": _truncate(display_text, 8000),
                "embeddingPacked": _pack_embedding(vec),
                "embeddingDim": len(vec),
                "refs": {k: v for k, v in (refs or {}).items() if v is not None},
                "updatedAt": _iso_now(),
            },
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception("journal_rag upsert_chunk")


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _list_user_chunks(user_id: str) -> tuple[list[dict[str, Any]], bool]:
    """Load RAG chunks for cosine search. Returns ``(items, truncated_at_cap)``."""
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("RAGCHUNK#"),
        "ProjectionExpression": (
            "#sk, displayText, embeddingPacked, embeddingDim, refs, ragKind, ragEntityId"
        ),
        "ExpressionAttributeNames": {"#sk": "SK"},
    }
    while True:
        resp = _table.query(**kwargs)  # type: ignore[union-attr]
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if len(items) >= _MAX_CHUNKS_SCAN:
            return items[:_MAX_CHUNKS_SCAN], True
        if not lek:
            return items, False
        kwargs["ExclusiveStartKey"] = lek


def search(user_id: str, query: str, top_k: int = 6) -> dict[str, Any]:
    """Embed query and return top_k chunks by cosine similarity."""
    if not enabled():
        return {
            "ok": False,
            "error": "Journal RAG is not configured (set BEDROCK_EMBEDDING_MODEL_ID and enable Titan in Bedrock).",
        }
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query is required"}
    qv = embed_text(q)
    if not qv:
        return {"ok": False, "error": "could not embed query"}
    items, truncated = _list_user_chunks(user_id)
    if not items:
        return {
            "ok": True,
            "query": q,
            "chunksLoaded": 0,
            "hits": [],
            "note": "No journal chunks indexed yet — log brews with taste/notes or add coffee/visit notes.",
        }
    scored: list[tuple[float, dict[str, Any]]] = []
    for it in items:
        vec = _unpack_embedding(it.get("embeddingPacked"))
        exp = int(it.get("embeddingDim") or 0)
        if exp and len(vec) != exp:
            continue
        if len(vec) != len(qv):
            continue
        scored.append((_cosine(qv, vec), it))
    scored.sort(key=lambda x: x[0], reverse=True)
    k = max(1, min(int(top_k), 12))
    hits = []
    for score, it in scored[:k]:
        hits.append(
            {
                "score": round(float(score), 4),
                "kind": it.get("ragKind"),
                "refs": it.get("refs") or {},
                "text": it.get("displayText") or "",
            }
        )
    out: dict[str, Any] = {
        "ok": True,
        "query": q,
        "chunksLoaded": len(items),
        "cappedToMaxChunks": truncated,
        "hits": hits,
    }
    if truncated:
        out["note"] = (
            f"Journal search capped at {_MAX_CHUNKS_SCAN} indexed chunks; "
            "older entries may be omitted from semantic recall."
        )
    return out


# --- sync helpers (called after successful writes) ---------------------------------


def _safe_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, Decimal):
        return _num(x) or ""
    return str(x).strip()


def sync_brew(user_id: str, brew: dict[str, Any], coffee: dict[str, Any] | None) -> None:
    bid = brew.get("brewId")
    if not bid:
        return
    c = coffee or {}
    lines = [
        "Brew journal entry.",
        f"Coffee: {_safe_str(c.get('name')) or brew.get('coffeeId')} (id {brew.get('coffeeId')}).",
        f"Roaster line: {_safe_str(c.get('roaster'))}.",
        f"Method: {_safe_str(brew.get('method'))}.",
    ]
    g = _num(brew.get("doseG"))
    y = _num(brew.get("yieldG") or brew.get("waterG"))
    if g:
        lines.append(f"Dose: {g}g.")
        if y:
            lines.append(f"Yield/water: {y}g.")
    if brew.get("ratio") is not None:
        lines.append(f"Ratio 1:{_num(brew.get('ratio'))}.")
    if brew.get("grind"):
        lines.append(f"Grind setting: {_safe_str(brew.get('grind'))}.")
    if brew.get("timeS") is not None:
        lines.append(f"Brew time: {_safe_str(brew.get('timeS'))}s.")
    if brew.get("tempC") is not None:
        lines.append(f"Temperature C: {_num(brew.get('tempC'))}.")
    if brew.get("rating") is not None:
        lines.append(f"Rating: {_safe_str(brew.get('rating'))}/10.")
    taste = _safe_str(brew.get("taste"))
    notes = _safe_str(brew.get("notes"))
    if taste:
        lines.append(f"Taste notes: {taste}")
    if notes:
        lines.append(f"Additional notes: {notes}")
    text = "\n".join(lines).strip()
    display = "\n".join(lines).strip()
    upsert_chunk(
        user_id,
        kind="BREW",
        entity_id=str(bid),
        text_for_embedding=text,
        display_text=display,
        refs={"brewId": bid, "coffeeId": brew.get("coffeeId")},
    )


def sync_coffee(user_id: str, coffee: dict[str, Any]) -> None:
    cid = coffee.get("coffeeId")
    if not cid:
        return
    if coffee.get("archived"):
        delete_chunk(user_id, "COFFEE", str(cid))
        return
    lines = [
        "Coffee bag journal.",
        f"Name: {_safe_str(coffee.get('name'))}.",
        f"Roaster: {_safe_str(coffee.get('roaster'))}.",
    ]
    if coffee.get("origin"):
        lines.append(f"Origin: {_safe_str(coffee.get('origin'))}.")
    if coffee.get("process"):
        lines.append(f"Process: {_safe_str(coffee.get('process'))}.")
    if coffee.get("roastDate"):
        lines.append(f"Roast date: {_safe_str(coffee.get('roastDate'))}.")
    n = _safe_str(coffee.get("notes"))
    if n:
        lines.append(f"Bag notes: {n}")
    text = "\n".join(lines).strip()
    if not text:
        delete_chunk(user_id, "COFFEE", str(cid))
        return
    upsert_chunk(
        user_id,
        kind="COFFEE",
        entity_id=str(cid),
        text_for_embedding=text,
        display_text=text,
        refs={"coffeeId": cid},
    )


def sync_visit(user_id: str, visit: dict[str, Any]) -> None:
    vid = visit.get("visitId")
    if not vid:
        return
    drinks = visit.get("drinks") or []
    if isinstance(drinks, list):
        drinks_s = ", ".join(str(d) for d in drinks if d)
    else:
        drinks_s = _safe_str(drinks)
    lines = [
        "Cafe visit journal.",
        f"Place: {_safe_str(visit.get('placeName'))}.",
        f"Date: {_safe_str(visit.get('visitDate'))}.",
    ]
    if visit.get("cafeId"):
        lines.append(f"cafeId: {visit.get('cafeId')}.")
    if visit.get("roasterId"):
        lines.append(f"roasterId: {visit.get('roasterId')}.")
    if drinks_s:
        lines.append(f"Drinks: {drinks_s}.")
    if visit.get("rating") is not None:
        lines.append(f"Rating: {_safe_str(visit.get('rating'))}/10.")
    n = _safe_str(visit.get("notes"))
    if n:
        lines.append(f"Visit notes: {n}")
    text = "\n".join(lines).strip()
    if not drinks_s and not n and visit.get("rating") is None:
        delete_chunk(user_id, "VISIT", str(vid))
        return
    upsert_chunk(
        user_id,
        kind="VISIT",
        entity_id=str(vid),
        text_for_embedding=text,
        display_text=text,
        refs={
            "visitId": vid,
            "cafeId": visit.get("cafeId"),
            "roasterId": visit.get("roasterId"),
        },
    )


def try_sync_brew(user_id: str, brew: dict[str, Any]) -> None:
    if not enabled():
        return
    try:
        c = ddb.get_coffee(user_id, str(brew.get("coffeeId") or ""))
        sync_brew(user_id, brew, c)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("journal_rag try_sync_brew")


def try_sync_coffee(user_id: str, coffee: dict[str, Any]) -> None:
    if not enabled():
        return
    try:
        sync_coffee(user_id, coffee)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("journal_rag try_sync_coffee")


def try_sync_visit(user_id: str, visit: dict[str, Any]) -> None:
    if not enabled():
        return
    try:
        sync_visit(user_id, visit)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("journal_rag try_sync_visit")
