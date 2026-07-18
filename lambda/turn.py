"""Chat turn assembly and execution.

``_prepare_turn`` builds the per-turn system prompt + tool config, shared by
both the buffered (``_run_turn``) and streaming (``stream_turn``) tool-use
loops so the two paths cannot silently diverge on system blocks / tools.
``generate_reply`` wraps ``_run_turn`` for the plain-JSON chat handler.

Model config (``_client``, ``_MODEL_ID``, ...) and prompt text
(``_SYSTEM_PROMPT_CORE``, ...) live in ``bedrock.py`` — the top-level facade —
and are referenced here as ``bedrock.<name>`` rather than imported by name.
That indirection is deliberate: the eval harness and several tests swap
``bedrock._client`` for a fake at runtime, and a ``from bedrock import
_client`` here would capture a stale reference that never sees the swap.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterator

import bedrock
import chat_context
import prompt_router
import recommend_cafes
import tools

logger = logging.getLogger(__name__)


def _deterministic_city_scout_destination(
    user_id: str,
    history: list[dict],
    user_text: str,
    *,
    force_trip_appendix: bool | None,
) -> recommend_cafes.ParsedDestination | None:
    """Resolve a self-contained "best coffee/cafes in X" ask to a destination,
    or ``None`` if this turn should stay on the open agent + appendix path.

    See ``prompt_router.extract_open_city_scout`` for the (deliberately
    narrow) trigger shape. ``_parse_destination`` additionally rejects
    pronouns and venue-name false positives, so a matched trigger can still
    fall through here.
    """
    if force_trip_appendix is False:
        return None
    raw_city = prompt_router.extract_open_city_scout(history, user_text)
    if raw_city is None:
        return None
    try:
        return recommend_cafes._parse_destination(raw_city)
    except ValueError:
        return None


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o) if o % 1 else int(o)
        return super().default(o)


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert Decimals to plain numbers for the Bedrock payload."""
    return json.loads(json.dumps(obj, cls=_DecimalEncoder))


@dataclass
class ToolCall:
    """One model-issued tool invocation and the dispatched result."""

    name: str
    input: dict[str, Any]
    output: dict[str, Any]


@dataclass
class TurnResult:
    """Full trace of one chat turn — text plus everything the eval harness asserts on."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    iterations: int = 0
    hit_iteration_cap: bool = False
    attachments: dict[str, bool] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)

    def names(self) -> list[str]:
        return [c.name for c in self.tool_calls]


def _accumulate_usage(total: dict[str, int], response: dict[str, Any]) -> None:
    """Sum token usage (incl. cache read/write) across converse rounds."""
    u = response.get("usage") or {}
    for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"):
        if key in u and isinstance(u[key], int):
            total[key] = total.get(key, 0) + u[key]


@dataclass
class _PreparedTurn:
    """Shared per-turn setup used by both the buffered (_run_turn) and streaming
    (stream_turn) code paths, so the two never drift apart."""

    messages: list[dict[str, Any]]
    base_system: list[dict[str, Any]]
    tool_config: dict[str, Any]
    active_tools: list[dict[str, Any]]
    attach_trip_appendix: bool
    attach_youtube: bool


def _prepare_turn(
    user_id: str,
    history: list[dict],
    user_text: str,
    *,
    client_timezone: str | None,
    force_trip_appendix: bool | None,
) -> _PreparedTurn:
    messages: list[dict[str, Any]] = []
    for h in history:
        role = "user" if h.get("role") == "USER" else "assistant"
        text = h.get("text") or ""
        if text:
            messages.append({"role": role, "content": [{"text": text}]})
    messages.append({"role": "user", "content": [{"text": user_text}]})

    clock_supplement = bedrock.chat_clock_system_text(user_id, client_timezone=client_timezone)
    attach_trip_appendix = (
        force_trip_appendix
        if force_trip_appendix is not None
        else prompt_router.want_trip_place_discovery_appendix(history, user_text)
    )
    attach_youtube = prompt_router._wants_youtube(user_text)

    # Cafe/visit tools live in the core set (always available). Only the
    # trip-discovery *prompt* appendix is conditional — tool availability is not.
    active_tools = list(tools.CORE_TOOL_SPECS)
    if attach_youtube:
        active_tools.extend(tools.YOUTUBE_TOOL_SPECS)
    tool_list: list[dict[str, Any]] = list(active_tools)
    if bedrock._PROMPT_CACHING:
        # Tool specs are large and stable across iterations/turns — cache them.
        tool_list.append({"cachePoint": {"type": "default"}})
    tool_config = {"tools": tool_list}

    journal_snapshot = bedrock._journal_snapshot_text(user_id)

    base_system: list[dict[str, Any]] = [{"text": bedrock._SYSTEM_PROMPT_CORE}]
    if attach_trip_appendix:
        base_system.append({"text": bedrock._APPENDIX_TRIP_PLACE_DISCOVERY})
    if bedrock._PROMPT_CACHING:
        # This checkpoint closes out the static instructions (core prompt +
        # optional trip appendix). That text is byte-identical across turns and
        # users, so this is the checkpoint that actually earns cross-turn cache
        # hits. It MUST sit before the per-turn clock/journal blocks below —
        # those change every request (clock to the second, journal on any
        # write) and would otherwise prevent this prefix from ever matching a
        # previously cached one.
        base_system.append({"cachePoint": {"type": "default"}})
    base_system.append({"text": clock_supplement})
    base_system.append({"text": journal_snapshot})
    if bedrock._PROMPT_CACHING:
        # Trailing checkpoint: the whole system list is replayed unchanged on
        # every tool-loop iteration within this turn, so this caches the
        # dynamic clock/journal blocks for the rest of the current turn even
        # though it rarely matches across turns.
        base_system.append({"cachePoint": {"type": "default"}})

    logger.info(
        "converse_attachments trip_place_discovery_appendix=%s tools=%d blocks=%s",
        attach_trip_appendix,
        len(active_tools),
        len(base_system),
    )

    return _PreparedTurn(
        messages=messages,
        base_system=base_system,
        tool_config=tool_config,
        active_tools=active_tools,
        attach_trip_appendix=attach_trip_appendix,
        attach_youtube=attach_youtube,
    )


def _dispatch_tool_call(
    user_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    *,
    max_web_searches: int | None,
    web_search_count: int,
) -> tuple[dict[str, Any], ToolCall, int]:
    """Run one model-issued tool call, applying the search_web budget.

    Returns ``(toolResult content block, ToolCall record, updated web_search_count)``.
    Shared by ``_run_turn`` and ``stream_turn`` so budget/logging behavior can't drift.
    """
    # Log argument keys only — values can contain user content (PII).
    arg_keys = sorted(tool_input.keys()) if isinstance(tool_input, dict) else None
    logger.info("tool_use name=%s arg_keys=%s", tool_name, arg_keys)

    # Deterministic web-search budget: each Tavily call costs several seconds, so
    # an over-eager model can blow past the 30s API timeout. Once the budget is
    # spent, short-circuit further searches with a synthetic result telling the
    # model to answer with what it has.
    if tool_name == "search_web" and max_web_searches is not None and web_search_count >= max_web_searches:
        result = {
            "ok": False,
            "error": "web_search_budget_exhausted",
            "message": (
                "Web-search budget reached for this request. Do not search again; "
                "answer now using the results you already have."
            ),
        }
        call = ToolCall(
            name=tool_name,
            input=tool_input if isinstance(tool_input, dict) else {},
            output=result,
        )
        block = {
            "toolResult": {
                "toolUseId": tool_use_id,
                "content": [{"json": _to_jsonable(result)}],
                "status": "error",
            }
        }
        return block, call, web_search_count

    if tool_name == "search_web" and max_web_searches is not None:
        web_search_count += 1

    result = tools.dispatch(tool_name, user_id, tool_input)
    call = ToolCall(
        name=tool_name,
        input=tool_input if isinstance(tool_input, dict) else {},
        output=result,
    )
    block = {
        "toolResult": {
            "toolUseId": tool_use_id,
            "content": [{"json": _to_jsonable(result)}],
            "status": "success" if result.get("ok", True) else "error",
        }
    }
    return block, call, web_search_count


def _run_turn(
    user_id: str,
    history: list[dict],
    user_text: str,
    *,
    client_timezone: str | None = None,
    force_trip_appendix: bool | None = None,
    max_web_searches: int | None = None,
) -> TurnResult:
    """Run a chat turn through Bedrock with tool-use enabled, returning a full trace.

    ``generate_reply`` wraps this and returns only ``.text``. The eval harness calls
    ``_run_turn`` directly to assert on tool calls, attachments, and token usage.

    ``force_trip_appendix`` overrides the heuristic router: pass ``False`` for
    self-contained flows (e.g. bean recommendations) that mention discovery/roasters
    but must NOT inherit the trip-scouting prompt's multi-search behavior."""
    dest = _deterministic_city_scout_destination(
        user_id, history, user_text, force_trip_appendix=force_trip_appendix
    )
    if dest is not None:
        text = recommend_cafes.recommend_cafes(user_id, dest.raw)
        return TurnResult(
            text=text,
            tool_calls=[],
            iterations=0,
            hit_iteration_cap=False,
            attachments={"trip_appendix": False, "youtube": False, "deterministic_city_scout": True},
            usage={},
        )

    prep = _prepare_turn(
        user_id,
        history,
        user_text,
        client_timezone=client_timezone,
        force_trip_appendix=force_trip_appendix,
    )
    messages = prep.messages
    base_system = prep.base_system
    tool_config = prep.tool_config

    final_text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    usage_total: dict[str, int] = {}

    iterations = 0
    hit_cap = False
    web_search_count = 0
    trip_ctx_token = chat_context.trip_place_discovery_active.set(prep.attach_trip_appendix)
    try:
        for iteration in range(bedrock._MAX_TOOL_ITERATIONS):
            iterations = iteration + 1
            response = bedrock._client.converse(
                modelId=bedrock._MODEL_ID,
                system=base_system,
                messages=messages,
                toolConfig=tool_config,
                inferenceConfig={
                    "maxTokens": bedrock._MAX_OUTPUT_TOKENS,
                    "temperature": bedrock._TEMPERATURE,
                },
            )
            _accumulate_usage(usage_total, response)

            stop_reason = response.get("stopReason")
            output_message = response["output"]["message"]
            content_blocks = output_message.get("content", [])

            # Collect any text the model produced this round.
            for block in content_blocks:
                if "text" in block and block["text"]:
                    final_text_parts.append(block["text"])

            logger.info(
                "converse_round iteration=%d stop_reason=%s text_len=%d tool_blocks=%d",
                iteration,
                stop_reason,
                sum(len(b.get("text", "")) for b in content_blocks),
                sum(1 for b in content_blocks if b.get("toolUse")),
            )

            if stop_reason != "tool_use":
                break

            # The model wants to call one or more tools. Append its message,
            # run the tools, then append the toolResult blocks as the next user message.
            messages.append({"role": "assistant", "content": content_blocks})

            tool_results: list[dict[str, Any]] = []
            for block in content_blocks:
                tu = block.get("toolUse")
                if not tu:
                    continue
                tool_result_block, call, web_search_count = _dispatch_tool_call(
                    user_id,
                    tu["name"],
                    tu.get("input", {}),
                    tu["toolUseId"],
                    max_web_searches=max_web_searches,
                    web_search_count=web_search_count,
                )
                tool_calls.append(call)
                tool_results.append(tool_result_block)

            if not tool_results:
                break

            messages.append({"role": "user", "content": tool_results})
        else:
            hit_cap = True
            final_text_parts.append(
                "(Stopped after maximum tool iterations. Try rephrasing.)"
            )
    finally:
        chat_context.trip_place_discovery_active.reset(trip_ctx_token)

    text = "\n".join(bedrock._strip_meta(p) for p in final_text_parts if p.strip())
    return TurnResult(
        text=text.strip() or "(no reply)",
        tool_calls=tool_calls,
        iterations=iterations,
        hit_iteration_cap=hit_cap,
        attachments={"trip_appendix": prep.attach_trip_appendix, "youtube": prep.attach_youtube},
        usage=usage_total,
    )


# ---------------------------------------------------------------------------
# Streaming variant — same tool loop, but surfaces progress live via SSE.
# ---------------------------------------------------------------------------


class _ThinkingStreamFilter:
    """Incrementally strips ``<thinking>...</thinking>`` blocks from a stream of
    text chunks, so raw model deltas can be forwarded live to the client without
    ever leaking hidden reasoning — even when a tag is split across chunks.

    Holds back at most ``len(open_tag) - 1`` characters at a time to detect a
    tag boundary that spans two chunks; call ``flush()`` once the stream ends to
    release any remaining safely-buffered text.
    """

    _OPEN = "<thinking>"
    _CLOSE = "</thinking>"

    def __init__(self) -> None:
        self._buf = ""
        self._in_thinking = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        out: list[str] = []
        while True:
            if self._in_thinking:
                end = self._buf.lower().find(self._CLOSE)
                if end == -1:
                    return "".join(out)
                self._buf = self._buf[end + len(self._CLOSE) :]
                self._in_thinking = False
                continue
            start = self._buf.lower().find(self._OPEN)
            if start == -1:
                safe_len = max(0, len(self._buf) - (len(self._OPEN) - 1))
                out.append(self._buf[:safe_len])
                self._buf = self._buf[safe_len:]
                return "".join(out)
            out.append(self._buf[:start])
            self._buf = self._buf[start + len(self._OPEN) :]
            self._in_thinking = True

    def flush(self) -> str:
        if self._in_thinking:
            return ""
        rest, self._buf = self._buf, ""
        return rest


_TOOL_STATUS_LABELS: dict[str, str] = {
    "get_youtube_transcript": "Reading the video transcript…",
    "retrieve_journal": "Searching your brew journal…",
    "search_places": "Looking up nearby cafés…",
    "list_coffees": "Checking your journal…",
    "list_roasters": "Checking your journal…",
    "list_equipment": "Checking your journal…",
    "list_brews": "Checking your journal…",
    "list_visits": "Checking your journal…",
    "list_cafes": "Checking your journal…",
    "add_coffee": "Updating your coffees…",
    "update_coffee": "Updating your coffees…",
    "delete_coffee": "Updating your coffees…",
    "add_roaster": "Checking roasters…",
    "update_roaster": "Checking roasters…",
    "search_known_roasters": "Checking roasters…",
    "add_equipment": "Updating your gear…",
    "update_equipment": "Updating your gear…",
    "log_brew": "Logging your brew…",
    "update_brew": "Logging your brew…",
    "delete_brew": "Logging your brew…",
    "add_cafe": "Updating your cafés…",
    "update_cafe": "Updating your cafés…",
    "log_visit": "Updating your cafés…",
    "update_visit": "Updating your cafés…",
    "delete_visit": "Updating your cafés…",
    "get_dialin_advice": "Working out dial-in advice…",
    "summarize_coffee": "Summarizing this coffee…",
    "lookup_coffee_term": "Looking up that term…",
    "get_preferences": "Checking your preferences…",
    "update_preferences": "Checking your preferences…",
}


def _tool_status_label(name: str, args: dict[str, Any]) -> str:
    """Friendly one-line status shown in the UI while a tool call is in flight."""
    if name == "search_web":
        query = (args or {}).get("query") if isinstance(args, dict) else None
        return f"Searching the web for “{query}”…" if query else "Searching the web…"
    return _TOOL_STATUS_LABELS.get(name, f"Using {name}…")


@dataclass
class StreamEvent:
    """One event yielded by ``stream_turn``.

    ``type`` is one of ``"status"`` (tool call started; ``data`` is
    ``{"tool": str, "label": str}``), ``"delta"`` (a chunk of assistant text;
    ``data`` is the chunk string), or ``"done"`` (turn finished; ``data`` is the
    ``TurnResult``, exactly what ``_run_turn`` would have returned).
    """

    type: str
    data: Any


def stream_turn(
    user_id: str,
    history: list[dict],
    user_text: str,
    *,
    client_timezone: str | None = None,
    force_trip_appendix: bool | None = None,
    max_web_searches: int | None = None,
) -> Iterator[StreamEvent]:
    """Like ``_run_turn``, but yields progress live via Bedrock's ``converse_stream``.

    Every iteration streams (not just the "final" one — which round is final isn't
    known until it finishes) so any incidental preamble text is shown too. Tool
    status events fire once a tool call's input has fully streamed in, so labels
    can reference the arguments (e.g. the search query).
    """
    dest = _deterministic_city_scout_destination(
        user_id, history, user_text, force_trip_appendix=force_trip_appendix
    )
    if dest is not None:
        yield StreamEvent("status", {"tool": "_start", "label": f"scouting cafés in {dest.raw}…"})
        text = recommend_cafes.recommend_cafes(user_id, dest.raw)
        yield StreamEvent("delta", text)
        yield StreamEvent(
            "done",
            TurnResult(
                text=text,
                tool_calls=[],
                iterations=0,
                hit_iteration_cap=False,
                attachments={"trip_appendix": False, "youtube": False, "deterministic_city_scout": True},
                usage={},
            ),
        )
        return

    prep = _prepare_turn(
        user_id,
        history,
        user_text,
        client_timezone=client_timezone,
        force_trip_appendix=force_trip_appendix,
    )
    messages = prep.messages
    base_system = prep.base_system
    tool_config = prep.tool_config

    final_text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    usage_total: dict[str, int] = {}
    thinking_filter = _ThinkingStreamFilter()

    iterations = 0
    hit_cap = False
    web_search_count = 0
    trip_ctx_token = chat_context.trip_place_discovery_active.set(prep.attach_trip_appendix)
    try:
        for iteration in range(bedrock._MAX_TOOL_ITERATIONS):
            iterations = iteration + 1
            response = bedrock._client.converse_stream(
                modelId=bedrock._MODEL_ID,
                system=base_system,
                messages=messages,
                toolConfig=tool_config,
                inferenceConfig={
                    "maxTokens": bedrock._MAX_OUTPUT_TOKENS,
                    "temperature": bedrock._TEMPERATURE,
                },
            )

            blocks: dict[int, dict[str, Any]] = {}
            order: list[int] = []
            stop_reason: str | None = None

            for event in response["stream"]:
                if "contentBlockStart" in event:
                    idx = event["contentBlockStart"]["contentBlockIndex"]
                    start = event["contentBlockStart"].get("start") or {}
                    order.append(idx)
                    if "toolUse" in start:
                        tu = start["toolUse"]
                        blocks[idx] = {
                            "kind": "toolUse",
                            "toolUseId": tu["toolUseId"],
                            "name": tu["name"],
                            "input_json": "",
                        }
                    else:
                        blocks[idx] = {"kind": "text", "text": ""}
                elif "contentBlockDelta" in event:
                    idx = event["contentBlockDelta"]["contentBlockIndex"]
                    delta = event["contentBlockDelta"].get("delta") or {}
                    b = blocks.setdefault(idx, {"kind": "text", "text": ""})
                    if idx not in order:
                        order.append(idx)
                    if delta.get("text"):
                        b["text"] = b.get("text", "") + delta["text"]
                        cleaned = thinking_filter.feed(delta["text"])
                        if cleaned:
                            yield StreamEvent("delta", cleaned)
                    elif "toolUse" in delta:
                        b["input_json"] = b.get("input_json", "") + (delta["toolUse"].get("input") or "")
                elif "contentBlockStop" in event:
                    idx = event["contentBlockStop"]["contentBlockIndex"]
                    b = blocks.get(idx)
                    if b and b.get("kind") == "toolUse":
                        try:
                            b["input"] = json.loads(b.get("input_json") or "{}")
                        except json.JSONDecodeError:
                            b["input"] = {}
                        yield StreamEvent(
                            "status",
                            {"tool": b["name"], "label": _tool_status_label(b["name"], b["input"])},
                        )
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason")
                elif "metadata" in event:
                    _accumulate_usage(usage_total, {"usage": (event["metadata"] or {}).get("usage")})

            content_blocks: list[dict[str, Any]] = []
            for idx in sorted(set(order)):
                b = blocks.get(idx)
                if not b:
                    continue
                if b["kind"] == "text":
                    if b["text"]:
                        final_text_parts.append(b["text"])
                        content_blocks.append({"text": b["text"]})
                else:
                    content_blocks.append(
                        {"toolUse": {"toolUseId": b["toolUseId"], "name": b["name"], "input": b.get("input", {})}}
                    )

            logger.info(
                "converse_stream_round iteration=%d stop_reason=%s text_len=%d tool_blocks=%d",
                iteration,
                stop_reason,
                sum(len(b.get("text", "")) for b in content_blocks if "text" in b),
                sum(1 for b in content_blocks if "toolUse" in b),
            )

            if stop_reason != "tool_use":
                break

            messages.append({"role": "assistant", "content": content_blocks})

            tool_results: list[dict[str, Any]] = []
            for block in content_blocks:
                tu = block.get("toolUse")
                if not tu:
                    continue
                tool_result_block, call, web_search_count = _dispatch_tool_call(
                    user_id,
                    tu["name"],
                    tu.get("input", {}),
                    tu["toolUseId"],
                    max_web_searches=max_web_searches,
                    web_search_count=web_search_count,
                )
                tool_calls.append(call)
                tool_results.append(tool_result_block)

            if not tool_results:
                break

            messages.append({"role": "user", "content": tool_results})
        else:
            hit_cap = True
            final_text_parts.append(
                "(Stopped after maximum tool iterations. Try rephrasing.)"
            )
    finally:
        chat_context.trip_place_discovery_active.reset(trip_ctx_token)

    trailing = thinking_filter.flush()
    if trailing:
        yield StreamEvent("delta", trailing)

    text = "\n".join(bedrock._strip_meta(p) for p in final_text_parts if p.strip())
    result = TurnResult(
        text=text.strip() or "(no reply)",
        tool_calls=tool_calls,
        iterations=iterations,
        hit_iteration_cap=hit_cap,
        attachments={"trip_appendix": prep.attach_trip_appendix, "youtube": prep.attach_youtube},
        usage=usage_total,
    )
    yield StreamEvent("done", result)


def generate_reply(
    user_id: str,
    history: list[dict],
    user_text: str,
    *,
    client_timezone: str | None = None,
) -> str:
    """Run a chat turn through Bedrock with tool-use enabled; return the reply text."""
    return _run_turn(
        user_id,
        history,
        user_text,
        client_timezone=client_timezone,
        max_web_searches=bedrock._CHAT_MAX_WEB_SEARCHES if bedrock._CHAT_MAX_WEB_SEARCHES > 0 else None,
    ).text


def _converse_text(system: str, user_block: str) -> str:
    """One tool-less Converse call; used by the closed-pool 'For You' rankers."""
    response = bedrock._client.converse(
        modelId=bedrock._MODEL_ID,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user_block}]}],
        inferenceConfig={"maxTokens": bedrock._MAX_OUTPUT_TOKENS, "temperature": 0.0},
    )
    output_message = response["output"]["message"]
    parts = [b["text"] for b in output_message.get("content", []) if b.get("text")]
    text = "\n".join(bedrock._strip_meta(p) for p in parts if p.strip()).strip()
    return text or "(no reply)"


def _stream_converse_text(system: str, user_block: str) -> Iterator[StreamEvent]:
    """Stream a tool-less Converse reply as delta events, then done with the full text."""
    thinking_filter = _ThinkingStreamFilter()
    parts: list[str] = []
    response = bedrock._client.converse_stream(
        modelId=bedrock._MODEL_ID,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user_block}]}],
        inferenceConfig={"maxTokens": bedrock._MAX_OUTPUT_TOKENS, "temperature": 0.0},
    )
    for event in response["stream"]:
        if "contentBlockDelta" not in event:
            continue
        delta = event["contentBlockDelta"].get("delta") or {}
        chunk = delta.get("text") or ""
        if not chunk:
            continue
        parts.append(chunk)
        cleaned = thinking_filter.feed(chunk)
        if cleaned:
            yield StreamEvent("delta", cleaned)
    trailing = thinking_filter.flush()
    if trailing:
        yield StreamEvent("delta", trailing)
    # Join token deltas as-is (including space-only chunks). Using "\n".join(parts)
    # previously turned every Bedrock token into its own line in the done payload,
    # so the UI looked fine mid-stream then jumped to jumbled markdown on finish.
    text = bedrock._strip_meta("".join(parts)).strip() or "(no reply)"
    yield StreamEvent("done", text)
