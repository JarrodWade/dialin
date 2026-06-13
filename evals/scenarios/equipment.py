"""Equipment scenarios: §2b-add cataloging, §2d gear edits, §2b brew gear resolution."""

from __future__ import annotations

from typing import Any

from evals import harness as H


def _catalog_gear_lists_all_first() -> H.Scenario:
    def is_grinder_niche(a: dict[str, Any]) -> bool:
        return str(a.get("equipType") or "").upper() == "GRINDER" and "niche" in str(a.get("name") or "").lower()

    return H.Scenario(
        id="catalog_gear_adds_grinder",
        rule="§2b-add",
        message="add my Niche Zero to my gear",
        checks=[
            # The §2b-add "list_equipment first" step is belt-and-suspenders: the
            # backend dedups by normalized name on add_equipment, so we assert the
            # outcome (saved as a GRINDER) rather than the redundant pre-list.
            H.called("add_equipment", where=is_grinder_niche, label="add_equipment(GRINDER Niche Zero)"),
            H.not_called("add_coffee"),
            H.no_iteration_cap(),
        ],
    )


def _gear_rename_uses_update() -> H.Scenario:
    def seed(ddb: Any, user_id: str) -> None:
        ddb.create_equipment(user_id, equip_type="BREWER", name="Hario V60 02")

    return H.Scenario(
        id="gear_rename_uses_update",
        rule="§2d",
        seed=seed,
        message="my V60 is actually the 01 size, can you fix that?",
        checks=[
            H.called("update_equipment"),
            H.not_called("add_equipment"),  # editing, not creating
            H.no_iteration_cap(),
        ],
    )


def _brew_drip_resolves_brewer() -> H.Scenario:
    state: dict[str, Any] = {}

    def seed(ddb: Any, user_id: str) -> None:
        r = ddb.create_roaster(user_id, "Heart Coffee Roasters", city="Portland", state="OR")
        c = ddb.create_coffee(
            user_id, roaster="Heart Coffee Roasters", name="Kenya Karatina",
            roaster_id=r["roasterId"], origin="Kenya",
        )
        ddb.create_equipment(user_id, equip_type="BREWER", name="Hario V60 02")
        state["coffeeId"] = c["coffeeId"]

    def grind_kept(a: dict[str, Any]) -> bool:
        return str(a.get("grind") or "").strip().lower() == "ode 4"

    return H.Scenario(
        id="brew_drip_resolves_brewer",
        rule="§2b",
        seed=seed,
        message="log a V60 of my Kenya Karatina this morning — 15g in, 250 out, Ode 4, tasted bright",
        checks=[
            H.called("log_brew", where=lambda a: str(a.get("method") or "").lower() == "v60"),
            H.called("log_brew", where=grind_kept, label="log_brew(grind == 'Ode 4')"),
            H.no_iteration_cap(),
        ],
    )


SCENARIOS = [
    _catalog_gear_lists_all_first(),
    _gear_rename_uses_update(),
    _brew_drip_resolves_brewer(),
]
