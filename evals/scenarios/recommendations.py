"""'For You' bean discovery scenarios.

Guards the recommend_beans behavior tuned against live feedback:
  - roaster CLASS is the driver (seed the web search with the user's own roaster
    names), not origin/process matching;
  - the trip-discovery appendix must NOT attach (it caused multi-search timeouts);
  - web searches are budget-capped so the turn fits under the 30s API timeout;
  - output is grouped North America first, then International.

The reply-text checks (grouping) only pass under the live model; the CI plumbing
test runs a no-tool fake where they fail by design — that's fine, it only asserts
the harness completes.
"""

from __future__ import annotations

from typing import Any

from evals import harness as H

# A uniformly cutting-edge, light-roast, third-wave journal — the class the
# recommender must classify and find peers for.
_MY_ROASTERS = ["Sey", "Shoebox", "Rufous", "Mythical", "Moxie"]


def _seed(ddb: Any, user_id: str) -> None:
    roaster_ids: dict[str, str] = {}
    for name in _MY_ROASTERS:
        roaster_ids[name] = ddb.create_roaster(user_id, name=name)["roasterId"]

    # A light-roast coffee with a high-rated brew gives the taste-level signal.
    coffee = ddb.create_coffee(
        user_id,
        roaster="Sey",
        name="Kiamugumo AA",
        roaster_id=roaster_ids["Sey"],
        origin="Kenya",
        process="washed",
        roast_level="light",
    )
    ddb.create_brew(user_id, coffee["coffeeId"], "V60", rating=9, taste="clean, red fruit, tea-like")

    ddb.update_profile(
        user_id,
        {
            "preferredRoastLevel": "light",
            "favoriteRoasters": ["Sey", "Moxie", "Futuro"],
            "experimentalPreference": "seek",
        },
        replace_lists=True,
    )


def _seeded_in_query(a: dict[str, Any]) -> bool:
    q = str(a.get("query") or "").lower()
    return any(name.lower() in q for name in _MY_ROASTERS)


def _for_you_discovery() -> H.Scenario:
    return H.Scenario(
        id="for_you_discovery",
        rule="for_you/recommend_beans",
        seed=_seed,
        # Run the real recommend_beans instruction + entrypoint settings.
        message_factory=lambda b: b._FOR_YOU_BEANS_INSTRUCTION,
        force_trip_appendix=False,
        max_web_searches=2,
        checks=[
            # Self-contained flow: must NOT inherit trip-scouting behavior.
            H.attachment(trip_appendix=False),
            # Grounds in stated taste before searching.
            H.called("get_preferences"),
            # Discovery is seeded with MY actual roaster names ("roasters like ...").
            H.called(
                "search_web",
                where=_seeded_in_query,
                label="search_web(seeded with my roaster names)",
            ),
            # Budget discipline keeps the turn under the 30s API timeout.
            H.call_count("search_web", max=2),
            # Output is geographically tiered, North America first.
            H.reply_matches(r"north america", label="reply groups by North America"),
            H.no_iteration_cap(),
        ],
    )


SCENARIOS = [_for_you_discovery()]
