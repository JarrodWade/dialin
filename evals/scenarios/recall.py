"""Recall scenarios: §2f thematic RAG, §3 dial-in advice, §4 coffee summary."""

from __future__ import annotations

from typing import Any

from evals import harness as H


def _theme_recall_uses_rag() -> H.Scenario:
    return H.Scenario(
        id="theme_recall_uses_rag",
        rule="§2f",
        message="what patterns keep showing up in my tasting notes lately?",
        checks=[
            H.called("retrieve_journal"),
            H.no_iteration_cap(),
        ],
    )


def _dialin_advice_for_method() -> H.Scenario:
    state: dict[str, Any] = {}

    def seed(ddb: Any, user_id: str) -> None:
        r = ddb.create_roaster(user_id, "Sweet Bloom Coffee Roasters", city="Lakewood", state="CO")
        c = ddb.create_coffee(
            user_id, roaster="Sweet Bloom Coffee Roasters", name="Colombia El Paraiso",
            roaster_id=r["roasterId"], origin="Colombia",
        )
        cid = c["coffeeId"]
        ddb.create_brew(user_id=user_id, coffee_id=cid, method="Espresso", grind="Ode 3", rating=6, taste="sour, bright")
        ddb.create_brew(user_id=user_id, coffee_id=cid, method="Espresso", grind="Ode 2", rating=8, taste="balanced, syrupy")
        state["coffeeId"] = cid

    return H.Scenario(
        id="dialin_advice_for_method",
        rule="§3",
        seed=seed,
        message="help me dial in my El Paraiso on espresso",
        checks=[
            H.called(
                "get_dialin_advice",
                where=lambda a: str(a.get("method") or "").lower() == "espresso"
                and a.get("coffeeId") == state.get("coffeeId"),
                label="get_dialin_advice(El Paraiso, Espresso)",
            ),
            H.no_iteration_cap(),
        ],
    )


def _best_pick_uses_summarize() -> H.Scenario:
    state: dict[str, Any] = {}

    def seed(ddb: Any, user_id: str) -> None:
        r = ddb.create_roaster(user_id, "Onyx Coffee Lab", city="Rogers", state="AR")
        c = ddb.create_coffee(
            user_id, roaster="Onyx Coffee Lab", name="Ethiopia Guji",
            roaster_id=r["roasterId"], origin="Ethiopia",
        )
        cid = c["coffeeId"]
        ddb.create_brew(user_id=user_id, coffee_id=cid, method="V60", grind="Ode 5", rating=7, taste="floral")
        ddb.create_brew(user_id=user_id, coffee_id=cid, method="V60", grind="Ode 4", rating=9, taste="clean, sweet")
        state["coffeeId"] = cid

    return H.Scenario(
        id="best_pick_uses_summarize",
        rule="§4",
        seed=seed,
        message="what's worked best for my Ethiopia Guji?",
        checks=[
            H.called(
                "summarize_coffee",
                where=lambda a: a.get("coffeeId") == state.get("coffeeId"),
                label="summarize_coffee(Ethiopia Guji)",
            ),
            H.no_iteration_cap(),
        ],
    )


def _specific_brew_recall_uses_brew_tool() -> H.Scenario:
    """§2f: asking about a specific past brew must call at least one brew-read tool.

    The check is intentionally broad (any of the three structured brew tools)
    to avoid being brittle about which one the model picks. If this fails
    consistently it means the model is answering from context alone — the
    signal to tighten §2f.
    """
    state: dict[str, Any] = {}

    def seed(ddb: Any, user_id: str) -> None:
        r = ddb.create_roaster(user_id, "S&W Craft Roasters", city="Nashville", state="TN")
        c = ddb.create_coffee(
            user_id, roaster="S&W Craft Roasters", name="Divino Nino White Honey Geisha",
            roaster_id=r["roasterId"], origin="Colombia",
        )
        ddb.create_brew(
            user_id=user_id, coffee_id=c["coffeeId"], method="OXO Rapid Brewer",
            grind="Soup method", rating=9, taste="lemon cake, syrupy",
        )
        state["coffeeId"] = c["coffeeId"]

    return H.Scenario(
        id="specific_brew_recall_uses_brew_tool",
        rule="§2f",
        seed=seed,
        message="tell me about my lemon cake OXO brew",
        checks=[
            H.called_any(
                ["list_brews", "get_dialin_advice", "summarize_coffee"],
                label="called_any(list_brews|get_dialin_advice|summarize_coffee)",
            ),
            H.no_iteration_cap(),
        ],
    )


SCENARIOS = [
    _theme_recall_uses_rag(),
    _dialin_advice_for_method(),
    _best_pick_uses_summarize(),
    _specific_brew_recall_uses_brew_tool(),
]
