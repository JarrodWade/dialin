"""Trip discovery scenarios: appendix routing, §5/§5c preferences-before-search,
city anchoring, §0 voice, and the §2e-wins-over-trip guard."""

from __future__ import annotations

from typing import Any

from evals import harness as H

# Phrases the model should NOT narrate (§0 / trip-appendix voice rule).
_SEARCH_NARRATION = ["i'll search", "let me search", "i'll run a search", "searching for", "fresh search"]


def _city_scout_grounded() -> H.Scenario:
    return H.Scenario(
        id="city_scout_grounded",
        rule="§5c/appendix",
        message="what are the best specialty coffee spots in Osaka?",
        checks=[
            H.attachment(trip_appendix=True),
            H.called("get_preferences"),
            H.called("search_web"),
            H.called_before("get_preferences", "search_web"),
            H.reply_excludes(_SEARCH_NARRATION),
            H.no_iteration_cap(),
        ],
    )


def _city_anchoring_search() -> H.Scenario:
    def kyoto_in_query(a: dict[str, Any]) -> bool:
        return "kyoto" in str(a.get("query") or "").lower()

    return H.Scenario(
        id="city_anchoring_search",
        rule="appendix (city anchoring)",
        message="heading to Kyoto next week — where should I drink?",
        checks=[
            H.attachment(trip_appendix=True),
            H.called("search_web", where=kyoto_in_query, label="search_web(query contains 'kyoto')"),
            H.no_iteration_cap(),
        ],
    )


def _named_visit_not_derailed() -> H.Scenario:
    # "log my visit" must win over trip discovery — appendix should NOT attach.
    def seed(ddb: Any, user_id: str) -> None:
        ddb.create_cafe(user_id, name="Blue Bottle Coffee", city="Oakland", state="CA")

    return H.Scenario(
        id="named_visit_not_derailed",
        rule="P0/§2e",
        seed=seed,
        client_timezone="America/Los_Angeles",
        message="log my visit to Blue Bottle yesterday, flat white, 8/10",
        checks=[
            H.attachment(trip_appendix=False),
            H.called("log_visit"),
            H.not_called("search_web"),
            H.no_iteration_cap(),
        ],
    )


SCENARIOS = [
    _city_scout_grounded(),
    _city_anchoring_search(),
    _named_visit_not_derailed(),
]
