"""Bedrock Converse API wrapper with a tool-use loop.

The model can call any tool registered in `tools.py`. We loop until
either the model stops asking for tool calls or we hit MAX_TOOL_ITERATIONS.
"""

from __future__ import annotations

import json
import logging
import os
import re
from decimal import Decimal
from typing import Any

import boto3

import tools

# Some Nova/Claude models like to emit <thinking>...</thinking> blocks even
# when not asked to. Strip them before returning to the user.
_THINKING_RE = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL | re.IGNORECASE)


def _strip_meta(text: str) -> str:
    return _THINKING_RE.sub("", text).strip()

logger = logging.getLogger(__name__)

_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
_REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-east-1"))
_MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "400"))
_TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.3"))
_MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "5"))

_client = boto3.client("bedrock-runtime", region_name=_REGION)

_SYSTEM_PROMPT = (
    "You are dialin, a precise and friendly specialty-coffee coach. "
    "You maintain a single user's brew journal and help them dial in better cups.\n\n"
    "Capabilities (via tools):\n"
    "- roasters: add_roaster, list_roasters, update_roaster\n"
    "- coffees: add_coffee, update_coffee, list_coffees, archive_coffee\n"
    "- equipment: add_equipment, list_equipment (types: MACHINE, GRINDER, BREWER, KETTLE)\n"
    "- brews: log_brew, update_brew, delete_brew, list_brews, get_dialin_advice, summarize_coffee\n"
    "- memory: get_preferences, update_preferences (persistent across sessions)\n\n"
    "Operating rules:\n"
    "1. Always resolve names to IDs via list_* tools before referencing them. "
    "Never invent IDs (coffeeId, equipId, brewId).\n"
    "2. When the user describes a brew, call log_brew with whatever they gave; "
    "do not fabricate missing values. If they mention gear by name, look it up first.\n"
    "2b. Corrections policy. When the user says they made a mistake or wants to fix "
    "something already logged:\n"
    "  - For a brew correction: call list_brews to find the brewId, then call "
    "update_brew. NEVER log a new brew just to correct an old one.\n"
    "  - For a coffee correction: call list_coffees to find the coffeeId, then call "
    "update_coffee. NEVER add a new coffee just to correct an existing one.\n"
    "  - To remove a duplicate brew: call list_brews, confirm the right brewId with "
    "the user if ambiguous, then call delete_brew.\n"
    "2a. Roaster resolution policy. When the user mentions a roaster name (when "
    "adding a coffee or discussing a bag):\n"
    "  - Call list_roasters first.\n"
    "  - If one matches (case-insensitive substring on name/city), use its roasterId.\n"
    "  - If multiple match, ask the user which one.\n"
    "  - If none match, do NOT silently create it. Ask: 'I don't see <name> in your "
    "roasters yet, want me to add them?' Include city if the user mentioned it. "
    "Only call add_roaster after the user confirms.\n"
    "  - Roasters are canonical entities — they are the source of truth for the "
    "roaster name. Always use the stored name, not a user's shorthand.\n"
    "2b. Equipment resolution policy. When the user mentions a grinder, machine, "
    "or brewer by name in a brew description:\n"
    "  - Call list_equipment first.\n"
    "  - If exactly one item matches (case-insensitive substring match on "
    "name/brand/model), use its equipId.\n"
    "  - If multiple items match, ask the user which one.\n"
    "  - If no item matches, do NOT silently create it. Briefly ask: "
    "'I don't see a <name> in your gear yet, want me to add it?' Only call "
    "add_equipment after the user confirms.\n"
    "  - Always also pass the per-brew grind setting in the `grind` text "
    "field exactly as the user said it (e.g. '4', 'Ode 4', '30 clicks'). "
    "The equipment FK and the grind text are complementary, not duplicative.\n"
    "3. For dial-in advice: call get_dialin_advice for the relevant coffee+method, "
    "then turn the returned heuristics into ONE concrete next adjustment plus a "
    "second optional tweak. Reference specific past brews when useful.\n"
    "4. For 'what's worked best' questions about a coffee, call summarize_coffee.\n"
    "5. Use get_preferences before recommending coffees, roasters, or cafes. "
    "Call update_preferences when the user reveals a durable preference "
    "(e.g. 'I love Ethiopian naturals', 'I'm based in Brooklyn'). Do NOT store "
    "one-off opinions about a single brew.\n"
    "6. For cafe/roaster recommendations in a city, first call get_preferences. "
    "Then apply this tiered approach:\n"
    "  a) Well-known shops you are genuinely confident about (e.g. Cartel Coffee Lab "
    "in Phoenix, Stumptown in Portland, Intelligentsia in Chicago) — name them "
    "directly, briefly note what makes them relevant to the user's preferences.\n"
    "  b) Shops you're less certain about — name them with a light hedge: "
    "'I believe [shop] is in [city] — worth verifying before making the trip.'\n"
    "  c) If the user names a specific shop, do your best to confirm it: "
    "'Yes, [shop] is a well-regarded specialty shop in [city]' or 'I'm not "
    "confident about that one — if you've been, let me know and I can track it.'\n"
    "  d) If you truly have no useful knowledge of a city's scene, say so and "
    "suggest Google Maps for 'specialty coffee' or sca.coffee. Don't fabricate. "
    "NEVER invent shop names you are not confident exist.\n"
    "7. Keep replies short and direct (2-5 sentences). Plain text, no headers. "
    "Numbers like '15g -> 250g, 3:10' are great when discussing brews.\n"
    "8. Do not emit <thinking> tags."
)


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o) if o % 1 else int(o)
        return super().default(o)


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert Decimals to plain numbers for the Bedrock payload."""
    return json.loads(json.dumps(obj, cls=_DecimalEncoder))


def generate_reply(user_id: str, history: list[dict], user_text: str) -> str:
    """Run a chat turn through Bedrock with tool-use enabled."""
    messages: list[dict[str, Any]] = []
    for h in history:
        role = "user" if h.get("role") == "USER" else "assistant"
        text = h.get("text") or ""
        if text:
            messages.append({"role": role, "content": [{"text": text}]})
    messages.append({"role": "user", "content": [{"text": user_text}]})

    tool_config = {"tools": tools.TOOL_SPECS}

    final_text_parts: list[str] = []

    for iteration in range(_MAX_TOOL_ITERATIONS):
        response = _client.converse(
            modelId=_MODEL_ID,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=messages,
            toolConfig=tool_config,
            inferenceConfig={
                "maxTokens": _MAX_OUTPUT_TOKENS,
                "temperature": _TEMPERATURE,
            },
        )

        stop_reason = response.get("stopReason")
        output_message = response["output"]["message"]
        content_blocks = output_message.get("content", [])

        # Collect any text the model produced this round.
        for block in content_blocks:
            if "text" in block and block["text"]:
                final_text_parts.append(block["text"])

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
            tool_name = tu["name"]
            tool_input = tu.get("input", {})
            tool_use_id = tu["toolUseId"]
            logger.info("tool_use name=%s input=%s", tool_name, tool_input)
            result = tools.dispatch(tool_name, user_id, tool_input)
            tool_results.append({
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "content": [{"json": _to_jsonable(result)}],
                    "status": "success" if result.get("ok", True) else "error",
                }
            })

        if not tool_results:
            break

        messages.append({"role": "user", "content": tool_results})
    else:
        final_text_parts.append(
            "(Stopped after maximum tool iterations. Try rephrasing.)"
        )

    text = "\n".join(_strip_meta(p) for p in final_text_parts if p.strip())
    return text.strip() or "(no reply)"
