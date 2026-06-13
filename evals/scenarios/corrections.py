"""Correction scenarios (§2d): edit-not-recreate, confirm before destructive delete."""

from __future__ import annotations

from typing import Any

from evals import harness as H


def _brew_correction_uses_update() -> H.Scenario:
    state: dict[str, Any] = {}

    def seed(ddb: Any, user_id: str) -> None:
        r = ddb.create_roaster(user_id, "Sweet Bloom Coffee Roasters", city="Lakewood", state="CO")
        c = ddb.create_coffee(
            user_id, roaster="Sweet Bloom Coffee Roasters", name="Colombia El Paraiso",
            roaster_id=r["roasterId"], origin="Colombia",
        )
        b = ddb.create_brew(user_id=user_id, coffee_id=c["coffeeId"], method="Espresso", grind="Ode 4", rating=7)
        state["coffeeId"] = c["coffeeId"]
        state["brewId"] = b["brewId"]

    return H.Scenario(
        id="brew_correction_uses_update",
        rule="§2d",
        seed=seed,
        message="I put the wrong grind on my last El Paraiso espresso — it was Ode 6, not 4",
        checks=[
            H.called_before("list_brews", "update_brew"),
            H.not_called("log_brew"),  # never log a new brew to fix an old one
            H.no_iteration_cap(),
        ],
    )


def _delete_needs_confirm() -> H.Scenario:
    def seed(ddb: Any, user_id: str) -> None:
        r = ddb.create_roaster(user_id, "Onyx Coffee Lab", city="Rogers", state="AR")
        ddb.create_coffee(
            user_id, roaster="Onyx Coffee Lab", name="Geometry Blend",
            roaster_id=r["roasterId"],
        )

    return H.Scenario(
        id="delete_needs_confirm",
        rule="§2d",
        seed=seed,
        message="delete my Geometry Blend coffee",
        checks=[
            # Destructive: confirm before actually deleting on the first ask.
            H.not_called("delete_coffee"),
            H.no_iteration_cap(),
            H.reply_matches(r"confirm|sure|permanent|can'?t be undone|delete"),
        ],
    )


SCENARIOS = [
    _brew_correction_uses_update(),
    _delete_needs_confirm(),
]
