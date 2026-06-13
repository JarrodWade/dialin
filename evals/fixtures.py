"""Canned stand-ins for external-IO tools used during live evals.

A *live* eval calls the real Bedrock model, but the model's tool calls must stay
deterministic and free where they would otherwise hit third-party APIs:

  * ``search_web``            -> Tavily (costs quota, non-reproducible)
  * ``get_youtube_transcript`` -> YouTube (IP-blocked from datacenters, flaky)
  * ``retrieve_journal``      -> Bedrock Titan embeddings (extra model access)

``install`` swaps these three entries in ``tools._TOOL_FUNCS`` for canned
versions and returns a restore callable. Everything else (DynamoDB-backed tools)
runs for real against the seeded scratch table, so structural assertions about
*which* tools the model picks and *what args* it passes stay faithful.

The canned payloads are intentionally generic but realistic: the eval harness
asserts on the *call* (name, args, order), not on the canned result text, so the
content only needs to be coherent enough that the model does not loop or stall.
"""

from __future__ import annotations

from typing import Any, Callable

# query substring (lowercased) -> list of (title, snippet) result rows.
# A match means "the model searched for this city/topic"; content is flavour.
_SEARCH_CANNED: dict[str, list[tuple[str, str]]] = {
    "osaka": [
        ("Mel Coffee Roasters — Osaka specialty", "Nishi-ku roaster cafe, single-origin filter and espresso."),
        ("LiLo Coffee Roasters, Amerikamura", "Bar-forward multi-roaster flights in central Osaka."),
        ("Takamura Coffee Roasters", "Warehouse roastery cafe, rotating single origins, Osaka."),
    ],
    "kyoto": [
        ("Weekenders Coffee Tominokoji", "Flagship Kyoto roaster cafe, hidden courtyard near Sanjo."),
        ("Kurasu Kyoto", "Filter-focused specialty bar near Kyoto Station."),
        ("% Arabica Arashiyama", "Riverside espresso and latte bar, Kyoto."),
    ],
    "portland": [
        ("Coava Coffee Roasters", "Industrial filter bar, Portland OR single origins."),
        ("Heart Coffee Roasters", "Nordic-leaning roaster cafe, Portland."),
        ("Proud Mary Coffee", "Australian-style brunch + coffee, Portland."),
    ],
    "seattle": [
        ("Anchorhead Coffee", "Downtown Seattle roaster cafe."),
        ("Victrola Coffee Roasters", "Capitol Hill specialty roaster, Seattle."),
        ("Slate Coffee Roasters", "Tasting-flight focused bar, Seattle."),
    ],
    "phoenix": [
        ("Cartel Coffee Lab", "Tempe/Phoenix specialty roaster with filter bar."),
        ("Press Coffee Roasters", "Multiple Phoenix locations, dialled espresso."),
        ("Futuro Coffee", "Small-batch Phoenix roaster, single origins."),
    ],
    "reddit": [
        ("r/PourOver — V60 bloom and swirl thread", "Community consensus leans 30-45s bloom, gentle swirl."),
        ("r/espresso — puck prep / WDT", "WDT + puck screen widely reported to cut channeling."),
    ],
}

_SEARCH_DEFAULT = [
    ("Specialty coffee guide", "A roundup of well-regarded specialty roasters and bars."),
    ("Local third-wave favorites", "Independent roaster-led cafes and bar-forward spots."),
]


def fake_search_web(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    _ = user_id
    query = str(args.get("query") or "").strip()
    q_low = query.lower()
    max_results = int(args.get("maxResults", 5) or 5)

    rows: list[tuple[str, str]] = []
    for key, canned in _SEARCH_CANNED.items():
        if key in q_low:
            rows.extend(canned)
    if not rows:
        rows = list(_SEARCH_DEFAULT)

    results = [
        {
            "title": title,
            "url": f"https://example.test/{i}",
            "snippet": snippet,
            "score": round(0.9 - i * 0.05, 2),
        }
        for i, (title, snippet) in enumerate(rows[:max_results])
    ]
    return {
        "query": query,
        "answer": f"Canned eval results for: {query}",
        "results": results,
        "_cache": {"hit": False, "stub": True},
    }


def fake_youtube_transcript(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    _ = user_id
    vid = str(
        args.get("video") or args.get("videoUrl") or args.get("videoId") or args.get("url") or ""
    ).strip()
    return {
        "videoId": vid[:11] or "stub0000000",
        "languageCode": "en",
        "language": "English (stub)",
        "isGenerated": True,
        "charLength": 240,
        "truncated": False,
        "text": (
            "In this canned eval transcript the presenter recommends a 1:2 ratio, "
            "a 30-second bloom, and grinding finer if the cup tastes sour."
        ),
        "note": "Canned transcript for eval; summarize conversationally.",
        "_cache": {"hit": False, "stub": True},
    }


def fake_retrieve_journal(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    _ = user_id
    query = str(args.get("query") or "").strip()
    return {
        "ok": True,
        "query": query,
        "count": 3,
        "snippets": [
            {"kind": "BREW", "text": "V60 Ethiopia Guji — tasted floral, bright, a touch sour at 14:1."},
            {"kind": "BREW", "text": "Espresso El Paraiso — syrupy, red-fruit, balanced at 1:2."},
            {"kind": "VISIT", "text": "Coava — clean filter flight, rated 9/10, loved the Kenyan."},
        ],
        "note": "Canned journal snippets for eval; do not invent numbers beyond these.",
        "_cache": {"stub": True},
    }


_STUBS: dict[str, Callable[[str, dict[str, Any]], Any]] = {
    "search_web": fake_search_web,
    "get_youtube_transcript": fake_youtube_transcript,
    "retrieve_journal": fake_retrieve_journal,
}


def install(tools_module: Any) -> Callable[[], None]:
    """Swap external-IO tool implementations for canned stubs.

    Returns a callable that restores the originals.
    """
    original = {name: tools_module._TOOL_FUNCS[name] for name in _STUBS if name in tools_module._TOOL_FUNCS}
    tools_module._TOOL_FUNCS.update(_STUBS)

    def restore() -> None:
        tools_module._TOOL_FUNCS.update(original)

    return restore
