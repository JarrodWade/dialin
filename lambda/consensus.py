"""Deterministic text-mining over café search-result snippets.

Surfaces venue names that are repeated across distinct Reddit threads
(consensus), explicitly praised, or already among the user's favorites — all
without another model call. Feeds the closed-pool café ranker in
``recommend_cafes.py`` with structured signal instead of raw search text alone.
"""

from __future__ import annotations

import re

_CONSENSUS_SKIP = frozenset({
    "reddit", "coffee", "cafe", "cafes", "shop", "shops", "roaster", "roasters",
    "san francisco", "sf", "the bay", "bay area", "third wave", "specialty coffee",
    "so much good coffee in the bay", "lots of good suggestions already",
    "reply", "share", "more replies", "deleted", "people also ask",
})

_WEAK_LIST_RE = re.compile(
    r"on my list to (?:still )?visit|still visit are|have on my list|"
    r"ones i have on my list|on my list to still",
    re.I,
)

_PRAISE_RE = re.compile(
    r"ton of praise|highly recommend|absolute favorite|my favorites? are|"
    r"should add|must add|next level|best/most|best coffee|fantastic at it|"
    r"great coffeeshop|excellent drinks|praised for",
    re.I,
)


def _unique_result_snippets(results_text: str) -> list[str]:
    """Dedupe Reddit snippets by thread title — Tavily often repeats the same thread."""
    snippets: list[str] = []
    seen_titles: set[str] = set()
    for line in results_text.splitlines():
        if not line.startswith("- ") or ": " not in line:
            continue
        title, body = line.split(": ", 1)
        title_key = title[2:].strip().lower()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        snippets.append(body)
    return snippets


def _clean_consensus_candidate(raw: str) -> str | None:
    s = raw.strip().strip("._-*#\"'()[]")
    s = re.sub(r"^\(\s*", "", s)
    s = re.sub(r"\s+was\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s*(highly recommend|etc\.?)$", "", s, flags=re.I).strip()
    if re.search(
        r"\b(has good|are next level|mochas|they own|my favorites|so far|has no idea)\b",
        s,
        re.I,
    ):
        return None
    s = re.sub(r"\s+in\s+(?:downtown\s+)?[\w ]+$", "", s, flags=re.I).strip()
    if re.search(r"\b(rd|st|ave|blvd|dr|way|ln)\.?\s*$", s, re.I):
        return None
    if not s or len(s) > 55 or s.count(" ") > 5:
        return None
    if len(s) < 3:
        return None
    # Short brand tokens (e.g. Sey, SEY) still appear in Reddit lists.
    if len(s) < 4 and not re.fullmatch(r"[A-Z][A-Za-z'&.\-]{1,2}|[A-Z]{2,4}", s):
        return None
    if re.search(
        r"https?://|r/|\.com|skip to|open menu|avatar|image \d|u/[A-Za-z0-9_]+",
        s,
        re.I,
    ):
        return None
    lower = s.lower()
    if lower in _CONSENSUS_SKIP or lower.startswith(
        ("the ", "a ", "to ", "so ", "but ", "if ", "for ", "reply ", "more ")
    ):
        return None
    if re.fullmatch(r"(reply|share|deleted|etc\.?)", lower):
        return None
    # Venue names in threads are usually capitalized or contain Coffee/Roasters.
    if not (re.search(r"[A-Z]", s) or re.search(r"coffee|roaster|caffe|lab", s, re.I)):
        return None
    return s


def _candidate_names_from_text(body: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;]|\band\b|\.\s+", body, flags=re.I):
        name = _clean_consensus_candidate(part)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _extract_consensus_venues(results_text: str) -> list[str]:
    """Surface venue names repeated across distinct Reddit threads (deterministic signal).

    Ignores neutral wishlist lines ('still want to visit…') — those inflate false consensus."""
    snippets = _unique_result_snippets(results_text)
    if not snippets:
        return []

    candidates: dict[str, str] = {}
    for body in snippets:
        if _WEAK_LIST_RE.search(body):
            continue
        for name in _candidate_names_from_text(body):
            key = name.lower()
            prev = candidates.get(key)
            if prev is None or (name[0].isupper() and not prev[0].isupper()):
                candidates[key] = name

    ranked: list[tuple[int, str]] = []
    for key, name in candidates.items():
        thread_hits = sum(1 for body in snippets if key in body.lower())
        if thread_hits >= 2:
            ranked.append((thread_hits, name))
    ranked.sort(key=lambda t: (-t[0], t[1].lower()))
    return [name for _, name in ranked[:12]]


def _extract_praise_venues(results_text: str) -> list[str]:
    """Venues explicitly praised in snippets — stronger than comma-list repetition."""
    snippets = _unique_result_snippets(results_text)
    ranked: list[str] = []
    seen: set[str] = set()

    def _add(name: str | None) -> None:
        if not name:
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        ranked.append(name)

    always_patterns = (
        r"should add\s+([A-Z][\w'&.\- ]{2,40}?)\s+to",
        r"add\s+([A-Z][\w'&.\- ]{2,40}?)\s+to your list",
        r"\.?\s*([A-Z][\w'&.\-]{2,30})\s+was(?: the main reason|\b|$)",
    )
    praise_patterns = (
        r"([A-Z][\w'&.\-]{2,40}?)\s+is\s+(?:my )?absolute favorite",
        r"and\s+([A-Z][\w'&.\- ]{2,30})\s+in\s+Tempe\b",
    )

    for body in snippets:
        for pat in always_patterns:
            for m in re.finditer(pat, body, flags=re.I):
                _add(_clean_consensus_candidate(m.group(1)))
        if not _PRAISE_RE.search(body):
            continue
        for pat in praise_patterns:
            for m in re.finditer(pat, body, flags=re.I):
                _add(_clean_consensus_candidate(m.group(1)))
        if _WEAK_LIST_RE.search(body):
            continue
        if body.count(",") >= 2 or "highly recommend" in body.lower():
            for name in _candidate_names_from_text(body):
                _add(name)

    ranked.sort(key=str.lower)
    return ranked[:8]


def _favorite_mentions_in_results(profile: dict, results_text: str) -> list[str]:
    """Favorite roasters/cafés that appear in city search snippets (deterministic)."""
    fav_names = [
        str(x).strip()
        for x in (
            *(profile.get("favoriteRoasters") or []),
            *(profile.get("favoriteCafes") or []),
        )
        if str(x).strip()
    ]
    if not fav_names:
        return []

    lower = results_text.lower()
    hits: list[str] = []
    seen: set[str] = set()
    for name in fav_names:
        key = name.lower()
        if key in seen:
            continue
        needles = {key}
        first = name.split()[0].strip()
        if len(first) >= 3:
            needles.add(first.lower())
        if any(re.search(rf"\b{re.escape(n)}\b", lower) for n in needles):
            seen.add(key)
            hits.append(name)
    return hits


def _format_consensus_block(consensus: list[str], results_text: str) -> str:
    """Build consensus block; flag bar-first candidates for the multi-roaster slot."""
    lines = ["CONSENSUS MENTIONS (appeared in multiple search results)"]
    for name in consensus:
        hint = ""
        key = name.lower()
        for rline in results_text.splitlines():
            if key not in rline.lower():
                continue
            rl = rline.lower()
            if any(
                m in rl
                for m in (
                    "multi-roaster",
                    "multi roaster",
                    "guest roaster",
                    "curated",
                    "highly recommend",
                    "bar-first",
                )
            ):
                hint = " — bar-first / curated; prefer for multi-roaster slot"
                break
        lines.append(f"- {name}{hint}")
    return "\n".join(lines) + "\n\n"
