"""'For You' bean recommendations — deterministic pipeline.

Prompt-only recommendations had a variance floor: the same taste graph
produced different shortlists run to run, and the free agent could wander
into parametric memory (re-recommending known names, inventing roasters,
naming producers/farms as roasters). The deterministic pipeline removes that
freedom by splitting the work into fixed server-side steps:

  1. The SERVER gathers seed roaster names from the taste graph
     (favoriteRoasters first, then logged journal roasters).
  2. The SERVER runs the "roasters like {seeds}" peer search itself — the
     exact move that produced the best hand-tested results — plus one
     community-biased follow-up. A fixed, capped number of searches.
  3. A single tool-less model call RANKS and FORMATS strictly from the
     candidate names those searches surfaced. It cannot search and is told to
     only use names present in the provided results.

Output is now a stable function of (taste graph -> search results); the model
only does selection + prose, which is what it's reliably good at.

The ranker prompt text and Bedrock client live in ``bedrock.py``, referenced
here as ``bedrock.<name>`` — see ``turn.py``'s module docstring for why.
"""

from __future__ import annotations

from typing import Iterator

import bedrock
import ddb
import tools
import turn

_SEED_ROASTER_LIMIT = 5
_FOR_YOU_MAX_SEARCHES = 2
# Community threads ("roasters like Sey?") name the actual cutting-edge peers
# (Prodigal, Tim Wendelboe, Coffee Collective, …); general web search returns
# the roasters' own shop pages and big-name SEO listicles instead. Restricting
# the peer search to Reddit is the single biggest quality lever we found.
_PEER_SEARCH_DOMAINS = ["reddit.com"]
_PEER_SEARCH_MAX_RESULTS = 8


def _gather_seed_roasters(user_id: str) -> tuple[list[str], list[str]]:
    """Return ``(seed_names, known_names)`` from the user's taste graph.

    ``known_names`` is every roaster the user already follows (favoriteRoasters
    + logged journal roasters, deduped case-insensitively, original casing) and
    becomes the exclusion list. ``seed_names`` is the leading slice of those —
    favorites first, since they're the user's stated north star — used to seed
    the ``roasters like {…}`` peer search."""
    profile = ddb.get_profile(user_id) or {}
    favorites = [
        str(x).strip() for x in (profile.get("favoriteRoasters") or []) if str(x).strip()
    ]
    logged = [
        str(r.get("name", "")).strip()
        for r in ddb.list_roasters(user_id)
        if str(r.get("name", "")).strip()
    ]

    known: list[str] = []
    seen: set[str] = set()
    for name in [*favorites, *logged]:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            known.append(name)
    return known[:_SEED_ROASTER_LIMIT], known


def _peer_search_queries(seeds: list[str]) -> list[str]:
    """Deterministic query set, capped to ``_FOR_YOU_MAX_SEARCHES``.

    Query 1 is the proven ``roasters like {my roasters}`` similarity search for
    personalized peers (tends to skew toward the user's region). Query 2 is a
    seed-independent international sweep (Europe / Nordic / Japan / Australia) so
    the International group is reliably populated even when the user's seeds skew
    local. Both run against Reddit (see ``_PEER_SEARCH_DOMAINS``)."""
    world = "best light roast coffee roasters Europe Nordic Japan Australia"
    if seeds:
        primary = "roasters like " + ", ".join(seeds)
    else:
        primary = "best modern light-roast specialty coffee roasters in the world"
    return [primary, world][:_FOR_YOU_MAX_SEARCHES]


def _run_peer_searches(user_id: str, seeds: list[str]) -> str:
    """Run the capped Reddit-scoped peer searches server-side and flatten them
    into a single text block of candidate roasters. This block is the ONLY pool
    of names the ranking model is allowed to draw from."""
    blocks: list[str] = []
    for query in _peer_search_queries(seeds):
        res = tools.dispatch(
            "search_web",
            user_id,
            {
                "query": query,
                "maxResults": _PEER_SEARCH_MAX_RESULTS,
                "includeDomains": _PEER_SEARCH_DOMAINS,
            },
        )
        if not res.get("ok"):
            blocks.append(f"Search: {query}\n(search unavailable: {res.get('error')})")
            continue
        # tools.dispatch wraps a successful payload as {"ok": True, "result": {...}}.
        payload = res.get("result") or {}
        lines = [f"Search: {query}"]
        answer = (payload.get("answer") or "").strip()
        if answer:
            lines.append(f"Summary: {answer}")
        for r in payload.get("results", []) or []:
            title = (r.get("title") or "").strip()
            snippet = (r.get("snippet") or "").strip()
            if title or snippet:
                lines.append(f"- {title}: {snippet}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks).strip()


def _format_recommendations(
    user_id: str, seeds: list[str], known: list[str], results_text: str
) -> str:
    """Single tool-less model call: rank + format strictly from ``results_text``."""
    user_block = _beans_rank_user_block(user_id, seeds, known, results_text)
    return turn._converse_text(bedrock._FOR_YOU_RANKER_SYSTEM, user_block)


def _beans_rank_user_block(
    user_id: str, seeds: list[str], known: list[str], results_text: str
) -> str:
    profile = ddb.get_profile(user_id) or {}
    ctx: list[str] = []
    if seeds:
        ctx.append("Roasters I already love (my class anchors): " + ", ".join(seeds) + ".")
    if known:
        ctx.append(
            "Roasters already in my journal/favorites — DO NOT recommend these back: "
            + ", ".join(known)
            + "."
        )
    roast = str(profile.get("preferredRoastLevel") or "").strip()
    if roast:
        ctx.append(f"My preferred roast level: {roast}.")
    disliked = [str(x).strip() for x in (profile.get("dislikedNotes") or []) if str(x).strip()]
    if disliked:
        ctx.append("Notes I dislike (avoid): " + ", ".join(disliked) + ".")
    exp = str(profile.get("experimentalPreference") or "").strip()
    if exp:
        ctx.append(f"Experimental-processing appetite: {exp}.")
    taste = "\n".join(ctx) or "No saved preferences; infer my class from the anchor roasters above."

    return (
        "MY TASTE GRAPH\n"
        + taste
        + "\n\nPEER-SEARCH RESULTS (your only candidate pool)\n"
        + (results_text or "(no results returned)")
    )


def recommend_beans(user_id: str) -> str:
    """Directional 'For You' roaster recommendations via the deterministic pipeline.

    Server gathers seed roasters from the taste graph, runs the capped peer
    searches itself, then asks the model to rank + format strictly from those
    candidates. No agent loop, so it stays well under the 30s API timeout and is
    a stable function of the user's taste graph and the live search results."""
    seeds, known = _gather_seed_roasters(user_id)
    results_text = _run_peer_searches(user_id, seeds)
    return _format_recommendations(user_id, seeds, known, results_text)


def stream_recommend_beans(user_id: str) -> Iterator[turn.StreamEvent]:
    """Same pipeline as ``recommend_beans``, with status + token streaming."""
    yield turn.StreamEvent("status", {"tool": "_start", "label": "checking your taste preferences…"})
    seeds, known = _gather_seed_roasters(user_id)
    yield turn.StreamEvent("status", {"tool": "search_web", "label": "finding peer roasters…"})
    results_text = _run_peer_searches(user_id, seeds)
    yield turn.StreamEvent("status", {"tool": "_rank", "label": "ranking picks for you…"})
    user_block = _beans_rank_user_block(user_id, seeds, known, results_text)
    yield from turn._stream_converse_text(bedrock._FOR_YOU_RANKER_SYSTEM, user_block)
