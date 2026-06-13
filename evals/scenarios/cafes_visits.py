"""Cafe & visit scenarios (§2e): roaster badge, no re-add of tracked places,
one-log-per-outing, visit corrections via update."""

from __future__ import annotations

from typing import Any

from evals import harness as H


def _cafe_roaster_badge() -> H.Scenario:
    def seed(ddb: Any, user_id: str) -> None:
        ddb.create_cafe(user_id, name="Anchorhead", city="Seattle", state="WA")

    return H.Scenario(
        id="cafe_roaster_badge",
        rule="§2e",
        seed=seed,
        message="Anchorhead actually roasts their own beans now",
        checks=[
            H.called("update_cafe", where=lambda a: H.truthy(a.get("isRoaster")),
                     label="update_cafe(isRoaster truthy)"),
            H.not_called("add_cafe"),
            H.no_iteration_cap(),
            H.reply_excludes(["cafeid", "isroaster", "duplicate_place"]),
        ],
    )


def _already_tracked_no_readd() -> H.Scenario:
    def seed(ddb: Any, user_id: str) -> None:
        ddb.create_cafe(user_id, name="Coava Coffee", neighborhood="SE", city="Portland", state="OR")

    return H.Scenario(
        id="already_tracked_no_readd",
        rule="§2e",
        seed=seed,
        message="I want to start tracking Coava Coffee",
        checks=[
            # It's already saved — resolve, then don't offer to add a duplicate.
            H.not_called("add_cafe"),
            H.no_iteration_cap(),
        ],
    )


def _named_visit_one_log() -> H.Scenario:
    state: dict[str, Any] = {}

    def seed(ddb: Any, user_id: str) -> None:
        cafe = ddb.create_cafe(user_id, name="Coava Coffee", city="Portland", state="OR")
        state["cafeId"] = cafe["cafeId"]

    return H.Scenario(
        id="named_visit_one_log",
        rule="§2e",
        seed=seed,
        client_timezone="America/Los_Angeles",
        message="log a visit to Coava yesterday — had a cortado, 9/10",
        checks=[
            H.call_count("log_visit", max=1),
            H.called("log_visit", where=lambda a: int(a.get("rating") or 0) == 9),
            H.not_called("add_cafe"),
            H.no_iteration_cap(),
        ],
    )


def _visit_correction_uses_update() -> H.Scenario:
    state: dict[str, Any] = {}

    def seed(ddb: Any, user_id: str) -> None:
        cafe = ddb.create_cafe(user_id, name="Coava Coffee", city="Portland", state="OR")
        v = ddb.log_visit(
            user_id, cafe_id=cafe["cafeId"], place_name="Coava Coffee",
            visit_date="2026-06-10", drinks=["cortado"], rating=9,
        )
        state["cafeId"] = cafe["cafeId"]
        state["visitId"] = v["visitId"]

    return H.Scenario(
        id="visit_correction_uses_update",
        rule="§2d",
        seed=seed,
        message="actually my recent Coava visit was an 8, not a 9 — fix it",
        checks=[
            H.called("update_visit"),
            H.not_called("log_visit"),  # correcting, never re-logging
            H.no_iteration_cap(),
        ],
    )


SCENARIOS = [
    _cafe_roaster_badge(),
    _already_tracked_no_readd(),
    _named_visit_one_log(),
    _visit_correction_uses_update(),
]
