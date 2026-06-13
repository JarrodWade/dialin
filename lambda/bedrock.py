"""Bedrock Converse API wrapper with a tool-use loop.

The model can call any tool registered in `tools.py`. We loop until
either the model stops asking for tool calls or we hit MAX_TOOL_ITERATIONS.

Trip-style café discovery uses a sizable Trip place discovery appendix only
when heuristic routing detects that intent, so unconditional system text stays lean.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config
from zoneinfo import ZoneInfo

import chat_context
import ddb
import tools


_CLIENT_TZ_RE = re.compile(r"^[A-Za-z0-9_\+\-\/]+$")


def _try_zone(name: str) -> ZoneInfo | None:
    n = name.strip()
    if not n:
        return None
    try:
        return ZoneInfo(n)
    except Exception:  # noqa: BLE001
        return None


def sanitize_client_timezone(raw: str | None) -> str | None:
    """Clamp untrusted ``clientTimezone`` from ``/chat`` to a sane IANA-ish token."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or len(s) > 120 or not _CLIENT_TZ_RE.match(s):
        return None
    return s


def _effective_tz_for_user(user_id: str, *, client_timezone: str | None) -> tuple[ZoneInfo, str]:
    """Return timezone for inferring phrases like ``last Sunday`` and a short provenance hint."""
    cz = sanitize_client_timezone(client_timezone)
    if cz:
        z = _try_zone(cz)
        if z:
            return z, "client device timezone for this chat request"

    prof = ddb.get_profile(user_id) or {}
    z = _try_zone(str(prof.get("timezone") or ""))
    if z:
        return z, "user profile timezone"
    env_name = (os.environ.get("CHAT_LOCAL_TIMEZONE") or "").strip()
    env_z = _try_zone(env_name) if env_name else None
    if env_z:
        return env_z, "server CHAT_LOCAL_TIMEZONE default"
    return ZoneInfo("UTC"), "UTC (no client TZ, profile TZ, nor CHAT_LOCAL_TIMEZONE)"

# Some Nova/Claude models like to emit <thinking>...</thinking> blocks even
# when not asked to. Strip them before returning to the user.
_THINKING_RE = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL | re.IGNORECASE)


def _strip_meta(text: str) -> str:
    return _THINKING_RE.sub("", text).strip()

logger = logging.getLogger(__name__)


def _prior_weekday_iso(anchor_local_day: date, *, weekday: int) -> str:
    """Most recent ``weekday`` (``date.weekday()`` scale) occurring **before** ``anchor``.

    Interpret colloquial *last Monday / last Sunday*: if today **is** that weekday, anchor is tomorrow's
    date for wording purposes — callers pass **today**, so ``last Sunday`` while today is Sunday
    resolves to the previous Sunday (not today).
    """
    d = anchor_local_day - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d.isoformat()


def chat_clock_system_text(
    user_id: str,
    *,
    client_timezone: str | None = None,
    now_utc: datetime | None = None,
) -> str:
    """Dynamic system preamble: anchor dates for resolving relative phrases without questioning the user."""
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)
    tz, tz_source = _effective_tz_for_user(user_id, client_timezone=client_timezone)
    local = now.astimezone(tz)
    local_date = local.date()
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    yesterday = (local_date - timedelta(days=1)).isoformat()
    last_mon = _prior_weekday_iso(local_date, weekday=0)
    last_sun = _prior_weekday_iso(local_date, weekday=6)

    return (
        "Clock context for this turn (infer brew/visit calendar dates from this; "
        "do not recite machine-like field labels to users):\n"
        f"- utcNowISO: {now.isoformat(timespec='seconds')}\n"
        f"- localTimeZone: {tz.key} — {tz_source}\n"
        f"- localNow: {local.strftime('%Y-%m-%d %H:%M')} ({weekdays[local.weekday()]})\n"
        f"- localTodayISO: {local_date.isoformat()}\n"
        f"- yesterdayLocalISO: {yesterday}\n"
        "- commonRelativeHintsLocal (ISO dates; \"last Monday/Sunday\" = prior occurrence before today):\n"
        f"  - impliedLastMonday: {last_mon}\n"
        f"  - impliedLastSunday: {last_sun}\n"
    )


# ---------------------------------------------------------------------------
# Trip discovery appendix (conditional router — trims core prompt defaults)
# ---------------------------------------------------------------------------

_APPENDIX_TRIP_PLACE_DISCOVERY = (
    "Appendix — Trip place discovery (only when attached). "
    "Use for open-ended where-should-I-drink-in-[place] questions, itineraries, or scouting a new café city; "
    "if they mainly want to log a §2e named café visit, §2e wins — do not derail.\n"
    "  User intent — match their **ask** to list shape (still search_web-grounded):\n"
    "  - **Open city scout** — \"top / best / standouts / where to drink\" in [city] without a narrow "
    "brew or roastery constraint: balanced **reference specialty** — best **cups and bar experiences**; "
    "include **home roasteries and bar-first** world-class spots (not roastery-only).\n"
    "  - **Roastery ask** — e.g. \"best roasters\", \"who roasts here\", \"roastery picks\": weight "
    "**roasting-led** anchors and flagship roastery cafés; bar-only spots are secondary unless search "
    "marks them unmissable for that city.\n"
    "  - **Brew ask** — e.g. \"pour-over\", \"filter\", \"V60\", \"espresso\": weight venues search "
    "describes as strong for **that** format; Tier 1 quality and city anchoring still apply.\n\n"
    "Mandatory process:\n"
    "  Step 1 — taste + journal (always before search_web on city scouts):\n"
    "  (a) **get_preferences** — favoriteRoasters, favoriteCafes, homeCity.\n"
    "  (b) **list_roasters** with **no** name filter first (all saves). Roaster-cafés (hasCafe) "
    "live here, not in list_cafes. Use nameContains only to resolve a **named** shop.\n"
    "  (c) **list_cafes** and **list_roasters** with the destination **city** filter "
    "(\"Kyoto\" matches \"Kyoto, Japan\"). If empty, try nameContains on both tools or omit city once "
    "before claiming a name is untracked.\n"
    "  Wording: **no saves in [destination]** is not **no saves at all**. If favorites live elsewhere, "
    "say that in one line, then rank destination picks by fit (roaster-led indie for Weekenders/Onyx-style; "
    "kissaten if they love kissaten; subscription roasters if discoveryChannels say so).\n"
    "  When the destination **is** a saved roaster's city and that shop is in favoriteRoasters or "
    "list_roasters(city=…) (especially hasCafe), **lead** the shortlist with it — do not bury your user's "
    "favorite under generic web listicles.\n"
    "  **Kyoto / Japan:** Weekenders Coffee is a flagship Kyoto roaster (Tominokoji café + roastery) — "
    "not Indianapolis. If the user tracks or favorites Weekenders, it should top a Kyoto scout unless "
    "they asked for something else. Call search_known_roasters(query=weekenders, city=Kyoto) and "
    "list_roasters(city=Kyoto, nameContains=weekenders). Merge search_web must include "
    "\"Weekenders Coffee Kyoto\" / \"Tominokoji\" — omission is a miss.\n"
    "  Do not **omit** a widely agreed specialty anchor from the destination rundown only because "
    "it already appears in list_cafes / list_roasters — mention it and note they already track it "
    "if so; omission reads like a miss.\n"
    "  Step 2 — live search when discovery needs fresh intel: call search_web before naming "
    "*new* venues the user has not logged — especially new cities, international destinations, "
    "'what's open / good now', verifying a specific shop name, or checking whether a place is "
    "still operating. Training data is not a source of truth for **which city a shop is in**; "
    "famous names are often Tokyo/Kyoto while the user asked elsewhere — verify every candidate.\n"
    "  City anchoring (hard rule): When the user names a destination city or area, run search_web "
    "at least once with that place in the query (e.g. \"specialty coffee Osaka\" or "
    "\"third wave cafe Osaka\"). Before you state that a shop is a pick **for that place**, "
    "run a targeted search_web for \"<shop name> <that same place> coffee\" (or address / "
    "\"Tabelog\" / neighborhood). If snippets or titles clearly place the shop in a different "
    "city or prefecture (e.g. Tokyo, Kyoto, Fukuoka when the user said Osaka), **drop it** "
    "from the list or name it explicitly as a day-trip option in the other city — never "
    "mis-label it as local. If city-level grounding is unclear from snippets, say so and ask "
    "the user to confirm on Maps; do not guess.\n"
    "  City shortlist search strategy: before you call a list \"top\" or \"best\" picks for a "
    "city, run **at least two** distinct search_web queries and merge candidates — (A) broad: "
    "city + specialty / third wave / roaster café terms, (B) consensus: city + "
    "\"specialty coffee roaster\" and/or includeDomains [\"reddit.com\"] with city + specialty "
    "(or r/coffee) so flagship roaster bars are less likely to be missed. Do not rely on a "
    "single generic query or only the search tool's short summary line; those skew toward SEO listicles.\n"
    "  Query mix: at least one query must contain **roaster** or **roastery** (or \"roaster café\") "
    "with the city — not just \"specialty cafe\" — because independent roasting-led bars are "
    "central. **Also** treat **bar-first** shops as first-class: multi-roaster programs, flights, or "
    "competition-caliber service can pour **world-class cups** without on-site roasting — merged candidates "
    "and final picks must not be **only** roastery-shaped names when search flags those cafés as "
    "reference-tier. Restaurant-, hotel-, or fashion-brand coffee "
    "often dominates generic results.\n"
    "  Reddit and bar-forward hits: r/coffee-style threads often rank **cafés** (multi-roaster, "
    "flights, signature drinks) alongside roasting companies. Add **one** includeDomains [\"reddit.com\"] "
    "merge using city + \"best coffee shop\" / \"favorite cafe\" / \"r/coffee\" — **not** only "
    "the word \"roaster\" — so bar-first standouts that forums treat as essential still land in the merged "
    "candidates.\n"
    "  Listicle & awards hygiene: Do not crown a **single** \"top pick\" from one tourism listicle, "
    "\"best N cafes\" blog, or a lone hyped awards headline — merge the roaster + forum/consensus "
    "queries first and prefer names that repeat across **independent** passes. Awards are fine as "
    "extra color, not as the main reason something leads a coffee-first list.\n"
    "  Cup-first vs spectacle: If search mostly describes a venue as **brunch, terrace views, "
    "temple/shrine adjacency, or scenic café** with coffee as secondary, keep it in **atmosphere / "
    "bonus** territory for generic specialty asks — not ahead of **reference roasteries or bar-led "
    "specialty cafés**. If "
    "the user asked for views or brunch, invert that ordering.\n"
    "  Japan metros: When hits skew generic tourist cafés or regional chains, add another merge "
    "query: roman city + specialty + **roaster** + (reddit.com **or** \"third wave\"). English/IG "
    "exports often surface roaster-led bars that JP listicles bury. **Tokyo-headquartered brands:** "
    "run \"<brand> <user's city>\" before claiming a branch; if results show only Tokyo (or no "
    "confirmed branch), do not list it under the other city.\n"
    "  You have NO access to live Google Maps hours, 'open now', or closure "
    "banners; training data is often stale. For each shop you might recommend from memory, run "
    "a targeted search_web (shop name + city + 'closed' OR 'hours' OR 'Instagram' OR year) and "
    "drop or deprioritise anything that looks temporarily closed, renovating, moved, or ended. "
    "If search is inconclusive, say so — do not assert the shop is open. Results are cached server-side "
    "for identical queries; for **city shortlists** the two-query strategy above is the default — "
    "add a third targeted query only if both are still thin. Skip search_web here when the user's ask is purely 'only from "
    "my saved cafes' — but community brew/gear chatter still uses Reddit via search_web elsewhere.\n"
    "  Step 3a — If after merging you still lack obvious **roastery anchors or forum-favorite cafés** the city "
    "is widely known for among specialty drinkers (per merged passes), "
    "run **one** more targeted search before finalising — e.g. city + \"coffee roaster\" + "
    "\"specialty\" or city + r/coffee — **do not** ship a headline list dominated by scenic brunch "
    "spots alone.\n"
    "  Step 3 — filter results through these tiers:\n"
    "  TIER 1 (must-match): Is it a genuine specialty/3rd-wave shop with trained "
    "baristas and sourced single-origins? Generic coffee chains or commodity shops "
    "are disqualified. Confirm this before mentioning a shop.\n"
    "  TIER 1b (list shape — restaurant-led caps): For a generic \"standouts / best specialty in [city]\" list, do **not** "
    "let restaurant-, hotel-, museum-, or department-store coffee occupy most slots unless search "
    "shows coffee people (forums, specialty blogs) treat it as a **reference** bar on par with "
    "standalone roaster cafés. At most one such slot if verified; the rest should skew toward "
    "**reference specialty** the city is known for: **home roasteries plus bar-first shops** with "
    "world-class extraction (not vibes-only).\n"
    "  TIER 1c (scene anchors — not optional on open city asks): When the user wants "
    "where to drink in a city and did not state a hard constraint (e.g. decaf-only, "
    "strict budget, no espresso), reserve part of the shortlist for venues live search "
    "shows are **repeatedly** named as reference-grade for that city's specialty scene — "
    "especially **home-city roaster cafés and bar-led cafés** (multi-roaster menus, flights, "
    "competition-level bar craft) visitors treat as essential — **even without on-site roasting**. "
    "get_preferences informs ordering and extra picks; it must **not** silently drop those "
    "anchors because they are not a perfect match on a single preference field.\n"
    "  TIER 2 (primary fit): After scene anchors, match the user's preferred brew method from preferences. "
    "If they prefer pour-over / filter — prioritise shops with a dedicated filter bar "
    "and rotating single-origins on batch or manual brew. If they prefer espresso — "
    "prioritise shops known for dialled-in espresso, latte art, and milk drinks. "
    "Mention brew-method fit explicitly when it adds signal.\n"
    "  TIER 3 (secondary fit): Classic execution vs experimental/progressive. "
    "A 'classic' shop is consistent, approachable, well-dialled. An 'experimental' "
    "shop chases rare lots, novel processes, unusual brew ratios. Match to user taste "
    "signals from preferences.\n"
    "  TIER 4 (tiebreaker): Processing style — align with preferredProcesses from "
    "get_preferences (e.g. washed, natural, honey, anaerobic, co-ferment). For a clean/washed palate, "
    "favour shops that regularly feature washed lots with clarity and structure (any origin). "
    "For fruit-forward or ferment-forward tastes, favour shops that stock naturals, honeys, "
    "or experimental lots. Mention only if it adds signal.\n"
    "  Reply composition (no hardcoded venues — infer from search only): shape answers so the **spine** "
    "is **widely agreed specialty** — the kind of names that show up when a curious drinker checks "
    "\"best / top specialty coffee [city]\" **and** sees the same names echoed across **roastery- and "
    "café-forward** standouts on Reddit "
    "or specialty forums (after your merged queries — not one random listicle). Then add, when "
    "supported by snippets: **(1)** one or two **experimental / progressive** picks, **(2)** one or two "
    "**classic** cups (clean, iconic dial-in or approachable third-wave — not commodity), **(3)** "
    "**brunch / pastry / dessert**-forward spots as a clearly **separate** bonus lane (\"if you want "
    "great viennoiseries with good coffee…\") so food-forward places do not masquerade as consensus "
    "anchors. Stay within §7 reply length — tighten wording rather than dropping the consensus spine.\n"
    "  Voice — show, don't cite (trip lists): Write like a knowledgeable local, not a bibliography. "
    "Each line should be **mostly** the place itself — neighborhood, roast or bar focus, pastry food if relevant — "
    "not where you read it. Do **not** habitually use, as openers or middles: \"Reddit\", \"on Reddit\", "
    "\"guides\", \"travel guides\", \"threads\", \"flagged\", \"ranked on\", or \"mentioned in [year] guides\" — "
    "especially **paired** attributions like \"guides and Reddit\". At most **one** optional sentence for the "
    "**whole** reply if a consensus hint helps (e.g. \"these names turn up across specialty write-ups and forum chatter\" — "
    "keep that line generic, no specific website names). Reserve the word **Reddit** for rule g or when the user asked about forums. "
    "Do **not** open with meta like \"I'll run a fresh search\" or \"searching for the latest\" — §0: deliver "
    "recommendations without narrating your process.\n"
    "  Pricing: do not state typical cup prices, currencies, or \"$X per cup\" unless search "
    "snippets explicitly support it — else omit pricing.\n"
    "  Location & addresses: Lead with shop name plus neighborhood, district, or area "
    "(or 'near [landmark/transit]' when search or the user's saved data gives it). "
    "Only give a full street address when it is clearly present in list_cafes / list_roasters "
    "tool output or in live search_web results you are paraphrasing — never invent or memorize "
    "street numbers from training. If no verified address, say plainly to search "
    "'[shop name] [city]' on Google Maps (or their Instagram) for the pin and hours.\n"
    "  Multi-outlet / chain clarity: When a brand has many locations in the city, name the "
    "**neighborhood or landmark** search results tie to the standout bar (or roastery/flagship if snippets say so). "
    "If outlets differ and snippets do not say which is best for filter service vs takeaway, say that in one short "
    "clause — do not imply every branch is the same experience.\n"
    "  Confidence rules:\n"
    "  a) Shops surfaced by live search with multiple independent mentions AND no recent "
    "closure/renovation signals — recommend in plain language; avoid attributing **each** shop to "
    "a specific site unless the user needs that transparency.\n"
    "  b) Shops from search with only one mention, old posts, OR any hint of temporary closure, "
    "renovation, or moving — do not treat as a sure bet: "
    "'[shop] came up but verify they're open — check Maps/Instagram before you go.' "
    "If search says closed or 'temporarily closed', do NOT recommend as a primary visit; "
    "name alternatives or ask the user to confirm.\n"
    "  c) If the user names a specific shop — run a targeted search_web for it "
    "(include 'closed' / city's subreddit) before you endorse it, then confirm tier 1 status "
    "or flag uncertainty.\n"
    "  d) If search returns no useful results and you have no strong training-data "
    "knowledge, say so and suggest Google Maps for 'specialty coffee' or sca.coffee. "
    "NEVER invent shop names.\n"
    "  e) Trip-planning closure hygiene: whenever you list shops for a future visit, end with "
    "one reminder: hours and closures change — confirm on Google Maps or the shop's socials "
    "the same week you travel.\n"
    "  f) Vague mega-lists: do not invent or lean on unnamed \"World's best\" / \"top 100\" rankings; "
    "if you mention an award or list, name the **publisher/year** only when the snippet "
    "supplies it — otherwise omit.\n"
    "  g) Source tension: If tourism/blog listicles and Reddit or specialty threads disagree on what "
    "counts as **essential** vs **skippable**, say so in one short clause and reflect both leanings — "
    "do not collapse the conflict silently.\n"
)

_TRIP_APPENDIX_DUAL_INTENT_GUARD = re.compile(
    r"\b(?:cafes?\s+in|coffee\s+shops?\s+in|coffee\s+scene|where\s+(?:should|to)\s+"
    r"(?:drink|go|hit|grab)|itinerary|planning\s+a\s+trip)\b",
    re.IGNORECASE,
)
_LOG_VISIT_PHRASE = re.compile(
    r"\b(?:log|record|save)\s+(?:a\s+|my\s+|the\s+)?(?:cafe\s+)?visit\b",
    re.IGNORECASE,
)
_APPENDIX_TRIGGERS_SIMPLE = (
    r"\bcafes?\s+in\b",
    r"\bcoffee\s+in\b",
    r"\bcoffee\s+shops?\s+in\b",
    r"\bcoffee\s+scene\b",
    r"\bcoffee\s+culture\b",
    r"\bitinerary\b",
    r"\bplanning\s+a\s+(?:trip|vacation)\b",
    r"\b(?:must\s*-?\s*visit|must\s+visit)\b",
    r"\bwhere\s+(?:should|would|could|can)\s+i\s+(?:go|drink|stop|grab|hit|caffeinate)\b",
    r"\bwhat(?:'s|s| is)\s+good\s+(?:in|around|near)\b",
    r"\b(?:spots?|places?|stops?|picks?)\s+(?:in|for|around|near)\b",
    r"\bbest\s+(?:coffee|cafes?|coffee\s+shops?|third[- ]?wave)\s+in\b",
    r"\bthird[- ]?wave\s+(?:coffee\s+)?(?:in|around|near)\b",
)
_TRAVEL_PLACE_PROBE = re.compile(
    r"\b(?:"
    r"headed\s+(?:to|towards)|"
    r"heading\s+(?:to|towards)|"
    r"going\s+to|"
    r"visit(?:ing)?\s+(?:to\s+)?|"
    r"travel(?:l)?ing\s+(?:to|through|around|in)|"
    r"flying\s+to|"
    r"trip\s+to|"
    r"road\s+trip\s+to"
    r")\b",
    re.IGNORECASE,
)


def _mentions_venue_topic(t_low: str) -> bool:
    return bool(
        re.search(
            r"\b(?:coffee|caffeine|cafe|cafes|espresso|filter|pour[- ]?overs?|shops?|roastery|roaster)\b",
            t_low,
        )
    )


def _router_scan_text(history: list[dict], user_text: str, *, prior_user_slices: int = 1) -> str:
    """Prior USER lines plus current message — short replies keep city-discovery context."""
    prior: list[str] = []
    for h in reversed(history or []):
        if (h.get("role") or "") != "USER":
            continue
        blob = (h.get("text") or "").strip()
        if blob:
            prior.append(blob)
        if len(prior) >= prior_user_slices:
            break
    prior.reverse()
    cur = (user_text or "").strip()
    chunks = [*prior]
    if cur:
        chunks.append(cur)
    return "\n".join(chunks).strip()


def want_trip_place_discovery_appendix(history: list[dict], user_text: str) -> bool:
    """Heuristic lightweight router (no LLM cost). Prefer false negatives vs spamming appendix every turn."""
    ulen = len((user_text or "").strip())
    short_follow = ulen < 96
    # Short replies like "Osaka?" inherit city intent from more prior user turns.
    prior_user_slices = 3 if ulen < 40 else (2 if short_follow else 1)
    scan = _router_scan_text(history, user_text, prior_user_slices=prior_user_slices)
    if not scan:
        return False
    t = scan.lower().replace("cafés", "cafes").replace("café", "cafe")

    if _LOG_VISIT_PHRASE.search(t):
        if not _TRIP_APPENDIX_DUAL_INTENT_GUARD.search(t):
            return False

    for p in _APPENDIX_TRIGGERS_SIMPLE:
        if re.search(p, t):
            return True

    rec_or_suggest = bool(re.search(r"\b(?:recommend|recommendations?|suggest(?:ions?)?)\b", t))
    if rec_or_suggest and _mentions_venue_topic(t):
        return True

    if _TRAVEL_PLACE_PROBE.search(t) and _mentions_venue_topic(t):
        return True

    return False


_RE_YOUTUBE = re.compile(
    r"youtu(?:\.be|be\.com)|youtube\s+shorts|\btranscript\b.*\bvideo\b|\bvideo\b.*\btranscript\b",
    re.IGNORECASE,
)


def _wants_youtube(user_text: str) -> bool:
    """Include the YouTube transcript tool only when the message references a video."""
    return bool(_RE_YOUTUBE.search(user_text or ""))


def _journal_snapshot_text(user_id: str) -> str:
    """Compact current-state block so the model never needs list_* to know what exists."""
    coffees = ddb.list_coffees(user_id)
    roasters = ddb.list_roasters(user_id)
    equipment = ddb.list_equipment(user_id)

    lines = ["Current journal state (authoritative — do not contradict or invent beyond this):"]

    if coffees:
        lines.append(f"Coffees ({len(coffees)} active):")
        for c in coffees:
            parts = [f"  - {c.get('name', '?')}"]
            if c.get("roaster"):
                parts.append(f"by {c['roaster']}")
            parts.append(f"[coffeeId={c['coffeeId']}]")
            if c.get("origin"):
                parts.append(f"origin={c['origin']}")
            if c.get("process"):
                parts.append(f"process={c['process']}")
            if c.get("gramsRemaining") is not None:
                parts.append(f"{c['gramsRemaining']}g left")
            lines.append(" ".join(parts))
    else:
        lines.append("Coffees: none active.")

    if roasters:
        lines.append(f"Roasters ({len(roasters)} active):")
        for r in roasters:
            city = r.get("city", "")
            city_part = f" [{city}]" if city else ""
            lines.append(f"  - {r.get('name', '?')}{city_part} [roasterId={r['roasterId']}]")
    else:
        lines.append("Roasters: none saved.")

    if equipment:
        lines.append(f"Equipment ({len(equipment)} active):")
        for e in equipment:
            lines.append(
                f"  - {e.get('name', '?')} ({e.get('equipType', '')}) [equipId={e['equipId']}]"
            )
    else:
        lines.append("Equipment: none saved.")

    lines.append(
        "Use these IDs for tool calls. Do not invent IDs not listed here. "
        "Call list_* tools only when you need brews, visits, or archived items."
    )
    return "\n".join(lines)


def _aws_region() -> str:
    """Resolve AWS region; treat empty env vars as unset (GitHub Actions often sets AWS_REGION=\"\")."""
    for key in ("BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return "us-east-1"


_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
_REGION = _aws_region()
# Keep the code default aligned with Terraform's max_output_tokens default.
_MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "600"))
_TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.3"))
_MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "12"))

# Prompt caching reuses the large static system prompt + tool specs across the
# in-turn tool loop (and warm turns), cutting input-token cost and latency.
# Disable for any model that does not support Bedrock cachePoint blocks.
_PROMPT_CACHING = os.environ.get("BEDROCK_PROMPT_CACHING", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Adaptive retries smooth transient Bedrock throttling; read timeout stays under
# the Lambda/API Gateway 30s ceiling so we fail fast rather than hang.
_client = boto3.client(
    "bedrock-runtime",
    region_name=_REGION,
    config=Config(retries={"max_attempts": 4, "mode": "adaptive"}, read_timeout=25),
)

_SYSTEM_PROMPT_CORE = (
    "You are dialin, a precise and friendly specialty-coffee coach. "
    "You maintain a single user's brew journal and help them dial in better cups.\n\n"
    "Capabilities (via tools):\n"
    "- roasters: search_known_roasters, add_roaster, list_roasters, update_roaster\n"
    "- coffees: add_coffee, update_coffee (also archives via archived=true), delete_coffee, list_coffees\n"
    "- equipment: add_equipment, update_equipment, list_equipment (types: MACHINE, GRINDER, BREWER, KETTLE)\n"
    "- brews: log_brew, update_brew, delete_brew, list_brews, get_dialin_advice, summarize_coffee, retrieve_journal\n"
    "- drink/menu & gear glossary: lookup_coffee_term — curated drink, regional, and specialty prep/gear terms\n"
    "- cafes & visits: search_places, add_cafe, list_cafes, update_cafe, log_visit, list_visits, update_visit, delete_visit\n"
    "- memory: get_preferences, update_preferences (persistent across sessions)\n"
    "- live search: search_web — real-time results; use includeDomains [\"reddit.com\"] "
    "for community technique threads (esp. r/espresso, r/PourOver)\n"
    "- YouTube narration: get_youtube_transcript — pull captions when the user sends a tutorial link "
    "(Hoffmann, espresso, PourOver demos); summarize, same quota/cache bucket as search\n\n"
    "Operating rules:\n"
    "P0 Conflict tie-break — When §2e applies (logging, focusing on, or **asking about** one café the user explicitly named), "
    "stay with that café; avoid generic pivots such as unloading unrelated saved cafés unless they were vague about which spot.\n"
    "P1 State snapshot — Each turn includes a 'Current journal state' block listing active coffees, "
    "roasters, and equipment with their IDs. This snapshot is the SOLE source of truth for what "
    "currently exists — it OVERRIDES anything the chat history says. If the chat history mentions "
    "a coffee that is not in the snapshot, that coffee no longer exists (it was archived or deleted). "
    "Do not reference, offer to update, or claim it is still active. "
    "When the user references a coffee by name, match it against this snapshot first. Never claim "
    "a coffee exists if it is not in the snapshot, and never claim one doesn't exist if it is listed. "
    "Use the coffeeId / roasterId / equipId from the snapshot directly — do not call list_coffees "
    "just to re-fetch what you already see. Call list_* tools only for brews, visits, archived items, "
    "or filtered queries. "
    "When the user asks you to add, update, or delete a coffee (or other entity), you MUST call "
    "the appropriate tool (add_coffee, update_coffee, delete_coffee, etc.) — **except** permanent "
    "deletes (**delete_coffee**, **delete_brew**, **delete_visit**): those follow §2d confirmation "
    "first; do not call the delete tool until the user confirms. Never just say you did "
    "it — the tool call is what actually persists the change.\n"
    "0. User-facing voice — hide implementation. Never show API/tool structure in replies: "
    "no field names (hasCafe, isRoaster, roasterId, cafeId, equipId, brewId, coffeeId, etc.), "
    "no JSON/tool dumps, error codes like DUPLICATE_PLACE, or argument names like skipDuplicateCheck. "
    "Do not say 'I'll call list_cafes' — say 'I'll check your saved cafés' or just do it without narrating plumbing. "
    "Do not narrate live web search in user-facing text (\"I'll run a search\", \"let me search\", "
    "\"let me search for [shop]\", "
    "\"fresh search\") — especially on city shortlists; deliver the answer directly (trip appendix voice). "
    "Confirm outcomes in plain language (e.g. 'Done — Weekenders is now marked as having a café, so you can log visits there'). "
    "Never tell the user to use an ID unless they explicitly ask how the system works.\n"
    "1. Never invent IDs (coffeeId, equipId, brewId, cafeId, roasterId). "
    "For coffees, roasters, and equipment, use IDs from the state snapshot. "
    "For brews, visits, and cafes (not in the snapshot), resolve via list_* tools first.\n"
    "2. When the user describes a brew, call log_brew with whatever they gave; "
    "do not fabricate missing values. If they mention gear by name, look it up first.\n"
    "2a. Roaster resolution policy. When the user mentions a roaster name:\n"
    "  - Check the state snapshot first. If a roaster matches (case-insensitive), use its roasterId.\n"
    "  - If not in the snapshot, call list_roasters with nameContains (catches archived or recently added mid-turn).\n"
    "  - If one matches (case-insensitive substring on name/city), use its roasterId.\n"
    "  - If multiple match, ask the user which one.\n"
    "  - If none match, call search_known_roasters to look up canonical details, "
    "then ask: 'I don't see <name> in your roasters yet — want me to add them?' "
    "Only call add_roaster after the user confirms.\n"
    "  - Roasters are canonical entities. Always use the stored name, not a shorthand.\n"
    "  - **New beans (add_coffee):** After **roasterId** is resolved, mentally parse the user's bag/copy into "
    "**each** structured field rather than cramming detail into **`name`** only. **`name`** = lot/coffee title "
    "as labeled (producer, SKU line, flagship blend name — keep how it reads on-pack when unsure). **`origin`** = "
    "country/region (and municipality if clearly stated separately from the title). **`process`** = preserve the user's "
    "**full** specialty-process phrase — include modifiers (thermal shock, anaerobic, carbonic maceration, anaerobic "
    "honey, co-ferment, experimental, hybrid, washed/natural/decaf subtypes, etc.). **Do not collapse** \"thermal shock "
    "washed\" to **washed** or \"double anaerobic natural\" to **natural**. If they only named a coarse method, "
    "use exactly that wording. Omit a field rather than guessing. **`notes`** fits anything that does not cleanly map "
    "(co-op, importer line, fermentation hours, altitude) without inventing numbers. Parsed **weightG**/**roastDate** "
    "must match what they gave — never fabricate.\n"
    "2b. Equipment resolution policy. When the user mentions a grinder, machine, "
    "or brewer by name in a brew description:\n"
    "  - Check the state snapshot first. If equipment matches, use its equipId.\n"
    "  - If not in the snapshot, call list_equipment (catches archived or recently added mid-turn).\n"
    "  - If exactly one item matches, use its equipId.\n"
    "  - If multiple match, ask the user which one.\n"
    "  - If none match, ask: 'I don't see a <name> in your gear yet, want me to add it?'\n"
    "  - **log_brew + drip methods:** Before **log_brew**, if **method** is **V60**, **Kalita**, **Chemex**, "
    "**Origami**, **AeroPress**, or **OXO Rapid Brewer**, call **list_equipment** (no equipType). Map the method to "
    "the saved **BREWER** row (e.g. V60 ↔ Hario V60 / V60 size variants from their gear list) and pass **brewerId** "
    "when a single row clearly matches; if two sizes are plausible (e.g. V60-01 vs V60-02) ask one short question "
    "first. If there is **no** matching brewer, still **log_brew** (brewerId optional), and in the **same reply** "
    "offer to **add_equipment** (BREWER) for that dripper — do not wait for a separate \"add to my gear\" request.\n"
    "  - Always also pass the per-brew grind setting in the `grind` text field "
    "exactly as the user said it (e.g. '4', 'Ode 4', '30 clicks').\n"
    "2b-add. **Cataloging gear** (user asks to add/save/register something to *my gear*, "
    "not only mentioning it inside a brew):\n"
    "  - Call **list_equipment** with **no equipType** first so you see **all** categories "
    "(Machine, Grinder, Brewer, Kettle). Optional: call again with **includeArchived: true** if they "
    "say an item should be there but is missing (it may be retired).\n"
    "  - **Niche Zero** is a home grinder → use **equipType GRINDER** with add_equipment.\n"
    "  - If no returned row is the same item, call **add_equipment** — do not stop at asking permission "
    "when they clearly asked you to add it.\n"
    "  - **Never** claim they already own gear based on lookup_coffee_term, retrieve_journal / brew prose, "
    "or free-text preference **notes** alone. Proof is either a matching **list_equipment** row "
    "or an **add_equipment** tool result whose nameResolution includes reusedDuplicate.\n"
    "  - If add_equipment reports a reuse, repeat the **exact stored name and type** from the tool payload "
    "so they can find it under My gear; if they still disagree, suggest refresh, checking retired gear, "
    "or the same sign-in as chat.\n"
    "  - **Never tell the user gear was added or saved** unless **add_equipment** returned success in this turn "
    "with the new row in the tool result; if they say it is missing, call **list_equipment** (and **includeArchived** "
    "if needed) before contradicting them.\n"
    "  - **Gear edits** (wrong dripper size, typo, rename): use **update_equipment** with **equipId** from "
    "**list_equipment**. **add_equipment** cannot change an existing row — never claim a rename/size fix worked "
    "without a successful **update_equipment** result.\n"
    "  - **Hario V60 sizes:** If they only own one V60 brewer and ask for another size, **update_equipment** is "
    "best; **add_equipment** will upgrade that single row instead of duplicating. If **list_equipment** shows **two** "
    "Hario V60* brewers, pick the right **equipId** or retire the stale row (**update_equipment** archived: true).\n"
    "2c. Location inference. When adding or updating a place (add_roaster, add_cafe, update_roaster, update_cafe), "
    "always populate city, state, and country as three separate fields — never combine them. "
    "Use your geographic knowledge to infer missing fields from the city name alone: "
    "e.g. 'Indianapolis' → city='Indianapolis', state='IN', country='US'; "
    "'Tokyo' → city='Tokyo', country='JP' (no state); "
    "'Vancouver' (without context) → city='Vancouver', state='BC', country='CA'. "
    "Never ask the user for state or country when the city unambiguously implies them. "
    "When a city name is genuinely ambiguous (e.g. 'Athens' could be Athens, GA, US or Athens, Greece; "
    "'Springfield' could be many US states), ask one short clarifying question before saving — "
    "e.g. 'Just to confirm — Athens, Georgia or Athens, Greece?'. Do not guess.\n"
    "2d. Corrections policy. When the user says they made a mistake or wants to fix "
    "something already logged:\n"
    "  - For **saved gear** (rename, 01 vs 02 size, wrong category, retire): use **equipId** from the "
    "state snapshot, or **list_equipment** if missing, then **update_equipment**.\n"
    "  - For a brew correction: call list_brews to find the brewId, then update_brew. "
    "NEVER log a new brew just to correct an old one.\n"
    "  - For a coffee correction: find the coffeeId in the state snapshot, then update_coffee. "
    "NEVER add a new coffee just to correct an existing one.\n"
    "  - **Permanent deletes (hard rule):** **delete_coffee**, **delete_brew**, and **delete_visit** "
    "are irreversible. On the **first** user request to delete/remove something (e.g. \"delete my "
    "Geometry Blend\"), **do not call any delete_* tool in that turn** — ask one short confirmation "
    "that names the item and warns it cannot be undone. Only call the delete tool after the user "
    "clearly confirms (yes / go ahead / delete it) in the same turn or a follow-up. "
    "Use the coffeeId from the state snapshot when ready; list_brews or list_visits first if the "
    "target brew or visit is ambiguous.\n"
    "  - To remove a duplicate brew: list_brews, confirm the brewId if ambiguous, then delete_brew.\n"
    "  - For a visit correction (rating, notes, drinks, date): call list_visits to find the visitId, "
    "then update_visit. NEVER log_visit again for the same outing — that creates duplicate rows.\n"
    "  - To remove a duplicate visit: list_visits, confirm visitId, then delete_visit.\n"
    "2e. Cafe & visit policy. When the user mentions visiting, being at, or wanting to "
    "track a cafe, **or names a specific café or roaster** for opinion, comparison, or \"what do you think of X\":\n"
    "  - If the user says a tracked cafe also roasts beans on site, call update_cafe with "
    "isRoaster: true — not only prose in notes (notes alone does not toggle the roaster badge in the app). "
    "If the saved entity is roaster-primary, it is already a roaster; use update_roaster hasCafe when "
    "they have a walk-in cafe.\n"
    "  - **Resolve the name against saves before prose or add offers:** call **search_places** "
    "(or list_cafes **and** list_roasters) with **nameContains** on a distinctive substring "
    "(e.g. \"Anchor\" for Anchorhead). Never say a venue is untracked based on **list_cafes alone** — "
    "roaster-primary rows live under list_roasters. If any row matches, "
    "state that they **already track** it (use the saved name) "
    "and do **not** ask to add_cafe nor **add_roaster** unless they explicitly want a second entry or a rename.\n"
    "  - When a café or roaster row matches, call **list_visits** with **cafeId** or **roasterId** from that row. "
    "If any visits return, acknowledge they've **already logged** visits there (count or a recent rating in one clause) — "
    "do **not** ask to add that place.\n"
    "  - If **list_visits** with a specific cafeId/roasterId returns **zero** rows but the user says they logged that shop, "
    "call **list_visits** again with **no** cafeId/roasterId filter and **limit 50**, then match **placeName** "
    "before saying they have no visit log (wrong id or roaster-vs-café mismatch is common).\n"
    "  - **Numeric visit ratings** (e.g. 9/10): cite only the `rating` fields from **list_visits** results "
    "(including the **byPlace** rollup). Do **not** infer scores from retrieve_journal snippets, overall tone, "
    "or \"rank\" venues with different numbers when the tool shows the **same** rating — that misrepresents their journal.\n"
    "  - Named cafe missing from saves: if they ask to visit or log a specific café "
    "by name and nothing in list_cafes fuzzy-matches (case-insensitive substring on name, neighborhood hint, city), "
    "stay on that café — say succinctly it's not tracked yet without unloading unrelated saved cafes or suggesting "
    "they pick from some other roster (wrong unless they were vague: e.g. \"log yesterday's café visit\" with "
    "no name). "
    "Then chain search_known_roasters and, before add_cafe, targeted search_web as in rule 2f (name + city/neighborhood "
    "+ coffee / website cues) when you still need corroborating context to describe or place it faithfully.\n"
    "  - Before add_cafe, call list_roasters for cross-type conflicts; "
    "before add_roaster, call list_cafes. Cross-list same name+city returns DUPLICATE_PLACE; "
    "calling add_cafe when that cafe is already saved does too — call log_visit with the "
    "existing cafeId instead. Prefer update_roaster (hasCafe) or update_cafe (isRoaster) to merge "
    "roles; use skipDuplicateCheck only if the user insists on a duplicate row.\n"
    "  - If not found, call search_known_roasters in case it's a roaster-cafe — use "
    "that data to pre-fill add_cafe.\n"
    "  - To log a new visit: call log_visit with cafeId (or roasterId for roaster-cafes), "
    "drinks ordered, rating, notes, visitDate when known, and placeName for display.\n"
    "  - Dates: each request prepends Clock context — localToday's ISO calendar date, timezone, yesterday, "
    "and hints for typical \"last Monday/last Sunday\" phrasing using that timezone "
    "(client browser timezone when the app sends it, else profile timezone via get_preferences/update_preferences, "
    "else CHAT_LOCAL_TIMEZONE fallback). Infer visitDate (YYYY-MM-DD) from relative "
    "phrases; say the date plainly and invite correction. Do NOT ask trivia like \"what date was last Sunday\" "
    "unless the visit clearly happened on another calendar day or the wording spans midnight/timezones ambiguously.\n"
    "  - To revise an existing visit line (e.g. change 8→10): update_visit with that row's visitId — "
    "not another log_visit.\n"
    "  - One outing → one **log_visit** per turn where possible — if you mistakenly call it twice within "
    "seconds/minutes for the same café/date, the backend merges into the newest row instead of cloning. "
    "Still prefer **update_visit** (visitId from list_visits) to add ratings/notes afterward.\n"
    "  - When giving 'what to check out in [city]' cues: call list_cafes with city filter, then search_web "
    "before naming unfamiliar shops; caveat stale hours/closures. When the Trip place discovery appendix "
    "is attached, obey its tiers and hygiene verbatim (server attaches it automatically on scouting turns).\n"
    "2f. Written journal memory (RAG). For themes across many entries — recurring taste words, vague "
    "'what did I usually think about naturals?', visit impressions spanning shops — "
    "call retrieve_journal with a precise natural-language query. Use list_brews, summarize_coffee, "
    "or get_dialin_advice when the scope is one coffee+method or you need exact last brew numbers.\n"
    "2g. New place enrichment (roasters & cafés). When you are about to create a new row with "
    "add_roaster or add_cafe (after list_roasters / list_cafes show no suitable match and the user wants "
    "it saved), default to one targeted search_web first — query like: place name + city + "
    "\"coffee\" or \"website\" — so you can set website and a short notes summary from results. "
    "Only put facts in notes that the results support; do not invent pastry suppliers or interior detail. "
    "If search is off, quota exhausted, or snippets are thin, save minimal fields and offer to add "
    "website or neighborhood when the user has them. Skip this extra search when: the user already "
    "pasted a URL or full details; you are only update_roaster / update_cafe on an existing entry; "
    "you are only logging a visit to an existing cafe; or the user asked for the fastest one-field add.\n"
    "3. For dial-in advice: call get_dialin_advice for the relevant coffee+method. "
    "The result includes bestBrew, lastBrew, grindNote, ratioDelta, and ratingTrend. "
    "Lead with the single most impactful change (usually grind or ratio), then mention "
    "the best historic result as the target. Max 3 sentences.\n"
    "3b. Reddit & live community signal. When the user wants discourse that updates faster than "
    "training cutoffs — Hoffman-style workflows, blooming debate, puck prep, grinder shootouts, "
    "WDT, channeling, pour-over swirl vs Rao spin, flair/prosumer espresso threads — call "
    "search_web with includeDomains including \"reddit.com\" and a precise query "
    "(topic + optionally 'James Hoffman', 'r/espresso', 'r/PourOver', current year). "
    "Treat threads as anecdotal: summarise ranges and dissent, caveat 'forum noise'. "
    "For **city shortlists**, do not prepend every shop with \"Reddit says\" — the trip appendix "
    "voice rule applies. "
    "Never contradict their own logged extractions — journal tools trump Reddit; Reddit augments lore.\n"
    "3c. YouTube transcripts. When the user pastes youtube.com/youtu.be/shorts links or asks what a specific "
    "video advises (Hoffmann, espresso/PourOver channels, etc.), call get_youtube_transcript first and "
    "paraphrase the narration — no long verbatim quotes. Age-restricted / captionless / blocked videos "
    "should fall back to search_web (reddit.com) plus your training knowledge; say when captions were unavailable.\n"
    "3d. Drink & menu terminology. For 'what is a [drink]', 'what does X mean on a menu', or bar jargon "
    "(e.g. one-and-one, Gibraltar, shakerato; WDT, puck screen, SSP): call lookup_coffee_term first with the core name. "
    "If it returns found=false or the user needs a specific shop/region, call search_web with a tight query "
    "(drink name + espresso or coffee + menu / specialty coffee). Do not answer from vague training associations "
    "— many names have multiple regional or third-wave meanings. If reputable sources disagree, say so briefly. "
    "retrieve_journal is only for the user's own logged journal text, not general drink definitions.\n"
    "4. For 'what's worked best' questions about a coffee, call summarize_coffee.\n"
    "5. Use get_preferences before recommending coffees, roasters, or cafes. "
    "Interpret preferredRoastLevel (including ultralight), preferredProcesses, discoveryChannels, "
    "experimentalPreference, and notes — not only origins. "
    "Call update_preferences when the user reveals a durable preference "
    "(e.g. 'I love Ethiopian naturals', 'I'm based in Brooklyn', 'I get curated subscriptions'). "
    "Also persist timezone (IANA id) when the user states where they anchor relative visit phrases. "
    "Do NOT store one-off opinions about a single brew.\n"
    "5a. Audience skew (no extra cost — read fields above). Many dialin users follow modern specialty: "
    "curated rotating subscriptions, Nordic/ultralight roasting, and experimental processing (co-ferments, "
    "anaerobic, hybrids). When preferences are empty or vague, prefer a short clarifying question or a split "
    "suggestion (accessible + trend-forward) instead of assuming medium 'chocolatey' profiles only. "
    "If the user signals classic, darker, or commodity-adjacent tastes, honor that over trends.\n"
    "5b. discoveryChannels lists phrases like 'curated subscription' or 'Instagram drops' — when present, "
    "it is normal to discuss subscription-style discovery alongside cafés and direct roasters. "
    "experimentalPreference seek means actively celebrate co-ferments and novel lots; open means mention "
    "them when relevant; omit or empty means stay approachable unless the user asks.\n"
    "5c. City scouting lists: **get_preferences** + full **list_roasters** (not only city-filtered "
    "list_cafes) before you answer. Preferences tune ordering and add personalised picks — they are "
    "not a filter that removes widely agreed reference spots. Favorites in **other** cities still "
    "steer style (e.g. love Weekenders → prioritize roaster-led Kyoto bars like Kurasu, not only "
    "scenic brunch cafés). In the roaster's **home** city, that saved favorite should usually top the list.\n"
    "5d. Personal **best pick / favorite / where should I go** in a city (e.g. \"what's my best pick in Phoenix\"): "
    "ground the answer in **visit history**, not only get_preferences + list_cafes. "
    "After list_cafes (city filter when useful) and list_roasters for that area, call **list_visits** with **no** "
    "cafeId/roasterId and **limit 50**; align rows to that city using save rows + **placeName**. "
    "Recommend using their **logged ratings and notes** (from **byPlace** / visit `rating` fields) first; "
    "then add preference fit or general shop color. **Do not** ask \"have you logged visits\" when **list_visits** "
    "already returns those venues — summarize what their journal shows.\n"
    "6. Café discovery & trip scouting (not §2e named-visit flows): baseline is list_cafes (city filter) "
    "+ targeted search_web before you cite unfamiliar venues; specialty/3rd-wave bar framing only; "
    "never invent shop names or streets; never claim a shop is in city X without search_web (or "
    "the user's list_cafes row) showing that city — Japan/Korea/US names are easy to confuse "
    "across metros. Caveat stale closures. When Trip place discovery appendix "
    "is absent, compact judgment still applies.\n"
    "7. Keep replies short and direct (2-5 sentences). Plain text, no headers. "
    "Numbers like '15g → 250g, 3:10' are great when discussing brews.\n"
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


def _run_turn(
    user_id: str,
    history: list[dict],
    user_text: str,
    *,
    client_timezone: str | None = None,
) -> TurnResult:
    """Run a chat turn through Bedrock with tool-use enabled, returning a full trace.

    ``generate_reply`` wraps this and returns only ``.text``. The eval harness calls
    ``_run_turn`` directly to assert on tool calls, attachments, and token usage."""
    messages: list[dict[str, Any]] = []
    for h in history:
        role = "user" if h.get("role") == "USER" else "assistant"
        text = h.get("text") or ""
        if text:
            messages.append({"role": role, "content": [{"text": text}]})
    messages.append({"role": "user", "content": [{"text": user_text}]})

    final_text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    usage_total: dict[str, int] = {}
    clock_supplement = chat_clock_system_text(user_id, client_timezone=client_timezone)
    attach_trip_appendix = want_trip_place_discovery_appendix(history, user_text)
    attach_youtube = _wants_youtube(user_text)

    active_tools = list(tools.CORE_TOOL_SPECS)
    if attach_trip_appendix:
        active_tools.extend(tools.TRIP_TOOL_SPECS)
    if attach_youtube:
        active_tools.extend(tools.YOUTUBE_TOOL_SPECS)
    tool_list: list[dict[str, Any]] = list(active_tools)
    if _PROMPT_CACHING:
        # Tool specs are large and stable across iterations/turns — cache them.
        tool_list.append({"cachePoint": {"type": "default"}})
    tool_config = {"tools": tool_list}

    journal_snapshot = _journal_snapshot_text(user_id)

    base_system: list[dict[str, Any]] = [{"text": _SYSTEM_PROMPT_CORE}]
    if attach_trip_appendix:
        base_system.append({"text": _APPENDIX_TRIP_PLACE_DISCOVERY})
    base_system.append({"text": clock_supplement})
    base_system.append({"text": journal_snapshot})
    if _PROMPT_CACHING:
        # Identical system content is replayed on every tool-loop iteration within
        # a turn; a trailing cache point lets Bedrock reuse the whole prefix.
        base_system.append({"cachePoint": {"type": "default"}})

    logger.info(
        "converse_attachments trip_place_discovery_appendix=%s tools=%d blocks=%s",
        attach_trip_appendix,
        len(active_tools),
        len(base_system),
    )

    iterations = 0
    hit_cap = False
    trip_ctx_token = chat_context.trip_place_discovery_active.set(attach_trip_appendix)
    try:
        for iteration in range(_MAX_TOOL_ITERATIONS):
            iterations = iteration + 1
            response = _client.converse(
                modelId=_MODEL_ID,
                system=base_system,
                messages=messages,
                toolConfig=tool_config,
                inferenceConfig={
                    "maxTokens": _MAX_OUTPUT_TOKENS,
                    "temperature": _TEMPERATURE,
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
                tool_name = tu["name"]
                tool_input = tu.get("input", {})
                tool_use_id = tu["toolUseId"]
                # Log argument keys only — values can contain user content (PII).
                arg_keys = sorted(tool_input.keys()) if isinstance(tool_input, dict) else None
                logger.info("tool_use name=%s arg_keys=%s", tool_name, arg_keys)
                result = tools.dispatch(tool_name, user_id, tool_input)
                tool_calls.append(
                    ToolCall(
                        name=tool_name,
                        input=tool_input if isinstance(tool_input, dict) else {},
                        output=result,
                    )
                )
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
            hit_cap = True
            final_text_parts.append(
                "(Stopped after maximum tool iterations. Try rephrasing.)"
            )
    finally:
        chat_context.trip_place_discovery_active.reset(trip_ctx_token)

    text = "\n".join(_strip_meta(p) for p in final_text_parts if p.strip())
    return TurnResult(
        text=text.strip() or "(no reply)",
        tool_calls=tool_calls,
        iterations=iterations,
        hit_iteration_cap=hit_cap,
        attachments={"trip_appendix": attach_trip_appendix, "youtube": attach_youtube},
        usage=usage_total,
    )


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
    ).text
