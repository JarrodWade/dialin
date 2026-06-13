"""Live prompt-quality scenarios, grouped into suites by rule family.

Each module exposes ``SCENARIOS: list[harness.Scenario]``. A scenario seeds
DynamoDB state, sends one user message to the live model, and asserts on the
resulting tool trace (and lightly on the reply text). Every scenario is tagged
with the system-prompt rule it guards so a failing pass-rate points at a rule.

Add a new suite by creating a module here and registering it in ``_MODULES``.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the harness (and lambda deps) at module import
    from evals.harness import Scenario

_MODULES = ("coffees", "equipment", "cafes_visits", "corrections", "trips", "recall")


def suites() -> dict[str, list["Scenario"]]:
    """Map suite name -> its scenarios. Imported lazily so the lambda path is set up first."""
    out: dict[str, list["Scenario"]] = {}
    for name in _MODULES:
        mod = import_module(f"evals.scenarios.{name}")
        out[name] = list(getattr(mod, "SCENARIOS", []))
    return out


def all_scenarios() -> list["Scenario"]:
    out: list["Scenario"] = []
    for scenarios in suites().values():
        out.extend(scenarios)
    return out
