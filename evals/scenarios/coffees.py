"""Coffee + roaster scenarios: P1 snapshot authority, §2a roaster resolution,
add_coffee field parsing."""

from __future__ import annotations

from typing import Any

from evals import harness as H


def _snapshot_known_coffee() -> H.Scenario:
    state: dict[str, Any] = {}

    def seed(ddb: Any, user_id: str) -> None:
        r = ddb.create_roaster(user_id, "Onyx Coffee Lab", city="Rogers", state="AR")
        c = ddb.create_coffee(
            user_id,
            roaster="Onyx Coffee Lab",
            name="Ethiopia Guji",
            roaster_id=r["roasterId"],
            origin="Ethiopia",
            process="natural",
        )
        state["coffeeId"] = c["coffeeId"]

    return H.Scenario(
        id="snapshot_known_coffee",
        rule="P1",
        seed=seed,
        message="what's the process on my Ethiopia Guji?",
        checks=[
            # The snapshot already lists it — no need to re-fetch the whole list.
            H.not_called("list_coffees"),
            H.no_iteration_cap(),
            H.reply_excludes(["coffeeid", "roasterid", "list_coffees"]),
            H.reply_matches(r"natural"),
        ],
    )


def _snapshot_absent_coffee() -> H.Scenario:
    def seed(ddb: Any, user_id: str) -> None:
        r = ddb.create_roaster(user_id, "Sweet Bloom Coffee Roasters", city="Lakewood", state="CO")
        ddb.create_coffee(
            user_id, roaster="Sweet Bloom Coffee Roasters", name="Colombia El Paraiso",
            roaster_id=r["roasterId"], origin="Colombia",
        )

    return H.Scenario(
        id="snapshot_absent_coffee",
        rule="P1",
        seed=seed,
        message="how many grams are left on my Kenya Nyeri?",
        checks=[
            # It is not in the journal — do not invent it or silently add one.
            H.not_called("add_coffee"),
            H.not_called("log_brew"),
            H.no_iteration_cap(),
            H.reply_matches(r"don'?t|do not|not .*(track|see|have|logged)|isn'?t|no .*kenya"),
        ],
    )


def _add_coffee_preserves_process() -> H.Scenario:
    def seed(ddb: Any, user_id: str) -> None:
        ddb.create_roaster(user_id, "Sey Coffee", city="Brooklyn", state="NY")

    def process_kept(a: dict[str, Any]) -> bool:
        p = str(a.get("process") or "").lower()
        return "anaerobic" in p and p not in ("natural", "washed")

    return H.Scenario(
        id="add_coffee_preserves_process",
        rule="§2a",
        seed=seed,
        message=(
            "Add a new bag from Sey: Ethiopia Gedeb, double anaerobic natural, "
            "roasted 2026-06-01, 250g"
        ),
        checks=[
            H.called("add_coffee", where=lambda a: bool(a.get("roasterId"))),
            H.called("add_coffee", where=process_kept, label="add_coffee(process preserves 'anaerobic')"),
            H.not_called("add_roaster"),  # Sey is already in the snapshot
            H.no_iteration_cap(),
        ],
    )


def _roaster_confirm_before_add() -> H.Scenario:
    # No roasters seeded: an unknown roaster must be confirmed before add_roaster/add_coffee.
    return H.Scenario(
        id="roaster_confirm_before_add",
        rule="§2a",
        message="add a coffee from Foxtail Coffee — Ethiopia Worka, washed",
        checks=[
            H.not_called("add_roaster"),
            H.not_called("add_coffee"),
            H.no_iteration_cap(),
            H.reply_matches(r"add|don'?t see|not .*(see|track)|want me to"),
        ],
    )


SCENARIOS = [
    _snapshot_known_coffee(),
    _snapshot_absent_coffee(),
    _add_coffee_preserves_process(),
    _roaster_confirm_before_add(),
]
